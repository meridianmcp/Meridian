"""scripts/gen_docs.py — Regenerate docs/mcp-tools.md from meridian/server.py.

Run:
    pixi run python scripts/gen_docs.py

This script calls the canonical ``mcp_tools_doc()`` generator in ``meridian.server``
(the same function the test ``test_docs_mcp_tools_matches_live_tool_doc`` validates)
and writes the output to ``docs/mcp-tools.md``.

The generated file is deterministic: diffs appear only when ``meridian/mcp_tools.py``
or the ``mcp_tools_doc()`` rendering logic in ``meridian/server.py`` changes.

CI (test.yml docs-check job) runs this and fails if the result differs from the
committed file, enforcing that docs always match implementation.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running from the repo root without installing the package.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


def main() -> None:
    # Import after sys.path tweak so the uninstalled package is found.
    import asyncio  # noqa: PLC0415
    from meridian.server import mcp_tools_doc  # noqa: PLC0415

    # Use loop.run_until_complete() to avoid asyncio.run() issues on Windows
    # with ProactorEventLoop (the server uses SelectorEventLoop; scripts are
    # fine with either, but this is the safe pattern for this codebase).
    loop = asyncio.new_event_loop()
    try:
        content = loop.run_until_complete(mcp_tools_doc())
    finally:
        loop.close()

    out_path = _REPO_ROOT / "docs" / "mcp-tools.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    n_lines = content.count("\n")
    print(f"Wrote {out_path.relative_to(_REPO_ROOT)} ({len(content)} bytes, {n_lines} lines)")


if __name__ == "__main__":
    main()
