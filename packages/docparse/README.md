# docparse

Deterministic **OOXML (.docx)** and **LaTeX** structural parsing — Meridian's
detachable document-intelligence sub-package (`d45c2cc8`).

This is a self-contained, standalone-installable package that lives **inside** the
Meridian monorepo (`packages/docparse/`) but depends on **nothing** from the rest of
Meridian. It is pure stdlib (`xml.etree`, `sqlite3`) — no third-party runtime deps.

## Install standalone

```bash
pip install -e ./packages/docparse
```

Within the Meridian repo it is wired as an editable path dependency (`pixi.toml`
`[pypi-dependencies]`), so `import docparse` resolves for the app and the test suite.

## What it does

- `docparse.docs_intel` — parse a `.docx` into a structural tree: headings
  (`document_outline`), full ordered paragraph/table content (`document_content_tree`),
  and Word field codes (TOC / SEQ / PAGEREF, flagged `needs_refresh`). Pure OOXML,
  no COM/VBA.
- `docparse.latex_intel` — parse LaTeX source structure (`analyze_latex`) with no PDF
  intermediary.

## Meridian integration

Meridian keeps importing `meridian.docs_intel` / `meridian.latex_intel`; those are now
thin compatibility shims that re-export `docparse`. New code should import from
`docparse` directly.

Governed by the parent repo's `LICENSE` (MSL-1.0).
