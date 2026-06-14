"""Tests for scripts/mcp_cors_proxy.py — the CORS+GET shim that makes a local
mcp-proxy reachable from claude.ai (which probes GET/OPTIONS that mcp-proxy 400s)."""
import importlib.util
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path


def _load_shim():
    path = Path(__file__).resolve().parents[1] / "scripts" / "mcp_cors_proxy.py"
    spec = importlib.util.spec_from_file_location("mcp_cors_proxy", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _serve(mod):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), mod._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def test_cors_shim_options_preflight_returns_204_with_cors():
    """OPTIONS /mcp -> 204 + Access-Control-* (mcp-proxy rejects this with 400)."""
    mod = _load_shim()
    srv, port = _serve(mod)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", method="OPTIONS")
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 204
            assert r.headers.get("Access-Control-Allow-Origin") == "*"
            assert "OPTIONS" in r.headers.get("Access-Control-Allow-Methods", "")
    finally:
        srv.shutdown()


def test_cors_shim_get_probe_returns_200_info_with_cors():
    """GET /mcp -> 200 + server-info JSON + CORS (the claude.ai validation probe)."""
    mod = _load_shim()
    srv, port = _serve(mod)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/mcp", timeout=5) as r:
            assert r.status == 200
            assert r.headers.get("Access-Control-Allow-Origin") == "*"
            body = json.loads(r.read())
            assert "name" in body and "transport" in body
    finally:
        srv.shutdown()
