"""caf95f81 — get_code_snippet truncation detection.

The external codebase-memory-mcp server silently truncates the source field
when a function is very long — returning a declared line range (start_line /
end_line) that is larger than the actual source text, with NO indicator that
truncation occurred. Meridian detects this in ``call_tunnel_tool`` (code-slot
branch) and attaches a ``truncation_warning`` key so the caller knows to
re-fetch the missing tail via a direct file read.

These tests exercise the pure ``_check_code_snippet_truncation`` helper
(positive, negative, and fail-open cases) and the end-to-end path through
``call_tunnel_tool`` with the tunnel JSON-RPC layer stubbed, mirroring the
style of tests/test_searchgraph_projectid_hint.py.
"""
from __future__ import annotations

import json

import pytest

from meridian.routes import tunnel


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _snippet_result(payload: dict) -> dict:
    """Wrap a get_code_snippet payload as an MCP tools/call result."""
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def _make_source(n_lines: int) -> str:
    """Build a source string with exactly ``n_lines`` lines."""
    return "\n".join(f"line {i}" for i in range(1, n_lines + 1))


# --------------------------------------------------------------------------- #
# _check_code_snippet_truncation — positive: warning fires on truncated snippet
# --------------------------------------------------------------------------- #

def test_truncation_warning_fires_when_short():
    """A snippet materially shorter than [start_line, end_line] gets a warning."""
    start_line = 5372
    end_line = 5485  # 114 lines expected
    # Return only ~70 lines — clearly truncated (> _TRUNCATION_SLACK short).
    source = _make_source(70)
    result = _snippet_result({
        "start_line": start_line,
        "end_line": end_line,
        "source": source,
    })
    out = tunnel._check_code_snippet_truncation(result)
    assert "truncation_warning" in out
    warning = out["truncation_warning"]
    assert "truncated" in warning.lower()
    # Reports actual vs expected counts.
    assert "114" in warning          # expected_lines = 5485 - 5372 + 1
    assert "70" in warning           # actual_lines
    assert "5372" in warning
    assert "5485" in warning


def test_truncation_warning_mentions_refetch_hint():
    """The warning message suggests re-fetching via a direct file read."""
    result = _snippet_result({
        "start_line": 1,
        "end_line": 50,
        "source": _make_source(20),
    })
    out = tunnel._check_code_snippet_truncation(result)
    warning = out.get("truncation_warning", "")
    # Should guide the caller toward a filesystem read.
    assert "read" in warning.lower() or "fetch" in warning.lower()


def test_truncation_warning_probes_code_field():
    """Truncation detection also works when the source is in the 'code' field."""
    result = _snippet_result({
        "start_line": 10,
        "end_line": 100,  # 91 lines expected
        "code": _make_source(40),
    })
    out = tunnel._check_code_snippet_truncation(result)
    assert "truncation_warning" in out


def test_truncation_warning_probes_snippet_field():
    """Truncation detection also works when the source is in the 'snippet' field."""
    result = _snippet_result({
        "start_line": 1,
        "end_line": 80,
        "snippet": _make_source(30),
    })
    out = tunnel._check_code_snippet_truncation(result)
    assert "truncation_warning" in out


# --------------------------------------------------------------------------- #
# _check_code_snippet_truncation — negative: no warning on complete snippet
# --------------------------------------------------------------------------- #

def test_no_warning_on_complete_snippet():
    """A snippet that matches the declared range does NOT get a warning."""
    start = 1
    end = 50
    result = _snippet_result({
        "start_line": start,
        "end_line": end,
        "source": _make_source(50),  # exact match
    })
    out = tunnel._check_code_snippet_truncation(result)
    assert "truncation_warning" not in out


def test_no_warning_within_slack():
    """A snippet that is within _TRUNCATION_SLACK lines of the range is not warned."""
    # Expected 50 lines; actual 49 — within the allowed slack (2 lines).
    result = _snippet_result({
        "start_line": 1,
        "end_line": 50,
        "source": _make_source(49),
    })
    out = tunnel._check_code_snippet_truncation(result)
    assert "truncation_warning" not in out


def test_no_warning_on_exactly_slack_boundary():
    """A snippet exactly at the slack boundary is not warned."""
    # Expected 50 lines; actual 48 — exactly at slack boundary (50 - 48 = 2 == slack).
    result = _snippet_result({
        "start_line": 1,
        "end_line": 50,
        "source": _make_source(48),
    })
    out = tunnel._check_code_snippet_truncation(result)
    assert "truncation_warning" not in out


