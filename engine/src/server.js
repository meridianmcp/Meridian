#!/usr/bin/env node
// Local HTTP server wrapping the outline engine + the write-back
// coordination layer (docs/write-back-spec.md), for the Chrome extension's
// popup to call. Plain stdlib http -- matches this repo's "minimal stack"
// convention; better-sqlite3 (via store.js/claims.js) is the one real
// dependency the write-back half needed, per the spec.
//
// Endpoints:
//   POST /outline   { text, project_id? }
//                    -> { nodes }  (no project_id -- unchanged, backward compatible)
//                    -> { nodes, matched, added, removed }  (project_id given)
//   POST /claim     { project_id, node_id, holder_token }
//                    -> { claimed: true, identity_confidence?, identity_confidence_reason? }
//                    -> { claimed: false, reason, holder_token_of_conflict? }
//   POST /lease     { project_id, holder_token }  (whole-document lease)
//                    -> { leased: true } / { leased: false, reason, holder_token_of_conflict? }
//   POST /release   { project_id, holder_token, node_id? }  -> { released: <count> }
//   GET  /claims?project_id=...  -> { claims: [...] }  (live claims only)
//   GET  /extension-version  -> { hash }  (mtime fingerprint of extension/,
//                    for background.js's self-reload poll -- see below)
//   GET  /health     ->  { ok: true }
//
// CORS: allows requests from any chrome-extension:// origin only (an
// extension's content/background scripts are the only intended caller;
// this is a localhost-only dev server, not exposed to the network beyond
// this machine, but still worth scoping the origin rather than using '*').

import { createServer } from "node:http";
import { createHash } from "node:crypto";
import { readdirSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { outlineText } from "./outline.js";
import { matchOutlines } from "./matching.js";
import { openStore, getProject, upsertProject } from "./store.js";
import { claimNode, leaseWholeDocument, releaseClaims, getLiveClaims } from "./claims.js";

// One shared connection for the lifetime of this process -- matches the
// spec's "one local Node process on one machine" framing (no pooling, no
// cross-process concern worth the complexity Meridian's own locks.py has
// for its multi-machine Fly.io deployment).
const db = openStore();

const PORT = process.env.MERIDIAN_LATEX_PORT
  ? Number(process.env.MERIDIAN_LATEX_PORT)
  : 8471;

// engine/src/server.js -> engine/src -> engine -> repo root -> extension/
const __dirname = dirname(fileURLToPath(import.meta.url));
const EXTENSION_DIR = join(__dirname, "..", "..", "extension");

/**
 * Self-reload support for the Chrome extension (see extension/background.js):
 * a cheap fingerprint of everything under extension/ that background.js's
 * ~3s polling loop can diff against its own last-seen value, without this
 * server re-reading every file's actual content on every poll. mtime, not
 * content, is the deliberate tradeoff -- cheap stat() calls are enough to
 * detect "a file under extension/ changed" for a local dev-reload signal;
 * it doesn't need to be a content-addressed hash the way node ids
 * (fnv1a32 in outline.js) do, since nothing here needs collision-proofing
 * against a real adversary, only against "nothing changed."
 *
 * Sorted filenames first, since readdirSync's order is platform/filesystem
 * dependent and the hash must be deterministic across polls on the same
 * unchanged directory.
 */
function computeExtensionVersionHash() {
  const names = readdirSync(EXTENSION_DIR).sort();
  const hash = createHash("sha256");
  for (const name of names) {
    const stat = statSync(join(EXTENSION_DIR, name));
    if (!stat.isFile()) continue; // subdirectories: not expected today, skip if any show up
    hash.update(`${name}:${stat.mtimeMs}`);
  }
  return hash.digest("hex");
}

function withCors(req, res) {
  const origin = req.headers.origin || "";
  if (origin.startsWith("chrome-extension://")) {
    res.setHeader("Access-Control-Allow-Origin", origin);
    res.setHeader("Access-Control-Allow-Methods", "POST, GET, OPTIONS");
    res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  }
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", (chunk) => {
      data += chunk;
      if (data.length > 20_000_000) {
        reject(new Error("body too large"));
        req.destroy();
      }
    });
    req.on("end", () => resolve(data));
    req.on("error", reject);
  });
}

const server = createServer(async (req, res) => {
  withCors(req, res);

  if (req.method === "OPTIONS") {
    res.writeHead(204);
    res.end();
    return;
  }

  if (req.method === "GET" && req.url === "/health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ ok: true }));
    return;
  }

  if (req.method === "GET" && req.url === "/extension-version") {
    try {
      const hash = computeExtensionVersionHash();
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ hash }));
    } catch (err) {
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: String(err && err.message || err) }));
    }
    return;
  }

  if (req.method === "POST" && req.url === "/outline") {
    try {
      const raw = await readBody(req);
      const { text, project_id } = JSON.parse(raw);
      if (typeof text !== "string" || !text.trim()) {
        res.writeHead(400, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "missing or empty 'text' field" }));
        return;
      }
      const nodes = outlineText(text);
      res.writeHead(200, { "Content-Type": "application/json" });
      if (typeof project_id === "string" && project_id) {
        // Auto-register (or update) this project -- see store.js: there is
        // no separate add/remove step, showing up here IS registration.
        // matchOutlines(oldNodes, newNodes) naturally handles "never seen
        // before" too: an empty `oldNodes` just reports every new node as
        // `added`, nothing as `matched`/`removed` -- no special-casing
        // needed for first-contact vs. a real re-parse.
        const existing = getProject(db, project_id);
        const oldNodes = existing ? JSON.parse(existing.last_outline) : [];
        const { matched, added, removed } = matchOutlines(oldNodes, nodes);
        upsertProject(db, project_id, nodes);
        res.end(JSON.stringify({ nodes, matched, added, removed }));
      } else {
        // No project_id -- old shape, unchanged. Nothing to diff against
        // and nothing gets persisted, exactly like before this endpoint
        // grew write-back awareness.
        res.end(JSON.stringify({ nodes }));
      }
    } catch (err) {
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: String(err && err.message || err) }));
    }
    return;
  }

  if (req.method === "POST" && req.url === "/claim") {
    try {
      const raw = await readBody(req);
      const { project_id, node_id, holder_token } = JSON.parse(raw);
      const result = claimNode(db, { project_id, node_id, holder_token });
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: String(err && err.message || err) }));
    }
    return;
  }

  if (req.method === "POST" && req.url === "/lease") {
    try {
      const raw = await readBody(req);
      const { project_id, holder_token } = JSON.parse(raw);
      const result = leaseWholeDocument(db, { project_id, holder_token });
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: String(err && err.message || err) }));
    }
    return;
  }

  if (req.method === "POST" && req.url === "/release") {
    try {
      const raw = await readBody(req);
      const { project_id, holder_token, node_id } = JSON.parse(raw);
      const result = releaseClaims(db, { project_id, holder_token, node_id });
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: String(err && err.message || err) }));
    }
    return;
  }

  if (req.method === "GET" && req.url && req.url.startsWith("/claims")) {
    try {
      const url = new URL(req.url, "http://127.0.0.1");
      const project_id = url.searchParams.get("project_id");
      if (!project_id) {
        res.writeHead(400, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "missing 'project_id' query parameter" }));
        return;
      }
      const claims = getLiveClaims(db, project_id);
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ claims }));
    } catch (err) {
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: String(err && err.message || err) }));
    }
    return;
  }

  res.writeHead(404, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ error: "not found" }));
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`meridian-latex outline server listening on http://127.0.0.1:${PORT}`);
});
