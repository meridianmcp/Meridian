"""932e8bd1 — meridian-docs [project] name must match the entry-point (uvx name-match invariant).

BUG (confirmed live, cache-clear ruled out staleness):
  `uvx --from <local-path> meridian-docs-mcp` fails with
  "meridian-docs-mcp was not found in the package registry" (or a variant with
  "meridian-docs") after the 58a044c7 entry-point rename.

ROOT CAUSE:
  58a044c7 renamed the console-script entry point from "meridian-docs" to
  "meridian-docs-mcp" (to fix the symmetric failure where command name == package
  name triggers a PyPI lookup). But the [project] name in pyproject.toml was left
  as "meridian-docs". In current uv (>=0.4), when `uvx --from <local-path> CMD`
  is called:
    1. uv installs the local package (named "meridian-docs") into an isolated venv.
    2. uv looks for a TOOL named "meridian-docs-mcp".
    3. If the tool name does NOT match the installed package name, uv falls back to
       a PyPI registry lookup for "meridian-docs-mcp" — which is NOT on PyPI.
    4. The registry lookup fails: "not found in the package registry".

  The existing tests (58a044c7) asserted that the SCRIPT was named "meridian-docs-mcp"
  and that the COMMAND used "meridian-docs-mcp" as the trailing token, but did NOT
  assert that `[project].name` (the installed package name) matches the script name.
  That gap is exactly why the bug shipped undetected.

FIX (932e8bd1):
  Rename `[project] name = "meridian-docs"` to `name = "meridian-docs-mcp"` in
  extensions/meridian-docs/pyproject.toml. Now the package name equals the tool name,
  so uvx finds the script without any registry fallback.

INVARIANT (the uvx name-match rule):
  For a local-path uvx invocation (`uvx --from <path> TOOL_NAME`), uv avoids the
  registry lookup only when the installed package name matches the tool name. Both
  58a044c7 (entry-point rename) and 932e8bd1 (package name rename) are required for
  the full fix: entry-point-name == package-name == tool-name.

Tests:
  (A) `[project] name` in pyproject.toml must be "meridian-docs-mcp", not "meridian-docs".
      This is the gap test: it would have FAILED against the old pyproject.toml and
      passes after the fix.
  (B) `[project] name` == entry-point script name (the name-match invariant).
      Ensures both sides of the equation stay in sync if either is ever changed.
  (C) The BUILTIN_PLUGINS command token and [project].name are consistent:
      the trailing token in ["uvx", "--from", <path>, TOKEN] must equal the
      [project.scripts] key, which in turn must equal [project].name.
  (D) KNOWN_PLUGIN_TOOLS "package" field for the meridian-docs catalog entry
      matches [project].name (keeps the catalog accurate after the rename).
  (E) No old "meridian-docs" package name remains in [project] (negative guard).
"""
from __future__ import annotations

import re
from pathlib import Path

try:
    import tomllib  # stdlib in Python 3.11+
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

from meridian import tunnel_plugins as tp


def _read_pyproject() -> dict:
    """Parse extensions/meridian-docs/pyproject.toml into a dict."""
    pyproject_path = Path(tp._MERIDIAN_DOCS_LOCAL_PATH) / "pyproject.toml"
    assert pyproject_path.is_file(), (
        f"pyproject.toml not found at {pyproject_path}; "
        "extensions/meridian-docs must contain a valid pyproject.toml"
    )
    with open(pyproject_path, "rb") as f:
        return tomllib.load(f)


# ---------------------------------------------------------------------------
# (A) [project] name must be "meridian-docs-mcp" (the gap test)
# ---------------------------------------------------------------------------

def test_pyproject_project_name_is_meridian_docs_mcp():
    """extensions/meridian-docs/pyproject.toml [project].name must be
    "meridian-docs-mcp", NOT "meridian-docs".

    This is the GAP TEST — the assertion that would have caught the 932e8bd1 bug
    before it shipped:

    - 58a044c7 renamed the ENTRY-POINT from "meridian-docs" to "meridian-docs-mcp"
      but left [project].name = "meridian-docs".
    - In current uv, `uvx --from <path> meridian-docs-mcp` looks up "meridian-docs-mcp"
      in the PyPI registry when the installed package name ("meridian-docs") doesn't
      match the tool name ("meridian-docs-mcp"). That registry lookup fails.
    - Renaming [project].name to "meridian-docs-mcp" makes the package name equal
      the tool name, so no registry lookup is triggered.

    This test fails against the OLD pyproject.toml (name = "meridian-docs") and
    passes after the 932e8bd1 fix (name = "meridian-docs-mcp")."""
    data = _read_pyproject()
    project_name = data.get("project", {}).get("name", "")
    assert project_name == "meridian-docs-mcp", (
        f"[project].name in extensions/meridian-docs/pyproject.toml is "
        f"{project_name!r}, expected 'meridian-docs-mcp' (932e8bd1). "
        "When the package name does not match the uvx tool name, uv falls back to "
        "a PyPI registry lookup that fails with 'not found in the package registry'. "
        "Rename [project] name to 'meridian-docs-mcp' to fix the spawn failure."
    )


# ---------------------------------------------------------------------------
# (B) Package name == entry-point script name (name-match invariant)
# ---------------------------------------------------------------------------