def test_warning_just_past_slack_boundary():
    """A snippet just past the slack boundary IS warned."""
    # Expected 50 lines; actual 47 — one past slack boundary (50 - 47 = 3 > 2).
    result = _snippet_result({
        "start_line": 1,
        "end_line": 50,
        "source": _make_source(47),
    })
    out = tunnel._check_code_snippet_truncation(result)
    assert "truncation_warning" in out


# --------------------------------------------------------------------------- #
# _check_code_snippet_truncation — fail-open: no exception, no false warning
# --------------------------------------------------------------------------- #

def test_fail_open_missing_start_line():
    """Missing start_line → skip enrichment silently."""
    result = _snippet_result({"end_line": 50, "source": _make_source(30)})
    out = tunnel._check_code_snippet_truncation(result)
    assert "truncation_warning" not in out
    assert out is result  # returned unchanged (no copy made)


def test_fail_open_missing_end_line():
    """Missing end_line → skip enrichment silently."""
    result = _snippet_result({"start_line": 1, "source": _make_source(30)})
    out = tunnel._check_code_snippet_truncation(result)
    assert "truncation_warning" not in out


def test_fail_open_missing_source_field():
    """No source/code/snippet field → skip enrichment silently."""
    result = _snippet_result({"start_line": 1, "end_line": 50})
    out = tunnel._check_code_snippet_truncation(result)
    assert "truncation_warning" not in out


def test_fail_open_non_dict_result():
    """A non-dict result (e.g. None, string) is returned unchanged without error."""
    assert tunnel._check_code_snippet_truncation(None) is None
    assert tunnel._check_code_snippet_truncation("raw string") == "raw string"
    assert tunnel._check_code_snippet_truncation(42) == 42


def test_fail_open_empty_content_list():
    """An empty content list → skip enrichment silently."""
    result = {"content": []}
    out = tunnel._check_code_snippet_truncation(result)
    assert "truncation_warning" not in out


def test_fail_open_non_text_content_block():
    """A content block that is not type=text → skip enrichment silently."""
    result = {"content": [{"type": "image", "data": "base64data"}]}
    out = tunnel._check_code_snippet_truncation(result)
    assert "truncation_warning" not in out


def test_fail_open_non_json_text_block():
    """A text block containing non-JSON text → skip enrichment silently."""
    result = {"content": [{"type": "text", "text": "Not JSON at all"}]}
    out = tunnel._check_code_snippet_truncation(result)
    assert "truncation_warning" not in out


def test_fail_open_non_integer_line_numbers():
    """String line numbers (wrong type) → skip enrichment silently."""
    result = _snippet_result({
        "start_line": "1",     # string, not int
        "end_line": "50",
        "source": _make_source(20),
    })
    out = tunnel._check_code_snippet_truncation(result)
    assert "truncation_warning" not in out


def test_fail_open_degenerate_range():
    """end_line < start_line → skip enrichment silently (degenerate range)."""
    result = _snippet_result({
        "start_line": 100,
        "end_line": 50,     # inverted
        "source": _make_source(20),
    })
    out = tunnel._check_code_snippet_truncation(result)
    assert "truncation_warning" not in out


def test_fail_open_missing_content_key():
    """Result dict with no 'content' key → returned unchanged."""
    result = {"result_type": "snippet"}
    out = tunnel._check_code_snippet_truncation(result)
    assert "truncation_warning" not in out
    assert out is result


# --------------------------------------------------------------------------- #
# End-to-end: call_tunnel_tool attaches the warning on a truncated response
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_call_tunnel_tool_attaches_truncation_warning(monkeypatch):
    """End-to-end: a truncated get_code_snippet response gets the warning attached."""
    tenant = "tenant-snippet-trunc"
    tool = "codebase__get_code_snippet"

    tunnel._tunnel_tool_routes[tenant] = {tool: "code"}
    tunnel._tunnel_code_sockets[tenant] = object()  # sentinel; never awaited

    snippet_payload = {
        "start_line": 5372,
        "end_line": 5485,  # 114 lines expected
        "source": _make_source(70),  # only 70 lines → truncated
    }

    async def _fake_jsonrpc(tenant_id, label, method, params, repo_path=None):
        assert method == "tools/call"
        return {
            "result": {
                "content": [{"type": "text", "text": json.dumps(snippet_payload)}]
            }
        }

    monkeypatch.setattr(tunnel, "_tunnel_jsonrpc", _fake_jsonrpc)

    try:
        result = await tunnel.call_tunnel_tool(
            tenant, tool, {"project_id": "my-repo", "qualified_name": "big_function"}
        )
    finally:
        tunnel._tunnel_tool_routes.pop(tenant, None)
        tunnel._tunnel_code_sockets.pop(tenant, None)

    assert result is not None
    assert "truncation_warning" in result
    warning = result["truncation_warning"]
    assert "truncated" in warning.lower()
    assert "114" in warning
    assert "70" in warning


