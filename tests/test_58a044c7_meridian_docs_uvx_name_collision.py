"""58a044c7 — meridian-docs entry-point renamed to avoid uvx package-name collision.

Bug (confirmed live, distinct from 105e56b9):
  `uvx --from <local-path> meridian-docs` fails with "meridian-docs was not found
  in the package registry". The relay now opens correctly (prior fix) but uvx fails
  loudly because of a known uv/uvx behavior: when the trailing command name matches
  the package name exactly (`--from <path>` installs "meridian-docs", then the
  command argument is also "meridian-docs"), uvx attempts a PyPI registry lookup for
  the command name after installing from the local path. Since "meridian-docs" is
  NOT published to PyPI, this lookup fails with the registry error.

Fix:
  Rename the console script entry-point in extensions/meridian-docs/pyproject.toml
  from "meridian-docs" to "meridian-docs-mcp". Update the tunnel_plugins.py command
  to match. The `--from <local-path>` still installs from the local extensions/
  meridian-docs directory; "meridian-docs-mcp" is unambiguously a script name in
  that package, not a PyPI package to fetch, so the registry lookup is not triggered.

Tests (all pure-unit, no subprocess/network/sleeps):
  (A) The BUILTIN_PLUGINS command uses "meridian-docs-mcp" (not "meridian-docs")
      at index 3 (the entry-point position in ["uvx", "--from", <path>, <cmd>]).
  (B) extensions/meridian-docs/pyproject.toml declares "meridian-docs-mcp" as the
      console script (not "meridian-docs") — the source of truth for the entry point.
  (C) The old "meridian-docs" entry-point is NOT declared in pyproject.toml
      (removing it ensures the registry-lookup collision can't reoccur).
  (D) resolve_plugins() also uses "meridian-docs-mcp" in the resolved command.
  (E) The command form check: ["uvx", "--from", <local-path>, "meridian-docs-mcp"].
"""
from __future__ import annotations

from pathlib import Path

import pytest

from meridian import tunnel_plugins as tp


# ---------------------------------------------------------------------------
# (A) BUILTIN_PLUGINS uses "meridian-docs-mcp" entry-point (not "meridian-docs")
# ---------------------------------------------------------------------------

def test_docs_command_uses_meridian_docs_mcp_entry_point():
    """The docs slot command must use 'meridian-docs-mcp' as the trailing entry-point
    argument, NOT 'meridian-docs'. Using 'meridian-docs' triggers uvx to look up the
    command as a PyPI package (same name as the local package), which fails because
    'meridian-docs' is not on PyPI (58a044c7)."""
    by_slot = {p["slot"]: p for p in tp.BUILTIN_PLUGINS}
    assert "docs" in by_slot, "docs slot must be in BUILTIN_PLUGINS"
    cmd = by_slot["docs"]["command"]
    assert isinstance(cmd, list) and len(cmd) >= 4, (
        f"docs command must be a list with at least 4 elements, got {cmd!r}"
    )
    # The entry-point (the trailing token) must be "meridian-docs-mcp", not
    # "meridian-docs". f886d37a inserted a "--no-cache" cache-busting flag after
    # "uvx", so the entry-point is checked positionally (last token) rather than at
    # a hardcoded index.
    assert cmd[-1] == "meridian-docs-mcp", (
        f"entry-point (last token) must be 'meridian-docs-mcp', got {cmd[-1]!r}. "
        "Using 'meridian-docs' triggers a uvx PyPI registry lookup that fails with "
        "'meridian-docs was not found in the package registry' (58a044c7)."
    )
    # The overall form must still be ["uvx", ..., "--from", <local-path>, "meridian-docs-mcp"].
    assert cmd[0] == "uvx" and "--from" in cmd, (
        f"command must start with 'uvx' and contain '--from', got {cmd!r}"
    )


def test_docs_command_does_not_use_bare_meridian_docs():
    """A direct guard: the entry-point must NOT be 'meridian-docs'. This test is
    intentionally separate from the positive assertion above so a refactor that
    changes the entry-point to something other than 'meridian-docs-mcp' fails
    explicitly on BOTH the positive AND the negative check."""
    by_slot = {p["slot"]: p for p in tp.BUILTIN_PLUGINS}
    cmd = by_slot["docs"]["command"]
    assert cmd[-1] != "meridian-docs", (
        "entry-point is 'meridian-docs' — this triggers the uvx PyPI registry "
        "lookup bug (58a044c7). Rename to 'meridian-docs-mcp' (or any name that "
        "does not match the package name) to fix it."
    )


# ---------------------------------------------------------------------------
# (B) pyproject.toml declares "meridian-docs-mcp" as the console script
# ---------------------------------------------------------------------------

def test_pyproject_declares_meridian_docs_mcp_script():
    """extensions/meridian-docs/pyproject.toml must declare 'meridian-docs-mcp'
    in [project.scripts] so uvx can find the entry-point after installing the
    local package (58a044c7)."""
    pyproject = Path(tp._MERIDIAN_DOCS_LOCAL_PATH) / "pyproject.toml"
    assert pyproject.is_file(), (
        f"pyproject.toml not found at {pyproject}; extensions/meridian-docs "
        "must have a valid pyproject.toml"
    )
    text = pyproject.read_text(encoding="utf-8")
    assert "meridian-docs-mcp" in text, (
        "pyproject.toml does not declare 'meridian-docs-mcp' in [project.scripts]; "
        "58a044c7 requires the entry-point to be renamed to avoid the uvx "
        "package-name/command-name collision"
    )


