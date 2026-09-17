#!/usr/bin/env node
// Minimal local HTTP server wrapping the outline engine, for the Chrome
// extension's background worker to call. Zero new dependencies (stdlib
// http only) -- matches this repo's "minimal stack" convention.
//
// Endpoints:
//   POST /outline   { text: "<.tex source>" }  ->  { nodes: [...] }
//   GET  /health     ->  { ok: true }
//
// CORS: allows requests from any chrome-extension:// origin only (an
// extension's content/background scripts are the only intended caller;
// this is a localhost-only dev server, not exposed to the network beyond
// this machine, but still worth scoping the origin rather than using '*').

import { createServer } from "node:http";
import { outlineText } from "./outline.js";

const PORT = process.env.MERIDIAN_LATEX_PORT
  ? Number(process.env.MERIDIAN_LATEX_PORT)
  : 8471;

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

  if (req.method === "POST" && req.url === "/outline") {
    try {
      const raw = await readBody(req);
      const { text } = JSON.parse(raw);
      if (typeof text !== "string" || !text.trim()) {
        res.writeHead(400, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "missing or empty 'text' field" }));
        return;
      }
      const nodes = outlineText(text);
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ nodes }));
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
