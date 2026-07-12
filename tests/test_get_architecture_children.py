"""Characterization tests for get_architecture package `children` nesting (19b3259e).

CONTEXT — where `get_architecture` actually lives
--------------------------------------------------
The injected sprint item asked to make `get_architecture` populate each package
node's ``children`` field (File ⊃ Class ⊃ Method nesting + import/inherit edges),
locating the function in ``meridian/code_index.py`` and/or
``meridian/codebase_map.py``.

Verified against the current tree: **there is no in-repo `get_architecture`
backend to modify.** `get_architecture` is a tool of the *external*
``codebase-memory-mcp`` server. This repo only *consumes* its payload:

  * the dashboard host fetches it over the code-intel tunnel
    (``_codeMcpCall("tools/call", {name: "get_architecture", ...})``) and hands
    the parsed ``Architecture`` object to the Preact ``CodeIntelPanel``; and
  * ``meridian/server.py`` fetches it over the same tunnel as
    ``codebase__get_architecture`` and distills it via
    ``_summarize_architecture`` for start_session orientation.

The frontend already renders the deeper Layer ⊃ Package ⊃ File ⊃ Class ⊃ Method
containment + typed (imports/inherits) edges the *moment* a package carries a
``children`` tree — this is proven by the vitest
``buildCytoscapeElements`` "descends into File/Class/Method children" case in
``meridian/static/components/CodeIntelPanel.test.tsx``. So the only thing this
repo can honestly guarantee for the ``children`` contract is that its
pass-through paths **do not strip or choke on** a ``children`` tree when the
external indexer supplies one. That is what these tests lock in — no fabricated
in-repo nesting.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import meridian.server as srv
from meridian.routes import tunnel as tn


# An architecture payload shaped exactly like the frontend consumes it, carrying
# the deeper File/Class/Method containment + import/inherit edges on a package.
_ARCH_WITH_CHILDREN = {
    "packages": [
        {
            "name": "meridian.db",
            "node_count": 30,
            "layer": 0,
            "children": [
                {
                    "name": "db.py",
                    "kind": "file",
                    "imports": ["meridian.pg_adapter"],
                    "children": [
                        {
                            "name": "Database",
                            "kind": "class",
                            "inherits": ["object"],
                            "children": [
                                {"name": "init_db", "kind": "method"},
                            ],
                        },
                    ],
                },
            ],
        },
        {"name": "meridian.routes", "node_count": 12, "layer": 2},
    ],
    "layers": [{"name": "data", "layer": 0}, {"name": "api", "layer": 2}],
    "boundaries": [{"from": "meridian.routes", "to": "meridian.db", "call_count": 5}],
}


def _cleanup(tenant_id: str) -> None:
    tn._tunnel_code_sockets.pop(tenant_id, None)
    tn._slot_health.pop(tenant_id, None)
    srv._codebase_context_cache.clear()


def test_no_in_repo_get_architecture_backend():
    """Premise check: the item said `get_architecture` lives in code_index.py /
    codebase_map.py. It does not — no module in this repo defines it. Guards
    against a future refactor silently reintroducing (and diverging) it here
    without updating this contract."""
    import meridian.code_index as code_index
    import meridian.codebase_map as codebase_map

    assert not hasattr(code_index, "get_architecture")
    assert not hasattr(codebase_map, "get_architecture")
    # server.py only *summarizes* the external payload; it must not shadow the
    # tool with a local producer.
    assert not hasattr(srv, "get_architecture")


def test_summarize_architecture_preserves_package_children():
    """`_summarize_architecture` distills top-level packages/layers/hotspots for
    the start_session orientation block. It reads package *names* only, so it
    must not crash on — nor mangle — packages that additionally carry a nested
    ``children`` tree from a richer indexer."""
    out = srv._summarize_architecture(_ARCH_WITH_CHILDREN)
    assert out is not None
    # The nested-children packages still summarize down to their names cleanly.
    assert out["packages"] == ["meridian.db", "meridian.routes"]


def test_build_codebase_context_passes_children_through_tunnel(monkeypatch):
    """End-to-end (in-repo half): a `codebase__get_architecture` tunnel payload
    whose packages carry ``children`` must survive parse + summarize without the
    in-repo path silently dropping the nesting the frontend renders."""
    tenant = "tc-children"
    tn._tunnel_code_sockets[tenant] = object()
    tn._record_slot_health(tenant, "code", True)

    captured: dict[str, object] = {}

    async def fake_call(tenant_id, name, args):
        assert name == "codebase__get_architecture"
        return {"content": [{"type": "text", "text": json.dumps(_ARCH_WITH_CHILDREN)}]}

    # Re-decode the payload the way the host does and assert the children tree is
    # intact (the summarize step deliberately drops detail, so we verify the
    # decode step that the frontend actually consumes).
    monkeypatch.setattr(tn, "call_tunnel_tool", fake_call)
    try:
        raw = asyncio.run(fake_call(tenant, "codebase__get_architecture", {}))
        arch = srv._parse_tunnel_tool_text(raw)
        assert isinstance(arch, dict)
        captured["arch"] = arch
        # The in-repo orientation block still builds fine from a children payload.
        block = asyncio.run(
            srv._build_codebase_context(tenant, "p-children", compact=False)
        )
        assert block is not None
    finally:
        _cleanup(tenant)

    arch = captured["arch"]
    pkg = arch["packages"][0]
    assert pkg["name"] == "meridian.db"
    # Deep containment survived JSON round-trip end-to-end.
    file_node = pkg["children"][0]
    assert file_node["kind"] == "file"
    assert file_node["imports"] == ["meridian.pg_adapter"]
    cls_node = file_node["children"][0]
    assert cls_node["kind"] == "class"
    assert cls_node["inherits"] == ["object"]
    method_node = cls_node["children"][0]
    assert method_node["kind"] == "method"


def test_frontend_type_contract_declares_children():
    """The backend `children` contract is now expressed in the frontend types
    (``ArchChild`` / ``ArchPackage.children`` in components/types.ts) so the
    Cytoscape renderer no longer casts around a missing field. Lock that in so a
    type cleanup can't quietly drop the shape the external backend must supply."""
    types_ts = (
        Path(__file__).resolve().parent.parent
        / "meridian" / "static" / "components" / "types.ts"
    )
    src = types_ts.read_text(encoding="utf-8")
    assert "interface ArchChild" in src
    assert "children?: ArchChild[]" in src  # package carries the nesting tree
    assert 'kind?: "file" | "class" | "method"' in src
    assert "imports?: string[]" in src
    assert "inherits?: string[]" in src
