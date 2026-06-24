"""Document ingestion — turn a file into plain text for a queryable note.

Stdlib only. Meridian's binary size is monitored and there are no PDF/docx
parsing dependencies installed, so extraction here never imports a third-party
library:

  * ``.txt`` / ``.md`` / ``.markdown`` / plain source files — read the bytes as
    UTF-8 (errors replaced) and return them verbatim.
  * ``.docx`` — a .docx is a zip; we read ``word/document.xml`` with the stdlib
    ``zipfile`` + ``xml.etree.ElementTree``, gather the ``<w:t>`` text runs, and
    insert a newline on every ``<w:p>`` paragraph boundary. No python-docx.
  * ``.pdf`` and everything else — NOT parsed server-side. The caller is told to
    extract the text with its own tooling and pass it as ``content`` instead.

Meridian is a coordination store, not an LLM: this module never summarizes. It
returns the raw extracted text; capping/summarizing is the caller's job.
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
import zipfile

__all__ = [
    "DOC_BODY_MAX_CHARS",
    "TRUNCATION_MARKER",
    "DocExtractionError",
    "UnsupportedDocumentError",
    "extract_text",
    "extract_docx_text",
    "cap_body",
]

# Plain-text extensions read directly as UTF-8. Anything code-like that is really
# just text is fair game — the point is "this file is already plain text".
_TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".mdx",
        ".rst",
        ".text",
        ".log",
        ".csv",
        ".tsv",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".xml",
        ".html",
        ".htm",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".java",
        ".c",
        ".h",
        ".cpp",
        ".cc",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".sh",
        ".sql",
    }
)

# WordprocessingML namespace — every run (<w:t>) and paragraph (<w:p>) lives here.
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W_P = f"{{{_W_NS}}}p"
_W_T = f"{{{_W_NS}}}t"
_W_TAB = f"{{{_W_NS}}}tab"
_W_BR = f"{{{_W_NS}}}br"

# Body cap for an ingested document note. Documents can be large; we keep the
# stored body bounded so a note stays renderable and searchable without blowing
# up the dashboard or an agent's context. Bodies longer than this are truncated
# with a clear marker (the prefix stays full-text searchable).
DOC_BODY_MAX_CHARS = 200_000
TRUNCATION_MARKER = "\n\n…[truncated]"


class DocExtractionError(Exception):
    """Raised when a supported document can't be read/parsed."""


class UnsupportedDocumentError(DocExtractionError):
    """Raised for a file type Meridian won't parse server-side (e.g. .pdf).

    The message tells the caller to extract the text itself and pass it as
    ``content`` to ``ingest_document``.
    """


def cap_body(text: str, limit: int = DOC_BODY_MAX_CHARS) -> str:
    """Truncate ``text`` to ``limit`` chars, appending a clear marker if cut.

    The kept prefix stays plain text so it remains full-text searchable. Short
    bodies pass through unchanged.
    """
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + TRUNCATION_MARKER


def extract_docx_text(data: bytes) -> str:
    """Extract plain text from raw .docx bytes (stdlib only).

    A .docx is a zip archive; the main body lives in ``word/document.xml``. We
    walk it in document order, emit each ``<w:t>`` run's text, render ``<w:tab>``
    as a tab and ``<w:br>`` as a newline, and break the line on every ``<w:p>``
    paragraph boundary so paragraphs survive as newlines.
    """
    import io

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            try:
                xml_bytes = zf.read("word/document.xml")
            except KeyError as exc:
                raise DocExtractionError(
                    "not a valid .docx: missing word/document.xml"
                ) from exc
    except zipfile.BadZipFile as exc:
        raise DocExtractionError("not a valid .docx: file is not a zip archive") from exc

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise DocExtractionError(f"could not parse word/document.xml: {exc}") from exc

    paragraphs: list[str] = []
    # Each <w:p> is one paragraph; gather the text of every descendant run/tab/br
    # in document order so inline formatting splits don't drop or reorder text.
    for para in root.iter(_W_P):
        parts: list[str] = []
        for node in para.iter():
            if node.tag == _W_T:
                parts.append(node.text or "")
            elif node.tag == _W_TAB:
                parts.append("\t")
            elif node.tag == _W_BR:
                parts.append("\n")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs)


def extract_text(path: str) -> str:
    """Extract plain text from a document at ``path`` (stdlib only).

    Supported:
      * plain-text / source files (``.txt``, ``.md``, ``.markdown``, ``.json``,
        common source extensions) — decoded as UTF-8 (errors replaced).
      * ``.docx`` — unzipped and parsed via :func:`extract_docx_text`.

    Raises:
      * :class:`FileNotFoundError` if ``path`` does not exist.
      * :class:`UnsupportedDocumentError` for ``.pdf`` and any other type we do
        not parse server-side, with a message telling the caller to pass the
        pre-extracted text as ``content``.
      * :class:`DocExtractionError` if a supported file can't be read/parsed.
    """
    if not path or not str(path).strip():
        raise DocExtractionError("path must be a non-empty string")
    if not os.path.exists(path):
        raise FileNotFoundError(f"no such file: {path}")
    if not os.path.isfile(path):
        raise DocExtractionError(f"not a file: {path}")

    ext = os.path.splitext(path)[1].lower()

    if ext == ".docx":
        with open(path, "rb") as fh:
            return extract_docx_text(fh.read())

    if ext in _TEXT_EXTENSIONS or ext == "":
        # Plain text (or an extensionless file we optimistically treat as text).
        try:
            with open(path, "rb") as fh:
                return fh.read().decode("utf-8", errors="replace")
        except OSError as exc:
            raise DocExtractionError(f"could not read {path}: {exc}") from exc

    if ext == ".pdf":
        raise UnsupportedDocumentError(
            "PDF text cannot be extracted server-side (no PDF library is "
            "installed). Extract the text with your own tools and pass it as "
            "the 'content' argument to ingest_document instead."
        )

    raise UnsupportedDocumentError(
        f"unsupported document type '{ext}'. Meridian extracts .txt/.md/.docx "
        f"server-side only. For '{ext}', extract the text yourself and pass it "
        f"as the 'content' argument to ingest_document."
    )