def test_pyproject_package_name_equals_script_name():
    """The uvx name-match invariant: [project].name must equal the [project.scripts]
    key so that `uvx --from <local-path> <entry-point>` works without a registry lookup.

    uv's rule (current stable): when `uvx --from SPEC TOOL` is used,
      - SPEC installs a package (here: from a local path).
      - If the installed package name matches TOOL, uv runs the script directly.
      - If they differ, uv falls back to a PyPI lookup for TOOL, which fails if
        TOOL is not on PyPI.

    Both the [project].name AND the [project.scripts] key must therefore be
    "meridian-docs-mcp" for the spawn to succeed. This test enforces that
    invariant so a future rename of one side without the other is caught immediately."""
    data = _read_pyproject()
    project_name = data.get("project", {}).get("name", "")
    scripts = data.get("project", {}).get("scripts", {})
    assert scripts, (
        "pyproject.toml [project.scripts] is empty — "
        "meridian-docs-mcp must be declared as a console script"
    )
    script_names = list(scripts.keys())
    assert project_name in script_names, (
        f"[project].name={project_name!r} does not appear as a script key in "
        f"[project.scripts]={script_names!r}. "
        "The uvx name-match invariant requires package name == entry-point name "
        "for `uvx --from <path> <name>` to work without a registry fallback (932e8bd1)."
    )


# ---------------------------------------------------------------------------
# (C) BUILTIN_PLUGINS command trailing token == pyproject script name == [project].name
# ---------------------------------------------------------------------------

def test_builtin_plugins_command_token_matches_pyproject():
    """The trailing token of the docs BUILTIN_PLUGINS command must equal both the
    [project.scripts] key AND [project].name.

    The full invariant chain (all three must be the same string):
      tunnel_plugins.py BUILTIN_PLUGINS docs command[3]
        == pyproject.toml [project.scripts] key
        == pyproject.toml [project].name

    A break at any link causes a spawn failure. This test checks all three links
    in one shot so future renames are caught regardless of which side changes."""
    data = _read_pyproject()
    project_name = data.get("project", {}).get("name", "")
    scripts = data.get("project", {}).get("scripts", {})
    script_names = list(scripts.keys())

    by_slot = {p["slot"]: p for p in tp.BUILTIN_PLUGINS}
    assert "docs" in by_slot, "docs slot must be in BUILTIN_PLUGINS"
    cmd = by_slot["docs"]["command"]
    assert isinstance(cmd, list) and len(cmd) >= 4, (
        f"docs command must have at least 4 tokens, got {cmd!r}"
    )
    cmd_tool_name = cmd[3]  # ["uvx", "--from", <path>, <tool-name>]

    # All three must agree.
    assert cmd_tool_name == project_name, (
        f"BUILTIN_PLUGINS docs command token ({cmd_tool_name!r}) != "
        f"[project].name ({project_name!r}). "
        "The installed package name must match the uvx tool name (932e8bd1)."
    )
    assert cmd_tool_name in script_names, (
        f"BUILTIN_PLUGINS docs command token ({cmd_tool_name!r}) is not declared "
        f"as a [project.scripts] key ({script_names!r}). "
        "uvx must be able to find the script in the installed package."
    )


# ---------------------------------------------------------------------------
# (D) KNOWN_PLUGIN_TOOLS "package" matches [project].name
# ---------------------------------------------------------------------------

def test_known_plugin_tools_package_matches_pyproject_name():
    """KNOWN_PLUGIN_TOOLS catalog entry for meridian-docs must have its 'package'
    field set to the current [project].name in pyproject.toml.

    After the 932e8bd1 rename, [project].name = "meridian-docs-mcp". The catalog
    entry's 'package' field is used for documentation and install-hint generation;
    keeping it accurate avoids misleading install instructions. (It is NOT used for
    the actual spawn, which uses BUILTIN_PLUGINS, but it should stay consistent.)"""
    data = _read_pyproject()
    project_name = data.get("project", {}).get("name", "")

    catalog = {t["name"]: t for t in tp.KNOWN_PLUGIN_TOOLS}
    assert "meridian-docs" in catalog, (
        "meridian-docs entry missing from KNOWN_PLUGIN_TOOLS"
    )
    catalog_package = catalog["meridian-docs"].get("package", "")
    assert catalog_package == project_name, (
        f"KNOWN_PLUGIN_TOOLS 'package' for meridian-docs is {catalog_package!r}, "
        f"but [project].name in pyproject.toml is {project_name!r}. "
        "Keep the catalog 'package' field in sync with the pyproject name (932e8bd1)."
    )


# ---------------------------------------------------------------------------
# (E) Negative guard: OLD "meridian-docs" must not appear as [project].name
# ---------------------------------------------------------------------------

def test_pyproject_project_name_is_not_old_meridian_docs():
    """Negative guard: [project].name must NOT be "meridian-docs".

    This is intentionally a separate test from (A) so that a refactor which
    changes [project].name to SOMETHING ELSE (not "meridian-docs-mcp" and
    not "meridian-docs") fails the POSITIVE test (A) while this negative test
    also flags the remaining "meridian-docs" name if it ever creeps back."""
    data = _read_pyproject()
    project_name = data.get("project", {}).get("name", "")
    assert project_name != "meridian-docs", (
        "[project].name is still 'meridian-docs' — this causes the live spawn "
        "failure 'not found in the package registry' (932e8bd1). "
        "The package name must match the entry-point name ('meridian-docs-mcp') "
        "for uvx --from <local-path> meridian-docs-mcp to work without a registry lookup."
    )
