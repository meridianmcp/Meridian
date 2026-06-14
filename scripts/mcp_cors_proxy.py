#!/usr/bin/env python3
"""CORS + GET shim for exposing a local MCP server to claude.ai.

Why this exists
---------------
``mcp-proxy`` bridges a stdio MCP server (e.g. @modelcontextprotocol/server-filesystem)
to HTTP, but only answers ``POST /mcp``. claude.ai's custom-connector validation
*also* probes the endpoint with ``GET /mcp`` and a CORS ``OPTIONS`` preflight before
the MCP handshake. mcp-proxy rejects those non-POST requests with ``400``, and
claude.ai reads that as "couldn't connect" — even though POST works fine.

Meridian's own working connector (server.py: GET /mcp + _SSE_CORS_HEADERS) shows
exactly what claude.ai needs: a 200 on GET, a 204 on OPTIONS, and
``Access-Control-Allow-*`` headers on every response. This shim adds precisely
that in front of mcp-proxy, forwarding POST untouched.

Usage
-----
    # 1. Run mcp-proxy locally (NO --tunnel; this shim is the public face):
    npx mcp-proxy --port 8808 -- npx -y @modelcontextprotocol/server-filesystem /path/to/repo

    # 2. Run this shim, forwarding to mcp-proxy:
    python scripts/mcp_cors_proxy.py --listen 8809 --backend http://127.0.0.1:8808

    # 3. Point your tunnel at the shim's port (8809), then add
    #    https://<tunnel>/mcp as a claude.ai connector.

Read-only; stdlib only (no pip installs).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Mirrors meridian/server.py:_SSE_CORS_HEADERS — the headers claude.ai needs.
_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, Mcp-Session-Id, Mcp-Protocol-Version",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Expose-Headers": "Mcp-Session-Id, Mcp-Protocol-Version",
}

_BACKEND = "http://127.0.0.1:8808"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    backend = _BACKEND  # overridden from --backend in main()

    # — helpers —------------------------------------------------------------
    def _send(self, status: int, body: bytes = b"", ctype: str = "application/json",
              extra: dict | None = None) -> None:
        self.send_response(status)
        for k, v in _CORS.items():
            self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if body:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    # — verbs —--------------------------------------------------------------
    def do_OPTIONS(self) -> None:  # noqa: N802 — CORS preflight
        self._send(204)

    def do_GET(self) -> None:  # noqa: N802 — connector validation probe
        # mcp-proxy 400s GET; answer it ourselves like Meridian's GET /mcp does.
        info = json.dumps({
            "name": "local-filesystem-mcp",
            "version": "1.0",
            "transport": "http",
        }).encode()
        self._send(200, info)

    def do_POST(self) -> None:  # noqa: N802 — forward the real MCP traffic
        length = int(self.headers.get("Content-Length", 0) or 0)
        payload = self.rfile.read(length) if length else b""
        # Always target /mcp on the backend — mcp-proxy only listens there;
        # claude.ai sends POST / which would 404 if we forwarded self.path.
        fwd = urllib.request.Request(
            self.backend.rstrip("/") + "/mcp",
            data=payload,
            method="POST",
            headers={
                "Content-Type": self.headers.get("Content-Type", "application/json"),
                "Accept": self.headers.get("Accept", "application/json, text/event-stream"),
            },
        )
        auth = self.headers.get("Authorization")
        if auth:
            fwd.add_header("Authorization", auth)
        session_id = self.headers.get("Mcp-Session-Id")
        if session_id:
            fwd.add_header("Mcp-Session-Id", session_id)
        proto_ver = self.headers.get("Mcp-Protocol-Version")
        if proto_ver:
            fwd.add_header("Mcp-Protocol-Version", proto_ver)
        try:
            with urllib.request.urlopen(fwd, timeout=30) as resp:
                body = resp.read()
                ctype = resp.headers.get("Content-Type", "application/json")
                extra = {
                    hdr: resp.headers.get(hdr)
                    for hdr in ("Mcp-Session-Id", "Mcp-Protocol-Version")
                    if resp.headers.get(hdr)
                }
                self._send(resp.status, body, ctype, extra or None)
        except urllib.error.HTTPError as exc:  # backend returned a real status
            body = exc.read() or b"{}"
            self._send(exc.code, body, exc.headers.get("Content-Type", "application/json"))
        except Exception as exc:  # backend unreachable
            self._send(502, json.dumps({"error": f"backend unreachable: {exc}"}).encode())

    def log_message(self, fmt: str, *args) -> None:  # quieter logs, one line/req
        sys.stderr.write("[mcp-cors-proxy] " + (fmt % args) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="CORS + GET shim in front of mcp-proxy for claude.ai")
    ap.add_argument("--listen", type=int, default=8809, help="port this shim listens on")
    ap.add_argument("--backend", default=_BACKEND, help="mcp-proxy base URL (default http://127.0.0.1:8808)")
    args = ap.parse_args()

    _Handler.backend = args.backend
    srv = ThreadingHTTPServer(("0.0.0.0", args.listen), _Handler)
    sys.stderr.write(
        f"[mcp-cors-proxy] listening on 0.0.0.0:{args.listen} -> {args.backend}/mcp\n"
        f"[mcp-cors-proxy] point your tunnel at port {args.listen}; "
        f"add https://<tunnel>/mcp as a connector in claude.ai\n"
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
