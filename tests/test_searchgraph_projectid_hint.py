"""7ef712a8 — code-intel graph tools identify a project by the LOCAL REPO-PATH
slug, not the Meridian planning-project name. When a code-intel graph lookup
misses on the project, ``call_tunnel_tool`` should raise an *enriched* error that
(a) explains the slug-vs-planning-name distinction and (b) lists the available
indexed project identifiers (closest match first) so the caller can retry.

These tests exercise the pure helpers plus the end-to-end error path in
``call_tunnel_tool`` (with the tunnel JSON-RPC layer stubbed).
"""
from __future__ import annotations

import pytest

from meridian.routes import tunnel


# --------------------------------------------------------------------------- #
# Pure predicate / ranking helpers
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("msg", [
    "project not found",
    "Project 'meridian-build' not found",
    "no project matching that id",
    "unknown project: meridian-build",
    "project does not exist",
    "that project is not indexed yet",
    # 1579bc1e — broadened phrasing/subject-noun coverage: confirmed live
    # failures showed the retry not firing because the actual text used a
    # different noun ("repository"/"repo" instead of "project") or a
    # phrasing variant (a contraction, or an extra word breaking what used
    # to be a required contiguous substring).
    "repository not found or not indexed",
    "repo is not indexed",
    "the repository hasn't been indexed yet",
    "could not find that project",
    "couldn't find repo 'meridian-build'",
    "no matching project for that repo-path slug",
    "unknown repository: meridian-build",
])
def test_is_project_not_found_matches_known_phrasings(msg):
    assert tunnel._is_project_not_found_error(msg) is True


@pytest.mark.parametrize("msg", [
    "",
    "syntax error in query",
    "file not found",  # 'file', not 'project'/'repo'/'repository'
    "rate limited",
    "the graph is empty",
    # Noun present, but no not-found-shaped phrase attached to it.
    "the repository returned 200 results",
    "project search completed successfully",
])
def test_is_project_not_found_ignores_unrelated_errors(msg):
    assert tunnel._is_project_not_found_error(msg) is False


def test_closest_project_ids_floats_best_match_first():
    available = [
        "some-other-repo",
        "C-Users-13144-Documents-Meridian-repository",
        "totally-unrelated",
    ]
    ranked = tunnel._closest_project_ids("Meridian-repository", available)
    assert ranked[0] == "C-Users-13144-Documents-Meridian-repository"
    # No entries dropped.
    assert sorted(ranked) == sorted(available)


def test_closest_project_ids_degenerate_inputs():
    assert tunnel._closest_project_ids("", ["a", "b"]) == ["a", "b"]
    assert tunnel._closest_project_ids("x", []) == []


# --------------------------------------------------------------------------- #
# _list_indexed_project_ids — payload extraction shapes
# --------------------------------------------------------------------------- #

def _text_result(obj):
    """Wrap a python object as an MCP tools/call text-content result."""
    import json
    return {"content": [{"type": "text", "text": json.dumps(obj)}]}


@pytest.mark.asyncio
async def test_list_indexed_project_ids_extracts_various_shapes(monkeypatch):
    payload = {"projects": [
        {"id": "C-Users-x-repo-a"},
        {"project_id": "C-Users-x-repo-b"},
        {"name": "repo-c"},
        "bare-string-id",
        {"id": "C-Users-x-repo-a"},  # dup, should collapse
    ]}

    async def _fake_call(tenant_id, name, args, **kw):
        assert name == "codebase__list_projects"
        return _text_result(payload)

    monkeypatch.setattr(tunnel, "call_tunnel_tool", _fake_call)
    ids = await tunnel._list_indexed_project_ids("tenant-1")
    assert ids == [
        "C-Users-x-repo-a", "C-Users-x-repo-b", "repo-c", "bare-string-id",
    ]


