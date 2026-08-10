"""f6912e2d — "Outputs hash/provenance checks" half of the executable
artifact recipe acceptance criteria.

Two things this file verifies, neither duplicating tests/test_docx_integrity_gate.py
(which covers the artifact_recipe SCHEMA / no-ambiguous-match / completeness
machinery in general — this file is the Outputs-specific slice of it):

1. extensions/meridian-outputs/README.md's tool table is not allowed to go
   stale again the way it had (search_outputs/annotate_outputs/
   classify_outputs/resolve_figure_output/npy_metadata/file_fingerprint/
   search_logs were documented; record_provenance/get_provenance/
   get_provenance_status/list_provenance/register_output_paths/
   get_convergence_state/find_outputs_by_source/bind_artifact_provenance/
   tag_output/check_staleness/find_stale_by_script/script_content_hash were
   not, despite being real, live @mcp.tool()-decorated tools). A recipe that
   declares ``outputs_provenance_check_required`` names
   meridian-outputs' hash/provenance tools by exact name
   (docx_integrity_gate.RECIPE_CHECK_REGISTRY points at this README) — that
   promise is only honest if every real tool actually appears here. This
   test reads server.py as TEXT (regex over ``@mcp.tool()``-decorated
   ``def`` names), never imports ``meridian_outputs`` itself — the package
   is an independently-installable extension, NOT a
   ``[pypi-dependencies]`` entry of the core ``meridian`` pixi env (see
   extensions/meridian-docs's identical boundary note in
   meridian/docx_integrity_gate.py's module docstring), so it is not
   importable in this repo's default test environment.
2. meridian.artifact_declaration's artifact_recipe
   ``outputs_provenance_check_required`` flag, exercised end-to-end through
   check_artifact_recipe_completeness for a "table"-kind item (the
   Outputs-producing artifact_kind, as opposed to document_only/figure),
   including the docx_integrity_gate.describe_required_checks resolution of
   that ONE flag to its exact meridian-outputs tool reference.
"""
from __future__ import annotations

import re
from pathlib import Path

from meridian import artifact_declaration as ad
from meridian import docx_integrity_gate as gate_module
from meridian import tool_requirements as tool_requirements_module

_REPO_ROOT = Path(__file__).resolve().parents[1]
_OUTPUTS_SERVER_PY = _REPO_ROOT / "extensions" / "meridian-outputs" / "meridian_outputs" / "server.py"
_OUTPUTS_README = _REPO_ROOT / "extensions" / "meridian-outputs" / "README.md"

_TOOL_DEF_RE = re.compile(r"@mcp\.tool\(\)\s*\ndef (\w+)\(", re.MULTILINE)


def _server_tool_names() -> list[str]:
    text = _OUTPUTS_SERVER_PY.read_text(encoding="utf-8")
    return _TOOL_DEF_RE.findall(text)


# ---------------------------------------------------------------------------
# 1. README tool-table completeness (repo-structure test — no import needed).
# ---------------------------------------------------------------------------

def test_outputs_server_has_at_least_the_known_tools():
    """Sanity check on the extraction regex itself -- if this starts
    failing, the regex (not the README) is what broke."""
    names = _server_tool_names()
    assert "search_outputs" in names
    assert "record_provenance" in names
    assert len(names) >= 18


def test_every_outputs_mcp_tool_is_documented_in_readme():
    """The regression this file exists to prevent: server.py grew
    record_provenance/get_provenance/get_provenance_status/list_provenance/
    register_output_paths/get_convergence_state/find_outputs_by_source/
    bind_artifact_provenance/tag_output/check_staleness/find_stale_by_script/
    script_content_hash as real tools, but README.md's table never grew with
    it -- an "exact MCP namespace/tool names" recipe pointing at this README
    for its Outputs hash/provenance checks would have been pointing at an
    incomplete list. Every real tool name must appear somewhere in the
    README text (word-boundary match, so e.g. "get_provenance" doesn't
    accidentally satisfy on a substring of "get_provenance_status" alone --
    both must be named)."""
    readme_text = _OUTPUTS_README.read_text(encoding="utf-8")
    tool_names = _server_tool_names()
    assert tool_names, "extraction found no tools -- regex likely broken"
    undocumented = [
        name for name in tool_names
        if not re.search(rf"`{re.escape(name)}`", readme_text)
    ]
    assert undocumented == [], f"tools missing from README.md's table: {undocumented}"


