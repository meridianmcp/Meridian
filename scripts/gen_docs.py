"""scripts/gen_docs.py — Regenerate docs/mcp-tools.md and docs/api-reference.md.

Run:
    pixi run python scripts/gen_docs.py

This script calls two canonical generators in ``meridian.server``:

* ``mcp_tools_doc()`` — reflects the live MCP tool schemas into docs/mcp-tools.md.
  The same function is validated by the test ``test_docs_mcp_tools_matches_live_tool_doc``.

* ``api_reference_doc()`` — reflects the live FastAPI route table into
  docs/api-reference.md.  The route inventory section is auto-generated; the
  hand-authored narrative sections are embedded as static strings in the generator
  so the committed file is always fully reproducible.
  Validated by the test ``test_docs_api_reference_matches_live_doc``.

Both generated files are deterministic: diffs appear only when the corresponding
implementation changes.

CI (test.yml docs-check job) runs this script and fails if either result differs from
the committed file, enforcing that docs always match implementation.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running from the repo root without installing the package.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


def _write(path: Path, content: str) -> None:
    """Write *content* to *path* with LF newlines (platform-independent)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Force LF newlines so the committed file is identical on every platform.
    # Without newline="\n", Path.write_text translates \n -> os.linesep (CRLF on
    # Windows), which makes the CI docs-check diff fail when it regenerates the
    # file on Linux (LF).
    path.write_text(content, encoding="utf-8", newline="\n")
    n_lines = content.count("\n")
    print(f"Wrote {path.relative_to(_REPO_ROOT)} ({len(content)} bytes, {n_lines} lines)")


def main() -> None:
    # Import after sys.path tweak so the uninstalled package is found.
    import asyncio  # noqa: PLC0415
    from meridian.server import api_reference_doc, mcp_tools_doc  # noqa: PLC0415

    # Use loop.run_until_complete() to avoid asyncio.run() issues on Windows
    # with ProactorEventLoop (the server uses SelectorEventLoop; scripts are
    # fine with either, but this is the safe pattern for this codebase).
    loop = asyncio.new_event_loop()
    try:
        mcp_content = loop.run_until_complete(mcp_tools_doc())
        api_content = loop.run_until_complete(api_reference_doc())
    finally:
        loop.close()

    _write(_REPO_ROOT / "docs" / "mcp-tools.md", mcp_content)
    _write(_REPO_ROOT / "docs" / "api-reference.md", api_content)


if __name__ == "__main__":
    main()