@pytest.mark.asyncio
async def test_call_tunnel_tool_no_warning_on_complete_response(monkeypatch):
    """End-to-end: a complete get_code_snippet response has no warning."""
    tenant = "tenant-snippet-ok"
    tool = "codebase__get_code_snippet"

    tunnel._tunnel_tool_routes[tenant] = {tool: "code"}
    tunnel._tunnel_code_sockets[tenant] = object()

    snippet_payload = {
        "start_line": 1,
        "end_line": 50,
        "source": _make_source(50),  # exact match
    }

    async def _fake_jsonrpc(tenant_id, label, method, params, repo_path=None):
        return {
            "result": {
                "content": [{"type": "text", "text": json.dumps(snippet_payload)}]
            }
        }

    monkeypatch.setattr(tunnel, "_tunnel_jsonrpc", _fake_jsonrpc)

    try:
        result = await tunnel.call_tunnel_tool(
            tenant, tool, {"project_id": "my-repo", "qualified_name": "small_func"}
        )
    finally:
        tunnel._tunnel_tool_routes.pop(tenant, None)
        tunnel._tunnel_code_sockets.pop(tenant, None)

    assert result is not None
    assert "truncation_warning" not in result


@pytest.mark.asyncio
async def test_call_tunnel_tool_no_warning_for_non_snippet_tool(monkeypatch):
    """End-to-end: truncation check does NOT fire for non-get_code_snippet tools."""
    tenant = "tenant-search-graph"
    tool = "codebase__search_graph"

    tunnel._tunnel_tool_routes[tenant] = {tool: "code"}
    tunnel._tunnel_code_sockets[tenant] = object()

    # A payload that WOULD look truncated if we ran the check — but we shouldn't.
    snippet_payload = {
        "start_line": 1,
        "end_line": 100,
        "source": _make_source(20),
    }

    async def _fake_jsonrpc(tenant_id, label, method, params, repo_path=None):
        return {
            "result": {
                "content": [{"type": "text", "text": json.dumps(snippet_payload)}]
            }
        }

    monkeypatch.setattr(tunnel, "_tunnel_jsonrpc", _fake_jsonrpc)

    try:
        result = await tunnel.call_tunnel_tool(
            tenant, tool, {"project_id": "my-repo", "query": "foo"}
        )
    finally:
        tunnel._tunnel_tool_routes.pop(tenant, None)
        tunnel._tunnel_code_sockets.pop(tenant, None)

    # No warning — the check only applies to get_code_snippet.
    assert result is not None
    assert "truncation_warning" not in result


@pytest.mark.asyncio
async def test_call_tunnel_tool_fail_open_on_missing_fields(monkeypatch):
    """End-to-end: missing payload fields → result returned without warning, no raise."""
    tenant = "tenant-snippet-missing"
    tool = "codebase__get_code_snippet"

    tunnel._tunnel_tool_routes[tenant] = {tool: "code"}
    tunnel._tunnel_code_sockets[tenant] = object()

    # Payload has no start_line / end_line / source.
    sparse_payload = {"qualified_name": "some_fn"}

    async def _fake_jsonrpc(tenant_id, label, method, params, repo_path=None):
        return {
            "result": {
                "content": [{"type": "text", "text": json.dumps(sparse_payload)}]
            }
        }

    monkeypatch.setattr(tunnel, "_tunnel_jsonrpc", _fake_jsonrpc)

    try:
        result = await tunnel.call_tunnel_tool(
            tenant, tool, {"project_id": "my-repo", "qualified_name": "some_fn"}
        )
    finally:
        tunnel._tunnel_tool_routes.pop(tenant, None)
        tunnel._tunnel_code_sockets.pop(tenant, None)

    # Fail-open: result returned, no truncation_warning, no exception.
    assert result is not None
    assert "truncation_warning" not in result
