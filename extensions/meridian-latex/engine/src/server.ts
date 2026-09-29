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
//   POST /provenance  { project_id, node_id, kind, field, old_value?, new_value, holder_token }
//                    -> { recorded: true, id } / { recorded: false, reason }
//                    (a durable local audit trail of applied edits -- see
//                    provenance.js's header comment for why this is a local
//                    ledger, not a live meridian-outputs call)
//   GET  /provenance?project_id=...&unsynced_only=true  -> { provenance: [...] }
//                    (for a later agent session to pull and push into
//                    meridian-outputs itself, then mark synced)
//   POST /provenance/mark-synced  { ids: [...] }  -> { marked: <count> }
//   GET  /zotero-lookup?key=<citekey>  -> { resolved: true, tag, title } /
//                    { resolved: false } / { resolved: null, reason }
//                    (citation-key validation against the local Zotero
//                    library's `:key:` tag convention -- see zotero.js)
//   GET  /extension-version  -> { hash }  (mtime fingerprint of extension/,
//                    for background.js's self-reload poll -- see below)
//   POST /open-overleaf  { projectUrl? }  -> { launched: true } /
//                    { launched: false, error, alreadyRunning? }
//                    (opens a real, visible, human-interactive Chrome window
//                    with the extension pre-loaded, pointed at an Overleaf
//                    project -- see browser.js. An alternative delivery path
//                    to the still-blocked OT client; at most one automation
//                    window is tracked at a time, a second call while one is
//                    already open returns alreadyRunning:true rather than
//                    piling up windows)
//   GET  /health     ->  { ok: true }
//
// CORS: allows requests from any chrome-extension:// origin only (an
// extension's content/background scripts are the only intended caller;
// this is a localhost-only dev server, not exposed to the network beyond
// this machine, but still worth scoping the origin rather than using '*').

import { createServer } from "node:http";
import type { IncomingMessage, ServerResponse } from "node:http";
import { createHash } from "node:crypto";
import { readdirSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { outlineText, type OutlineNode } from "./outline.js";
import { matchOutlines } from "./matching.js";
import { openStore, getProject, upsertProject } from "./store.js";
import { claimNode, leaseWholeDocument, releaseClaims, getLiveClaims } from "./claims.js";
import { recordEdit, listProvenance, markSynced } from "./provenance.js";
import { lookupCitationKey } from "./zotero.js";
import { launchOverleafBrowser, type LaunchedChromeLike } from "./browser.js";

// One shared connection for the lifetime of this process -- matches the
// spec's "one local Node process on one machine" framing (no pooling, no
// cross-process concern worth the complexity Meridian's own locks.py has
// for its multi-machine Fly.io deployment).
const db = openStore();

// Tracks the one automation browser opened via POST /open-overleaf (see
// below), so a repeated call doesn't pile up multiple Chrome windows.
// Cleared automatically when that Chrome process actually exits (the user
// closed the window, or it crashed) via the `.process` exit listener
// attached right after a successful launch -- not just on an explicit
// server-side kill, which this route never calls (the window is meant to
// stay open under the human's own control).
let activeOverleafBrowser: LaunchedChromeLike | null = null;

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
function computeExtensionVersionHash(): string {
  const names = readdirSync(EXTENSION_DIR).sort();
  const hash = createHash("sha256");
  for (const name of names) {
    const stat = statSync(join(EXTENSION_DIR, name));
    if (!stat.isFile()) continue; // subdirectories: not expected today, skip if any show up
    hash.update(`${name}:${stat.mtimeMs}`);
  }
  return hash.digest("hex");
}

// Real bug found 2026-09-18 via independent code review: this used to only
// check the "chrome-extension://" SCHEME prefix, not a specific extension
// id -- Access-Control-Allow-Origin was reflected back to ANY installed
// browser extension on this machine, not just this repo's own, meaning any
// other (e.g. malicious/compromised) extension could read/write this
// project's outline/claims/provenance/Zotero-lookup data via this local
// server. Fixed by pinning the extension's id: manifest.json now declares a
// "key" (a public key, safe to commit -- it is NOT the private signing key,
// which this project never generates or needs, since Chrome Web Store
// publishing uses its own signing key regardless), which makes Chrome
// assign this SAME extension id every time it's loaded unpacked, instead
// of a fresh random one per load. Reloading the extension after this
// change picks up the new fixed id -- see README's "Running it locally".
const EXTENSION_ORIGIN = "chrome-extension://ekdmjppbmdohipibjlogobkodikcffob";

function withCors(req: IncomingMessage, res: ServerResponse): void {
  const origin = req.headers.origin || "";
  if (origin === EXTENSION_ORIGIN) {
    res.setHeader("Access-Control-Allow-Origin", origin);
    res.setHeader("Access-Control-Allow-Methods", "POST, GET, OPTIONS");
    res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  }
}

function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    let data = "";
    let rejected = false;
    req.on("data", (chunk) => {
      if (rejected) return; // already given up -- stop accumulating further bytes
      data += chunk;
      if (data.length > 20_000_000) {
        rejected = true;
        data = ""; // release what's accumulated so far, nothing more to hold onto
        // Real bug found 2026-09-18 via independent code review: this used
        // to call req.destroy() right here, which tears down the
        // underlying TCP connection immediately -- before the route
        // handler's own catch block ever got a chance to send its intended
        // "body too large" JSON error through the still-intact `res`. The
        // client saw a connection reset instead of a clean error response.
        // Just reject and let the caller respond normally.
        reject(new Error("body too large"));
      }
    });
    req.on("end", () => {
      if (!rejected) resolve(data);
    });
    req.on("error", reject);
  });
}

