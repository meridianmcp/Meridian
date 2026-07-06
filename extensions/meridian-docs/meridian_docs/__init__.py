"""meridian-docs — standalone OOXML (DOCX) intelligence as an MCP server.

The DOCX parser (``docs_intel``) is stdlib-only and vendored verbatim from the
Meridian monorepo (``meridian/docs_intel.py``). This package repackages it as a
uvx-installable MCP server so any client can query .docx structure without a
Meridian install. Until the parser is fully extracted, keep this copy in sync
with the source of truth in the Meridian repo.
"""

__version__ = "0.1.0"
