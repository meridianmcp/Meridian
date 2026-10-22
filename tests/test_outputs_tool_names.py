"""Guard against an MCP tool name registered by both Meridian core and meridian-outputs.

Core registers its tools as dict entries in the ``_MCP_TOOLS_LIST`` literal of
``meridian/mcp_tools.py``. The meridian-outputs extension registers its tools
with ``@mcp.tool()`` in ``extensions/meridian-outputs/meridian_outputs/server.py``.
Three names are registered by both today. That overlap is accepted and listed in
``KNOWN_DUPLICATES``. Any other name registered by both fails the test, and the
failure message names it.

The test reads both files as text and parses them with ``ast``. It imports
nothing from ``meridian_outputs``, so it runs in the core environment without
the extension installed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CORE_TOOLS_PY = _REPO / "meridian" / "mcp_tools.py"
_OUTPUTS_SERVER_PY = _REPO / "extensions" / "meridian-outputs" / "meridian_outputs" / "server.py"

KNOWN_DUPLICATES = frozenset({"search_outputs", "annotate_outputs", "find_outputs_by_source"})

# A core tool that the extension does not register. Adding it to the extension
# must create a new duplicate.
_PROBE_TOOL = "register_run_artifact"


def _literal_name(entry: ast.expr) -> str:
    assert isinstance(entry, ast.Dict), "a _MCP_TOOLS_LIST entry is not a dict literal"
    for key, value in zip(entry.keys, entry.values):
        if isinstance(key, ast.Constant) and key.value == "name":
            assert isinstance(value, ast.Constant) and isinstance(value.value, str), (
                "a _MCP_TOOLS_LIST entry has a non-literal 'name'"
            )
            return value.value
    raise AssertionError("a _MCP_TOOLS_LIST entry has no literal 'name' key")


def core_tool_names(source: str) -> set[str]:
    """Return the tool names in the ``_MCP_TOOLS_LIST`` literal of meridian/mcp_tools.py."""
    names: set[str] = set()
    for node in ast.parse(source).body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_MCP_TOOLS_LIST"
        ):
            assert isinstance(node.value, ast.List), "_MCP_TOOLS_LIST is no longer a list literal"
            names.update(_literal_name(entry) for entry in node.value.elts)
    assert names, "found no tools in _MCP_TOOLS_LIST"
    return names


def _mcp_tool_name(decorator: ast.expr, func_name: str) -> str | None:
    """Return the name a ``@mcp.tool(...)`` decorator registers, or None for any other decorator.

    FastMCP registers the function's own name unless the decorator passes ``name=``.
    """
    call = decorator if isinstance(decorator, ast.Call) else None
    target = call.func if call is not None else decorator
    if not (
        isinstance(target, ast.Attribute)
        and target.attr == "tool"
        and isinstance(target.value, ast.Name)
        and target.value.id == "mcp"
    ):
        return None
    if call is not None:
        for keyword in call.keywords:
            if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                return str(keyword.value.value)
    return func_name


def outputs_tool_names(source: str) -> set[str]:
    """Return the tool names registered by ``@mcp.tool()`` functions in the outputs server."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            registered = _mcp_tool_name(decorator, node.name)
            if registered is not None:
                names.add(registered)
    assert names, "found no @mcp.tool() functions in the meridian-outputs server"
    return names


def unlisted_duplicates(core_names: set[str], outputs_names: set[str]) -> list[str]:
    """Return the names registered by both sides that are not in KNOWN_DUPLICATES, sorted."""
    return sorted((core_names & outputs_names) - KNOWN_DUPLICATES)


def assert_no_unlisted_duplicates(core_names: set[str], outputs_names: set[str]) -> None:
    new = unlisted_duplicates(core_names, outputs_names)
    if new:
        raise AssertionError(
            "tool name(s) registered by both meridian core and meridian-outputs and not in "
            f"KNOWN_DUPLICATES: {', '.join(new)}. Rename one side, or add the name to "
            "KNOWN_DUPLICATES in tests/test_outputs_tool_names.py after review."
        )


def test_known_duplicates_only():
    core_names = core_tool_names(_CORE_TOOLS_PY.read_text(encoding="utf-8"))
    outputs_names = outputs_tool_names(_OUTPUTS_SERVER_PY.read_text(encoding="utf-8"))
    overlap = core_names & outputs_names
    assert overlap == KNOWN_DUPLICATES, (
        f"overlap {sorted(overlap)} differs from KNOWN_DUPLICATES {sorted(KNOWN_DUPLICATES)}"
    )
    assert_no_unlisted_duplicates(core_names, outputs_names)


def test_new_duplicate_fails():
    core_names = core_tool_names(_CORE_TOOLS_PY.read_text(encoding="utf-8"))
    outputs_source = _OUTPUTS_SERVER_PY.read_text(encoding="utf-8")
    assert _PROBE_TOOL in core_names, f"probe {_PROBE_TOOL} is no longer a core tool; pick another"
    assert _PROBE_TOOL not in outputs_tool_names(outputs_source), (
        f"probe {_PROBE_TOOL} is already an outputs tool; pick another"
    )

    added = outputs_source + (
        "\n\n@mcp.tool()\n"
        f"def {_PROBE_TOOL}(outputs_dir: str) -> dict:\n"
        "    return {}\n"
    )
    outputs_names = outputs_tool_names(added)
    assert unlisted_duplicates(core_names, outputs_names) == [_PROBE_TOOL]
    with pytest.raises(AssertionError, match=_PROBE_TOOL):
        assert_no_unlisted_duplicates(core_names, outputs_names)