const server = createServer(async (req: IncomingMessage, res: ServerResponse) => {
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
      res.end(JSON.stringify({ error: String((err && (err as Error).message) || err) }));
    }
    return;
  }

  if (req.method === "POST" && req.url === "/outline") {
    try {
      const raw = await readBody(req);
      const { text, project_id } = JSON.parse(raw) as { text: unknown; project_id: unknown };
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
        const oldNodes: OutlineNode[] = existing ? (JSON.parse(existing.last_outline) as OutlineNode[]) : [];
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
      res.end(JSON.stringify({ error: String((err && (err as Error).message) || err) }));
    }
    return;
  }

  if (req.method === "POST" && req.url === "/claim") {
    try {
      const raw = await readBody(req);
      const { project_id, node_id, holder_token } = JSON.parse(raw) as {
        project_id: unknown;
        node_id: unknown;
        holder_token: unknown;
      };
      const result = claimNode(db, { project_id, node_id, holder_token });
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: String((err && (err as Error).message) || err) }));
    }
    return;
  }

  if (req.method === "POST" && req.url === "/lease") {
    try {
      const raw = await readBody(req);
      const { project_id, holder_token } = JSON.parse(raw) as { project_id: unknown; holder_token: unknown };
      const result = leaseWholeDocument(db, { project_id, holder_token });
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: String((err && (err as Error).message) || err) }));
    }
    return;
  }

  if (req.method === "POST" && req.url === "/release") {
    try {
      const raw = await readBody(req);
      const { project_id, holder_token, node_id } = JSON.parse(raw) as {
        project_id: unknown;
        holder_token: unknown;
        node_id?: unknown;
      };
      const result = releaseClaims(db, { project_id, holder_token, node_id });
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: String((err && (err as Error).message) || err) }));
    }
    return;
  }

  if (req.method === "POST" && req.url === "/provenance") {
    try {
      const raw = await readBody(req);
      const { project_id, node_id, kind, field, old_value, new_value, holder_token } = JSON.parse(raw) as {
        project_id: unknown;
        node_id: unknown;
        kind: unknown;
        field: unknown;
        old_value?: unknown;
        new_value: unknown;
        holder_token: unknown;
      };
      const result = recordEdit(db, { project_id, node_id, kind, field, old_value, new_value, holder_token });
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: String((err && (err as Error).message) || err) }));
    }
    return;
  }

  if (req.method === "POST" && req.url === "/provenance/mark-synced") {
    try {
      const raw = await readBody(req);
      const { ids } = JSON.parse(raw) as { ids: unknown };
      const result = markSynced(db, ids);
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: String((err && (err as Error).message) || err) }));
    }
    return;
  }

  if (req.method === "GET" && req.url && req.url.startsWith("/provenance")) {
    try {
      const url = new URL(req.url, "http://127.0.0.1");
      const project_id = url.searchParams.get("project_id");
      if (!project_id) {
        res.writeHead(400, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "missing 'project_id' query parameter" }));
        return;
      }
      const unsynced_only = url.searchParams.get("unsynced_only") === "true";
      const provenance = listProvenance(db, { project_id, unsynced_only });
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ provenance }));
    } catch (err) {
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: String((err && (err as Error).message) || err) }));
    }
    return;
  }

  if (req.method === "GET" && req.url && req.url.startsWith("/zotero-lookup")) {
    try {
      const url = new URL(req.url, "http://127.0.0.1");
      const key = url.searchParams.get("key");
      if (!key) {
        res.writeHead(400, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "missing 'key' query parameter" }));
        return;
      }
      // autoStart: true (2026-09-24, Adam's ask) -- this is the one real,
      // human-facing call site (the popup UI's citation-check request), so
      // it's the right place to opt into launching Zotero if it's not
      // running. lookupCitationKey defaults autoStart to false since it's
      // also a library function other/test callers use.
      const result = await lookupCitationKey(key, { autoStart: true });
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: String((err && (err as Error).message) || err) }));
    }
    return;
  }

  if (req.method === "POST" && req.url === "/open-overleaf") {
    try {
      const raw = await readBody(req);
      const { projectUrl } = (raw ? JSON.parse(raw) : {}) as { projectUrl?: string };
      // Only one automation window tracked at a time -- a repeated call
      // (e.g. the popup's own "open browser" button clicked twice) must not
      // pile up a second, third, ... Chrome window. `activeOverleafBrowser`
      // is cleared on process exit or a real launch failure, never assumed
      // stale otherwise -- there is no reliable, cheap way to ask an
      // already-launched chrome-launcher instance "are you still actually
      // running" short of tracking its own lifecycle, and a false "still
      // running" (blocking a legitimate re-launch) is a far smaller problem
      // than silently accumulating orphaned Chrome windows.
      if (activeOverleafBrowser) {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ launched: false, alreadyRunning: true, error: "An automation browser window is already open." }));
        return;
      }
      const launchResult = await launchOverleafBrowser(projectUrl ? { projectUrl } : {});
      // `!== null` (not a bare truthy check on `launchResult.error`):
      // LaunchOverleafBrowserResult's two branches share `error: null` vs
      // `error: string`, and an explicit equality check against the literal
      // `null` is what lets TS's discriminated-union narrowing eliminate the
      // `chrome: null` branch below -- a plain truthy check doesn't reliably
      // narrow the ENCLOSING object union this way. Every real `error`
      // string this function returns is a fixed, non-empty prefix (see
      // browser.ts), so this is not a behavior change in practice.
      if (launchResult.error !== null) {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ launched: false, error: launchResult.error }));
        return;
      }
      // Destructuring `{chrome, error}` up front (as the untyped JS version
      // did) severs the discriminated-union link between the two fields --
      // TS can no longer tell `chrome` is non-null here once `error` has been
      // narrowed on its own local binding. Keeping (and narrowing through)
      // the whole `launchResult` object instead preserves that link, same
      // convention browser.test.ts's own consumption of this result already
      // uses.
      const { chrome } = launchResult;
      activeOverleafBrowser = chrome;
      chrome.process?.once("exit", () => {
        if (activeOverleafBrowser === chrome) activeOverleafBrowser = null;
      });
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ launched: true }));
    } catch (err) {
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: String((err && (err as Error).message) || err) }));
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
      res.end(JSON.stringify({ error: String((err && (err as Error).message) || err) }));
    }
    return;
  }

  res.writeHead(404, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ error: "not found" }));
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`meridian-latex outline server listening on http://127.0.0.1:${PORT}`);
});
