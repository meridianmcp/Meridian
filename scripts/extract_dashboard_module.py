"""Extract top-level functions from dashboard.js into a sibling ES module.

Refactor helper for the v1.1 dashboard.js extraction sprint (item c47759ba):
get dashboard.js from ~15.7k lines under 11k by moving tab renderers into
dashboard-rewind/files/notes/settings.js. Run once per module, then verify
with `node build.mjs && pixi run smoke-bundle && pixi run python -m pytest
tests/test_ui.py` and commit. One-time migration helper, safe to delete after
the v1.1 extraction lands.

Mechanics (matches the existing module pattern, e.g. dashboard-sprint.js):
  * A top-level function spans from `^(async )?function NAME(` to the first
    subsequent line that is exactly `}` (column-0 closing brace — reliable in
    this file because every nested brace is indented).
  * Extracted code gets an `export ` prefix and all bare `state.` references
    rewritten to `window.state.` (extracted modules can't see dashboard.js's
    `state` binding; the smoke check enforces this). No target function shadows
    `state` locally (verified), so the global rewrite is safe.
  * Each moved name is removed from dashboard.js's footer Object.assign list and
    added to the module's own footer Object.assign so inline handlers and
    cross-file references keep resolving after IIFE bundling.

Usage:
  pixi run python scripts/extract_dashboard_module.py <module_basename> <header> <fn> [fn ...]
e.g.
  pixi run python scripts/extract_dashboard_module.py dashboard-notes \
      "per-project notes wiki tab" loadNotesTab
"""
from __future__ import annotations

import re
import sys
import pathlib

STATIC = pathlib.Path("meridian/static")
DASH = STATIC / "dashboard.js"


def find_block(lines: list[str], name: str) -> tuple[int, int]:
    """Return (start, end) inclusive line indices for a top-level function."""
    pat = re.compile(r"^(async\s+)?function " + re.escape(name) + r"\b")
    start = next((i for i, ln in enumerate(lines) if pat.match(ln)), None)
    if start is None:
        sys.exit(f"ERROR: function {name} not found in dashboard.js")
    end = next(
        (j for j in range(start + 1, len(lines)) if lines[j].rstrip("\r\n") == "}"),
        None,
    )
    if end is None:
        sys.exit(f"ERROR: closing brace for {name} not found")
    return start, end


def transform(block: str) -> str:
    """Add `export ` prefix and rewrite bare `state.` -> `window.state.`."""
    block = re.sub(r"^(async\s+)?function ", lambda m: "export " + m.group(0), block, count=1)
    block = re.sub(r"(?<!window\.)(?<!\w)state\.", "window.state.", block)
    return block


def main() -> None:
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    module = sys.argv[1]               # e.g. dashboard-notes
    header = sys.argv[2]               # human description
    fns = sys.argv[3:]

    text = DASH.read_text(encoding="utf-8")
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)

    spans = {fn: find_block(lines, fn) for fn in fns}
    extracted = {fn: transform("".join(lines[s:e + 1])) for fn, (s, e) in spans.items()}

    moved_lines = sum(e - s + 1 for s, e in spans.values())
    print(f"Extracting {len(fns)} fn(s), {moved_lines} lines -> {module}.js")
    for fn, (s, e) in spans.items():
        print(f"  {fn}: dashboard.js lines {s + 1}-{e + 1} ({e - s + 1} lines)")

    # Remove spans from dashboard.js (reverse order so indices stay valid).
    for fn in sorted(fns, key=lambda f: spans[f][0], reverse=True):
        s, e = spans[fn]
        del lines[s:e + 1]
    new_dash = "".join(lines)

    # --- update dashboard.js footer Object.assign: drop moved names ---
    m = re.search(r"Object\.assign\(window, \{([^}]*)\}\);", new_dash)
    if not m:
        sys.exit("ERROR: footer Object.assign(window, {...}) not found in dashboard.js")
    names = [n.strip() for n in m.group(1).split(",") if n.strip()]
    kept = [n for n in names if n not in fns]
    removed = [n for n in names if n in fns]
    print(f"Footer: removing {len(removed)} name(s) from dashboard.js: {removed}")
    new_assign = "Object.assign(window, { " + ", ".join(kept) + " });"
    new_dash = new_dash[:m.start()] + new_assign + new_dash[m.end():]

    # --- ensure import line present near the top imports ---
    imp = f'import "./{module}.js";'
    if imp not in new_dash:
        import_lines = list(re.finditer(r'^import "\./dashboard-[\w-]+\.js";\s*$', new_dash, re.M))
        last = import_lines[-1]
        new_dash = new_dash[:last.end()] + nl + imp + new_dash[last.end():]
        print(f"Added import: {imp}")
    else:
        print(f"Import already present: {imp}")

    DASH.write_text(new_dash, encoding="utf-8", newline="")
    print(f"dashboard.js now {new_dash.count(chr(10)) + 1} lines")

    # --- write/extend the module file ---
    mod_path = STATIC / f"{module}.js"
    footer_re = re.compile(r"try \{ Object\.assign\(window, \{([^}]*)\}\); \} catch.*$", re.M)
    body = (nl + nl).join(extracted[fn].rstrip() for fn in fns)

    if mod_path.exists():
        existing = mod_path.read_text(encoding="utf-8")
        fm = footer_re.search(existing)
        if not fm:
            sys.exit(f"ERROR: existing {module}.js has no footer Object.assign to merge into")
        existing_names = [n.strip() for n in fm.group(1).split(",") if n.strip()]
        all_names = existing_names + [f for f in fns if f not in existing_names]
        head = existing[:fm.start()].rstrip() + nl + nl
        new_mod = head + body + nl + nl + _footer(all_names, nl)
        print(f"Extended existing {module}.js (merged {len(fns)} fn into footer)")
    else:
        head = (
            f"// {module}.js — {header} extracted from dashboard.js" + nl
            + "// Re-exposes its symbols on window so inline handlers + cross-file"
            + " references resolve after esbuild IIFE bundling." + nl + nl
        )
        new_mod = head + body + nl + nl + _footer(fns, nl)
        print(f"Wrote new {module}.js")

    mod_path.write_text(new_mod, encoding="utf-8", newline="")
    print(f"{module}.js now {new_mod.count(chr(10)) + 1} lines")


def _footer(names: list[str], nl: str) -> str:
    return (
        "// --- esbuild: re-expose top-level symbols as globals so inline" + nl
        + "// handlers and cross-file references keep resolving after IIFE bundling." + nl
        + "try { Object.assign(window, { " + ", ".join(names) + " }); } catch (e) {}" + nl
    )


if __name__ == "__main__":
    main()
