"""meridian-plugin-base — shared OOXML/document-parse core for Meridian plugins.

This package is the resolvable common base that future plugin extras
(meridian[docs], meridian[research], meridian[figma], ...) depend on so they do
not each have to vendor the same stdlib-only parsing code.

Today's surface area (v0.1):

  - :mod:`meridian_plugin_base.ooxml` — OOXML namespace constants, low-level
    element helpers (_q, _is_heading, _heading_level), and the
    :func:`document_content_tree` function that turns a .docx into a structured
    block tree. This is the canonical source of the function that was previously
    duplicated in:
      * packages/docparse/docparse/docs_intel.py  (document_content_tree, ~60 lines)
      * extensions/meridian-docs/meridian_docs/_vendored_content_tree.py  (vendored copy)
      * meridian/doc_store.py (partial re-impl for elements_from_docx_content_tree)

  - :mod:`meridian_plugin_base.ingest_client` — stdlib-only HTTP client for the
    hosted Meridian /mcp endpoint. Extracted from
    extensions/meridian-docs/meridian_docs/local_ingest.py (_call_mcp_tool,
    call_hosted_ingest, call_hosted_ingest_structure). Plugins that need to call
    back to the hosted server share this single implementation rather than each
    vendoring their own urllib-based JSON-RPC wrapper.

See PUBLISHING.md for how to put this on PyPI so plugin packages can declare a
real `meridian-plugin-base>=0.1` dependency in their pyproject.toml.
"""
from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["ooxml", "ingest_client"]