def test_hash_provenance_tools_specifically_documented():
    """The exact set of tools an outputs_provenance_check_required recipe
    flag resolves against (see RECIPE_CHECK_REGISTRY below) — named
    explicitly, not just "somewhere in the file"."""
    readme_text = _OUTPUTS_README.read_text(encoding="utf-8")
    for name in (
        "record_provenance", "get_provenance", "get_provenance_status",
        "list_provenance", "find_outputs_by_source", "bind_artifact_provenance",
        "tag_output", "check_staleness", "find_stale_by_script", "script_content_hash",
    ):
        assert re.search(rf"`{name}`", readme_text), f"{name} not documented in README.md"


# ---------------------------------------------------------------------------
# 2. artifact_recipe's outputs_provenance_check_required flag, end-to-end.
# ---------------------------------------------------------------------------

def _outputs_recipe(**overrides) -> dict:
    base = {
        "execution_path": "local",
        "rollback_policy": "manual_restore",
        "checks": {"outputs_provenance_check_required": True},
        "focused_tests": ["tests/test_outputs_provenance.py::test_hash_provenance_tools_specifically_documented"],
    }
    base.update(overrides)
    return base


def test_outputs_recipe_flag_resolves_to_meridian_outputs_registry_entry():
    item = {"artifact_recipe": ad.serialize_artifact_recipe(_outputs_recipe())}
    described = gate_module.describe_required_checks(item)
    assert described["declared"] is True
    assert set(described["required"]) == {"outputs_provenance_check_required"}
    reference = described["required"]["outputs_provenance_check_required"]
    assert "meridian-outputs" in reference
    assert "record_provenance" in reference or "get_provenance_status" in reference


def test_table_kind_item_completeness_with_outputs_recipe():
    """A "table" artifact_kind item (the Outputs-producing kind, alongside
    "figure") with a complete recipe -- including an exact tool_requirements
    entry naming meridian-outputs' record_provenance by namespace+name (the
    "exact MCP namespace/tool names" acceptance criterion) -- is reported
    complete."""
    item = {
        "artifact_kind": "table",
        "planned_output": ad.serialize_planned_output({
            "source_type": "code",
            "targets": [{
                "uri": "outputs/results_summary.csv",
                "selector": {"type": "range", "start_line": 1, "end_line": 1},
                "target_kind": "planned_new",
            }],
            "provenance_required": True,
        }),
        "artifact_recipe": ad.serialize_artifact_recipe(_outputs_recipe()),
        "tool_requirements": tool_requirements_module.canonical_json(
            tool_requirements_module.normalize_tool_requirements([{
                "name": "record_provenance",
                "server_or_namespace": "meridian-outputs",
                "required_or_preferred": "required",
                "purpose": "record reproducibility metadata for the promoted table",
            }])
        ),
    }
    result = ad.check_artifact_recipe_completeness(item)
    assert result["complete"] is True, result["missing"]
    assert result["artifact_kind"] == "table"


def test_table_kind_item_missing_outputs_recipe_is_incomplete():
    item = {"artifact_kind": "table"}
    result = ad.check_artifact_recipe_completeness(item)
    assert result["complete"] is False
    assert "artifact_recipe" in result["missing"]
    assert "tool_requirements" in result["missing"]
    assert "planned_output" in result["missing"]


def test_normalize_artifact_recipe_accepts_outputs_provenance_check_flag():
    normalized = ad.normalize_artifact_recipe(_outputs_recipe())
    assert normalized["checks"]["outputs_provenance_check_required"] is True
    assert normalized["checks"]["structural_check_required"] is False
    assert normalized["checks"]["word_com_render_check_required"] is False
