# meridian-docs

Standalone, **stdlib-only** OOXML (Word `.docx`) intelligence, packaged as an MCP
server. It's the parser layer extracted from [Meridian](https://usemeridian.us) —
free, MIT-licensed, no third-party dependencies for parsing (`python-docx` is
*not* required).

> **Status: skeleton.** The parser (`meridian_docs/docs_intel.py`) is vendored
> verbatim from the Meridian monorepo (`meridian/docs_intel.py`). A true
> extraction (making Meridian import this package instead of its in-tree copy) is
> deferred; until then keep the two copies in sync.

## Install

```bash
uvx meridian-docs          # run without installing
# or
uv tool install meridian-docs
```

## Tools

| Tool | What it does |
|------|--------------|
| `document_outline(path)` | Heading outline of a `.docx` (counts + ordered headings). Stateless. |
| `parse_document(path)` | Ordered paragraph records (`index`, `para_id`, `style`, `text`). |
| `index_document(path, index_db_path)` | Build a sidecar SQLite index for navigation. |
| `get_structure(index_db_path)` | Heading outline from a built index. |
| `get_paragraph(index_db_path, para_id)` | One paragraph by id. |
| `search_paragraphs(index_db_path, query, limit)` | Substring search over paragraphs. |

## Why

Meridian's paid value-add is coordination + persistent memory. The document
parser is genuinely useful on its own (e.g. mapping a thesis chapter's structure
before reading it), so it's distributed free here. The Meridian `word` tunnel
slot can default to `uvx meridian-docs`.

## License

MIT — see [LICENSE](./LICENSE).
