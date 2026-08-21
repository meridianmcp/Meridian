"""README/tool-surface parity contract for meridian-outputs (item 69719f48).

Prior state: ``README.md``'s ``## Tools`` table only documented 7 of the 19
``@mcp.tool()``-decorated functions actually exposed by
``meridian_outputs/server.py`` -- ``register_output_paths``,
``get_convergence_state``, ``record_provenance``, ``get_provenance``,
``get_provenance_status``, ``list_provenance``, ``find_outputs_by_source``,
``bind_artifact_provenance``, ``tag_output``, ``check_staleness``,
``find_stale_by_script``, and ``script_content_hash`` were all silently
undocumented.

This test parses ``server.py``'s AST (no import needed -- avoids pulling in
optional runtime deps like tantivy/duckdb just to enumerate tool names) for
every function decorated with ``@mcp.tool()`` and asserts each one appears as
a backtick-quoted entry (`` `tool_name` ``) in ``README.md``. It is intended
to catch future drift the same way this gap itself went unnoticed: a new
``@mcp.tool()`` added to ``server.py`` without a matching README row.

Note: this repo's README-parity contract is about the REAL, currently
exposed tool surface only. It deliberately does not (and must not) assert
anything about tool names that don't exist in ``server.py`` -- see the
sprint-item discussion in git history (69719f48) for why a handful of
tool names once associated with this item (``validate_output_semantics``,
``write_artifact_registry``, ``resolve_artifact_registry``,
``serialize_provenance_envelope``, ``parse_provenance_envelope``) are absent
from this check: none of them exist anywhere in this package as of this
commit (confirmed via repo-wide search), so documenting them here would
fabricate API surface rather than restore parity.
"""
from __future__ import annotations

import ast
from pathlib import Path

_EXT_ROOT = Path(__file__).parent.parent
_SERVER_PY = _EXT_ROOT / "meridian_outputs" / "server.py"
_README = _EXT_ROOT / "README.md"


def _is_mcp_tool_decorator(node: ast.expr) -> bool:
    """True for a decorator shaped like ``@mcp.tool()`` or ``@mcp.tool``."""
    target = node
    if isinstance(target, ast.Call):
        target = target.func
    if isinstance(target, ast.Attribute):
        return (
            target.attr == "tool"
            and isinstance(target.value, ast.Name)
            and target.value.id == "mcp"
        )
    return False


def _declared_tool_names() -> list[str]:
    """Every function name decorated with ``@mcp.tool()`` in server.py, in
    source order."""
    tree = ast.parse(_SERVER_PY.read_text(encoding="utf-8"), filename=str(_SERVER_PY))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(_is_mcp_tool_decorator(dec) for dec in node.decorator_list):
                names.append(node.name)
    return names


class TestReadmeToolCoverage:
    def test_server_declares_the_expected_tool_set(self) -> None:
        """Sanity check on the parser itself: fails loudly (rather than
        silently passing on an empty list) if server.py's shape changes in a
        way the AST walk above no longer recognises."""
        names = _declared_tool_names()
        assert len(names) >= 19, (
            f"expected at least 19 @mcp.tool() functions in server.py, "
            f"found {len(names)}: {names}"
        )

    def test_every_declared_tool_is_documented_in_readme(self) -> None:
        readme_text = _README.read_text(encoding="utf-8")
        missing = [
            name for name in _declared_tool_names()
            if f"`{name}`" not in readme_text
        ]
        assert not missing, (
            "The following @mcp.tool() functions in server.py have no "
            f"backtick-quoted entry in README.md: {missing}"
        )

    def test_readme_does_not_document_nonexistent_tools(self) -> None:
        """Guards the other direction: README must not claim a tool exists
        (as a live, undecorated `Tools` table entry) that isn't actually
        declared in server.py -- e.g. fabricated/aspirational entries."""
        declared = set(_declared_tool_names())
        readme_text = _README.read_text(encoding="utf-8")
        tools_section = readme_text.split("## Tools", 1)[1]
        tools_section = tools_section.split("\n## ", 1)[0]
        for line in tools_section.splitlines():
            line = line.strip()
            if not line.startswith("| `"):
                continue
            name = line.split("`")[1]
            assert name in declared, (
                f"README documents `{name}` as a live tool but it is not "
                "declared with @mcp.tool() in server.py"
            )
