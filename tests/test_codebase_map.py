"""Tests for 5813affe — package-level codebase map (graphviz) rendering.

Covers the pure DOT builder (no graphviz needed), the GraphvizMissingError path
when ``dot`` is absent, and the POST /projects/{id}/codebase-map route's
graceful degradation + success behaviour (graphviz mocked).
"""
from __future__ import annotations

import pytest

from meridian import codebase_map


_GRAPH = {
    "packages": [
        {"name": "meridian.db", "node_count": 120, "layer": 0},
        {"name": "meridian.routes", "node_count": 40, "layer": 1},
        {"name": "tests", "node_count": 10, "layer": 2},
    ],
    "edges": [
        {"source": "meridian.routes", "target": "meridian.db", "value": 12},
        {"source": "tests", "target": "meridian.db", "value": 3},
        {"source": "meridian.db", "target": "meridian.db", "value": 99},  # self → dropped
        {"source": "ghost", "target": "meridian.db", "value": 1},          # dangling → dropped
    ],
}


def test_build_dot_structure():
    dot = codebase_map.build_dot(_GRAPH)
    assert dot.startswith("digraph codebase {")
    assert dot.rstrip().endswith("}")
    # All three packages are nodes.
    for name in ("meridian.db", "meridian.routes", "tests"):
        assert f'"{name}"' in dot
    # Valid cross-package edges are present.
    assert '"meridian.routes" -> "meridian.db"' in dot
    assert '"tests" -> "meridian.db"' in dot
    # Self-edge and dangling edge are dropped.
    assert '"meridian.db" -> "meridian.db"' not in dot
    assert "ghost" not in dot


def test_build_dot_hotspots_subgraph():
    # 4 packages; top_n=2 keeps only the two busiest by edge weight.
    g = {
        "packages": [
            {"name": "a", "node_count": 1}, {"name": "b", "node_count": 1},
            {"name": "c", "node_count": 1}, {"name": "d", "node_count": 1},
        ],
        "edges": [
            {"source": "a", "target": "b", "value": 50},  # a,b busiest
            {"source": "c", "target": "d", "value": 1},
        ],
    }
    dot = codebase_map.build_dot(g, hotspots_only=True, top_n=2)
    assert '"a"' in dot and '"b"' in dot
    assert '"c"' not in dot and '"d"' not in dot


def test_build_dot_escapes_quotes():
    dot = codebase_map.build_dot({"packages": [{"name": 'we"ird', "node_count": 1}], "edges": []})
    assert '\\"' in dot  # the embedded quote is escaped


def test_build_dot_empty():
    dot = codebase_map.build_dot({"packages": [], "edges": []})
    assert dot.startswith("digraph codebase {")


def test_render_map_raises_when_graphviz_missing(monkeypatch):
    monkeypatch.setattr(codebase_map.shutil, "which", lambda name: None)
    with pytest.raises(codebase_map.GraphvizMissingError):
        codebase_map.render_map(_GRAPH, "out.png")


def test_render_map_shells_dot_when_available(monkeypatch, tmp_path):
    """When dot exists, render_map invokes it with the DOT source on stdin."""
    captured = {}

    monkeypatch.setattr(codebase_map.shutil, "which", lambda name: "/usr/bin/dot")

    class _Proc:
        returncode = 0
        stderr = b""

    def fake_run(cmd, input=None, **kw):
        captured["cmd"] = cmd
        captured["input"] = input
        return _Proc()

    monkeypatch.setattr(codebase_map.subprocess, "run", fake_run)
    out = str(tmp_path / "map.png")
    assert codebase_map.render_map(_GRAPH, out) == out
    assert captured["cmd"][0] == "/usr/bin/dot"
    assert "-Tpng" in captured["cmd"]
    assert b"digraph codebase" in captured["input"]


# ---------------------------------------------------------------------------
# Route — POST /projects/{id}/codebase-map
# ---------------------------------------------------------------------------

def test_codebase_map_route_graphviz_missing(client, monkeypatch):
    """503 with an actionable graphviz_missing message when dot is absent."""
    from meridian import codebase_map as cm
    monkeypatch.setattr(cm.shutil, "which", lambda name: None)
    proj = client.post("/projects", json={"name": "mapproj"}).json()
    r = client.post(
        f"/projects/{proj['id']}/codebase-map",
        json={"packages": [{"name": "a", "node_count": 1}], "edges": []},
    )
    assert r.status_code == 503
    body = r.json()
    assert body["error"] == "graphviz_missing"
    assert "Graphviz" in body["message"]


def test_codebase_map_route_no_packages(client):
    proj = client.post("/projects", json={"name": "mapproj2"}).json()
    r = client.post(f"/projects/{proj['id']}/codebase-map", json={"packages": [], "edges": []})
    assert r.status_code == 400


def test_codebase_map_route_success(client, monkeypatch):
    """With dot mocked, the route returns a base64 PNG data URI."""
    from meridian import codebase_map as cm
    monkeypatch.setattr(cm.shutil, "which", lambda name: "/usr/bin/dot")

    class _Proc:
        returncode = 0
        stderr = b""

    def fake_run(cmd, input=None, **kw):
        # The output path is the 3rd+4th args (-o <path>); write a fake PNG there.
        out_idx = cmd.index("-o") + 1
        with open(cmd[out_idx], "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\nFAKE")
        return _Proc()

    monkeypatch.setattr(cm.subprocess, "run", fake_run)
    proj = client.post("/projects", json={"name": "mapproj3"}).json()
    r = client.post(
        f"/projects/{proj['id']}/codebase-map",
        json={"packages": [{"name": "a", "node_count": 1}], "edges": []},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["image"].startswith("data:image/png;base64,")
    assert body["format"] == "png"
