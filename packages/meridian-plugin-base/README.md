# meridian-plugin-base

Shared OOXML/document-parse core for Meridian plugin extensions.

**Status: not yet published to PyPI.** This package exists in the Meridian monorepo
as the concrete groundwork for the `meridian[docs]` modular-extras architecture.
See `PUBLISHING.md` for the steps required to put it on PyPI.

## What lives here

### `meridian_plugin_base.ooxml`

Canonical stdlib-only OOXML parsing primitives:

- Namespace constants (`_W`, `_W14`)
- Low-level element helpers (`_q`, `_is_heading`, `_heading_level`, `_paragraph_text`, ...)
- `document_content_tree(source)` — parse a `.docx` ZIP into a structured block tree

This is the function that was previously duplicated in:
- `packages/docparse/docparse/docs_intel.py` (first canonical home)
- `extensions/meridian-docs/meridian_docs/_vendored_content_tree.py` (vendored copy,
  needed because `uvx --from <local-path>` isolated envs cannot resolve path-based deps)

### `meridian_plugin_base.ingest_client`

Stdlib-only HTTP client for calling back to the hosted Meridian `/mcp` endpoint:

- `call_mcp_tool(tool_name, params, ...)` — core JSON-RPC caller (SSE + plain JSON)
- `call_hosted_ingest(...)` — wrapper for `ingest_document`
- `call_hosted_ingest_structure(...)` — wrapper for `ingest_document_structure`

Previously duplicated inside `extensions/meridian-docs/meridian_docs/local_ingest.py`.

## Zero third-party runtime dependencies

Pure stdlib only (`zipfile`, `xml.etree.ElementTree`, `sqlite3`, `urllib.request`).
Every plugin can depend on this without pulling in FastAPI, psycopg, anthropic, etc.

## How plugins use this (once published)

```toml
# extensions/meridian-docs/pyproject.toml
[project]
dependencies = [
    "mcp>=1.0",
    "meridian-plugin-base>=0.1",   # <-- replaces _vendored_content_tree.py
]
```

```python
# extensions/meridian-docs/meridian_docs/local_ingest.py (after migration)
from meridian_plugin_base.ooxml import document_content_tree
from meridian_plugin_base.ingest_client import call_hosted_ingest, call_hosted_ingest_structure
```

## Local development

```bash
pip install -e packages/meridian-plugin-base   # editable install
python -c "from meridian_plugin_base.ooxml import document_content_tree; print('ok')"
```