@pytest.mark.asyncio
async def test_list_indexed_project_ids_never_raises(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("tunnel down")

    monkeypatch.setattr(tunnel, "call_tunnel_tool", _boom)
    assert await tunnel._list_indexed_project_ids("t") == []


# --------------------------------------------------------------------------- #
# _enrich_code_intel_project_error — the actionable message
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_enrich_adds_slug_hint_and_available_ids(monkeypatch):
    async def _fake_list(tenant_id):
        return [
            "some-other-repo",
            "C-Users-13144-Documents-Meridian-repository",
        ]

    monkeypatch.setattr(tunnel, "_list_indexed_project_ids", _fake_list)
    out = await tunnel._enrich_code_intel_project_error(
        "tenant-1",
        "codebase__search_graph",
        {"project_id": "meridian-build", "query": "foo"},
        "project not found",
    )
    # Explains the identifier is the repo-path slug, not the planning name.
    assert "repo-path" in out.lower()
    assert "planning-project name" in out.lower()
    # Echoes what the caller passed.
    assert "meridian-build" in out
    # Lists the available identifiers, closest-match first + retry suggestion.
    assert "C-Users-13144-Documents-Meridian-repository" in out
    assert "Retry with the closest match" in out
    # Original message is preserved.
    assert "project not found" in out


@pytest.mark.asyncio
async def test_enrich_noop_for_non_project_tool(monkeypatch):
    called = False

    async def _fake_list(tenant_id):
        nonlocal called
        called = True
        return ["x"]

    monkeypatch.setattr(tunnel, "_list_indexed_project_ids", _fake_list)
    # index_repository is not a project-lookup tool → leave the error untouched.
    out = await tunnel._enrich_code_intel_project_error(
        "t", "codebase__index_repository", {}, "project not found"
    )
    assert out == "project not found"
    assert called is False


@pytest.mark.asyncio
async def test_enrich_noop_for_unrelated_error(monkeypatch):
    out = await tunnel._enrich_code_intel_project_error(
        "t", "codebase__search_graph", {"query": "x"}, "query syntax error"
    )
    assert out == "query syntax error"


@pytest.mark.asyncio
async def test_enrich_guidance_when_nothing_indexed(monkeypatch):
    async def _empty(tenant_id):
        return []

    monkeypatch.setattr(tunnel, "_list_indexed_project_ids", _empty)
    out = await tunnel._enrich_code_intel_project_error(
        "t", "codebase__search_graph", {"project_id": "meridian-build"},
        "unknown project",
    )
    assert "index_repository" in out
    assert "repo-path" in out.lower()


# --------------------------------------------------------------------------- #
# End-to-end: call_tunnel_tool re-raises the ENRICHED error
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_call_tunnel_tool_enriches_project_not_found(monkeypatch):
    tenant = "tenant-e2e"
    tool = "codebase__search_graph"

    # Route the tool to the 'code' slot and register a fake socket so the
    # in-tenant guard passes.
    tunnel._tunnel_tool_routes[tenant] = {tool: "code"}
    tunnel._tunnel_code_sockets[tenant] = object()  # sentinel; never awaited

    async def _fake_jsonrpc(tenant_id, label, method, params, repo_path=None):
        assert method == "tools/call"
        # The graph tool call errors on the project id.
        return {"error": {"message": "project 'meridian-build' not found"}}

    async def _fake_list(tenant_id):
        return ["C-Users-13144-Documents-Meridian-repository"]

    monkeypatch.setattr(tunnel, "_tunnel_jsonrpc", _fake_jsonrpc)
    monkeypatch.setattr(tunnel, "_list_indexed_project_ids", _fake_list)

    try:
        with pytest.raises(RuntimeError) as exc:
            await tunnel.call_tunnel_tool(
                tenant, tool, {"project_id": "meridian-build", "query": "foo"}
            )
    finally:
        tunnel._tunnel_tool_routes.pop(tenant, None)
        tunnel._tunnel_code_sockets.pop(tenant, None)

    msg = str(exc.value)
    assert "repo-path" in msg.lower()
    assert "C-Users-13144-Documents-Meridian-repository" in msg
    # Original error text survives.
    assert "meridian-build" in msg


@pytest.mark.asyncio
async def test_call_tunnel_tool_leaves_non_project_error_untouched(monkeypatch):
    tenant = "tenant-e2e-2"
    tool = "codebase__search_graph"
    tunnel._tunnel_tool_routes[tenant] = {tool: "code"}
    tunnel._tunnel_code_sockets[tenant] = object()

    async def _fake_jsonrpc(tenant_id, label, method, params, repo_path=None):
        return {"error": {"message": "query syntax error near '('"}}

    monkeypatch.setattr(tunnel, "_tunnel_jsonrpc", _fake_jsonrpc)

    try:
        with pytest.raises(RuntimeError) as exc:
            await tunnel.call_tunnel_tool(tenant, tool, {"query": "("})
    finally:
        tunnel._tunnel_tool_routes.pop(tenant, None)
        tunnel._tunnel_code_sockets.pop(tenant, None)

    msg = str(exc.value)
    assert "query syntax error" in msg
    assert "repo-path" not in msg.lower()


@pytest.mark.asyncio
async def test_call_tunnel_tool_error_dict_without_message_key_still_surfaces_text(
    monkeypatch,
):
    """1579bc1e — regression for a real gap: call_tunnel_tool used to build
    the raised error's text via ``err.get("message")`` alone. A JSON-RPC
    error object that carries its human-readable text under a different key
    (``"error"``/``"detail"``/a string ``"data"`` field — not every server
    strictly follows the JSON-RPC "message" convention) made ``.get("message")``
    return None, and ``str(None)`` produced the literal text "None" instead of
    the real diagnostic. That silently destroyed the "project not found"
    signal before it ever reached ``_is_project_not_found_error``, so the
    downstream retry-without-project_id fallback in prospect.py could never
    fire — regardless of how good the phrase-matching itself is. The fix
    probes "message", "error", "detail", then a string "data" field before
    falling back to stringifying the whole dict."""
    tenant = "tenant-e2e-nomsgkey"
    tool = "codebase__search_graph"
    tunnel._tunnel_tool_routes[tenant] = {tool: "code"}
    tunnel._tunnel_code_sockets[tenant] = object()

    async def _fake_jsonrpc(tenant_id, label, method, params, repo_path=None):
        # No "message" key at all — text lives in "data" instead.
        return {"error": {"code": 404, "data": "project not found or not indexed"}}

    async def _fake_list(tenant_id):
        return []

    monkeypatch.setattr(tunnel, "_tunnel_jsonrpc", _fake_jsonrpc)
    monkeypatch.setattr(tunnel, "_list_indexed_project_ids", _fake_list)

    try:
        with pytest.raises(RuntimeError) as exc:
            await tunnel.call_tunnel_tool(
                tenant, tool, {"project_id": "meridian-build", "query": "foo"}
            )
    finally:
        tunnel._tunnel_tool_routes.pop(tenant, None)
        tunnel._tunnel_code_sockets.pop(tenant, None)

    msg = str(exc.value)
    # The real diagnostic text must survive — never collapse to "None".
    assert "project not found or not indexed" in msg
    assert msg.strip() != "None"
    # It must also still be recognisable as a project-not-found error, since
    # that's what the whole retry mechanism depends on.
    assert tunnel._is_project_not_found_error(msg) is True


@pytest.mark.asyncio
async def test_call_tunnel_tool_error_string_data_falls_back_when_no_error_key(
    monkeypatch,
):
    """Same gap, minimal shape: only "data" is present (no "message", no
    "error" string). Must still surface the real text, not "None"."""
    tenant = "tenant-e2e-dataonly"
    tool = "codebase__search_graph"
    tunnel._tunnel_tool_routes[tenant] = {tool: "code"}
    tunnel._tunnel_code_sockets[tenant] = object()

    async def _fake_jsonrpc(tenant_id, label, method, params, repo_path=None):
        return {"error": {"code": 404, "data": "repository is not indexed"}}

    async def _fake_list(tenant_id):
        return []

    monkeypatch.setattr(tunnel, "_tunnel_jsonrpc", _fake_jsonrpc)
    monkeypatch.setattr(tunnel, "_list_indexed_project_ids", _fake_list)

    try:
        with pytest.raises(RuntimeError) as exc:
            await tunnel.call_tunnel_tool(tenant, tool, {"query": "foo"})
    finally:
        tunnel._tunnel_tool_routes.pop(tenant, None)
        tunnel._tunnel_code_sockets.pop(tenant, None)

    msg = str(exc.value)
    assert "repository is not indexed" in msg
    assert msg.strip() != "None"