# ---------------------------------------------------------------------------
# (C) The OLD "meridian-docs" console script is NOT declared in pyproject.toml
# ---------------------------------------------------------------------------

def test_pyproject_does_not_declare_old_meridian_docs_script():
    """extensions/meridian-docs/pyproject.toml must NOT declare 'meridian-docs' as a
    console script entry-point. Keeping the old name alongside the new one would mean
    the collision could still occur if someone reverts the tunnel_plugins command.

    Note: the string 'meridian-docs' will appear elsewhere in the file (package name,
    comments, README reference) — this check is specifically for the [project.scripts]
    section."""
    pyproject = Path(tp._MERIDIAN_DOCS_LOCAL_PATH) / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    # Find the [project.scripts] section and check it doesn't define "meridian-docs ="
    # (which would be the old entry-point form). We do a targeted check rather than
    # searching the whole file (which would match the package name, comments, etc.).
    import re
    scripts_match = re.search(
        r"\[project\.scripts\](.*?)(?:\[|\Z)", text, re.DOTALL
    )
    if scripts_match:
        scripts_section = scripts_match.group(1)
        # The old entry-point form: `meridian-docs = "..."` (where the key is exactly
        # "meridian-docs", not "meridian-docs-mcp" or another variant).
        old_entry = re.search(r'(?<!["\w-])meridian-docs\s*=', scripts_section)
        assert old_entry is None, (
            "pyproject.toml [project.scripts] still has a 'meridian-docs' key; "
            "remove it to prevent the uvx name-collision reoccurrence (58a044c7). "
            f"Found in scripts section: {scripts_section.strip()!r}"
        )


# ---------------------------------------------------------------------------
# (D) resolve_plugins() carries the renamed entry-point
# ---------------------------------------------------------------------------

def test_resolve_plugins_uses_meridian_docs_mcp():
    """resolve_plugins(None) must return the docs slot with 'meridian-docs-mcp' as
    the entry-point so the tunnel client spawns the right command (58a044c7)."""
    resolved = tp.resolve_plugins(None)
    by_slot = {p["slot"]: p for p in resolved}
    assert "docs" in by_slot
    cmd = by_slot["docs"]["command"]
    assert isinstance(cmd, list) and len(cmd) >= 4
    assert cmd[-1] == "meridian-docs-mcp", (
        f"resolve_plugins docs command has entry-point {cmd[-1]!r}, expected "
        "'meridian-docs-mcp' (58a044c7)"
    )


# ---------------------------------------------------------------------------
# (E) Full command form check
# ---------------------------------------------------------------------------

def test_docs_full_command_form():
    """The full docs slot command must be
    ['uvx', '--no-cache', '--from', <local-path>, 'meridian-docs-mcp'] (58a044c7 +
    f886d37a). All positional constraints must hold simultaneously."""
    by_slot = {p["slot"]: p for p in tp.BUILTIN_PLUGINS}
    cmd = by_slot["docs"]["command"]
    assert cmd[0] == "uvx", f"cmd[0] must be 'uvx', got {cmd[0]!r}"
    assert "--from" in cmd, f"'--from' must be present, got {cmd!r}"
    from_idx = cmd.index("--from")
    local_path = cmd[from_idx + 1]
    # Local path must point at the extensions/ directory (not a bare package name).
    assert "extensions" in local_path and "meridian-docs" in local_path, (
        f"the --from value must be the local extensions/meridian-docs path, got {local_path!r}"
    )
    assert "/" in local_path or "\\" in local_path, (
        f"the --from value must be a filesystem path (with separators), got {local_path!r}"
    )
    assert cmd[-1] == "meridian-docs-mcp", (
        f"cmd[-1] (entry-point) must be 'meridian-docs-mcp', got {cmd[-1]!r}"
    )


# ---------------------------------------------------------------------------
# (F) 6ee033c0 — the [project] name in pyproject.toml is ALSO renamed, not
# just the console-script entry-point.
# ---------------------------------------------------------------------------

def test_pyproject_project_name_is_not_bare_meridian_docs():
    """extensions/meridian-docs/pyproject.toml's [project] table must declare
    name = "meridian-docs-mcp" (or any name distinct from the bare
    "meridian-docs" string), not just rename the console-script entry-point.

    58a044c7 only renamed [project.scripts]; the [project] name itself was
    left as the literal "meridian-docs" string, which is the gap 6ee033c0
    reports as still unfixed. A distribution name that still reads
    "meridian-docs" keeps `uvx --from <local-path> ...` ambiguous with the
    (nonexistent) PyPI package of the same name.
    """
    import tomllib

    pyproject = Path(tp._MERIDIAN_DOCS_LOCAL_PATH) / "pyproject.toml"
    assert pyproject.is_file(), (
        f"pyproject.toml not found at {pyproject}; extensions/meridian-docs "
        "must have a valid pyproject.toml"
    )
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project_name = data["project"]["name"]
    assert project_name != "meridian-docs", (
        "[project] name in pyproject.toml is still the bare 'meridian-docs' "
        "string (6ee033c0) — only the console-script entry-point was renamed "
        "by 58a044c7. Rename [project] name too (e.g. 'meridian-docs-mcp') so "
        "the distribution name itself doesn't collide with the nonexistent "
        "PyPI package of the same name."
    )
    assert project_name == "meridian-docs-mcp", (
        f"[project] name should be 'meridian-docs-mcp' to match the renamed "
        f"console-script entry-point, got {project_name!r}"
    )
