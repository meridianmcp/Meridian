"""Tiered document-structure store — 9ee6d2ec.

Today document *structure* is never persisted: ``get_document_structure``
(:func:`meridian.docs_intel.document_outline`) and ``get_latex_structure``
(:func:`meridian.latex_intel.analyze_latex`) are stateless pure parses, and
``ingest_document`` stores only flat text as a ``kind='document'`` note. This
module adds a **pluggable, tiered store** for the parsed structural tree so a
document's outline can be persisted, listed, retrieved, and diffed across
sessions without re-parsing.

Design (self-contained, mirrors the ``docs_intel`` sidecar / ``demo_db``
precedents):

* The store owns its OWN two tables (``doc_documents`` / ``doc_elements``),
  created with ``CREATE TABLE IF NOT EXISTS`` on whatever connection it is
  given at init. It deliberately lives **outside** the main migration machinery
  (``db.CREATE_TABLES`` / ``db/migrations.py`` / ``pg_adapter`` migration
  tuples) — exactly like the per-file docs_intel sidecar and the second
  ``demo_db`` — so it stays self-contained and avoids the migration-parity /
  inline-index landmines entirely.
* One implementation runs on BOTH backends. It uses ``?`` placeholders and the
  shared dual-backend connection returned by :func:`meridian.db.init_db`
  (aiosqlite *or* the psycopg3 :class:`~meridian.pg_adapter.PostgresConnection`
  which translates ``?`` → ``%s`` transparently). Timestamps are generated in
  Python (ISO-8601 UTC) so the same SQL is valid on both.
* ``commit()`` is called unconditionally after writes: aiosqlite needs it, and
  the pg adapter makes it a no-op (autocommit) — matching the ~140 existing call
  sites.

Tiered backend selection (:func:`resolve_doc_store_target` /
:func:`open_doc_store_for`):

* ``MERIDIAN_DOC_STORE_URL`` override always wins.
* ``pro`` / ``admin`` plans WITH a tenant cloud-Postgres URL → the structure
  tables live in the tenant's own Postgres (seamless team access, no tunnel
  dependency).
* everything else (free / standard, self-hosted, or no pg url) → a local SQLite
  sidecar file ``{data_dir}/doc_structure.db`` (zero infra cost, fine for solo
  use).

Pure library where it can be: :func:`resolve_doc_store_target`,
:func:`elements_from_docx_outline`, and :func:`elements_from_latex_analysis` are
synchronous and unit-testable without opening a database.

06df6ab3 additionally adds:

* A FOURTH self-contained table, ``doc_equations`` (same outside-migrations
  pattern as the three above) — one row per parsed/authored Word equation
  (OMML), with fuzzy-dedup on a normalized LaTeX key (:func:`normalize_latex`,
  :meth:`DocStructureStore.put_equations`). Reading parses ``<m:oMath>`` straight
  out of a .docx's ``word/document.xml`` via lxml (:func:`parse_docx_equations`
  — never a PDF round-trip); writing pipes ``latex2mathml`` through a
  hand-written MathML -> OOXML mapper (:func:`latex_to_omml`) — see that
  function's docstring for why this isn't Microsoft's MML2OMML.XSL stylesheet.
* ``kind='figure'`` / ``kind='table'`` values on the EXISTING ``doc_elements``
  table (NOT a new table) via :func:`elements_from_docx_content_tree`, nested "by
  section" through the same heading-stack mechanism as headings.
* :meth:`DocStructureStore.reindex_document` — the one orchestrator entry point
  tying the outline+figures/tables (`put_document`) and equations
  (`put_equations`) passes together for a single .docx.

8ca89e8f additionally adds:

* A SEVENTH self-contained table, ``doc_flag_links`` (same outside-migrations
  pattern as the six above) — a durable, append-only record that a given
  ``doc_elements`` id's underlying numbers were produced with a config flag
  set to a particular value (:meth:`DocStructureStore.link_flag_state`), plus
  the reverse/forward lookups needed for a staleness check
  (:meth:`DocStructureStore.get_flag_links`). The flag SCAN itself (which
  flags exist, where, with what default) stays in :mod:`meridian.flag_registry`
  — that module's :func:`~meridian.flag_registry.diff_flag_links` is the
  stateless comparison between a recorded link and a fresh scan. This is the
  anchor side of that check, reusing the SAME durable ``doc_elements`` id every
  other linkage in this module anchors to (figures/tables/captions) rather than
  inventing a parallel id space.
"""
from __future__ import annotations

import asyncio
import difflib
import io
import itertools
import json
import os
import re
import shutil
import tempfile
import threading
import uuid
import hashlib
import logging
import zipfile
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable

from lxml import etree as _LET

from .zotero_client import resolve_citation_ref

_log = logging.getLogger(__name__)

# Cross-store resolve-through seam (d2a3537a): a callable that maps a figure's
# ``file_path`` to its ``outputs_index`` row (or ``None`` when the path names no
# indexed output). Injected so ``doc_store`` never imports/owns the DuckDB
# outputs index — the two stores stay decoupled connective tissue, not one merged
# database. In production this is ``OutputsFtsIndex.resolve_output`` or a partial
# of :func:`meridian.outputs_indexer.resolve_figure_output`.
OutputResolver = Callable[[str], "dict[str, Any] | None"]


# ---------------------------------------------------------------------------
# Schema (owned by this store — NOT part of db.CREATE_TABLES / migrations)
# ---------------------------------------------------------------------------

# Two tables: a document header row and its ordered structural elements. Every
# statement here is created together on the store's own fresh connection, so the
# ``CREATE INDEX IF NOT EXISTS`` lines are safe (there is no pre-existing prod
# table to crash against — the inline-index landmine only bites base-schema
# literals run over an already-migrated DB).
_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS doc_documents (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        source TEXT,
        doc_type TEXT NOT NULL,
        title TEXT,
        content_hash TEXT,
        element_count INTEGER NOT NULL DEFAULT 0,
        link_status TEXT NOT NULL DEFAULT 'live',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS doc_elements (
        id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        parent_id TEXT,
        ordinal INTEGER NOT NULL,
        level INTEGER,
        kind TEXT NOT NULL,
        text TEXT,
        ref TEXT
    )
    """,
    # Third self-contained table (fefb596a): a citation/reference *graph* over the
    # stored elements. Today it holds intra-document ``cites`` edges from a
    # ``kind='citation'`` element to a matching local ``kind='bibliography'``
    # entry; the ``target_kind`` / ``target_document_id`` columns leave room for
    # the future cross-document (Zotero / document / element) resolver without a
    # schema change. Same self-contained pattern as the two tables above — created
    # here by ``ensure_schema``, deliberately OUTSIDE the main migration machinery.
    """
    CREATE TABLE IF NOT EXISTS doc_edges (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        source_element_id TEXT NOT NULL,
        edge_kind TEXT NOT NULL,
        target_kind TEXT NOT NULL,
        target_ref TEXT,
        target_element_id TEXT,
        target_document_id TEXT,
        resolved_at TEXT,
        created_at TEXT NOT NULL
    )
    """,
    # Fourth self-contained table (06df6ab3): parsed Word/OMML equations, one row
    # per <m:oMath> — either read straight out of a .docx (element_id = the
    # containing paragraph's w14:paraId) or written via the index_equation MCP
    # tool (element_id NULL when there is no anchoring paragraph). Deliberately
    # NOT a doc_elements kind — equations carry OMML/LaTeX payload columns that
    # don't fit the generic element shape, and dedup needs its own
    # latex_normalized index. Same self-contained pattern as the three tables
    # above — created here by ``ensure_schema``, OUTSIDE the main migration
    # machinery.
    """
    CREATE TABLE IF NOT EXISTS doc_equations (
        id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        element_id TEXT,
        ordinal INTEGER NOT NULL,
        omml_raw TEXT,
        latex_normalized TEXT,
        semantic_label TEXT,
        created_at TEXT NOT NULL
    )
    """,
    # Fifth self-contained table (c623e648): the SEMANTIC figure index -- one row
    # per indexed figure, dedup/similarity keyed on a normalized caption. This is
    # COMPLEMENTARY to (not a replacement for) the ``kind='figure'`` doc_elements
    # rows that carry a figure's section-tree PLACEMENT -- those answer "where in
    # the outline does this figure sit"; ``doc_figures`` answers "have I already
    # indexed a figure with this caption / at this path" (fuzzy near-dup + ranked
    # lookup), the exact parallel of what latex_normalized dedup does for
    # equations. Deliberately NOT a doc_elements kind -- figures carry a file_path
    # + caption + normalized_caption dedup key that don't fit the generic element
    # shape. Same self-contained pattern as the four tables above -- created here
    # by ``ensure_schema``, OUTSIDE the main migration machinery.
    """
    CREATE TABLE IF NOT EXISTS doc_figures (
        id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        element_id TEXT,
        ordinal INTEGER NOT NULL,
        file_path TEXT,
        caption TEXT,
        normalized_caption TEXT,
        semantic_label TEXT,
        file_exists INTEGER,
        caption_element_id TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_doc_documents_project_source "
    "ON doc_documents (project_id, source)",
    "CREATE INDEX IF NOT EXISTS idx_doc_elements_document_ordinal "
    "ON doc_elements (document_id, ordinal)",
    "CREATE INDEX IF NOT EXISTS idx_doc_edges_project_kind "
    "ON doc_edges (project_id, edge_kind)",
    "CREATE INDEX IF NOT EXISTS idx_doc_edges_source_element "
    "ON doc_edges (source_element_id)",
    "CREATE INDEX IF NOT EXISTS idx_doc_equations_document_ordinal "
    "ON doc_equations (document_id, ordinal)",
    "CREATE INDEX IF NOT EXISTS idx_doc_figures_document_ordinal "
    "ON doc_figures (document_id, ordinal)",
    # Sixth self-contained table (2622182d): the SEMANTIC table index -- one row
    # per indexed table, dedup/similarity keyed on a normalized caption. This is
    # COMPLEMENTARY to (not a replacement for) the ``kind='table'`` doc_elements
    # rows that carry a table's section-tree PLACEMENT. Unlike doc_figures, tables
    # have no image file asset -- instead they carry a ``table_index`` (their
    # document-order table number) and an optional ``paired_figure_id`` linking to
    # a related figure in the same section (a table-of-results paired with a
    # results figure, for example). Same self-contained pattern as the five tables
    # above -- created here by ``ensure_schema``, OUTSIDE the main migration
    # machinery.
    """
    CREATE TABLE IF NOT EXISTS doc_tables (
        id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        element_id TEXT,
        ordinal INTEGER NOT NULL,
        table_index INTEGER,
        caption TEXT,
        normalized_caption TEXT,
        semantic_label TEXT,
        paired_figure_id TEXT,
        caption_element_id TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_doc_tables_document_ordinal "
    "ON doc_tables (document_id, ordinal)",
    # Seventh self-contained table (8ca89e8f): durable flag-state provenance --
    # one row per "this doc_elements id's underlying numbers were produced with
    # flag_name=recorded_value" claim, anchored to the SAME doc_elements id every
    # other linkage in this module anchors to (figures/tables/captions), so a
    # section, paragraph, figure, OR table is covered by one mechanism (they are
    # all doc_elements rows -- see elements_from_docx_content_tree). Insert-only /
    # append-only (mirrors the repo's provenance convention elsewhere -- task_log,
    # DECISIONS.md): a section can be re-verified multiple times as flags change,
    # and each recording is a fact about a point in time, not a mutable "current
    # state" -- callers collapse to the latest link per (element_id, flag_name)
    # via flag_registry.dedupe_flag_links before a drift check.
    # ``recorded_value``/``recorded_default`` are JSON-encoded TEXT so any
    # JSON-scalar flag value/default (str/int/float/bool/None) round-trips
    # exactly -- see _encode_flag_json/_decode_flag_link. ``source_file`` +
    # ``source_line`` optionally pin the EXACT call site recorded (from
    # flag_registry's scan) so a later drift check can distinguish "this flag's
    # default changed" from "a different, same-named flag elsewhere changed".
    # ``seq`` is a process-local monotonic counter (see _next_flag_link_seq) --
    # NOT a wall-clock value. Two links for the same (element, flag) recorded
    # back-to-back can legitimately land on the SAME ``created_at`` (ISO
    # timestamp resolution/OS clock granularity is coarser than "two awaited
    # DB inserts in a row" on some platforms), and ``id`` is a random UUID with
    # zero relationship to insertion order -- neither is a safe tiebreaker for
    # "which link is latest". ``seq`` is assigned synchronously (before the
    # awaited INSERT), so sequential ``await link_flag_state(...)`` calls from
    # the same caller are ordered correctly regardless of timestamp collisions.
    # Same self-contained pattern as the six tables above -- created here by
    # ``ensure_schema``, OUTSIDE the main migration machinery.
    """
    CREATE TABLE IF NOT EXISTS doc_flag_links (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        document_id TEXT NOT NULL,
        element_id TEXT NOT NULL,
        flag_name TEXT NOT NULL,
        recorded_value TEXT,
        recorded_default TEXT,
        source_file TEXT,
        source_line INTEGER,
        created_at TEXT NOT NULL,
        seq INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_doc_flag_links_project_flag "
    "ON doc_flag_links (project_id, flag_name)",
    "CREATE INDEX IF NOT EXISTS idx_doc_flag_links_element "
    "ON doc_flag_links (element_id)",
)


def _now_iso() -> str:
    """ISO-8601 UTC timestamp (matches the stringly-typed created_at/updated_at)."""
    return datetime.now(timezone.utc).isoformat()


def _row_get(row: Any, key: str) -> Any:
    """Read a column from a backend row (aiosqlite.Row *or* pg dict)."""
    if row is None:
        return None
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _row_to_dict(row: Any, keys: Iterable[str]) -> dict[str, Any] | None:
    """Materialise a backend row into a plain dict for the given columns."""
    if row is None:
        return None
    return {k: _row_get(row, k) for k in keys}


_DOC_COLUMNS = (
    "id", "project_id", "source", "doc_type", "title",
    "content_hash", "element_count", "link_status", "created_at", "updated_at",
)

# doc_documents.link_status — explicit lifecycle of the link between a stored
# document header and its physical source .docx (14015718):
#   * ``live``        — the source column points at a real, writable .docx;
#                       insert_equation/update_paragraph write back to it and
#                       reindex_document keeps it in sync. The default, and the
#                       only state the store implemented before this column.
#   * ``deprecated``  — a link that once existed but whose file moved / was
#                       renamed / superseded. The header row persists as history;
#                       write-backs surface the ordinary missing-file error.
#   * ``independent`` — a standalone captured snapshot with NO live file to write
#                       back to (archived drafts, ingest-once-to-query). Write
#                       attempts refuse LOUDLY with a distinct no-write-back error
#                       so an independent doc is never confused with a live doc
#                       whose file is merely temporarily missing.
_LINK_STATUS_LIVE = "live"
_LINK_STATUS_DEPRECATED = "deprecated"
_LINK_STATUS_INDEPENDENT = "independent"
_LINK_STATUSES = frozenset(
    {_LINK_STATUS_LIVE, _LINK_STATUS_DEPRECATED, _LINK_STATUS_INDEPENDENT}
)


def _normalize_link_status(value: Any) -> str:
    """Coerce an arbitrary link_status input to a valid enum value.

    Unknown / blank / non-string inputs fall back to ``'live'`` (the backward-
    compatible default), so a bad caller value can never produce an out-of-band
    status or crash the write. The enum is enforced at the app layer — the
    SQLite ``ADD COLUMN`` migration path can't carry a CHECK constraint (matching
    how every other additive enum column in this repo is handled)."""
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _LINK_STATUSES:
            return v
    return _LINK_STATUS_LIVE
_ELEMENT_COLUMNS = (
    "id", "document_id", "parent_id", "ordinal", "level", "kind", "text", "ref",
)
_EDGE_COLUMNS = (
    "id", "project_id", "source_element_id", "edge_kind", "target_kind",
    "target_ref", "target_element_id", "target_document_id", "resolved_at",
    "created_at",
)
_EQUATION_COLUMNS = (
    "id", "document_id", "element_id", "ordinal", "omml_raw",
    "latex_normalized", "semantic_label", "created_at",
)
_FIGURE_COLUMNS = (
    "id", "document_id", "element_id", "ordinal", "file_path", "caption",
    "normalized_caption", "semantic_label", "file_exists", "caption_element_id",
    "created_at",
)
_TABLE_COLUMNS = (
    "id", "document_id", "element_id", "ordinal", "table_index", "caption",
    "normalized_caption", "semantic_label", "paired_figure_id",
    "caption_element_id", "created_at",
)
_FLAG_LINK_COLUMNS = (
    "id", "project_id", "document_id", "element_id", "flag_name",
    "recorded_value", "recorded_default", "source_file", "source_line",
    "created_at", "seq",
)

# Process-local monotonic counter for doc_flag_links.seq (8ca89e8f). A plain
# itertools.count() rather than a wall-clock read: it is assigned SYNCHRONOUSLY
# (before the awaited INSERT), so it is immune to the timestamp-collision +
# random-UUID-tiebreak trap that made ordering by (created_at, id) unreliable
# for "which link is latest" -- see the schema comment above doc_flag_links.
_flag_link_seq_counter = itertools.count()


def _next_flag_link_seq() -> int:
    """Next monotonically increasing ``doc_flag_links.seq`` value."""
    return next(_flag_link_seq_counter)


def _encode_flag_json(value: Any) -> str:
    """JSON-encode a flag value/default for ``doc_flag_links`` storage.

    Always produces TEXT (never SQL NULL) so ``recorded_default=None`` — a
    legitimate "this flag has no default" claim, exactly what
    :func:`meridian.flag_registry._literal_or_none` already reports for an
    unparseable default — round-trips as JSON ``null`` rather than being
    conflated with "no row"/"column absent". Falls back to ``repr()`` wrapped
    as a JSON string for the (should-never-happen) case of a non-JSON-safe
    value, so a link write can never raise on an odd value.
    """
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return json.dumps(repr(value))


def _decode_flag_link(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Decode a ``doc_flag_links`` row's JSON-encoded value/default columns."""
    if row is None:
        return None
    out = dict(row)
    for key in ("recorded_value", "recorded_default"):
        raw = out.get(key)
        if isinstance(raw, str):
            try:
                out[key] = json.loads(raw)
            except (ValueError, TypeError):
                pass  # leave the raw string -- defensive, should not happen
    return out


# Fuzzy-match ratio (difflib) at/above which two equations' latex_normalized
# values count as a "near-duplicate" for put_equations' advisory dedup surface.
_EQUATION_DEDUP_THRESHOLD = 0.85

# The same advisory-dedup threshold, applied to figures' normalized_caption
# (c623e648) -- mirrors _EQUATION_DEDUP_THRESHOLD exactly.
_FIGURE_DEDUP_THRESHOLD = 0.85

# The same advisory-dedup threshold, applied to tables' normalized_caption
# (2622182d) -- mirrors _FIGURE_DEDUP_THRESHOLD exactly.
_TABLE_DEDUP_THRESHOLD = 0.85

# Sentinel level for a heading dict that carries no usable level. Deliberately
# large so a level-less heading nests as a deep leaf rather than colliding with a
# real top-level heading (docx H1 == 1; LaTeX \part == 0) and masquerading as a
# root. Real parser output always supplies an int level, so this is defensive.
_UNKNOWN_LEVEL = 99


# fefb596a — chars a DOI legitimately continues with. A DOI has a very permissive
# suffix, so a substring occurrence of one DOI inside a stored source is only a
# WHOLE-DOI boundary when the char right after it is NOT one of these (or the
# string ends) — this stops a prefix DOI ('10.1/knuth') from capturing a longer
# one ('10.1/knuth-extended') when both could appear in a source.
_DOI_CONT = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-/;()<>[]:")


def _doi_bounded_in(source_lower: str, doi_norm: str) -> bool:
    """True if ``doi_norm`` occurs in ``source_lower`` at a whole-DOI boundary.

    Both args must already be lower-cased. Any occurrence whose following char is
    end-of-string or a non-DOI-continuation char counts; an occurrence followed
    by another DOI char (i.e. it's merely a prefix of a longer DOI) does not.
    """
    if not doi_norm:
        return False
    start = 0
    n = len(doi_norm)
    while True:
        i = source_lower.find(doi_norm, start)
        if i < 0:
            return False
        end = i + n
        nxt = source_lower[end] if end < len(source_lower) else ""
        if nxt == "" or nxt not in _DOI_CONT:
            return True
        start = i + 1


# ---------------------------------------------------------------------------
# Parse-output → element mapping (pure; unit-testable without a DB)
# ---------------------------------------------------------------------------

def elements_from_docx_outline(outline: dict[str, Any]) -> list[dict[str, Any]]:
    """Map :func:`docs_intel.document_outline` output to store elements.

    ``outline`` is ``{paragraph_count, heading_count, headings:[{level, text,
    para_id}], citations:[{source, marker_text, keys, para_id, section_ordinal}],
    ...}``. Emission order (so parent/citation edges resolve on store):

    1. **Headings** — each becomes a ``kind='heading'`` element carrying its
       ``level``, ``text`` and ``ref`` (the docx ``w14:paraId``). Parent edges
       are inferred by heading-level nesting: a heading attaches under the
       nearest preceding heading of a strictly smaller level (``parent_ordinal``),
       else it is a root. Ordinals are assigned in document order.
    2. **Citations** (75d2196d) — one ``kind='citation'`` element per citation
       key (a Zotero group cite ``{a,b}`` expands to two, exactly like the LaTeX
       ``\\cite{a,b}`` path). ``text`` = the raw marker, ``ref`` = the citation
       key, ``level=None``, ``parent_ordinal`` = the ordinal of the enclosing
       section's heading element (mapped from the marker's ``section_ordinal``;
       ``None`` before the first heading). A marker with no resolvable keys still
       emits one element (``ref=None``) so it is never silently dropped. These
       elements feed the same intra-document citation->bibliography edge
       materialisation the LaTeX path uses.
    """
    headings = (outline or {}).get("headings") or []
    elements: list[dict[str, Any]] = []
    # stack of (ordinal, level) for the open ancestor chain.
    stack: list[tuple[int, int]] = []
    # A heading's document-order index (the ``section_ordinal`` the citation
    # parser reports) maps to the ordinal we assign that heading element here.
    # They are identical today (headings are appended first, in order), but the
    # explicit map keeps citation parent resolution correct if that changes.
    section_ordinal_to_element_ordinal: dict[int, int] = {}
    for ordinal, h in enumerate(headings):
        level = h.get("level")
        lvl = int(level) if isinstance(level, (int, float)) else 1
        while stack and stack[-1][1] >= lvl:
            stack.pop()
        parent_ordinal = stack[-1][0] if stack else None
        section_ordinal_to_element_ordinal[ordinal] = ordinal
        elements.append(
            {
                "ordinal": ordinal,
                "level": lvl,
                "kind": "heading",
                "text": h.get("text", ""),
                "ref": h.get("para_id"),
                "parent_ordinal": parent_ordinal,
            }
        )
        stack.append((ordinal, lvl))

    ordinal = len(headings)
    citations = (outline or {}).get("citations") or []
    for cite in citations:
        section_ordinal = cite.get("section_ordinal")
        parent_ordinal = (
            section_ordinal_to_element_ordinal.get(section_ordinal)
            if isinstance(section_ordinal, int)
            else None
        )
        marker_text = cite.get("marker_text", "")
        keys = cite.get("keys") or []
        # One element per citation key (mirrors the LaTeX one-per-key expansion);
        # a keyless marker still yields one element so it is not dropped.
        for key in keys or [None]:
            elements.append(
                {
                    "ordinal": ordinal,
                    "level": None,
                    "kind": "citation",
                    "text": marker_text,
                    "ref": key,
                    "parent_ordinal": parent_ordinal,
                }
            )
            ordinal += 1
    return elements


def elements_from_latex_analysis(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Map :func:`latex_intel.analyze_latex` output to store elements.

    ``analysis`` is ``{heading_count, headings:[{level, kind, text}], tree,
    bibliography:[{key, ...}], citations:[{key, marker_text, section_ordinal}],
    ...}``. Emission order (so parent/citation edges resolve on store):

    1. **Headings** — nested by heading-level (like the docx path); ``kind``
       preserves the LaTeX sectioning command (``section`` / ``subsection`` / …)
       and ``ref`` is ``None`` (LaTeX headings carry no stable id).
    2. **Citations** (fefb596a) — one ``kind='citation'`` element per in-text
       citation key, ``text`` = the raw marker (``\\cite{..}``), ``ref`` = the
       citation key, ``level=None``, ``parent_ordinal`` = the ordinal of the
       enclosing section's heading element (mapped from ``section_ordinal``;
       ``None`` if the citation precedes any heading). Interleaving them by their
       enclosing section keeps parent edges consistent with the heading tree.
    3. **Bibliography** — flat ``kind='bibliography'`` root elements (``ref`` =
       citation key) after the heading/citation elements, so the whole parsed
       structure — and the citation→bibentry edges materialised on store — is
       persisted.
    """
    headings = (analysis or {}).get("headings") or []
    elements: list[dict[str, Any]] = []
    stack: list[tuple[int, int]] = []
    ordinal = 0
    # Map a heading's document-order index (the ``section_ordinal`` the citation
    # parser reports) to the ordinal we assign that heading element here. They are
    # identical today (headings are appended first, in order), but the explicit map
    # keeps citation parent resolution correct even if that ever changes.
    section_ordinal_to_element_ordinal: dict[int, int] = {}
    for heading_index, h in enumerate(headings):
        level = h.get("level")
        # NB: LaTeX \part is a legitimate level 0, so a missing level must NOT
        # fall back to 0 (that would masquerade as a top-level part) — use the
        # deep _UNKNOWN_LEVEL sentinel instead.
        lvl = int(level) if isinstance(level, (int, float)) else _UNKNOWN_LEVEL
        while stack and stack[-1][1] >= lvl:
            stack.pop()
        parent_ordinal = stack[-1][0] if stack else None
        section_ordinal_to_element_ordinal[heading_index] = ordinal
        elements.append(
            {
                "ordinal": ordinal,
                "level": lvl,
                "kind": h.get("kind") or "section",
                "text": h.get("text", ""),
                "ref": None,
                "parent_ordinal": parent_ordinal,
            }
        )
        stack.append((ordinal, lvl))
        ordinal += 1

    citations = (analysis or {}).get("citations") or []
    for cite in citations:
        section_ordinal = cite.get("section_ordinal")
        parent_ordinal = (
            section_ordinal_to_element_ordinal.get(section_ordinal)
            if isinstance(section_ordinal, int)
            else None
        )
        elements.append(
            {
                "ordinal": ordinal,
                "level": None,
                "kind": "citation",
                "text": cite.get("marker_text", ""),
                "ref": cite.get("key"),
                "parent_ordinal": parent_ordinal,
            }
        )
        ordinal += 1

    bibliography = (analysis or {}).get("bibliography") or []
    for entry in bibliography:
        # Compact citation text: prefer title, else raw, else the key.
        text = entry.get("title") or entry.get("raw") or entry.get("key") or ""
        elements.append(
            {
                "ordinal": ordinal,
                "level": None,
                "kind": "bibliography",
                "text": text,
                "ref": entry.get("key"),
                "parent_ordinal": None,
            }
        )
        ordinal += 1
    return elements


def compute_content_hash(elements: list[dict[str, Any]]) -> str:
    """Deterministic content hash of an ordered element list (change detection)."""
    hasher = hashlib.sha256()
    for el in elements:
        parts = (
            str(el.get("ordinal")),
            str(el.get("level")),
            str(el.get("kind")),
            str(el.get("text") or ""),
            str(el.get("ref") or ""),
        )
        hasher.update("\x1f".join(parts).encode("utf-8"))
        hasher.update(b"\x1e")
    return hasher.hexdigest()


def _docx_current_content_hash(source_path: str) -> str:
    """Compute the same structural content hash :func:`_docx_staleness_check`
    (and :meth:`DocStructureStore.reindex_document`) use, straight from
    ``source_path``'s CURRENT on-disk bytes.

    5988a5bb — factored out of :func:`_docx_staleness_check` so
    ``update_paragraph``'s opt-in ``expected_content_hash`` precondition gate
    can compute (and, when the caller also wants the advisory staleness
    warning, REUSE) this exact same value without parsing the document
    twice. Unlike :func:`_docx_staleness_check`, this raises on failure — it
    has no "advisory, fails open" contract of its own; each caller decides
    whether a failure here should block or degrade.
    """
    from .docs_intel import document_content_tree  # noqa: PLC0415 — lazy, optional
    tree = document_content_tree(source_path)
    return compute_content_hash(elements_from_docx_content_tree(tree))


async def _docx_staleness_check(
    doc_row: dict[str, Any], source_path: str, *, current_hash: str | None = None
) -> dict[str, Any] | None:
    """eab6930a — compare the source .docx's CURRENT structural content hash
    against the hash recorded at last ingest/reindex (``doc_row['content_hash']``),
    reusing :func:`compute_content_hash` exactly as :meth:`DocStructureStore.reindex_document`
    does — no new hashing scheme, per the item's own pointer.

    A mismatch means the file changed on disk (e.g. opened and edited directly
    in Word) since doc_store last cached it: a write targeting a ``para_id``
    resolved from that stale index may not mean what the caller thinks, even
    though ``para_id`` lookups themselves always re-read the file fresh (the
    id itself will still very likely resolve — Word's w14:paraId is stable
    across most edits — the risk is the caller's mental model of the
    surrounding content, not a wrong-paragraph write).

    ``current_hash`` (5988a5bb) lets a caller that already computed the
    current on-disk hash for its own purposes (``update_paragraph``'s
    ``expected_content_hash`` gate) pass it straight through instead of
    parsing the document a second time — computed here exactly as before
    when omitted.

    Returns a warning dict when stale, else ``None`` — including when no
    ``content_hash`` was ever recorded (fails open, mirroring docs_intel's
    ``check_staleness`` "no-source-tracked" fail-open case) or when the fresh
    parse itself fails (the caller's own write-path error handling already
    covers a genuinely unreadable/corrupt file; this check must never be what
    blocks a write). Advisory only — never raises, never blocks the write.
    """
    stored_hash = doc_row.get("content_hash")
    if not stored_hash:
        return None
    if current_hash is None:
        try:
            current_hash = _docx_current_content_hash(source_path)
        except Exception:  # noqa: BLE001 — best-effort; never blocks the write
            return None
    if current_hash == stored_hash:
        return None
    return {
        "stale": True,
        "reason": (
            "the source .docx's content has changed on disk since it was last "
            "indexed (ingest_document/reindex_document) -- it may have been "
            "edited outside Meridian (e.g. opened directly in Word). This "
            "write proceeded against the CURRENT file (para_id lookup always "
            "re-reads fresh), but the caller's own understanding of the "
            "document's structure may be stale -- consider reindex_document "
            "before further edits if the target was chosen from cached structure."
        ),
        "stored_content_hash": stored_hash,
        "current_content_hash": current_hash,
    }


# ---------------------------------------------------------------------------
# Figures/tables (06df6ab3) — NOT a new table: new kind='figure'/'table' values
# on the EXISTING doc_elements table, nested "by section" via the same
# heading-stack mechanism elements_from_docx_outline already uses.
# ---------------------------------------------------------------------------

def _seq_field_caption(block: dict[str, Any], label: str) -> str | None:
    """The block's text IF one of its fields is a ``SEQ <label> ...`` field.

    Word captions a figure/table with a paragraph like "Figure 1: ..." carrying a
    ``SEQ Figure \\* ARABIC`` field (docs_intel already extracts these per-block
    fields, a62e5b4f) — this is the real, already-parsed signal used to recognize
    a caption paragraph, rather than guessing from paragraph style names.
    """
    for field in block.get("fields") or []:
        if field.get("field_type") != "SEQ":
            continue
        instruction = (field.get("instruction") or "").strip()
        parts = instruction.split(maxsplit=1)
        arg = parts[1] if len(parts) > 1 else (parts[0] if parts else "")
        if arg.strip().lower().startswith(label.lower()):
            return block.get("text") or ""
    return None


def elements_from_docx_content_tree(tree: dict[str, Any]) -> list[dict[str, Any]]:
    """Map :func:`docs_intel.document_content_tree` output to heading + figure/
    table ``doc_elements`` (06df6ab3), in ONE shared ordinal + heading-nesting
    pass so figures/tables parent to their enclosing section exactly like a
    heading does in :func:`elements_from_docx_outline`.

    * ``kind='heading'`` — identical shape/nesting to ``elements_from_docx_outline``.
    * ``kind='table'`` — one element per real ``<w:tbl>`` block (docs_intel's
      ``_table_node``, reused via ``document_content_tree``); ``text`` is the
      joined row/cell text, ``ref`` is the caption (a neighboring paragraph's
      ``SEQ Table ...`` field, if present, else ``None``).
    * ``kind='figure'`` — a paragraph carrying a ``SEQ Figure ...`` field (Word's
      real captioning mechanism); ``text``/``ref`` are both the caption text (ref
      is the label/caption per the sprint spec). A captionless embedded image has
      no stable OOXML "this is a figure" marker without parsing drawing/blip XML
      (a much bigger scope increase) — out of scope for this pass.

    Plain body paragraphs (non-heading, non-caption) are NOT persisted as
    elements — unchanged scope from ``elements_from_docx_outline``.
    """
    blocks = (tree or {}).get("blocks") or []
    elements: list[dict[str, Any]] = []
    stack: list[tuple[int, int]] = []
    ordinal = 0
    n = len(blocks)

    for i, block in enumerate(blocks):
        kind = block.get("kind")
        if kind == "heading":
            level = block.get("level")
            lvl = int(level) if isinstance(level, (int, float)) else 1
            while stack and stack[-1][1] >= lvl:
                stack.pop()
            parent_ordinal = stack[-1][0] if stack else None
            elements.append({
                "ordinal": ordinal,
                "level": lvl,
                "kind": "heading",
                "text": block.get("text", ""),
                "ref": block.get("para_id"),
                "parent_ordinal": parent_ordinal,
            })
            stack.append((ordinal, lvl))
            ordinal += 1
        elif kind == "table":
            parent_ordinal = stack[-1][0] if stack else None
            rows = block.get("rows") or []
            text = "\n".join(" | ".join(row) for row in rows)
            caption = None
            if i > 0:
                caption = _seq_field_caption(blocks[i - 1], "table")
            if caption is None and i + 1 < n:
                caption = _seq_field_caption(blocks[i + 1], "table")
            elements.append({
                "ordinal": ordinal,
                "level": None,
                "kind": "table",
                "text": text,
                "ref": caption,
                "parent_ordinal": parent_ordinal,
            })
            ordinal += 1
        elif kind == "paragraph":
            caption = _seq_field_caption(block, "figure")
            if caption is not None:
                parent_ordinal = stack[-1][0] if stack else None
                elements.append({
                    "ordinal": ordinal,
                    "level": None,
                    "kind": "figure",
                    "text": block.get("text", "") or caption,
                    "ref": caption,
                    "parent_ordinal": parent_ordinal,
                })
                ordinal += 1
    return elements


# ---------------------------------------------------------------------------
# Word equations (OMML) — 06df6ab3.
#
# READING parses <m:oMath> directly out of word/document.xml inside the .docx
# ZIP via lxml — never a PDF round-trip (PDF conversion rasterizes/destroys OMML
# structure; see HKUDS/RAG-Anything#259 for the confirmed real-world gap this
# avoids).
#
# WRITING pipes latex2mathml (pure-Python LaTeX -> standard MathML) through a
# small HAND-WRITTEN MathML -> OOXML <m:oMath> element mapper
# (``_mathml_to_omml`` below), rather than Microsoft's own MML2OMML.XSL
# stylesheet. That stylesheet ships ONLY inside a licensed Microsoft Office
# install (there is no legitimate standalone/redistributable copy) and this
# environment has neither Office installed nor network access to a legitimate
# source for it — a genuine external blocker, not a shortcut. The sprint spec's
# suggested fallback, the PyPI package ``tex2word``, was evaluated and
# deliberately NOT added: it is an obscure, low-adoption third-party dependency
# whose PyPI summary reads as an unusually exact match for this very sprint
# item's wording ("native OMML + auto-renumbering cross-ref fields") — enough of
# a supply-chain red flag for a production dependency that it warranted human
# review rather than an autonomous `pip install`. See the sprint report for the
# full rationale. Instead, ``_mathml_to_omml`` hand-implements the common OMML
# subset (runs, fractions, super/subscripts, radicals, delimited groups) —a
# real, working, tested conversion for everyday equations, not a stub; anything
# outside that subset (matrices, stacked/aligned systems, ...) degrades to a
# flattened literal text run rather than raising, so the rest of the expression
# still converts.
# ---------------------------------------------------------------------------

_DOCX_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_DOCX_W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
_OMML_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_MATHML_NS = "http://www.w3.org/1998/Math/MathML"
_OMML_NSMAP = {"m": _OMML_NS}

# MathML tags that are pure grouping/sequencing — descend into children directly
# (OMML has no "row" wrapper element; a sequence is just sibling m:r/m:... nodes).
_MATHML_ROW_TAGS = frozenset({"math", "mrow", "mstyle", "mpadded", "semantics", "mphantom"})
# MathML leaf text tags -> a single OMML text run (m:r/m:t).
_MATHML_TEXT_TAGS = frozenset({"mi", "mn", "mo", "mtext"})


def _om(tag: str) -> str:
    return f"{{{_OMML_NS}}}{tag}"


def _mm(tag: str) -> str:
    return f"{{{_MATHML_NS}}}{tag}"


def _local_tag(el: Any) -> str:
    """The unqualified (no-namespace) tag name of an lxml element."""
    tag = el.tag
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _node_text(node: Any) -> str:
    return "".join(node.itertext())


def _append_run(parent: Any, text: str) -> None:
    if not text:
        return
    run = _LET.SubElement(parent, _om("r"))
    t = _LET.SubElement(run, _om("t"))
    t.text = text


def _append_mathml(node: Any, parent: Any) -> None:
    """Append ``node`` (a MathML element) onto ``parent`` (an OMML container),
    recursively converting the common subset. See module docstring for scope."""
    tag = _local_tag(node)
    if tag in _MATHML_ROW_TAGS:
        for child in node:
            _append_mathml(child, parent)
        return
    if tag in _MATHML_TEXT_TAGS:
        text = node.text or ""
        if text.strip().casefold() in {"min", "max", "argmin", "argmax", "sup", "inf"}:
            func = _LET.SubElement(parent, _om("func"))
            f_name = _LET.SubElement(func, _om("fName"))
            _append_run(f_name, text)
            _LET.SubElement(func, _om("e"))
        else:
            _append_run(parent, text)
        return
    if tag == "msup":
        kids = list(node)
        base = kids[0] if len(kids) > 0 else None
        exp = kids[1] if len(kids) > 1 else None
        sup = _LET.SubElement(parent, _om("sSup"))
        e = _LET.SubElement(sup, _om("e"))
        if base is not None:
            _append_mathml(base, e)
        sup_el = _LET.SubElement(sup, _om("sup"))
        if exp is not None:
            _append_mathml(exp, sup_el)
        return
    if tag == "msub":
        kids = list(node)
        base = kids[0] if len(kids) > 0 else None
        sub = kids[1] if len(kids) > 1 else None
        s = _LET.SubElement(parent, _om("sSub"))
        e = _LET.SubElement(s, _om("e"))
        if base is not None:
            _append_mathml(base, e)
        sub_el = _LET.SubElement(s, _om("sub"))
        if sub is not None:
            _append_mathml(sub, sub_el)
        return
    if tag == "msubsup":
        kids = list(node)
        base = kids[0] if len(kids) > 0 else None
        sub = kids[1] if len(kids) > 1 else None
        sup = kids[2] if len(kids) > 2 else None
        ss = _LET.SubElement(parent, _om("sSubSup"))
        e = _LET.SubElement(ss, _om("e"))
        if base is not None:
            _append_mathml(base, e)
        sub_el = _LET.SubElement(ss, _om("sub"))
        if sub is not None:
            _append_mathml(sub, sub_el)
        sup_el = _LET.SubElement(ss, _om("sup"))
        if sup is not None:
            _append_mathml(sup, sup_el)
        return
    if tag == "mfrac":
        kids = list(node)
        num = kids[0] if len(kids) > 0 else None
        den = kids[1] if len(kids) > 1 else None
        f = _LET.SubElement(parent, _om("f"))
        n_el = _LET.SubElement(f, _om("num"))
        n_expr = _LET.SubElement(n_el, _om("e"))
        if num is not None:
            _append_mathml(num, n_expr)
        d_el = _LET.SubElement(f, _om("den"))
        d_expr = _LET.SubElement(d_el, _om("e"))
        if den is not None:
            _append_mathml(den, d_expr)
        return
    if tag == "msqrt":
        rad = _LET.SubElement(parent, _om("rad"))
        rad_pr = _LET.SubElement(rad, _om("radPr"))
        deg_hide = _LET.SubElement(rad_pr, _om("degHide"))
        deg_hide.set(_om("val"), "1")
        _LET.SubElement(rad, _om("deg"))
        e = _LET.SubElement(rad, _om("e"))
        for child in node:
            _append_mathml(child, e)
        return
    if tag == "mroot":
        kids = list(node)
        base = kids[0] if len(kids) > 0 else None
        index = kids[1] if len(kids) > 1 else None
        rad = _LET.SubElement(parent, _om("rad"))
        deg_el = _LET.SubElement(rad, _om("deg"))
        if index is not None:
            _append_mathml(index, deg_el)
        e = _LET.SubElement(rad, _om("e"))
        if base is not None:
            _append_mathml(base, e)
        return
    if tag == "mfenced":
        d = _LET.SubElement(parent, _om("d"))
        expr = _LET.SubElement(d, _om("e"))
        for child in node:
            _append_mathml(child, expr)
        return
    if tag == "mtable":
        arr = _LET.SubElement(parent, _om("eqArr"))
        for row in node:
            if _local_tag(row) != "mtr":
                continue
            row_expr = _LET.SubElement(arr, _om("e"))
            for cell in row:
                cell_expr = _LET.SubElement(row_expr, _om("e"))
                for child in cell:
                    _append_mathml(child, cell_expr)
        return
    if tag in {"munder", "mover", "munderover"}:
        kids = list(node)
        base = kids[0] if kids else None
        lower = kids[1] if tag == "munder" and len(kids) > 1 else None
        upper = kids[1] if tag == "mover" and len(kids) > 1 else None
        if tag == "munderover":
            lower = kids[1] if len(kids) > 1 else None
            upper = kids[2] if len(kids) > 2 else None
        upper_text = _node_text(upper).strip() if upper is not None else ""
        if tag == "mover" and len(upper_text) == 1 and upper_text in {"^", "~", "¯", "ˉ", "→", "⃗", "ˆ"}:
            acc = _LET.SubElement(parent, _om("acc"))
            acc_pr = _LET.SubElement(acc, _om("accPr"))
            _LET.SubElement(acc_pr, _om("chr"), {_om("val"): upper_text})
            expr = _LET.SubElement(acc, _om("e"))
            if base is not None:
                _append_mathml(base, expr)
            return
        container = _LET.SubElement(parent, _om("nary" if tag == "munderover" else ("limLow" if tag == "munder" else "limUpp")))
        expr = _LET.SubElement(container, _om("e"))
        if base is not None:
            _append_mathml(base, expr)
        if lower is not None:
            target = _LET.SubElement(container, _om("sub" if tag == "munderover" else "lim"))
            _append_mathml(lower, target)
        if upper is not None:
            target = _LET.SubElement(container, _om("sup" if tag == "munderover" else "lim"))
            _append_mathml(upper, target)
        return
    # Unrecognized construct (mtable/mmultiscripts/menclose/...) — degrade to a
    # literal flattened text run so the rest of the expression still converts.
    _append_run(parent, _node_text(node))


def _mathml_to_omml(mathml_root: Any) -> Any:
    """Convert a parsed MathML ``<math>`` (lxml) element into a real ``<m:oMath>``."""
    omath = _LET.Element(_om("oMath"), nsmap=_OMML_NSMAP)
    _append_mathml(mathml_root, omath)
    return omath


def latex_to_omml(latex: str | None) -> str | None:
    """Best-effort LaTeX -> real OOXML ``<m:oMath>`` XML string, or ``None``.

    Pipeline: ``latex2mathml`` (pure Python) -> standard MathML -> the
    hand-written ``_mathml_to_omml`` mapper above (see the module docstring for
    why this doesn't use Microsoft's MML2OMML.XSL). Never raises: any failure
    (missing dependency, unparsable LaTeX, malformed MathML) returns ``None``.
    """
    if not isinstance(latex, str) or not latex.strip():
        return None
    try:
        import latex2mathml.converter as _l2m  # noqa: PLC0415 — optional/lazy
    except Exception:  # noqa: BLE001 — dependency genuinely missing
        _log.debug("latex2mathml unavailable; cannot convert %r", latex)
        return None
    try:
        mathml = _l2m.convert(latex)
        mathml_root = _LET.fromstring(mathml.encode("utf-8"))
        omath = _mathml_to_omml(mathml_root)
        raw = _LET.tostring(omath, encoding="unicode")
        _validate_omml_structure(raw)
        return raw
    except Exception:  # noqa: BLE001 — conversion is best-effort, never raises
        _log.debug("latex_to_omml failed for %r", latex, exc_info=True)
        return None


def _omml_flatten_text(omml_raw: str | None) -> str:
    """Concatenate every ``<m:t>`` run inside a raw OMML string (best-effort).

    NOT a real OMML -> LaTeX reverse conversion (that is a much bigger
    undertaking, out of scope here) — this is a flattened literal-text surrogate
    used purely as the fuzzy-dedup key for equations that only carry OMML (no
    LaTeX source), e.g. ones parsed straight out of a .docx. Returns ``""`` (never
    raises) for blank/malformed input.
    """
    if not omml_raw:
        return ""
    try:
        raw_bytes = omml_raw.encode("utf-8") if isinstance(omml_raw, str) else bytes(omml_raw)
        el = _LET.fromstring(raw_bytes)
    except Exception:  # noqa: BLE001
        return ""
    return "".join(t.text or "" for t in el.iter(_om("t")))


_OMML_REQUIRED_CHILDREN: dict[str, tuple[str, ...]] = {
    "f": ("num", "den"), "sSub": ("e", "sub"), "sSup": ("e", "sup"),
    "sSubSup": ("e", "sub", "sup"), "rad": ("e",), "acc": ("accPr", "e"),
    "d": ("e",), "eqArr": ("e",), "func": ("fName", "e"),
    "limLow": ("e", "lim"), "limUpp": ("e", "lim"), "nary": ("e",),
}
_OMML_FALLBACK_MARKERS = {
    "fraction": {"f"}, "cases": {"eqArr"}, "matrix": {"eqArr"},
    "summation": {"nary", "limLow", "limUpp"}, "subscript": {"sSub", "sSubSup"},
    "superscript": {"sSup", "sSubSup"}, "argmin": {"func", "nary", "limLow", "limUpp"},
}


def _validate_omml_structure(omml_raw: str) -> Any:
    """Validate one semantic ``m:oMath`` before a source DOCX is mutated."""
    try:
        root = _LET.fromstring(omml_raw.encode("utf-8") if isinstance(omml_raw, str) else bytes(omml_raw))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"OMML payload is not valid XML: {exc}") from exc
    if root.tag == _om("oMathPara"):
        raise ValueError("OMML m:oMath root required; m:oMathPara is not accepted for insertion")
    if root.tag != _om("oMath"):
        raise ValueError(f"OMML m:oMath root required; got m:{_local_tag(root)}")
    for element in root.iter():
        name = _local_tag(element)
        required = _OMML_REQUIRED_CHILDREN.get(name)
        if required:
            children = {_local_tag(child) for child in element}
            missing = [child for child in required if child not in children]
            if missing:
                raise ValueError(f"OMML <m:{name}> is missing required child element(s): {', '.join(missing)}")
        if name in {"num", "den"} and element.find(_om("e")) is None:
            raise ValueError(f"OMML <m:{name}> must contain <m:e>")
    flat = _omml_flatten_text(omml_raw).casefold()
    names = {_local_tag(element) for element in root.iter()}
    for marker, structural_names in _OMML_FALLBACK_MARKERS.items():
        if marker in flat and not names.intersection(structural_names):
            raise ValueError(f"OMML contains flattened fallback text {marker!r} without its structural element")
    return root


def parse_docx_equations(source: str | bytes | bytearray) -> list[dict[str, Any]]:
    """Parse every ``<m:oMath>`` in ``word/document.xml`` via lxml (06df6ab3).

    Reads the real OOXML tree directly out of the .docx ZIP — NEVER a PDF
    round-trip (PDF conversion rasterizes/destroys OMML structure). Returns an
    ordered list of ``{ordinal, element_id, omml_raw}`` — ``element_id`` is the
    containing paragraph's stable ``w14:paraId`` (synthesized ``p{index}`` when
    absent, matching :func:`docs_intel.parse_docx`'s convention); ``omml_raw`` is
    the equation's exact serialized ``<m:oMath>...</m:oMath>`` XML. A document
    with no equations (or no body) returns ``[]``.
    """
    if isinstance(source, (bytes, bytearray)):
        zf = zipfile.ZipFile(io.BytesIO(bytes(source)))
    else:
        zf = zipfile.ZipFile(source)
    try:
        with zf.open("word/document.xml") as handle:
            xml_bytes = handle.read()
    finally:
        zf.close()
    root = _LET.fromstring(xml_bytes)
    w_p = f"{{{_DOCX_W_NS}}}p"
    w14_para_id = f"{{{_DOCX_W14_NS}}}paraId"
    m_omath = _om("oMath")
    equations: list[dict[str, Any]] = []
    ordinal = 0
    for idx, p in enumerate(root.iter(w_p)):
        para_id = p.get(w14_para_id) or f"p{idx}"
        for math_el in p.iter(m_omath):
            equations.append({
                "ordinal": ordinal,
                "element_id": para_id,
                "omml_raw": _LET.tostring(math_el, encoding="unicode"),
            })
            ordinal += 1
    return equations


# ---------------------------------------------------------------------------
# .docx write-back helpers (51a595e7 + f978e588) -- open / find / save + run edit
# ---------------------------------------------------------------------------
#
# The READ side (parse_docx_equations / docs_intel) already reaches straight into
# a .docx's ``word/document.xml`` with lxml + zipfile. These helpers are the
# WRITE-side mirror needed to author OMML (insert_equation) OR edit paragraph
# text/runs (update_paragraph) directly into the source .docx WITHOUT a
# python-docx dependency: parse the document part, mutate the tree in place, and
# rewrite ONLY that one zip entry (every other part -- styles, rels, media -- is
# copied through byte-for-byte). All are pure/synchronous and unit-testable.
#
# insert_equation (51a595e7) and update_paragraph (f978e588) were built in
# parallel and each authored their own copy of the open/find/save primitives.
# This is the deduped, reconciled canonical set that BOTH tools call.
# ---------------------------------------------------------------------------

_DOCX_DOCUMENT_PART = "word/document.xml"


def _load_docx_xml(source: str | bytes | bytearray) -> tuple[bytes, Any]:
    """Read a .docx's ``word/document.xml`` and return ``(raw_bytes, root)``.

    ``raw_bytes`` is the *whole* .docx ZIP as bytes (so the caller can rewrite a
    single part via :func:`_save_docx_xml` without re-reading a path that may have
    changed); ``root`` is the parsed ``<w:document>`` lxml element. ``source`` is
    a filesystem path OR the raw .docx bytes. Raises the underlying zipfile/lxml
    error on a genuinely malformed/missing input (callers guard):
    ``zipfile.BadZipFile`` for a non-.docx, ``KeyError`` for a missing document
    part.
    """
    if isinstance(source, (bytes, bytearray)):
        raw = bytes(source)
    else:
        with open(source, "rb") as fh:
            raw = fh.read()
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        with zf.open(_DOCX_DOCUMENT_PART) as handle:
            xml_bytes = handle.read()
    root = _LET.fromstring(xml_bytes)
    return raw, root


class AmbiguousParagraphIdError(ValueError):
    """827b6bdc -- fail-closed rejection of a para_id that resolves to MORE
    than one ``<w:p>`` element.

    Raised by :func:`_find_paragraph_by_id` / :func:`_find_paragraph_with_index`
    instead of silently returning their first match. Word's ``w14:paraId`` is
    SUPPOSED to be unique per paragraph, but nothing enforces that on read --
    a malformed / hand-edited / merged .docx can carry the same native id on
    two distinct paragraphs (the reference case: two paragraphs both carrying
    ``w14:paraId="6BDC5378"``). First-match-wins addressing on a WRITE path
    (``DocStructureStore.update_paragraph`` and everything else that resolves
    a para_id through these two functions) would silently edit whichever
    paragraph happened to be encountered first during the tree walk -- which
    may not be the one the caller meant -- with no error and no warning. That
    is a strictly worse failure mode than refusing outright, so this fails
    closed instead. Subclasses ``ValueError`` because every existing
    para_id-resolution failure in this module (unknown id, missing/invalid
    para_id argument) already raises ``ValueError`` -- an ambiguous id is
    just another way a para_id fails to resolve to a single answer, so
    existing ``except ValueError`` callers keep working without changes;
    ``isinstance(exc, AmbiguousParagraphIdError)`` distinguishes this specific
    cause when a caller wants to.
    """

    def __init__(self, message: str, *, para_id: str, matches: list[dict[str, Any]]):
        super().__init__(message)
        self.para_id = para_id
        self.matches = matches


def _resolve_paragraph_matches(root: Any, para_id: str) -> list[tuple[int, Any]]:
    """Shared match logic for :func:`_find_paragraph_by_id` /
    :func:`_find_paragraph_with_index` (827b6bdc): every ``<w:p>`` in *root*
    whose native ``w14:paraId`` (or, absent one, the synthesized ``p{idx}``
    positional fallback) equals ``para_id``, as ``[(idx, element), ...]`` in
    document order. Normally a single match; more than one means ``para_id``
    is ambiguous in this document. Never raises -- callers decide what an
    empty / multi-element result means.
    """
    target = para_id.strip()
    w_p = f"{{{_DOCX_W_NS}}}p"
    w14_para_id = f"{{{_DOCX_W14_NS}}}paraId"
    matches: list[tuple[int, Any]] = []
    for idx, p in enumerate(root.iter(w_p)):
        real_id = p.get(w14_para_id)
        synthetic_id = f"p{idx}"
        if real_id == target or (real_id is None and synthetic_id == target) or synthetic_id == target:
            matches.append((idx, p))
    return matches


def _raise_ambiguous_paragraph_id(para_id: str, matches: list[tuple[int, Any]]) -> None:
    indices = [idx for idx, _ in matches]
    raise AmbiguousParagraphIdError(
        f"para_id={para_id!r} matches {len(matches)} paragraphs in document "
        f"order (indices {indices}) -- refusing to guess which one is the "
        "target; the source .docx likely has a duplicated native w14:paraId "
        "(a malformed/hand-edited/merged document). Use "
        "repair_duplicate_para_ids to resolve the collision before "
        "addressing this id again.",
        para_id=para_id,
        matches=[
            {"index": idx, "text": _paragraph_plain_text(p)[:200]}
            for idx, p in matches
        ],
    )


def _find_paragraph_by_id(root: Any, para_id: str) -> Any | None:
    """Return the ``<w:p>`` element whose id == ``para_id``, or ``None``.

    The id is the paragraph's stable ``w14:paraId`` attribute; when a paragraph
    carries none, a synthesized ``p{index}`` id is matched instead -- EXACTLY the
    convention :func:`parse_docx_equations` / ``docs_intel.parse_docx`` use for
    ``element_id``, so a ``para_id`` obtained from either read side round-trips
    here. The synthesized index counts every ``<w:p>`` in document order. Returns
    the bare paragraph element (or ``None``); callers needing the body-order index
    use :func:`_find_paragraph_with_index`.

    827b6bdc -- raises :class:`AmbiguousParagraphIdError` when ``para_id``
    matches MORE than one ``<w:p>`` element (a duplicated native
    ``w14:paraId`` in the source document) instead of silently returning
    whichever one the tree walk reached first -- see that class's docstring
    for why first-match-wins is unsafe here.
    """
    if not isinstance(para_id, str) or not para_id.strip():
        return None
    matches = _resolve_paragraph_matches(root, para_id.strip())
    if not matches:
        return None
    if len(matches) > 1:
        _raise_ambiguous_paragraph_id(para_id.strip(), matches)
    return matches[0][1]


def _find_paragraph_with_index(root: Any, para_id: str) -> tuple[Any, int] | None:
    """Like :func:`_find_paragraph_by_id` but also return the body-order ``idx``.

    Returns ``(element, idx)`` where ``idx`` is the paragraph's zero-based
    position among every ``<w:p>`` in document order (matching the ``p{index}``
    synthesized-id convention), or ``None`` when nothing matches. Shares the exact
    match rule -- and, per 827b6bdc, the same :class:`AmbiguousParagraphIdError`
    fail-closed behavior on a duplicated id -- with :func:`_find_paragraph_by_id`.
    """
    if not isinstance(para_id, str) or not para_id.strip():
        return None
    matches = _resolve_paragraph_matches(root, para_id.strip())
    if not matches:
        return None
    if len(matches) > 1:
        _raise_ambiguous_paragraph_id(para_id.strip(), matches)
    idx, p = matches[0]
    return p, idx


class DocxWriteVerificationError(OSError):
    """dccc2311 -- fail-closed rejection of a staged DOCX write transaction.

    Raised by :func:`_write_docx_transaction` when the disposable staged
    artifact fails structural verification BEFORE it is ever promoted over
    the live file -- the destination is guaranteed byte-for-byte untouched
    whenever this is raised. Subclasses ``OSError`` so it is caught by any
    existing broad ``except OSError`` / ``except Exception`` guard around a
    docx write (e.g. ``insert_equation``'s ``except Exception as exc:`` when
    opening/writing the source .docx) without requiring call-site changes,
    while still being distinguishable via ``isinstance()`` by tests/callers
    that want the specific fail-closed-verification failure mode rather than
    a generic disk/permission error.
    """

    def __init__(self, message: str, *, manifest: dict[str, Any] | None = None):
        super().__init__(message)
        self.manifest = manifest or {}


class DocxPostWriteVerificationError(OSError):
    """5988a5bb -- fail-closed rejection AFTER promotion, when the specific
    edit a write transaction was supposed to make cannot be confirmed on disk.

    Distinct from :class:`DocxWriteVerificationError` (dccc2311), which fires
    BEFORE promotion and guarantees the destination is untouched.
    :func:`_write_docx_transaction`'s structural manifest only proves the
    archive as a WHOLE wasn't corrupted (media/style/relationship counts
    unchanged) -- it has no idea what the write was actually FOR. This error
    fires after the staged artifact has already replaced the destination,
    when a caller-specific check (e.g. re-reading a target paragraph's text)
    finds the promoted file doesn't actually contain the intended edit -- a
    silent no-op or wrong-target write that the structural manifest alone
    would never catch. A best-effort restore from ``dest + ".bak"`` is
    attempted before this is raised -- but ONLY when it is safe to do so
    (see :func:`_safe_restore_after_verification_failure`: a compare-and-swap
    check confirming no OTHER writer has promoted something newer to
    ``dest`` since this writer's own promotion). "Untouched" cannot be
    guaranteed here the way it is for :class:`DocxWriteVerificationError`
    (this fires AFTER promotion), only "restored on a best-effort basis when
    safe" -- check ``.manifest.get("restored")`` for whether it was
    attempted and succeeded. When it was NOT safe to restore (a genuine
    concurrent write landed), :class:`DocxConcurrentWriteConflictError` is
    raised instead of this class -- the two are deliberately distinguishable
    via ``isinstance()`` so a caller/operator can tell "my own write simply
    didn't verify" apart from "someone else's write landed after mine and I
    backed off rather than clobber it." Subclasses ``OSError`` for the same
    broad-except compatibility as :class:`DocxWriteVerificationError`.
    """

    def __init__(self, message: str, *, manifest: dict[str, Any] | None = None):
        super().__init__(message)
        self.manifest = manifest or {}


class DocxConcurrentWriteConflictError(OSError):
    """5988a5bb (finding 1) -- a post-write verification failure that could NOT
    be safely auto-corrected because a DIFFERENT writer's promotion landed on
    the same destination after this writer's own promotion.

    Raised by ``update_paragraph`` / ``merge_paragraph_draft`` instead of
    :class:`DocxPostWriteVerificationError` when
    :func:`_safe_restore_after_verification_failure` finds that the
    destination's CURRENT on-disk bytes no longer match what THIS writer
    itself promoted (via ``_write_docx_transaction``'s ``promoted_sha256``
    fingerprint). In that situation the original verification failure this
    writer observed is very likely a FALSE POSITIVE caused by the other
    writer's legitimate, already-promoted edit landing in between this
    writer's own promotion and its own verify -- restoring from this
    writer's ``.bak`` would silently destroy that other, completed write.

    This error is deliberately raised WITHOUT touching the destination any
    further: it is left exactly as the other writer left it. Distinguishable
    via ``isinstance()`` from :class:`DocxPostWriteVerificationError` (a
    same-writer verification failure that WAS safe to restore) so a caller
    can tell the two apart rather than treat every post-write failure
    identically. Subclasses ``OSError`` for the same broad-except
    compatibility as the other docx write-transaction errors in this module.

    Note this only catches a CROSS-PROCESS race (or same-process code that
    bypasses :func:`_docx_promotion_lock`) -- a same-process, same-thread-
    reentrant race is already closed entirely by that lock now spanning the
    full stage+promote -> verify -> conditional-restore sequence (see its
    module-level comment).
    """

    def __init__(self, message: str, *, manifest: dict[str, Any] | None = None):
        super().__init__(message)
        self.manifest = manifest or {}


_DOCX_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

# dccc2311 -- single serialized canonical-merge point per destination path,
# mirroring extensions/meridian-docs/meridian_docs/docs_intel.py's
# _docx_promotion_lock (an independent copy, not a shared import -- see the
# module-docstring note above on WRITE-side duplication with docs_intel:
# this core package intentionally never imports the standalone
# stdlib-only extension, and vice versa). Keyed on the destination's
# normalized absolute path so two threads racing to promote a staged draft
# into the SAME .docx can never interleave their promotion; distinct
# destinations never block each other.
#
# 5988a5bb -- widened from ``threading.Lock`` to ``threading.RLock``. Callers
# with a caller-specific post-write check (``update_paragraph`` /
# ``merge_paragraph_draft``) now hold this SAME lock across their entire
# stage+promote -> verify -> conditional-restore sequence, not just the
# promote step -- ``_write_docx_transaction`` still acquires it internally
# for its own stage+promote step, so a caller holding it at the top needs
# reentrant acquisition from the SAME thread to avoid deadlocking itself.
# This closes the SAME-PROCESS race window between promotion and a
# subsequent verify/restore completely: two concurrent async tasks/threads
# within THIS process can never interleave one writer's promotion with
# another writer's verify-then-restore. It does NOT -- and structurally
# cannot, since a lock (reentrant or not) is process-local -- protect
# against a DIFFERENT process promoting to the same ``dest`` in that window.
# That is the realistic threat model here: this codebase runs one process
# per client session in the self-hosted deployment model (AGENTS.md), so
# two legitimate writers racing the same file are almost always two
# processes, not two threads in one. The cross-process case is instead
# covered by the compare-and-swap fingerprint check in ``update_paragraph``
# / ``merge_paragraph_draft`` (comparing ``dest``'s CURRENT on-disk bytes
# against what THIS writer itself promoted before deciding whether a
# verification-failure restore is safe) -- see 5988a5bb finding notes.
_DOCX_PROMOTION_LOCKS: dict[str, threading.RLock] = {}
_DOCX_PROMOTION_LOCKS_GUARD = threading.Lock()


def _docx_promotion_lock(dest: str) -> threading.RLock:
    """Return the process-wide, reentrant promotion lock for ``dest``'s
    canonical path (5988a5bb -- reentrant so a caller can hold it across a
    stage+promote -> verify -> conditional-restore sequence while
    ``_write_docx_transaction`` also reentrantly acquires it internally for
    its own stage+promote step; see the module-level comment above)."""
    key = os.path.normcase(os.path.abspath(dest))
    with _DOCX_PROMOTION_LOCKS_GUARD:
        lock = _DOCX_PROMOTION_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _DOCX_PROMOTION_LOCKS[key] = lock
        return lock


def _docx_media_count(raw: bytes) -> int:
    """Count ``word/media/*`` parts in a (pre- or post-write) .docx ZIP."""
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        return sum(1 for name in zf.namelist() if name.startswith("word/media/"))


def _docx_style_count(raw: bytes) -> int:
    """Count ``<w:style>`` elements in ``word/styles.xml`` (0 when absent)."""
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        if "word/styles.xml" not in zf.namelist():
            return 0
        data = zf.read("word/styles.xml")
    try:
        root = _LET.fromstring(data)
    except _LET.XMLSyntaxError:
        return 0
    return sum(1 for _ in root.iter(f"{{{_DOCX_W_NS}}}style"))


_DOCX_HEADER_FOOTER_RE = re.compile(r"^word/(?:header|footer)\d+\.xml$")


def _docx_equation_count(raw: bytes) -> int:
    """Count ``<m:oMath>`` elements across ``word/document.xml`` plus any
    ``word/header<N>.xml`` / ``word/footer<N>.xml`` parts present."""
    total = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = [
            name for name in zf.namelist()
            if name == _DOCX_DOCUMENT_PART or _DOCX_HEADER_FOOTER_RE.match(name)
        ]
        for name in names:
            try:
                part_root = _LET.fromstring(zf.read(name))
            except _LET.XMLSyntaxError:
                continue
            total += sum(1 for _ in part_root.iter(f"{{{_OMML_NS}}}oMath"))
    return total


def _docx_relationship_count(raw: bytes) -> int:
    """Count ``<Relationship>`` elements across every ``*.rels`` part."""
    total = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        for name in zf.namelist():
            if not name.endswith(".rels"):
                continue
            try:
                rels_root = _LET.fromstring(zf.read(name))
            except _LET.XMLSyntaxError:
                continue
            total += sum(1 for _ in rels_root.iter(f"{{{_DOCX_PKG_REL_NS}}}Relationship"))
    return total


def _docx_structural_manifest(raw: bytes) -> dict[str, int]:
    """Structural fingerprint used to gate a write transaction (dccc2311).

    Counts the four structural families a write transaction must never
    silently lose: embedded media (images), paragraph styles, equations, and
    OOXML package relationships. Computed identically on the PRE-write bytes
    and the STAGED post-write bytes so a caller can compare before ever
    promoting a staged artifact into the live file.
    """
    return {
        "media_count": _docx_media_count(raw),
        "style_count": _docx_style_count(raw),
        "equation_count": _docx_equation_count(raw),
        "relationship_count": _docx_relationship_count(raw),
    }


def _docx_manifest_hash(changed_parts: dict[str, bytes]) -> str:
    """Deterministic SHA-256 over the parts a write transaction actually changed.

    Pure function of ``changed_parts`` (sorted by name first, so caller
    iteration order never affects the result) -- the same input always
    yields the same hash, and it identifies the transaction's actual DELTA
    rather than conflating it with everything else in the archive that
    stayed the same.
    """
    h = hashlib.sha256()
    for name in sorted(changed_parts):
        name_bytes = name.encode("utf-8")
        h.update(len(name_bytes).to_bytes(4, "big"))
        h.update(name_bytes)
        data = changed_parts[name]
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()


def _check_artifact_provenance(artifact_provenance: "dict[str, Any] | None") -> None:
    """Fail-closed gate on a caller-supplied, pre-computed artifact-provenance
    binding verdict (sprint item 6d02f343 -- bind figure/table/equation
    artifacts to per-file provenance and fail closed on mismatched writes).

    ``artifact_provenance`` is the plain dict returned by
    ``meridian_outputs.provenance.bind_artifact_provenance`` (or an
    equivalent caller-built dict sharing its ``{"all_clear": bool,
    "bindings": [...]}`` shape). This module deliberately never imports
    ``meridian_outputs`` itself -- it is a separate, optionally-installed
    extension (see that package's own ``provenance_status.py`` docstring for
    the established pattern: the CALLER computes the provenance verdict and
    hands the resulting plain dict in here, duck-typed, rather than this
    module reaching across the package boundary). ``None`` (the default)
    means the caller did not ask for this check -- zero behavior change for
    every write path that predates this item.

    Raises :class:`DocxWriteVerificationError` -- BEFORE promotion, so
    ``dest`` is guaranteed byte-for-byte untouched -- when
    ``artifact_provenance`` was supplied but is missing/malformed, or is
    supplied and not cleanly ``all_clear``. Never silently treats "could not
    check" as "passed": an opt-in caller that asked for this gate gets an
    explicit reject, not a best-effort skip.
    """
    if artifact_provenance is None:
        return
    if not isinstance(artifact_provenance, dict) or "all_clear" not in artifact_provenance:
        raise DocxWriteVerificationError(
            "post-write verification failed: artifact_provenance was "
            "supplied but is not a valid binding-verdict dict (missing "
            "'all_clear') -- refusing to promote a write whose artifact "
            "provenance this cannot confirm",
            manifest={"artifact_provenance": artifact_provenance},
        )
    if not artifact_provenance.get("all_clear"):
        rejected = [
            binding
            for binding in (artifact_provenance.get("bindings") or [])
            if binding.get("status") != "resolved"
        ]
        raise DocxWriteVerificationError(
            "post-write verification failed: one or more figure/table/"
            "equation artifacts failed provenance binding (orphaned, "
            "hash-mismatched, or unresolved) -- discarding the staged "
            "artifact instead of promoting a write with unverified "
            "artifact provenance",
            manifest={
                "artifact_provenance": artifact_provenance,
                "rejected_bindings": rejected,
            },
        )


def _write_docx_transaction(
    payload: bytes,
    dest: str,
    *,
    pre_manifest: dict[str, int],
    protected_keys: tuple[str, ...],
    changed_parts: dict[str, bytes],
    artifact_provenance: "dict[str, Any] | None" = None,
) -> dict[str, Any]:
    """Stage / verify / (serialized) promote a DOCX write transaction (dccc2311).

    1. STAGE -- ``payload`` (the complete, already-repackaged ZIP) is flushed
       to a disposable temp file in ``dest``'s own directory. It is NEVER
       written to ``dest`` directly -- ``dest`` is untouched unless and until
       verification (step 2) passes.
    2. VERIFY -- the staged file is re-opened FRESH FROM DISK (never the
       in-memory ``payload`` object, which would just re-validate the build
       step's own intent) and its structural manifest
       (:func:`_docx_structural_manifest`) is compared against
       ``pre_manifest`` for every key in ``protected_keys``. Any mismatch --
       or a staged artifact that isn't even a valid .docx -- is a
       fail-closed verification failure: :class:`DocxWriteVerificationError`
       is raised, the staged file is discarded, and ``dest`` is left
       byte-for-byte untouched.
    3. PROMOTE -- the ONLY point at which the live file changes is inside
       :func:`_docx_promotion_lock`'s single serialized canonical-merge
       point for ``dest``: an ``os.replace`` (atomic on the same filesystem)
       swaps the verified staged artifact over ``dest``. Two concurrent
       writers targeting the same ``dest`` can never interleave their
       promotions. A pre-existing ``dest`` is backed up to ``dest + ".bak"``
       immediately before promotion (best-effort, non-fatal on failure --
       a doc write must not fail because backup housekeeping did).

    Returns ``{"manifest_hash", "pre_counts", "post_counts", "promoted_sha256"}``.
    ``promoted_sha256`` (5988a5bb) is a full-body SHA-256 over the EXACT
    bytes this call promoted (the staged artifact, re-read fresh from disk
    after flush -- never the in-memory ``payload`` object), independent of
    ``manifest_hash`` (which only hashes ``changed_parts``, i.e. the
    transaction's intended delta). It is the "what did THIS writer actually
    put on disk" fingerprint a caller-specific post-write check uses to tell
    apart "verification failed but nobody has touched dest since I promoted"
    (safe to restore my own pre-image) from "a different writer's promotion
    has already landed since mine" (restoring would destroy that writer's
    completed work -- see ``update_paragraph`` / ``merge_paragraph_draft``).

    ``artifact_provenance`` (6d02f343, optional) -- when supplied, checked
    via :func:`_check_artifact_provenance` immediately after step 2's
    structural manifest passes and before step 3 ever promotes: a
    caller-computed figure/table/equation provenance-binding verdict
    (``meridian_outputs.provenance.bind_artifact_provenance``'s return
    shape) that is not cleanly ``all_clear`` fails the transaction closed
    exactly like a structural mismatch -- ``dest`` stays untouched. ``None``
    (the default) skips this check entirely, unchanged behavior for every
    caller that predates this item.
    """
    parent = os.path.dirname(os.path.abspath(dest)) or "."
    os.makedirs(parent, exist_ok=True)
    manifest_hash = _docx_manifest_hash(changed_parts)
    post_counts: dict[str, int] | None = None
    promoted_sha256: str | None = None

    staged_path: str | None = None
    try:
        fd, staged_path = tempfile.mkstemp(
            prefix=".meridian-docx-stage-", suffix=".tmp", dir=parent
        )
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())

        with open(staged_path, "rb") as fh:
            staged_bytes = fh.read()
        promoted_sha256 = hashlib.sha256(staged_bytes).hexdigest()
        try:
            post_counts = _docx_structural_manifest(staged_bytes)
        except (zipfile.BadZipFile, KeyError) as exc:
            raise DocxWriteVerificationError(
                f"post-write verification failed: the staged artifact for {dest} "
                f"is not a valid .docx after being flushed to disk: {exc} -- "
                f"discarding it, {dest} is untouched",
                manifest={"pre_counts": pre_manifest, "post_counts": None},
            ) from exc

        mismatches = {
            key: {"expected": pre_manifest.get(key), "actual": post_counts.get(key)}
            for key in protected_keys
            if post_counts.get(key) != pre_manifest.get(key)
        }
        if mismatches:
            raise DocxWriteVerificationError(
                "post-write verification failed: the staged .docx does not "
                "preserve structural elements this write must never lose "
                f"({dest}) -- discarding the staged artifact instead of "
                f"promoting a corrupted write; {dest} is untouched",
                manifest={
                    "pre_counts": pre_manifest,
                    "post_counts": post_counts,
                    "count_mismatches": mismatches,
                },
            )

        # 6d02f343 -- structural verification passed; only NOW is it safe to
        # additionally gate on artifact provenance (per this item's own
        # instruction that provenance checks must run "only after structural
        # verification succeeds"). Still strictly before promotion below.
        _check_artifact_provenance(artifact_provenance)

        with _docx_promotion_lock(dest):
            if os.path.exists(dest):
                backup_path = dest + ".bak"
                try:
                    shutil.copy2(dest, backup_path)
                except OSError as exc:
                    _log.warning(
                        "could not write backup %r before overwriting %r: %s",
                        backup_path, dest, exc,
                    )
            os.replace(staged_path, dest)
            staged_path = None
    finally:
        if staged_path:
            try:
                os.unlink(staged_path)
            except OSError:
                pass

    return {
        "manifest_hash": manifest_hash,
        "pre_counts": pre_manifest,
        "post_counts": post_counts,
        "promoted_sha256": promoted_sha256,
    }


def _save_docx_xml(
    raw: bytes,
    root: Any,
    dest: str,
    *,
    artifact_provenance: "dict[str, Any] | None" = None,
) -> dict[str, Any]:
    """Rewrite ``word/document.xml`` with ``root`` into a copy of the .docx at ``dest``.

    Every OTHER zip entry from ``raw`` is copied through unchanged (byte-for-byte,
    preserving compression), so styles / relationships / media / content-types are
    untouched -- only the document part is replaced with the mutated tree.

    Hardened (dccc2311) to route through :func:`_write_docx_transaction`'s
    disposable-worker-artifact pipeline: the new archive is staged to a temp
    file (never written to ``dest`` directly), re-read fresh from disk, and
    its media/style/relationship counts are gated to be UNCHANGED against
    the pre-write counts -- this function only ever rewrites
    ``word/document.xml``, so those three families can never legitimately
    move here; a mismatch means the staged artifact is corrupt (e.g. a
    truncated/interrupted flush), not that an intentional edit happened.
    Equation count is intentionally NOT gated -- editing
    ``word/document.xml`` (insert_equation, update_paragraph, ...) is the
    entire point of this function's callers and is expected to change it.
    Verification failure leaves ``dest`` byte-for-byte untouched (fail
    closed) and raises :class:`DocxWriteVerificationError`. The live file is
    only ever mutated inside the single serialized promotion point for
    ``dest`` (:func:`_docx_promotion_lock`), so concurrent writers targeting
    the same document can never race their promotion.

    c034fa24 -- when ``dest`` already exists (the common in-place-overwrite case
    every docx-mutating tool routes through: update_paragraph, link_figure_caption,
    ...), the current on-disk bytes are copied to ``dest + ".bak"`` first. A single
    most-recent backup, overwritten on each subsequent save (not unbounded
    per-edit history) -- enough to recover from a bad edit or a corrupted write,
    without indefinitely growing disk usage on a document edited many times.
    Best-effort: a failure to write the backup is logged but never blocks the
    actual save -- a doc write must not fail because backup housekeeping did.

    Returns the write-transaction manifest, ``{"manifest_hash", "pre_counts",
    "post_counts", "promoted_sha256"}`` (dccc2311; ``promoted_sha256`` added
    5988a5bb) -- callers that don't need it (most existing call sites,
    including every test that pre-dates this change) simply ignore the
    return value, exactly as they did when this returned ``None``.

    ``artifact_provenance`` (6d02f343, optional, keyword-only) -- forwarded
    unchanged to :func:`_write_docx_transaction`'s own fail-closed gate (see
    its docstring): a caller-computed figure/table/equation
    provenance-binding verdict that is supplied and not cleanly
    ``all_clear`` rejects this write before promotion, same as a structural
    mismatch. ``None`` (the default) -- unchanged behavior for every
    existing caller.
    """
    new_document = _LET.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src:
        infos = src.infolist()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in infos:
                data = src.read(info.filename)
                if info.filename == _DOCX_DOCUMENT_PART:
                    data = new_document
                # Preserve each entry's original compression type.
                dst.writestr(info, data)

    return _write_docx_transaction(
        out.getvalue(),
        dest,
        pre_manifest=_docx_structural_manifest(raw),
        protected_keys=("media_count", "style_count", "relationship_count"),
        changed_parts={_DOCX_DOCUMENT_PART: new_document},
        artifact_provenance=artifact_provenance,
    )


def _restore_docx_backup(dest: str) -> bool:
    """Best-effort restore of ``dest`` from the ``dest + ".bak"`` copy
    :func:`_write_docx_transaction` writes immediately before each promotion.

    5988a5bb -- mirrors extensions/meridian-docs/meridian_docs/docs_intel.py's
    ``_restore_docx_backup`` (independent copy, not shared -- see the module
    docstring's note on WRITE-side duplication with docs_intel). Used when a
    caller-specific post-write check (:func:`_verify_paragraph_write`) finds
    that promotion succeeded structurally but the intended edit did not
    actually land -- the "destination is guaranteed untouched" guarantee
    :class:`DocxWriteVerificationError` gives only holds BEFORE promotion;
    this is the best-effort recovery for AFTER. Returns whether the restore
    succeeded; a missing or unreadable backup is reported (not raised) so the
    caller can surface it in the error it raises rather than mask the
    original verification failure.

    5988a5bb (finding 2) -- routed through the SAME stage-to-temp-in-
    ``dest``'s-own-directory + fsync + ``os.replace`` pattern every other
    write in this module uses, instead of writing straight into ``dest`` via
    ``shutil.copy2``. An interrupted restore (disk full, AV lock, permission
    revoked mid-copy) now leaves ``dest`` and the disposable temp file
    untouched -- ``os.replace`` is atomic on the same filesystem, so ``dest``
    is either the OLD content (interrupted before replace) or the FULLY
    restored backup content (replace completed); it can never be left
    truncated or partially overwritten as a direct in-place copy risked.
    """
    backup = dest + ".bak"
    if not os.path.exists(backup):
        return False
    parent = os.path.dirname(os.path.abspath(dest)) or "."
    staged_path: str | None = None
    try:
        fd, staged_path = tempfile.mkstemp(
            prefix=".meridian-docx-restore-", suffix=".tmp", dir=parent
        )
        with open(backup, "rb") as src, os.fdopen(fd, "wb") as fh:
            shutil.copyfileobj(src, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(staged_path, dest)
        staged_path = None
        return True
    except OSError:
        return False
    finally:
        if staged_path:
            try:
                os.unlink(staged_path)
            except OSError:
                pass


def _docx_file_sha256(path: str) -> str | None:
    """SHA-256 over ``path``'s raw bytes, read fresh from disk.

    5988a5bb -- the compare-and-swap fingerprint used to tell "dest still
    holds exactly what THIS writer promoted" apart from "a different writer
    has already promoted something newer" (see
    :func:`_safe_restore_after_verification_failure`). Returns ``None``
    (rather than raising) when ``path`` cannot be read -- a missing/
    unreadable file is itself informative to the caller (it can never match
    a real ``promoted_sha256``), not a reason to blow up a post-write
    verification-failure handler that is already in an error path.
    """
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _safe_restore_after_verification_failure(
    write_dest: str,
    promoted_sha256: str | None,
) -> tuple[bool, bool]:
    """Compare-and-swap-safe gate in front of :func:`_restore_docx_backup` (5988a5bb, finding 1).

    Only called once a caller-specific post-write check (e.g.
    :func:`_verify_paragraph_write`) has already found a mismatch on
    ``write_dest``. A bare, unconditional restore is unsafe: if a
    DIFFERENT writer (a different process -- see the threat-model note on
    :func:`_docx_promotion_lock`) promoted its own write to ``write_dest``
    in the window between THIS writer's own promotion and THIS writer's
    verify, the mismatch this writer is reacting to is a false positive
    caused by that other writer's legitimate, already-promoted work --
    blindly restoring from THIS writer's own ``.bak`` would silently
    destroy it.

    Re-reads ``write_dest``'s CURRENT on-disk bytes (fresh from disk) and
    compares their SHA-256 against ``promoted_sha256`` --
    ``_write_docx_transaction``'s own fingerprint of exactly what THIS
    writer promoted (returned as ``transaction["promoted_sha256"]``).

    Returns ``(safe_to_restore, restored)``:

    * ``(True, restored)`` -- ``write_dest`` still held exactly what this
      writer promoted (nobody has touched it since); a restore was
      attempted and ``restored`` reports whether it succeeded.
    * ``(False, False)`` -- ``write_dest``'s current bytes do NOT match what
      this writer promoted (a newer write from elsewhere is on disk);
      restore was deliberately NOT attempted, and ``write_dest`` is left
      exactly as that other writer left it.
    """
    current_sha256 = _docx_file_sha256(write_dest) if os.path.isfile(write_dest) else None
    safe_to_restore = (
        promoted_sha256 is not None
        and current_sha256 is not None
        and current_sha256 == promoted_sha256
    )
    if not safe_to_restore:
        return False, False
    return True, _restore_docx_backup(write_dest)


def _verify_paragraph_write(source_path: str, para_id: str, expected_text: str) -> str | None:
    """Mandatory post-write verification for a paragraph-targeted edit (5988a5bb).

    ``_write_docx_transaction``'s structural manifest (media/style/equation/
    relationship counts) only proves the archive as a WHOLE wasn't corrupted
    by the write -- it has no idea what the write was actually supposed to
    DO. A silent no-op (e.g. a serialization bug that promotes an unchanged
    ``document.xml``) or a wrong-paragraph write would sail through it
    completely undetected. This closes that gap for ``update_paragraph`` /
    ``merge_paragraph_draft`` specifically: re-reads ``source_path`` FRESH
    FROM DISK -- never the in-memory ``root`` a caller just serialized, which
    would only re-validate the build step's own intent, not what actually
    landed -- re-locates ``para_id`` by the same rule
    :func:`_find_paragraph_by_id` always uses, and confirms its plain text
    now matches ``expected_text``.

    Returns ``None`` on success, or a human-readable mismatch description on
    failure. Never raises itself and never touches the file -- the caller
    decides what to do with a failure, including whether/how to restore a
    backup.

    827b6bdc -- if ``para_id`` is now AMBIGUOUS on re-read (only reachable via
    a genuine concurrent writer: our own pre-write resolution already proved
    it unique, and this write never touches w14:paraId), that is reported as
    a mismatch string here too rather than letting
    :class:`AmbiguousParagraphIdError` escape -- this keeps the "never raises
    itself" contract intact, and routes the failure through the SAME already-
    hardened compare-and-swap safe-restore-or-conflict handling the caller
    already applies to every other verification failure, instead of adding a
    second, untested raise-based failure path alongside it.
    """
    try:
        _, root2 = _load_docx_xml(source_path)
    except (OSError, zipfile.BadZipFile, KeyError, _LET.XMLSyntaxError) as exc:
        return f"could not re-read {source_path} after writing it: {exc}"
    try:
        p2 = _find_paragraph_by_id(root2, para_id)
    except AmbiguousParagraphIdError as exc:
        return f"paragraph {para_id!r} became ambiguous in {source_path} after writing it: {exc}"
    if p2 is None:
        return f"paragraph {para_id!r} not found in {source_path} after writing it"
    actual_text = _paragraph_plain_text(p2)
    if actual_text != expected_text:
        return (
            f"paragraph {para_id!r} text mismatch after writing {source_path}: "
            f"expected {expected_text!r}, found {actual_text!r} on disk"
        )
    return None


def _mint_para_id(taken: set[str]) -> str:
    """Mint a fresh ``w14:paraId`` (Word's own 8-hex-char uppercase format),
    reserving it in *taken* immediately so repeated calls within the same
    repair batch never collide with each other, not just with ids already on
    disk (827b6bdc). Independently implemented rather than imported: mirrors
    extensions/meridian-docs/meridian_docs/docs_intel.py's ``_new_para_id``
    (same convention) but this module has no dependency on that standalone
    package, and that package correspondingly has none on this one.
    """
    while True:
        candidate = uuid.uuid4().hex[:8].upper()
        if candidate not in taken:
            taken.add(candidate)
            return candidate


def repair_duplicate_para_ids(
    source: str | bytes | bytearray,
    dest_path: str | None = None,
) -> dict[str, Any]:
    """827b6bdc -- explicit, opt-in repair for a .docx whose native
    ``w14:paraId`` values are duplicated across more than one paragraph (the
    reference regression: two paragraphs both carrying
    ``w14:paraId="6BDC5378"``).

    Read-side callers -- :func:`docparse.docs_intel.document_content_tree`,
    :func:`meridian_docs.index_docx_structure` via its vendored copy, and
    this module's own :func:`_find_paragraph_by_id` /
    :func:`_find_paragraph_with_index` -- detect a duplicate and either
    report it (read paths) or fail closed (write paths). None of them ever
    call this function: renumbering an existing document's ids is a
    deliberate, separately-invoked mutation, never a side effect of reading
    or addressing it.

    For every native id shared by 2+ paragraphs, the FIRST occurrence (in
    document order) keeps its id unchanged; every subsequent occurrence is
    assigned a fresh id via :func:`_mint_para_id` that collides with nothing
    already in the document (native or freshly minted earlier in this same
    call).

    ``dest_path`` is REQUIRED to actually write anything. Omitted (the
    default): this is a pure, read-only MAPPING -- no bytes are written
    anywhere and ``source`` (a path or raw bytes) is never touched, whether
    or not duplicates were found. Given: the repaired document is written to
    ``dest_path`` via :func:`_save_docx_xml` (the same hardened
    disposable-staged-artifact + fail-closed structural-manifest pipeline
    every other write in this module routes through) -- NEVER implicitly
    back onto a path-based ``source``; a caller that wants to replace the
    original in place must pass that same path as ``dest_path``
    deliberately. Nothing is written when there is nothing to fix, even if
    ``dest_path`` was given.

    Returns ``{duplicates_found, remapped, applied, dest_path}`` --
    ``duplicates_found`` is the count of DISTINCT ids that were duplicated;
    ``remapped`` is ``[{old_para_id, new_para_id, index, text}]`` (one entry
    per RENUMBERED occurrence, i.e. every occurrence after the first of each
    duplicated id) in document order; ``applied`` is whether a write actually
    happened. Raises whatever :func:`_load_docx_xml` / :func:`_save_docx_xml`
    raise on a malformed/unreadable/unwritable document -- this function
    performs no post-write re-verification beyond what
    :func:`_save_docx_xml` already guarantees structurally, since (unlike
    ``update_paragraph``) there is no single expected paragraph TEXT to
    re-confirm here.
    """
    raw, root = _load_docx_xml(source)
    w_p = f"{{{_DOCX_W_NS}}}p"
    w14_para_id = f"{{{_DOCX_W14_NS}}}paraId"
    occurrences_by_id: dict[str, list[tuple[int, Any]]] = {}
    for idx, p in enumerate(root.iter(w_p)):
        real_id = p.get(w14_para_id)
        if not real_id:
            continue
        occurrences_by_id.setdefault(real_id, []).append((idx, p))

    taken = set(occurrences_by_id.keys())
    remapped: list[dict[str, Any]] = []
    duplicates_found = 0
    for old_id, occurrences in occurrences_by_id.items():
        if len(occurrences) < 2:
            continue
        duplicates_found += 1
        # The first occurrence keeps old_id; every later one is renumbered.
        for idx, p in occurrences[1:]:
            new_id = _mint_para_id(taken)
            p.set(w14_para_id, new_id)
            remapped.append({
                "old_para_id": old_id,
                "new_para_id": new_id,
                "index": idx,
                "text": _paragraph_plain_text(p)[:200],
            })
    remapped.sort(key=lambda r: r["index"])

    applied = False
    if remapped and dest_path:
        _save_docx_xml(raw, root, dest_path)
        applied = True

    return {
        "duplicates_found": duplicates_found,
        "remapped": remapped,
        "applied": applied,
        "dest_path": dest_path if applied else None,
    }


def _resolve_docx_draft_dest(
    source_path: str,
    draft_output_path: str | None,
    wave_run_id: str | None,
    session_id: str | None,
) -> str | None:
    """Opt-in wave-scoped draft-mode validation for ``update_paragraph`` (5988a5bb).

    Mirrors extensions/meridian-docs/meridian_docs/docs_intel.py's
    ``_resolve_draft_dest`` (fe989980) -- independent copy, not shared (see
    the module docstring's note on WRITE-side duplication with docs_intel).
    ``draft_output_path`` and ``wave_run_id`` must be supplied together or
    not at all; omitting both is the legacy path (returns ``None``, meaning
    "write ``source_path`` directly", byte-identical to pre-5988a5bb
    behavior).

    Unlike the sibling extension (stdlib-only, no DB access), doc_store.py
    registers the isolated draft against ``meridian.db.docx_merge``'s real
    manifest/anchor tables, which are keyed on a real session identity --
    a draft with no owner session could never be claimed, declared, or later
    merged, so ``session_id`` is ALSO required whenever draft mode is
    requested (a doc_store.py-specific tightening of the sibling's contract,
    not present there because it has nothing to key a session-scoped claim
    on).

    Raises ``ValueError`` on any validation failure -- update_paragraph's own
    contract already raises ``ValueError`` for every other precondition
    failure, so a caller-visible ``{"error": ...}`` dict here (the sibling's
    convention, needed only because it has no exception-based contract to
    join) would be an inconsistent surface for this module's actual callers.
    """
    if bool(wave_run_id) != bool(draft_output_path):
        raise ValueError(
            "wave_run_id and draft_output_path must be provided together -- "
            "wave-scoped drafting requires both an isolated draft target and "
            "the wave identifier that scopes its merge manifest"
        )
    if not draft_output_path:
        return None
    if not session_id:
        raise ValueError(
            "session_id is required together with draft_output_path/"
            "wave_run_id -- the isolated draft is registered against a real "
            "docx_merge manifest keyed on session identity"
        )
    dest = draft_output_path.strip()
    if not dest:
        raise ValueError("draft_output_path must be a non-empty path")
    if os.path.normcase(os.path.abspath(dest)) == os.path.normcase(os.path.abspath(source_path)):
        raise ValueError(
            "draft_output_path must differ from the document's source path -- "
            "a wave-scoped draft must be an isolated artifact, never the "
            "canonical file itself"
        )
    return dest


def _insert_omath_at_position(
    paragraph: Any, omath_el: Any, position: str
) -> tuple[str | None, str | None]:
    """Insert a parsed ``<m:oMath>`` element relative to ``paragraph`` in place.

    * ``append``  -- append the ``<m:oMath>`` as the last child *inside* the
      paragraph (an inline equation trailing the paragraph's runs).
    * ``before`` / ``after`` -- wrap the ``<m:oMath>`` in its OWN new ``<w:p>``
      (a display equation on its own line) and splice that paragraph immediately
      before / after ``paragraph`` in the parent (body) element.

    ``append`` is the default because a bare ``<m:oMath>`` is only valid as a
    child of a ``<w:p>`` (or an ``<m:oMathPara>``); splicing it as a body sibling
    directly would produce a malformed document, hence the ``<w:p>`` wrapper for
    before/after.
    """
    if position == "append":
        paragraph.append(omath_el)
        return None, None
    # before / after -- build a standalone paragraph carrying the equation.
    new_p = _LET.Element(f"{{{_DOCX_W_NS}}}p")
    root = paragraph.getroottree().getroot() if hasattr(paragraph, "getroottree") else None
    w14_para_id = f"{{{_DOCX_W14_NS}}}paraId"
    w14_text_id = f"{{{_DOCX_W14_NS}}}textId"
    taken_para_ids = {
        value for p in (root.iter(f"{{{_DOCX_W_NS}}}p") if root is not None else ())
        if (value := p.get(w14_para_id))
    }
    taken_text_ids = {
        value for p in (root.iter(f"{{{_DOCX_W_NS}}}p") if root is not None else ())
        if (value := p.get(w14_text_id))
    }
    para_id = _mint_para_id(taken_para_ids)
    text_id = _mint_para_id(taken_text_ids)
    new_p.set(w14_para_id, para_id)
    new_p.set(w14_text_id, text_id)
    p_pr = _LET.SubElement(new_p, f"{{{_DOCX_W_NS}}}pPr")
    _LET.SubElement(
        p_pr,
        f"{{{_DOCX_W_NS}}}jc",
        {f"{{{_DOCX_W_NS}}}val": "center"},
    )
    new_p.append(omath_el)
    parent = paragraph.getparent()
    if parent is None:
        # No parent to splice into (a detached paragraph) -- degrade to an inline
        # append so the equation is never silently dropped.
        paragraph.append(omath_el)
        return None, None
    index = list(parent).index(paragraph)
    if position == "before":
        parent.insert(index, new_p)
    else:  # after
        parent.insert(index + 1, new_p)
    return para_id, text_id


def _paragraph_plain_text(p: Any) -> str:
    """Concatenate every ``<w:t>`` run text inside a paragraph (read-side parity)."""
    w_t = f"{{{_DOCX_W_NS}}}t"
    return "".join(t.text or "" for t in p.iter(w_t))


# The subset of run properties (``<w:rPr>``) a caller may set per run. Each maps a
# friendly key on a run dict to its OOXML toggle element local-name; a truthy
# value emits ``<w:{tag}/>``. Deliberately small (bold/italic/underline) -- the
# common inline emphasis set -- everything else on the original run is dropped when
# runs are replaced, which is the documented contract.
_RUN_TOGGLE_PROPS: tuple[tuple[str, str], ...] = (
    ("bold", "b"),
    ("italic", "i"),
    ("underline", "u"),
)


def _normalize_runs(new_text_or_runs: str | list[Any]) -> list[dict[str, Any]]:
    """Coerce the ``new_text_or_runs`` argument into a list of run dicts.

    Accepts EITHER a plain string (one run, no formatting) OR a list of runs,
    where each run is either a bare string or a ``{text, bold?, italic?,
    underline?}`` dict. Returns a normalized ``[{text, bold, italic, underline}]``
    list. A ``None``/empty input yields a single empty-text run so the paragraph
    is emptied rather than left untouched.
    """
    if new_text_or_runs is None:
        return [{"text": ""}]
    if isinstance(new_text_or_runs, str):
        return [{"text": new_text_or_runs}]
    runs: list[dict[str, Any]] = []
    for item in new_text_or_runs:
        if isinstance(item, str):
            runs.append({"text": item})
        elif isinstance(item, dict):
            run = {"text": str(item.get("text") or "")}
            for friendly, _tag in _RUN_TOGGLE_PROPS:
                if item.get(friendly):
                    run[friendly] = True
            runs.append(run)
        else:
            run = {"text": str(item)}
            runs.append(run)
    return runs or [{"text": ""}]


def _set_paragraph_runs(p: Any, runs: list[dict[str, Any]]) -> None:
    """Replace every ``<w:r>`` run in paragraph ``p`` with ``runs`` (in place).

    The paragraph's own properties (``<w:pPr>`` -- its style, numbering, etc.) are
    PRESERVED: only run children are removed and rebuilt. Each new run is a
    ``<w:r>`` carrying an optional ``<w:rPr>`` toggle set (bold/italic/underline)
    and a single ``<w:t xml:space="preserve">`` so leading/trailing spaces survive
    Word's whitespace collapsing.
    """
    w = _DOCX_W_NS
    w_r = f"{{{w}}}r"
    w_ppr = f"{{{w}}}pPr"
    # Remove existing runs (and any bookmark/hyperlink run wrappers' bare runs);
    # keep pPr and everything that is not a top-level run.
    for child in list(p):
        if child.tag == w_r:
            p.remove(child)
    # Rebuild: runs go AFTER pPr (OOXML requires pPr first when present).
    ppr = p.find(w_ppr)
    insert_at = list(p).index(ppr) + 1 if ppr is not None else 0
    for offset, run in enumerate(runs):
        r_el = _LET.Element(w_r)
        toggles = [(tag, run.get(friendly)) for friendly, tag in _RUN_TOGGLE_PROPS]
        if any(val for _tag, val in toggles):
            rpr = _LET.SubElement(r_el, f"{{{w}}}rPr")
            for tag, val in toggles:
                if val:
                    _LET.SubElement(rpr, f"{{{w}}}{tag}")
        t_el = _LET.SubElement(r_el, f"{{{w}}}t")
        # xml:space=preserve so runs with leading/trailing spaces round-trip.
        t_el.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        t_el.text = run.get("text") or ""
        p.insert(insert_at + offset, r_el)


# Delimiter pairs normalize_latex strips when they wrap the WHOLE string (an
# author pasting "$x^2$" or "\(x^2\)" should dedup-match a bare "x^2").
_MATH_DELIM_PAIRS: tuple[tuple[str, str], ...] = (
    ("$$", "$$"), ("\\[", "\\]"), ("\\(", "\\)"), ("$", "$"),
)


def normalize_latex(latex: str | None) -> str:
    """Canonicalize a LaTeX source string for the fuzzy-dedup key (06df6ab3).

    Strips whole-string math delimiters (``$...$``, ``$$...$$``, ``\\(...\\)``,
    ``\\[...\\]``) and removes ALL whitespace — LaTeX whitespace is
    semantically insignificant (``E=mc^2`` and ``E = m c^2`` are the same
    equation), and a difflib char-ratio comparison is otherwise dominated by
    incidental spacing differences rather than real content differences.
    Deterministic and pure — never raises, ``None``/blank input yields ``""``.
    """
    s = (latex or "").strip()
    if not s:
        return ""
    for open_d, close_d in _MATH_DELIM_PAIRS:
        if s.startswith(open_d) and s.endswith(close_d) and len(s) >= len(open_d) + len(close_d):
            s = s[len(open_d): len(s) - len(close_d)].strip()
            break
    return re.sub(r"\s+", "", s)


def _equation_similarity(a: str, b: str) -> float:
    """difflib fuzzy-match ratio (same approach already used for note-dedup in
    ``mcp/handler.py``'s ``add_note`` near-duplicate warning, 6e4e2371)."""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def normalize_caption(caption: str | None) -> str:
    """Canonicalize a figure caption for the fuzzy-dedup key (c623e648).

    The figure analogue of :func:`normalize_latex`: lower-cases, strips a
    leading auto-numbered label (``Figure 3:`` / ``Fig. 12 -`` / ``Figure 3.``),
    collapses all runs of whitespace to a single space, and drops surrounding
    whitespace. Caption prose IS word-order sensitive (unlike LaTeX), so -- unlike
    :func:`normalize_latex` -- whitespace is *collapsed*, not removed entirely, to
    keep word boundaries for a meaningful difflib ratio. Deterministic and
    pure -- never raises, ``None``/blank input yields ``""``.
    """
    s = (caption or "").strip().lower()
    if not s:
        return ""
    # Strip a leading "figure N" / "fig. N" auto-number label plus its trailing
    # separator (``:``/``.``/``-``/em-dash), if present.
    s = re.sub(r"^(?:figure|fig\.?)\s*\d+\s*[:.\-–—]?\s*", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _figure_similarity(a: str, b: str) -> float:
    """difflib fuzzy-match ratio between two normalized captions (c623e648).

    Same difflib approach as :func:`_equation_similarity`; kept as its own
    function so figures and equations can diverge later without entangling."""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def normalize_table_caption(caption: str | None) -> str:
    """Canonicalize a table caption for the fuzzy-dedup key (2622182d).

    The table analogue of :func:`normalize_caption`: lower-cases, strips a
    leading auto-numbered label (``Table 3:`` / ``Tbl. 12 -`` / ``Table 3.``),
    collapses all runs of whitespace to a single space, and drops surrounding
    whitespace. Deterministic and pure -- never raises, ``None``/blank input
    yields ``""``.
    """
    s = (caption or "").strip().lower()
    if not s:
        return ""
    # Strip a leading "table N" / "tbl. N" auto-number label plus its trailing
    # separator (``:``/``.``/``-``/em-dash), if present.
    s = re.sub(r"^(?:table|tbl\.?)\s*\d+\s*[:.\-–—]?\s*", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _table_similarity(a: str, b: str) -> float:
    """difflib fuzzy-match ratio between two normalized table captions (2622182d)."""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

class DocStructureStore:
    """Persistent, backend-agnostic store for parsed document structure.

    Constructed with an already-open connection from
    :func:`meridian.db.init_db` (aiosqlite *or* the pg adapter). Owns its two
    tables via :meth:`ensure_schema`. All writes call ``commit()``
    unconditionally (real on aiosqlite, a no-op on the autocommitting pg
    adapter).
    """

    def __init__(self, db: Any) -> None:
        self._db = db

    # -- schema --------------------------------------------------------------

    async def ensure_schema(self) -> None:
        """Create the store's tables + indexes if absent (idempotent).

        The base ``CREATE TABLE IF NOT EXISTS`` literals only take effect on a
        FRESH database — an already-existing ``doc_documents`` table is left
        untouched by them. So after applying the base schema we run additive
        ``ALTER TABLE`` migrations for columns introduced after this store first
        shipped (:meth:`_migrate_add_columns`), guarded on the column's presence
        so they are safe to re-run on both fresh and already-populated DBs.
        """
        for stmt in _SCHEMA_STATEMENTS:
            await self._db.execute(stmt)
        await self._db.commit()
        await self._migrate_add_columns()

    async def _column_exists(self, table: str, column: str) -> bool:
        """True if ``column`` exists on ``table`` in this DB (dual-backend).

        ``PRAGMA table_info`` runs natively on aiosqlite; the psycopg3 adapter
        intercepts it and answers from ``information_schema.columns`` — so this
        single query is correct on BOTH backends (mirrors ``db.migrations``'
        ``_column_exists``). Rows come back as an aiosqlite ``Row`` (``row[1]``
        is the name) or a pg dict (``row['name']``); handle both."""
        async with self._db.execute(f"PRAGMA table_info({table})") as cur:
            rows = await cur.fetchall()
        names = {
            (r["name"] if isinstance(r, dict) else r[1])
            for r in rows
        }
        return column in names

    async def _migrate_add_columns(self) -> None:
        """Idempotent additive-column migrations for already-existing DBs.

        Because ``doc_documents`` is created with ``CREATE TABLE IF NOT EXISTS``,
        editing the CREATE literal does NOT add a new column to a database that
        already has the table. Each column introduced after the store's first
        release is added here with a presence-guarded ``ALTER TABLE ... ADD
        COLUMN`` — a plain additive alter (nullable-or-defaulted, no inline index,
        no CHECK) that is safe on both SQLite and Postgres and a no-op once the
        column is present. New rows already carry the column from the CREATE
        literal; existing rows get the DEFAULT.
        """
        # 14015718 — link_status. DEFAULT 'live' so every pre-existing row (stored
        # before this column existed) reads back as a live, writable link, exactly
        # preserving the store's prior only-implemented behaviour.
        if not await self._column_exists("doc_documents", "link_status"):
            await self._db.execute(
                "ALTER TABLE doc_documents "
                "ADD COLUMN link_status TEXT NOT NULL DEFAULT 'live'"
            )
            await self._db.commit()
        # 0ff8b982 — caption_element_id: durable linkage from a doc_figures row to
        # the doc_elements element (kind='figure' paragraph carrying the SEQ field)
        # that IS that figure's caption, by stable element id rather than paragraph
        # proximity. DEFAULT NULL so every pre-existing figure row (stored before
        # this column) reads back as unlinked (not yet confirmed), which is honest
        # — they can be back-filled via the link_figure_caption MCP tool.
        if not await self._column_exists("doc_figures", "caption_element_id"):
            await self._db.execute(
                "ALTER TABLE doc_figures ADD COLUMN caption_element_id TEXT"
            )
            await self._db.commit()
        # 42d398a5 — caption_element_id: the doc_tables analogue of doc_figures'
        # column above -- durable linkage from a doc_tables row to the
        # doc_elements element (kind='table' paragraph carrying the SEQ field)
        # that IS that table's caption, by stable element id rather than
        # paragraph proximity. DEFAULT NULL so every pre-existing table row
        # (stored before this column) reads back as unlinked (not yet
        # confirmed), which is honest -- they can be back-filled via the
        # link_table_caption MCP tool.
        if not await self._column_exists("doc_tables", "caption_element_id"):
            await self._db.execute(
                "ALTER TABLE doc_tables ADD COLUMN caption_element_id TEXT"
            )
            await self._db.commit()

    # -- internals -----------------------------------------------------------

    async def _fetch_document_row(
        self, project_id: str, source: str | None
    ) -> dict[str, Any] | None:
        """Return the stored doc dict for (project_id, source), or None.

        ``source`` participates in the identity: a resolvable source matches the
        row with that exact source; a ``None`` source matches only rows stored
        without a source.
        """
        if source is not None:
            async with self._db.execute(
                "SELECT * FROM doc_documents "
                "WHERE project_id = ? AND source = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (project_id, source),
            ) as cur:
                row = await cur.fetchone()
        else:
            async with self._db.execute(
                "SELECT * FROM doc_documents "
                "WHERE project_id = ? AND source IS NULL "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (project_id,),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_dict(row, _DOC_COLUMNS)

    async def _delete_elements(self, document_id: str) -> None:
        await self._db.execute(
            "DELETE FROM doc_elements WHERE document_id = ?", (document_id,)
        )

    async def _delete_edges_for_document(self, document_id: str) -> None:
        """Delete every edge whose source element belongs to ``document_id``.

        Edges reference their source only by ``source_element_id`` (there is no
        ``document_id`` column on ``doc_edges``), so a document's edges are the
        ones whose source element is (or was) one of its elements. We delete via a
        subquery over ``doc_elements`` *before* the elements themselves are
        removed on an upsert, so no stale/orphan edges survive a re-store.
        """
        await self._db.execute(
            "DELETE FROM doc_edges WHERE source_element_id IN "
            "(SELECT id FROM doc_elements WHERE document_id = ?)",
            (document_id,),
        )

    async def _materialize_citation_edges(
        self, project_id: str, prepared: list[dict[str, Any]], now: str
    ) -> None:
        """Derive intra-document ``cites`` edges from an element list (in-process).

        For each ``kind='citation'`` element, look for a ``kind='bibliography'``
        element in the SAME element set whose ``ref`` matches the citation's
        ``ref`` (case-insensitive exact citation-key match). On a match, insert a
        resolved ``doc_edges`` row (``edge_kind='cites'``, ``target_kind='bibentry'``,
        ``target_element_id`` = the bib element's id, ``resolved_at`` = ``now``).
        A citation with NO matching bib entry is a DANGLING marker: the citation
        element is kept by the caller, but NO edge is written (honest — we never
        fabricate a target). ``prepared`` elements already carry assigned ``id``s.
        """
        # Index bibliography elements by normalised ref (lowercased, trimmed key).
        bib_by_ref: dict[str, str] = {}
        for el in prepared:
            if el.get("kind") == "bibliography":
                ref = el.get("ref")
                if isinstance(ref, str) and ref.strip():
                    # First writer wins on a duplicate key (stable, deterministic).
                    bib_by_ref.setdefault(ref.strip().lower(), el["id"])

        for el in prepared:
            if el.get("kind") != "citation":
                continue
            ref = el.get("ref")
            key = ref.strip().lower() if isinstance(ref, str) and ref.strip() else None
            target_element_id = bib_by_ref.get(key) if key else None
            if target_element_id is None:
                # Dangling citation — keep the element, write no edge.
                continue
            await self._db.execute(
                "INSERT INTO doc_edges "
                "(id, project_id, source_element_id, edge_kind, target_kind, "
                "target_ref, target_element_id, target_document_id, resolved_at, "
                "created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex,
                    project_id,
                    el["id"],
                    "cites",
                    "bibentry",
                    ref,
                    target_element_id,
                    None,
                    now,
                    now,
                ),
            )

    # -- write ---------------------------------------------------------------

    async def put_document(
        self,
        project_id: str,
        doc_type: str,
        elements: list[dict[str, Any]],
        *,
        source: str | None = None,
        title: str | None = None,
        content_hash: str | None = None,
        link_status: str | None = None,
    ) -> dict[str, Any]:
        """Store (or replace) a document's structure and return its doc dict.

        Upsert semantics: when ``source`` is resolvable and a document already
        exists for ``(project_id, source)``, its old elements + row are deleted
        and reinserted in place (stable ``id``, refreshed ``updated_at``). With
        no ``source`` it always inserts a fresh document — two anonymous stores
        never silently merge (mirrors ``db.ingest_document``).

        ``elements`` is an ordered list of dicts
        ``{ordinal, level, kind, text, ref, parent_ordinal|parent_id}``. Parent
        edges are resolved: ``parent_id`` (an explicit stored id) wins; else
        ``parent_ordinal`` is mapped to the assigned id of the element at that
        ordinal.

        ``link_status`` (14015718) records the lifecycle of the link to the
        physical source: ``live`` (default — writable, kept in sync),
        ``deprecated`` (file gone/superseded; kept as history), or
        ``independent`` (a standalone snapshot never meant to be written back).
        An unknown/omitted value defaults to ``live``. On an upsert-by-source,
        an explicit ``link_status`` overrides the stored one; omitting it (None)
        PRESERVES the existing row's status rather than silently reverting a
        deprecated/independent doc back to live.

        NB: the delete-then-reinsert upsert is NOT wrapped in a single
        transaction — the shared adapter runs each ``execute`` on its own pooled
        connection under autocommit (matching the ~140 existing call sites), so a
        crash mid-upsert can leave a torn write. This is acceptable because
        structure persistence is best-effort (all call sites guard it and never
        surface a failure) and self-healing: the next ``ingest``/parse of the
        same source re-derives and re-upserts the full structure.
        """
        src = source.strip() if isinstance(source, str) and source.strip() else None
        now = _now_iso()
        ordered = list(elements or [])
        ch = content_hash if content_hash is not None else compute_content_hash(ordered)

        existing = await self._fetch_document_row(project_id, src) if src else None
        if existing is not None:
            doc_id = existing["id"]
            created_at = existing.get("created_at") or now
            # Preserve the existing link_status when the caller omits one (None),
            # so re-storing a deprecated/independent doc's structure doesn't
            # silently revert it to 'live'. An explicit value always wins.
            if link_status is None:
                ls = _normalize_link_status(existing.get("link_status"))
            else:
                ls = _normalize_link_status(link_status)
            # Drop old edges FIRST (subquery over the still-present elements), then
            # the elements + doc row, so an upsert leaves no stale/orphan edges.
            await self._delete_edges_for_document(doc_id)
            await self._delete_elements(doc_id)
            await self._db.execute(
                "DELETE FROM doc_documents WHERE id = ?", (doc_id,)
            )
        else:
            doc_id = uuid.uuid4().hex
            created_at = now
            ls = _normalize_link_status(link_status)

        # Assign element ids first so parent_ordinal edges can be resolved to ids.
        ordinal_to_id: dict[int, str] = {}
        prepared: list[dict[str, Any]] = []
        for el in ordered:
            el_id = el.get("id") or uuid.uuid4().hex
            ordinal = el.get("ordinal")
            if isinstance(ordinal, int):
                ordinal_to_id[ordinal] = el_id
            prepared.append({**el, "id": el_id})

        await self._db.execute(
            "INSERT INTO doc_documents "
            "(id, project_id, source, doc_type, title, content_hash, "
            "element_count, link_status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                doc_id, project_id, src, doc_type, title, ch,
                len(prepared), ls, created_at, now,
            ),
        )

        for el in prepared:
            parent_id = el.get("parent_id")
            if parent_id is None:
                parent_ordinal = el.get("parent_ordinal")
                if isinstance(parent_ordinal, int):
                    parent_id = ordinal_to_id.get(parent_ordinal)
            level = el.get("level")
            await self._db.execute(
                "INSERT INTO doc_elements "
                "(id, document_id, parent_id, ordinal, level, kind, text, ref) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    el["id"], doc_id, parent_id,
                    int(el.get("ordinal") or 0),
                    int(level) if isinstance(level, (int, float)) else None,
                    el.get("kind") or "element",
                    el.get("text"),
                    el.get("ref"),
                ),
            )

        # Materialise intra-document citation edges as a side-effect of storing the
        # elements (self-healing, same best-effort contract). On a resolvable
        # (source) upsert the old edges were already dropped above; on an anonymous
        # store the fresh doc_id guarantees no pre-existing edges to collide with.
        await self._materialize_citation_edges(project_id, prepared, now)

        await self._db.commit()
        stored = await self._fetch_document_row(project_id, src) if src else None
        if stored is not None:
            return stored
        # Anonymous (no source) — fetch directly by id.
        async with self._db.execute(
            "SELECT * FROM doc_documents WHERE id = ?", (doc_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_dict(row, _DOC_COLUMNS) or {
            "id": doc_id, "project_id": project_id, "source": src,
            "doc_type": doc_type, "title": title, "content_hash": ch,
            "element_count": len(prepared), "link_status": ls,
            "created_at": created_at, "updated_at": now,
        }

    # -- cross-document resolution (opt-in, network) -------------------------

    async def _citation_elements_needing_zotero(
        self, project_id: str
    ) -> list[dict[str, Any]]:
        """Return this project's ``kind='citation'`` elements with a non-blank
        ``ref`` that do NOT already carry a resolved ``zotero_item`` edge.

        The idempotency filter: a citation element is skipped once any
        ``doc_edges`` row exists with that element as source and
        ``target_kind='zotero_item'``. Joined in SQL (via a NOT-EXISTS subquery)
        so re-running :meth:`resolve_zotero_edges` only fills gaps and never
        re-hits the network for an already-linked marker.
        """
        sql = (
            "SELECT e.id AS id, e.document_id AS document_id, e.ref AS ref, "
            "e.text AS text "
            "FROM doc_elements e "
            "JOIN doc_documents d ON d.id = e.document_id "
            "WHERE d.project_id = ? AND e.kind = 'citation' "
            "AND e.ref IS NOT NULL AND TRIM(e.ref) <> '' "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM doc_edges z "
            "  WHERE z.source_element_id = e.id "
            "  AND z.target_kind = 'zotero_item'"
            ") "
            "ORDER BY e.document_id ASC, e.ordinal ASC, e.id ASC"
        )
        async with self._db.execute(sql, (project_id,)) as cur:
            rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "id": _row_get(r, "id"),
                    "document_id": _row_get(r, "document_id"),
                    "ref": _row_get(r, "ref"),
                    "text": _row_get(r, "text"),
                }
            )
        return out

    async def _find_document_for_doi(
        self, project_id: str, doi: str, title: str | None
    ) -> str | None:
        """Best-effort: id of a same-project ``doc_documents`` row for a cited DOI.

        The further cross-document hop — if the paper a marker cites has ALSO
        been ingested into this project, link the edge straight to that stored
        document. Match strategy (simple, best-effort, first hit wins):

        1. a ``doc_documents.source`` that contains the DOI (case-insensitive
           substring — a source URL/path such as ``https://doi.org/10.x`` or a
           filename carrying the DOI), then
        2. a ``doc_documents.title`` equal to the Zotero item's title
           (case-insensitive), as a fallback when the DOI is not in the source.

        Returns the matching document id, or ``None``. Never raises upward (the
        caller guards per-element regardless).
        """
        doi_norm = doi.strip().lower()
        if doi_norm:
            # fefb596a — coarse SQL candidates (source contains the DOI substring),
            # then a BOUNDED check in Python so a DOI that is a *prefix* of a longer
            # DOI present in some source (e.g. '10.1/knuth' vs '10.1/knuth-extended')
            # cannot mis-link target_document_id. DOIs continue with a broad charset,
            # so a real whole-DOI boundary is end-of-string or a non-DOI-continuation
            # char (see _doi_bounded_in). First bounded match by created_at wins.
            like = f"%{doi_norm}%"
            async with self._db.execute(
                "SELECT id, source FROM doc_documents "
                "WHERE project_id = ? AND source IS NOT NULL "
                "AND LOWER(source) LIKE ? "
                "ORDER BY created_at ASC, id ASC LIMIT 25",
                (project_id, like),
            ) as cur:
                rows = await cur.fetchall()
            for r in rows or []:
                src = _row_get(r, "source")
                if isinstance(src, str) and _doi_bounded_in(src.lower(), doi_norm):
                    doc_id = _row_get(r, "id")
                    if doc_id:
                        return doc_id
        title_norm = title.strip().lower() if isinstance(title, str) and title.strip() else None
        if title_norm:
            async with self._db.execute(
                "SELECT id FROM doc_documents "
                "WHERE project_id = ? AND title IS NOT NULL "
                "AND LOWER(title) = ? "
                "ORDER BY created_at ASC, id ASC LIMIT 1",
                (project_id, title_norm),
            ) as cur:
                row = await cur.fetchone()
            doc_id = _row_get(row, "id")
            if doc_id:
                return doc_id
        return None

    async def resolve_zotero_edges(
        self,
        project_id: str,
        *,
        resolver: Callable[..., Awaitable[dict[str, Any] | None]] = resolve_citation_ref,
        max_items: int | None = None,
    ) -> dict[str, Any]:
        """Resolve citation markers to Zotero items and materialise cross-doc edges.

        A **separate, opt-in pass** — deliberately NOT part of the ``put_document``
        / ingest hot path, because ``resolver`` makes network calls to Zotero's
        local API. Walks every ``kind='citation'`` element in ``project_id`` that
        does not yet have a ``target_kind='zotero_item'`` edge
        (:meth:`_citation_elements_needing_zotero`) and, for each, calls
        ``resolver(element.ref)``.

        On a hit (``resolver`` returns a normalized item dict), inserts a resolved
        ``doc_edges`` row: ``edge_kind='cites'``, ``target_kind='zotero_item'``,
        ``target_ref`` = the item's DOI if present else ``"zotero:"+zotero_key``,
        ``target_element_id=NULL``, ``resolved_at=now``. If the resolved DOI also
        matches a same-project ``doc_documents`` row (the cited paper is itself
        ingested — :meth:`_find_document_for_doi`), the edge's
        ``target_document_id`` is set to that document (the further cross-document
        hop). A ``resolver`` miss (``None``) writes NO edge — the marker stays
        unresolved and a later run retries it.

        **Idempotent:** citation elements that already carry a ``zotero_item``
        edge are filtered out up front, so re-running only fills gaps and never
        duplicates. **Guarded per element:** one resolver/DB failure is logged and
        skipped; it does not abort the whole pass.

        ``resolver`` is injectable (tests pass a stub; production uses
        :func:`meridian.zotero_client.resolve_citation_ref`). ``max_items`` caps
        how many *unresolved* markers are attempted this pass (``None`` = all).

        Returns ``{"resolved", "unresolved", "cross_doc_linked"}`` counts.
        """
        pending = await self._citation_elements_needing_zotero(project_id)
        if isinstance(max_items, int) and max_items >= 0:
            pending = pending[:max_items]

        resolved = 0
        unresolved = 0
        cross_doc_linked = 0

        for el in pending:
            ref = el.get("ref")
            try:
                item = await resolver(ref)
            except Exception:  # noqa: BLE001 — one bad resolve must not abort the pass
                _log.debug("zotero resolve failed for ref=%r", ref, exc_info=True)
                item = None
            if not isinstance(item, dict) or not item.get("zotero_key"):
                unresolved += 1
                continue
            try:
                doi = item.get("doi")
                doi = doi if isinstance(doi, str) and doi.strip() else None
                zotero_key = item.get("zotero_key")
                target_ref = doi if doi else f"zotero:{zotero_key}"
                target_document_id: str | None = None
                if doi:
                    target_document_id = await self._find_document_for_doi(
                        project_id, doi, item.get("title")
                    )
                now = _now_iso()
                await self._db.execute(
                    "INSERT INTO doc_edges "
                    "(id, project_id, source_element_id, edge_kind, target_kind, "
                    "target_ref, target_element_id, target_document_id, "
                    "resolved_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        uuid.uuid4().hex,
                        project_id,
                        el["id"],
                        "cites",
                        "zotero_item",
                        target_ref,
                        None,
                        target_document_id,
                        now,
                        now,
                    ),
                )
                await self._db.commit()
                resolved += 1
                if target_document_id is not None:
                    cross_doc_linked += 1
            except Exception:  # noqa: BLE001 — a write failure skips this marker only
                _log.debug(
                    "zotero edge write failed for element=%s", el.get("id"),
                    exc_info=True,
                )
                unresolved += 1

        return {
            "resolved": resolved,
            "unresolved": unresolved,
            "cross_doc_linked": cross_doc_linked,
        }

    # -- read ----------------------------------------------------------------

    async def get_document(
        self, project_id: str, source: str
    ) -> dict[str, Any] | None:
        """Return the stored document header for (project_id, source), or None."""
        src = source.strip() if isinstance(source, str) else None
        if not src:
            return None
        return await self._fetch_document_row(project_id, src)

    async def get_structure(
        self, project_id: str, source: str
    ) -> dict[str, Any] | None:
        """Return ``{document, elements:[...]}`` for (project_id, source), or None.

        Elements are ordered by ``ordinal`` and carry their stored ``parent_id``
        edge (the real structural edge, not a computed-on-read heading trick).
        """
        doc = await self.get_document(project_id, source)
        if doc is None:
            return None
        async with self._db.execute(
            "SELECT * FROM doc_elements WHERE document_id = ? ORDER BY ordinal ASC",
            (doc["id"],),
        ) as cur:
            rows = await cur.fetchall()
        elements = [_row_to_dict(r, _ELEMENT_COLUMNS) for r in rows]
        return {"document": doc, "elements": elements}

    async def get_edges(
        self,
        project_id: str,
        *,
        source_element_id: str | None = None,
        document_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return citation/reference edges for a project (fefb596a).

        Filters (all optional, ANDed):

        * ``source_element_id`` — only edges originating from that element.
        * ``document_id`` — only edges whose source element belongs to that
          document (joined via ``doc_elements`` since ``doc_edges`` stores the
          source only by element id).

        Rows are ordered by ``created_at`` then ``id`` for a stable result and
        materialised into plain dicts over :data:`_EDGE_COLUMNS`.
        """
        clauses = ["project_id = ?"]
        params: list[Any] = [project_id]
        if source_element_id is not None:
            clauses.append("source_element_id = ?")
            params.append(source_element_id)
        if document_id is not None:
            clauses.append(
                "source_element_id IN "
                "(SELECT id FROM doc_elements WHERE document_id = ?)"
            )
            params.append(document_id)
        sql = (
            "SELECT * FROM doc_edges WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at ASC, id ASC"
        )
        async with self._db.execute(sql, tuple(params)) as cur:
            rows = await cur.fetchall()
        return [_row_to_dict(r, _EDGE_COLUMNS) for r in rows]

    async def get_citation_graph(
        self,
        project_id: str,
        *,
        source: str | None = None,
        document_id: str | None = None,
    ) -> dict[str, Any]:
        """Return the citation markers of a project and each marker's edges.

        The read side of the whole citation graph (fefb596a): each
        ``kind='citation'`` element is a *marker*, carrying BOTH its intra-document
        ``bibentry`` edges (materialised on store by
        :meth:`_materialize_citation_edges`) AND its cross-document ``zotero_item``
        edges (materialised by :meth:`resolve_zotero_edges`). One query per layer,
        joined in-process by ``source_element_id`` so the caller gets a single
        ``{markers:[{...edges...}]}`` shape.

        Optional filters (ANDed): ``document_id`` restricts to one stored
        document; ``source`` resolves to that document first (returns an empty
        graph if the source is unknown). With neither, every citation marker in
        the project is returned.

        Shape::

            {"markers": [
                {"element_id", "document_id", "ordinal", "ref", "text",
                 "edges": [<edge dict>, ...]},
                ...
            ]}

        Markers are ordered by ``(document_id, ordinal)``; each marker's ``edges``
        are ordered ``bibentry`` before ``zotero_item`` then by ``created_at``.
        """
        # Resolve a source filter to its document id (empty graph if unknown).
        resolved_document_id = document_id
        if source is not None:
            doc = await self.get_document(project_id, source)
            if doc is None:
                return {"markers": []}
            resolved_document_id = doc["id"]

        clauses = ["d.project_id = ?", "e.kind = 'citation'"]
        params: list[Any] = [project_id]
        if resolved_document_id is not None:
            clauses.append("e.document_id = ?")
            params.append(resolved_document_id)
        sql = (
            "SELECT e.id AS id, e.document_id AS document_id, e.ordinal AS ordinal, "
            "e.ref AS ref, e.text AS text "
            "FROM doc_elements e "
            "JOIN doc_documents d ON d.id = e.document_id "
            "WHERE " + " AND ".join(clauses) + " "
            "ORDER BY e.document_id ASC, e.ordinal ASC, e.id ASC"
        )
        async with self._db.execute(sql, tuple(params)) as cur:
            marker_rows = await cur.fetchall()

        markers: list[dict[str, Any]] = []
        edges_by_source: dict[str, list[dict[str, Any]]] = {}
        for r in marker_rows:
            el_id = _row_get(r, "id")
            entry = {
                "element_id": el_id,
                "document_id": _row_get(r, "document_id"),
                "ordinal": _row_get(r, "ordinal"),
                "ref": _row_get(r, "ref"),
                "text": _row_get(r, "text"),
                "edges": [],
            }
            markers.append(entry)
            if isinstance(el_id, str):
                edges_by_source[el_id] = entry["edges"]

        if edges_by_source:
            # One edge query for all markers; attach by source element id. Reuse
            # the same doc filter so we never pull edges from other documents.
            edge_clauses = ["project_id = ?"]
            edge_params: list[Any] = [project_id]
            if resolved_document_id is not None:
                edge_clauses.append(
                    "source_element_id IN "
                    "(SELECT id FROM doc_elements WHERE document_id = ?)"
                )
                edge_params.append(resolved_document_id)
            edge_sql = (
                "SELECT * FROM doc_edges WHERE "
                + " AND ".join(edge_clauses)
                + " ORDER BY created_at ASC, id ASC"
            )
            async with self._db.execute(edge_sql, tuple(edge_params)) as cur:
                edge_rows = await cur.fetchall()
            # Order edges bibentry-first, then zotero_item, then anything else.
            _kind_rank = {"bibentry": 0, "zotero_item": 1}
            for er in edge_rows:
                edge = _row_to_dict(er, _EDGE_COLUMNS)
                bucket = edges_by_source.get(edge.get("source_element_id"))
                if bucket is not None:
                    bucket.append(edge)
            for entry in markers:
                entry["edges"].sort(
                    key=lambda ed: _kind_rank.get(ed.get("target_kind"), 99)
                )

        return {"markers": markers}

    async def get_element_by_id(
        self, element_id: str
    ) -> dict[str, Any] | None:
        """Return a single stored element (with its parent doc) by its id, or None.

        2976e168 — the read primitive the GENERIC POINTER resolver needs for a
        ``selector.type='node_id'`` pointer: a doc_store element id (9ee6d2ec)
        resolves to ``{element:{...}, document:{...}}`` (the element row plus its
        owning document header) so the pointer can surface a source/title. Returns
        None for an unknown id. Never raises upward (the resolver guards, but this
        stays a plain best-effort lookup).
        """
        if not isinstance(element_id, str) or not element_id.strip():
            return None
        async with self._db.execute(
            "SELECT * FROM doc_elements WHERE id = ?", (element_id.strip(),)
        ) as cur:
            row = await cur.fetchone()
        element = _row_to_dict(row, _ELEMENT_COLUMNS)
        if element is None:
            return None
        document: dict[str, Any] | None = None
        doc_id = element.get("document_id")
        if isinstance(doc_id, str) and doc_id:
            async with self._db.execute(
                "SELECT * FROM doc_documents WHERE id = ?", (doc_id,)
            ) as cur:
                drow = await cur.fetchone()
            document = _row_to_dict(drow, _DOC_COLUMNS)
        return {"element": element, "document": document}

    async def list_documents(self, project_id: str) -> list[dict[str, Any]]:
        """Return all stored document headers for a project (newest first)."""
        async with self._db.execute(
            "SELECT * FROM doc_documents WHERE project_id = ? "
            "ORDER BY updated_at DESC, id DESC",
            (project_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_dict(r, _DOC_COLUMNS) for r in rows]

    async def delete_document(self, project_id: str, source: str) -> bool:
        """Delete a document (its elements + edges) by source. Return True if removed."""
        doc = await self.get_document(project_id, source)
        if doc is None:
            return False
        # Drop edges FIRST (subquery over the still-present elements) so deleting a
        # document leaves no orphan edges behind.
        await self._delete_edges_for_document(doc["id"])
        await self._delete_elements(doc["id"])
        await self._db.execute(
            "DELETE FROM doc_documents WHERE id = ?", (doc["id"],)
        )
        await self._db.commit()
        return True

    async def set_link_status(
        self, project_id: str, source: str, link_status: str
    ) -> dict[str, Any] | None:
        """Update a stored document's ``link_status`` and return its refreshed row.

        14015718 — the explicit lifecycle transition primitive: promote a doc to
        ``deprecated`` (file gone/superseded, keep as history) or ``independent``
        (standalone snapshot, never write back), or restore it to ``live``. An
        unknown/blank status is coerced to ``live`` (:func:`_normalize_link_status`).
        Returns the updated document dict, or ``None`` if no such document exists.
        """
        doc = await self.get_document(project_id, source)
        if doc is None:
            return None
        ls = _normalize_link_status(link_status)
        await self._db.execute(
            "UPDATE doc_documents SET link_status = ?, updated_at = ? WHERE id = ?",
            (ls, _now_iso(), doc["id"]),
        )
        await self._db.commit()
        return await self.get_document(project_id, source)

    # -- equations (OMML) — 06df6ab3 -----------------------------------------

    async def get_equations(self, document_id: str) -> list[dict[str, Any]]:
        """Return every stored equation for a document, ordered by ``ordinal``."""
        async with self._db.execute(
            "SELECT * FROM doc_equations WHERE document_id = ? ORDER BY ordinal ASC",
            (document_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_dict(r, _EQUATION_COLUMNS) for r in rows]

    async def find_similar_equations(
        self, document_id: str, latex: str, *, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Fuzzy-match ``latex`` (normalized) against this document's stored
        equations; returns every stored equation carrying a ``score`` (difflib
        ratio against its ``latex_normalized``), best match first, capped at
        ``limit``. Never raises; an empty/unknown document yields ``[]``."""
        norm = normalize_latex(latex)
        existing = await self.get_equations(document_id)
        scored = [
            {**eq, "score": round(_equation_similarity(norm, eq.get("latex_normalized") or ""), 4)}
            for eq in existing
        ]
        scored.sort(key=lambda e: e["score"], reverse=True)
        lim = limit if isinstance(limit, int) and limit > 0 else 5
        return scored[:lim]

    async def find_symbol_usages(
        self, document_id: str, symbol_or_equation_id: str
    ) -> dict[str, Any]:
        """Cross-reference tracking for a defined symbol/equation (9605edb0).

        Resolves ``symbol_or_equation_id`` to a single normalized-LaTeX *target*
        and returns every place in ``document_id`` where that target reappears,
        so a later mention can be checked to point back to the DEFINITION instead
        of assuming the reader remembers it.

        Resolution — two accepted input shapes, tried in this order:

        * an existing ``doc_equations.id`` in this document → the target is that
          row's stored ``latex_normalized`` (authoritative; no re-normalization);
        * anything else → a raw symbol / LaTeX source, normalized with the SAME
          :func:`normalize_latex` that produced every ``latex_normalized`` value,
          so the comparison is apples-to-apples (never a bespoke normalization).

        Scans TWO surfaces of the same document and merges the hits:

        * ``doc_equations`` — any equation (the target row itself, or another
          element) whose ``latex_normalized`` equals the resolved target;
        * ``doc_elements`` — any paragraph whose ``text`` *textually contains* the
          target symbol (a normalized, whitespace-insensitive substring test), so
          prose reuse of a symbol defined in an equation is caught too.

        Each hit carries ``element_id``, ``document_id``, ``ordinal``,
        ``matched_text`` (the equation's LaTeX or the paragraph text), ``context``
        (``"equation"`` or ``"paragraph"``), and a ``is_definition`` /
        ``is_reuse`` classification: the EARLIEST hit by ordinal is the
        definition, every later hit is a reuse. Hits are ordered by ``ordinal``
        (then a stable ``context``/``id`` tiebreak) so the definition sorts first.

        Returns ``{target, resolved_from, hits:[...]}``. Never raises: an empty
        target (blank symbol, or an equation id whose row has no normalized latex)
        or a document with no matches yields ``{..., "hits": []}``.
        """
        raw = (symbol_or_equation_id or "").strip()
        if not raw:
            return {"target": "", "resolved_from": None, "hits": []}

        equations = await self.get_equations(document_id)

        # Resolve the target normalized-LaTeX. An exact doc_equations.id match in
        # THIS document is authoritative (use its stored latex_normalized as-is);
        # otherwise treat the input as a raw symbol/LaTeX string and normalize it
        # with the same normalize_latex that produced every latex_normalized.
        target = ""
        resolved_from = "symbol"
        by_id = {eq.get("id"): eq for eq in equations}
        if raw in by_id:
            target = (by_id[raw].get("latex_normalized") or "").strip()
            resolved_from = "equation_id"
        else:
            target = normalize_latex(raw)

        if not target:
            return {"target": "", "resolved_from": resolved_from, "hits": []}

        hits: list[dict[str, Any]] = []

        # Surface 1 — equations with a matching normalized latex (same or other
        # elements). Exact equality on the normalized key, not a fuzzy ratio: this
        # is "the SAME equation reappears", not "a similar one".
        for eq in equations:
            if (eq.get("latex_normalized") or "").strip() == target:
                hits.append({
                    "element_id": eq.get("element_id"),
                    "equation_id": eq.get("id"),
                    "document_id": eq.get("document_id"),
                    "ordinal": eq.get("ordinal"),
                    "matched_text": eq.get("latex_normalized"),
                    "context": "equation",
                })

        # Surface 2 — paragraphs textually containing the symbol. Compare on the
        # normalized (whitespace-stripped) forms so "E = m c^2" in prose still
        # matches the "E=mc^2" target. Only kind='paragraph'/'text' bodies are
        # scanned so a heading/figure caption isn't mistaken for a reuse.
        async with self._db.execute(
            "SELECT * FROM doc_elements WHERE document_id = ? ORDER BY ordinal ASC",
            (document_id,),
        ) as cur:
            element_rows = await cur.fetchall()
        for row in element_rows:
            el = _row_to_dict(row, _ELEMENT_COLUMNS)
            if el is None:
                continue
            text = el.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            if target in re.sub(r"\s+", "", text):
                hits.append({
                    "element_id": el.get("id"),
                    "equation_id": None,
                    "document_id": el.get("document_id"),
                    "ordinal": el.get("ordinal"),
                    "matched_text": text,
                    "context": "paragraph",
                })

        # Order by ordinal so the definition (earliest) sorts first; stable tie
        # break keeps equation hits ahead of paragraph hits at one ordinal and
        # keeps the result deterministic across backends.
        _context_rank = {"equation": 0, "paragraph": 1}
        hits.sort(key=lambda h: (
            h.get("ordinal") if isinstance(h.get("ordinal"), int) else 0,
            _context_rank.get(h.get("context"), 9),
            str(h.get("equation_id") or ""),
            str(h.get("element_id") or ""),
        ))

        # Classify: the FIRST hit is the definition, every later one is a reuse.
        for i, h in enumerate(hits):
            h["is_definition"] = i == 0
            h["is_reuse"] = i > 0

        return {"target": target, "resolved_from": resolved_from, "hits": hits}

    async def put_equations(
        self,
        document_id: str,
        equations: list[dict[str, Any]],
        *,
        dedup_threshold: float = _EQUATION_DEDUP_THRESHOLD,
    ) -> dict[str, Any]:
        """Insert a batch of equations for ``document_id``.

        Every equation is inserted — dedup here is ADVISORY, not blocking,
        mirroring the existing ``add_note`` near-duplicate pattern in
        ``mcp/handler.py`` (6e4e2371): a fuzzy-matched near-duplicate is
        *surfaced* to the caller rather than silently vanishing (skipped) or
        silently piling up unnoticed (the sprint spec's "surface near-matches
        instead of silently duplicating").

        Each ``equations`` item is ``{omml_raw?, latex?, semantic_label?,
        element_id?, ordinal?}`` — provide ``omml_raw`` (real OMML XML, e.g. from
        :func:`parse_docx_equations`) OR ``latex`` (a source string; OMML is
        generated best-effort via :func:`latex_to_omml` when ``omml_raw`` is
        absent — ``None`` on an unsupported/unparsable construct, never raises).
        ``latex_normalized`` is derived from ``latex`` when given, else from a
        flattened read of ``omml_raw`` (:func:`_omml_flatten_text`) — a
        surrogate dedup key when only OMML is available (no real OMML->LaTeX
        reverse conversion; see module docstring).

        Returns ``{"inserted": [...], "near_duplicates": [...]}``; a
        ``near_duplicates`` entry is ``{equation_id, matched_id, matched_latex,
        score}`` for every inserted equation whose best fuzzy match (against
        already-stored equations AND earlier equations in this same batch)
        scores ``>= dedup_threshold``.
        """
        existing = await self.get_equations(document_id)
        next_ordinal = (max((e.get("ordinal") or 0) for e in existing) + 1) if existing else 0
        inserted: list[dict[str, Any]] = []
        near_duplicates: list[dict[str, Any]] = []
        pool = list(existing)

        for eq in equations or []:
            omml_raw = eq.get("omml_raw")
            latex_raw = eq.get("latex")
            if not omml_raw and latex_raw:
                omml_raw = latex_to_omml(latex_raw)
            latex_normalized = (
                normalize_latex(latex_raw) if latex_raw else _omml_flatten_text(omml_raw)
            )

            best_score = 0.0
            best_match: dict[str, Any] | None = None
            if latex_normalized:
                for cand in pool:
                    score = _equation_similarity(
                        latex_normalized, cand.get("latex_normalized") or ""
                    )
                    if score > best_score:
                        best_score, best_match = score, cand

            eq_id = eq.get("id") or uuid.uuid4().hex
            ordinal = eq.get("ordinal") if isinstance(eq.get("ordinal"), int) else next_ordinal
            next_ordinal = max(next_ordinal, ordinal + 1)
            now = _now_iso()
            semantic_label = eq.get("semantic_label")
            element_id = eq.get("element_id")

            await self._db.execute(
                "INSERT INTO doc_equations "
                "(id, document_id, element_id, ordinal, omml_raw, "
                "latex_normalized, semantic_label, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (eq_id, document_id, element_id, ordinal, omml_raw,
                 latex_normalized, semantic_label, now),
            )
            row = {
                "id": eq_id, "document_id": document_id, "element_id": element_id,
                "ordinal": ordinal, "omml_raw": omml_raw,
                "latex_normalized": latex_normalized, "semantic_label": semantic_label,
                "created_at": now,
            }
            inserted.append(row)
            pool.append(row)

            if best_match is not None and best_score >= dedup_threshold:
                near_duplicates.append({
                    "equation_id": eq_id,
                    "matched_id": best_match.get("id"),
                    "matched_latex": best_match.get("latex_normalized"),
                    "score": round(best_score, 4),
                })

        if inserted:
            await self._db.commit()
        return {"inserted": inserted, "near_duplicates": near_duplicates}

    async def add_equation(
        self,
        document_id: str,
        omml_or_latex: str,
        *,
        semantic_label: str | None = None,
        element_id: str | None = None,
    ) -> dict[str, Any]:
        """Index ONE equation — the ``index_equation`` MCP tool's primitive.

        ``omml_or_latex`` is auto-detected: a string starting with ``<`` is
        treated as raw OMML XML (stored as-is); anything else is treated as a
        LaTeX source (OMML is generated best-effort via :func:`latex_to_omml`).
        Returns ``{"equation": <inserted row, or None if not inserted>,
        "near_duplicates": [...]}`` (see :meth:`put_equations`).
        """
        raw = (omml_or_latex or "").strip()
        eq: dict[str, Any] = {"semantic_label": semantic_label, "element_id": element_id}
        if raw.startswith("<"):
            eq["omml_raw"] = raw
        else:
            eq["latex"] = raw
        result = await self.put_equations(document_id, [eq])
        return {
            "equation": result["inserted"][0] if result["inserted"] else None,
            "near_duplicates": result["near_duplicates"],
        }

    # -- equation write-back (51a595e7) --------------------------------------

    async def _delete_equations(self, document_id: str) -> None:
        """Drop every stored equation row for a document (idempotent)."""
        await self._db.execute(
            "DELETE FROM doc_equations WHERE document_id = ?", (document_id,)
        )

    async def resync_document_equations(
        self, document_id: str, file_path: str | bytes | bytearray
    ) -> dict[str, Any]:
        """Re-derive a document's equation index straight from its .docx (51a595e7).

        The sidecar's equation rows are a *cache* of the ``<m:oMath>`` elements
        physically present in the source .docx. After a write-back (insert /
        update / delete of an equation in the file) that cache is stale, so this
        drops the document's existing ``doc_equations`` rows and re-runs
        :meth:`put_equations` over a FRESH :func:`parse_docx_equations` parse —
        a targeted, self-contained resync (no separate re-verify step). Returns
        the ``put_equations`` result (``{inserted, near_duplicates}``).
        """
        raw_equations = parse_docx_equations(file_path)
        eq_batch = [
            {
                "element_id": eq.get("element_id"),
                "ordinal": eq.get("ordinal"),
                "omml_raw": eq.get("omml_raw"),
                "latex_normalized": _omml_flatten_text(eq.get("omml_raw")),
            }
            for eq in raw_equations
        ]
        await self._delete_equations(document_id)
        return await self.put_equations(document_id, eq_batch)

    async def insert_equation(
        self,
        project_id: str,
        source: str,
        para_id: str,
        equation_id_or_omml: str,
        *,
        position: str = "append",
    ) -> dict[str, Any]:
        """Write an OMML equation directly into a stored document's source .docx.

        Collapses the old 4-5 step manual flow (resolve doc -> open zip -> parse
        xml -> splice OMML -> rewrite zip -> reindex) into one call (51a595e7):

        1. Resolve the ``doc_documents`` row for ``(project_id, source)`` and the
           physical .docx path from its ``source`` column (the path/URL it was
           ingested/reindexed under).
        2. Determine the OMML: if ``equation_id_or_omml`` is the id of an existing
           ``doc_equations`` row for this document, reuse that row's ``omml_raw``;
           else a string starting with ``<`` is treated as raw OMML XML, and
           anything else as a LaTeX source converted best-effort via
           :func:`latex_to_omml`.
        3. Open the .docx, find the paragraph whose ``w14:paraId`` (or synthesized
           ``p{idx}``) == ``para_id``, splice the ``<m:oMath>`` at ``position``
           (``append`` inside the paragraph, or ``before`` / ``after`` it as its
           own display-equation paragraph), and rewrite ``word/document.xml`` back
           into the .docx in place.
        4. Resync this document's equation index from the modified file
           (:meth:`resync_document_equations`) so the new equation is queryable —
           no separate re-verify step.

        Returns ``{document_id, source, para_id, position, omml, resync}`` on
        success, or ``{error}`` for a bad para_id / unresolvable OMML / missing
        file. Never mutates the file when the equation or paragraph can't be
        resolved (fail-before-write).
        """
        pos = (position or "append").strip().lower()
        if pos not in ("append", "before", "after"):
            return {"error": f"position must be append|before|after, got {position!r}"}

        doc_row = await self.get_document(project_id, source)
        if doc_row is None:
            return {
                "error": (
                    f"no stored document for source={source!r} — ingest_document "
                    "or reindex_document it first"
                ),
            }
        document_id = doc_row["id"]
        # 14015718 — refuse loudly on an independent (no-write-back) document with
        # a DISTINCT error, before any file resolution, so it is never confused
        # with a live doc whose file is merely temporarily missing.
        if _normalize_link_status(doc_row.get("link_status")) == _LINK_STATUS_INDEPENDENT:
            return {
                "error": (
                    f"document {document_id} is marked independent (no write-back): "
                    "it is a standalone captured snapshot with no live source file — "
                    "insert_equation cannot write into it"
                ),
            }
        docx_path = doc_row.get("source")
        if not isinstance(docx_path, str) or not docx_path.strip():
            return {"error": f"stored document {document_id} has no source path to write back to"}
        if not os.path.isfile(docx_path):
            return {"error": f"source .docx not found on disk: {docx_path!r}"}

        # eab6930a — advisory staleness check, computed against the PRE-write file.
        stale_warning = await _docx_staleness_check(doc_row, docx_path)

        # --- resolve the OMML to insert (fail before touching the file) --------
        try:
            omml_raw = await self._resolve_omml_payload(document_id, equation_id_or_omml)
        except ValueError as exc:
            return {"error": str(exc)}
        if omml_raw is None:
            return {
                "error": (
                    "could not resolve an OMML equation from "
                    f"equation_id_or_omml={equation_id_or_omml!r} (unknown equation "
                    "id, and not parseable as raw OMML or convertible LaTeX)"
                ),
            }

        # --- open, locate paragraph, splice, save ------------------------------
        try:
            raw, root = _load_docx_xml(docx_path)
        except Exception as exc:  # noqa: BLE001 — malformed/locked file
            return {"error": f"could not open source .docx: {exc}"}
        try:
            paragraph = _find_paragraph_by_id(root, para_id)
        except AmbiguousParagraphIdError as exc:
            # 827b6bdc — fail closed the same as a not-found id: this call
            # site's established contract is an {"error": ...} dict, never a
            # raise, and nothing has been mutated yet either way.
            return {"error": str(exc)}
        if paragraph is None:
            return {"error": f"no paragraph with id {para_id!r} in {docx_path!r}"}
        try:
            omath_el = _LET.fromstring(
                omml_raw.encode("utf-8") if isinstance(omml_raw, str) else bytes(omml_raw)
            )
        except Exception as exc:  # noqa: BLE001 — payload wasn't valid XML after all
            return {"error": f"resolved OMML is not valid XML: {exc}"}
        inserted_para_id, inserted_text_id = _insert_omath_at_position(paragraph, omath_el, pos)
        try:
            transaction = _save_docx_xml(raw, root, docx_path)
        except Exception as exc:  # noqa: BLE001 — write failure (perms/disk)
            return {"error": f"could not write back to source .docx: {exc}"}

        # --- resync the sidecar equation index from the modified file ----------
        resync = await self.resync_document_equations(document_id, docx_path)
        result = {
            "document_id": document_id,
            "source": source,
            "para_id": para_id,
            "position": pos,
            "omml": _LET.tostring(omath_el, encoding="unicode"),
            "inserted_para_id": inserted_para_id,
            "inserted_text_id": inserted_text_id,
            "resync": resync,
            # dccc2311 — deterministic identifier of exactly what this write
            # transaction changed (see _docx_manifest_hash).
            "manifest_hash": transaction.get("manifest_hash"),
        }
        if stale_warning is not None:
            result["stale_warning"] = stale_warning
        return result

    async def _resolve_omml_payload(
        self, document_id: str, equation_id_or_omml: str
    ) -> str | None:
        """Resolve ``equation_id_or_omml`` to a real OMML XML string, or ``None``.

        Resolution order (51a595e7):

        1. An id of an existing ``doc_equations`` row for THIS document whose
           ``omml_raw`` is non-empty -> reuse that stored OMML verbatim.
        2. A string starting with ``<`` -> treat as raw OMML XML (as-is).
        3. Anything else -> a LaTeX source, converted best-effort via
           :func:`latex_to_omml` (``None`` on an unsupported/unparsable construct).
        """
        raw = (equation_id_or_omml or "").strip()
        if not raw:
            return None
        # (1) existing equation id for this document.
        existing = await self.get_equations(document_id)
        for eq in existing:
            if eq.get("id") == raw and eq.get("omml_raw"):
                _validate_omml_structure(eq["omml_raw"])
                return eq["omml_raw"]
        # (2) raw OMML XML.
        if raw.startswith("<"):
            _validate_omml_structure(raw)
            return raw
        # (3) LaTeX source.
        converted = latex_to_omml(raw)
        if converted is not None:
            _validate_omml_structure(converted)
        return converted

    # -- figures (semantic index) — c623e648 ---------------------------------

    async def get_figures(self, document_id: str) -> list[dict[str, Any]]:
        """Return every indexed figure for a document, ordered by ``ordinal``."""
        async with self._db.execute(
            "SELECT * FROM doc_figures WHERE document_id = ? ORDER BY ordinal ASC",
            (document_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_dict(r, _FIGURE_COLUMNS) for r in rows]

    async def find_similar_figures(
        self,
        document_id: str,
        description_or_path: str,
        *,
        limit: int = 5,
        output_resolver: "OutputResolver | None" = None,
    ) -> list[dict[str, Any]]:
        """Fuzzy-match a free-text description OR a file path against this
        document's indexed figures; returns every stored figure carrying a
        ``score`` (the better of the difflib ratio against its
        ``normalized_caption`` and against its ``file_path``), best match first,
        capped at ``limit``. Mirrors :meth:`find_similar_equations`. Never
        raises; an empty/unknown document yields ``[]``.

        Cross-store resolve-through (d2a3537a): when ``output_resolver`` is given
        (a callable ``file_path -> output_row | None``, e.g.
        ``OutputsFtsIndex.resolve_output`` or
        :func:`meridian.outputs_indexer.resolve_figure_output` partially applied),
        every returned figure that carries a ``file_path`` is resolved THROUGH to
        its ``outputs_index`` row and gets a ``linked_output`` key: the matching
        output row (path, generating_script, is_archival/canonical_path,
        fingerprint) when the figure's file names an already-indexed run output,
        else ``None``. So "does this plot already exist as a run output?" and
        "where is it referenced in my thesis?" become one lookup. A resolver that
        raises is swallowed (``linked_output`` stays ``None``) — the fuzzy match
        must never be crashed by the cross-store hop."""
        query = (description_or_path or "").strip()
        norm = normalize_caption(query)
        query_lower = query.lower()
        existing = await self.get_figures(document_id)
        scored: list[dict[str, Any]] = []
        for fig in existing:
            cap_score = _figure_similarity(norm, fig.get("normalized_caption") or "")
            path = (fig.get("file_path") or "").strip().lower()
            path_score = _figure_similarity(query_lower, path) if path else 0.0
            row = {**fig, "score": round(max(cap_score, path_score), 4)}
            if output_resolver is not None:
                row["linked_output"] = self._resolve_output(fig, output_resolver)
            scored.append(row)
        scored.sort(key=lambda f: f["score"], reverse=True)
        lim = limit if isinstance(limit, int) and limit > 0 else 5
        return scored[:lim]

    @staticmethod
    def _resolve_output(
        figure: dict[str, Any], output_resolver: "OutputResolver",
    ) -> dict[str, Any] | None:
        """Resolve ONE figure through to its ``outputs_index`` row (d2a3537a).

        Calls ``output_resolver`` with the figure's ``file_path`` and returns the
        linked output row, or ``None`` when the figure has no ``file_path`` or
        names no indexed output. A resolver that raises resolves to ``None`` — the
        cross-store hop is advisory glue and must never crash the caller."""
        file_path = figure.get("file_path")
        if not isinstance(file_path, str) or not file_path.strip():
            return None
        try:
            return output_resolver(file_path)
        except Exception:  # noqa: BLE001 — a resolver failure is a clean miss
            _log.debug("figure output resolver failed for %r", file_path, exc_info=True)
            return None

    async def resolve_figure_output(
        self, document_id: str, file_path: str, output_resolver: "OutputResolver",
    ) -> dict[str, Any] | None:
        """Thin resolve-through for a single stored figure by ``file_path``.

        Cross-store glue (d2a3537a): find this document's figure whose
        ``file_path`` matches ``file_path`` and return ``{figure, linked_output}``
        — the figure row plus the ``outputs_index`` row it resolves through to
        (``None`` linked_output when the file names no indexed output). Returns
        ``None`` when the document has no figure at that path. Path matching is
        exact on the stored string (the outputs-index side does the
        normalization); ``output_resolver`` is the injected
        ``file_path -> output_row | None`` seam. Never raises."""
        target = (file_path or "").strip()
        if not target:
            return None
        for fig in await self.get_figures(document_id):
            fp = fig.get("file_path")
            if isinstance(fp, str) and fp.strip() == target:
                return {
                    "figure": fig,
                    "linked_output": self._resolve_output(fig, output_resolver),
                }
        return None

    async def _find_all_caption_candidates(
        self, document_id: str, element_id: str | None
    ) -> list[str]:
        """Return ALL ``kind='figure'`` elements in the same section as ``element_id``.

        0ff8b982 — advisory lookup: when a doc_figures row's ``caption_element_id``
        is not given, find every ``kind='figure'`` doc_elements row (SEQ-field
        caption paragraph) that shares the same parent section as the anchor
        ``element_id``. Returns an ordered list of element ids (empty when
        element_id is None or the section has no figure-caption elements).

        When exactly ONE candidate exists -> ``put_figures`` surfaces it as
        ``suggested_caption_element_id`` (unambiguous advisory suggestion).
        When MULTIPLE candidates exist -> ``put_figures`` surfaces all of them
        as ``suggested_caption_candidates`` (the "Figure 3b used twice" ambiguity
        scenario from the real thesis -- two caption elements in one section that
        must be disambiguated via :meth:`set_figure_caption_link`, not silently
        auto-resolved). ADVISORY ONLY -- never auto-applied.
        """
        if not isinstance(element_id, str) or not element_id.strip():
            return []
        async with self._db.execute(
            "SELECT parent_id FROM doc_elements WHERE id = ?",
            (element_id.strip(),),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return []
        parent_id = _row_get(row, "parent_id")
        async with self._db.execute(
            "SELECT id FROM doc_elements "
            "WHERE document_id = ? AND kind = 'figure' AND parent_id IS ? "
            "ORDER BY ordinal ASC",
            (document_id, parent_id),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_get(r, "id") for r in rows if _row_get(r, "id")]

    async def put_figures(
        self,
        document_id: str,
        figures: list[dict[str, Any]],
        *,
        dedup_threshold: float = _FIGURE_DEDUP_THRESHOLD,
    ) -> dict[str, Any]:
        """Insert a batch of figures for ``document_id``.

        The figure analogue of :meth:`put_equations` -- same ADVISORY (never
        blocking) dedup contract: every figure is inserted, and a fuzzy-matched
        near-duplicate (same-document normalized-caption difflib ratio ``>=
        dedup_threshold``, against already-stored figures AND earlier figures in
        this same batch) is SURFACED via ``near_duplicates`` rather than silently
        dropped or silently piled up.

        Each ``figures`` item is ``{file_path?, caption?, semantic_label?,
        element_id?, ordinal?, caption_element_id?}``. The referenced ``file_path``
        is checked against disk: a missing file is FLAGGED (``file_exists`` on the
        row, and a ``missing_files`` entry in the result) -- never a hard failure,
        because a figure can be indexed before its asset lands or when the store
        runs on a different host than the captured path.

        NEW vs original put_figures (0ff8b982): when ``caption_element_id`` is
        not given, the nearest ``kind='figure'`` doc_elements element in the same
        structural section (the SEQ-field caption paragraph) is surfaced as a
        SUGGESTION in ``suggested_caption_element_id`` on the inserted row (NOT
        auto-applied -- advisory only, same pattern as near_duplicates and
        doc_tables' paired_figure_id). When MULTIPLE caption elements exist in
        the same section (the "Figure 3b used twice" ambiguity scenario), ALL
        candidates are surfaced in ``suggested_caption_candidates`` (a list of
        element ids) so the caller can confirm the correct one via
        link_figure_caption rather than having the store silently pick one.

        Returns ``{"inserted": [...], "near_duplicates": [...],
        "missing_files": [...]}``; a ``near_duplicates`` entry is
        ``{figure_id, matched_id, matched_caption, score}`` and a
        ``missing_files`` entry is ``{figure_id, file_path}``.
        """
        existing = await self.get_figures(document_id)
        next_ordinal = (max((f.get("ordinal") or 0) for f in existing) + 1) if existing else 0
        inserted: list[dict[str, Any]] = []
        near_duplicates: list[dict[str, Any]] = []
        missing_files: list[dict[str, Any]] = []
        pool = list(existing)

        for fig in figures or []:
            caption = fig.get("caption")
            normalized_caption = normalize_caption(caption)
            file_path = fig.get("file_path")

            best_score = 0.0
            best_match: dict[str, Any] | None = None
            if normalized_caption:
                for cand in pool:
                    score = _figure_similarity(
                        normalized_caption, cand.get("normalized_caption") or ""
                    )
                    if score > best_score:
                        best_score, best_match = score, cand

            # Advisory on-disk existence check -- flag (don't fail) a missing asset.
            exists: int | None = None
            if isinstance(file_path, str) and file_path.strip():
                try:
                    exists = 1 if os.path.isfile(file_path) else 0
                except (OSError, ValueError):
                    exists = 0

            fig_id = fig.get("id") or uuid.uuid4().hex
            ordinal = fig.get("ordinal") if isinstance(fig.get("ordinal"), int) else next_ordinal
            next_ordinal = max(next_ordinal, ordinal + 1)
            now = _now_iso()
            semantic_label = fig.get("semantic_label")
            element_id = fig.get("element_id")
            caption_element_id = fig.get("caption_element_id")

            # 0ff8b982 — advisory suggestion: when no caption_element_id is given,
            # find all caption-shaped elements (kind='figure') in the same structural
            # section. Surface:
            #   * a single suggestion as suggested_caption_element_id when there is
            #     exactly one candidate (unambiguous), or
            #   * ALL candidates as suggested_caption_candidates when there are
            #     multiple (the "Figure 3b used twice" ambiguity scenario).
            # Never auto-applied -- advisory only.
            suggested_caption_element_id: str | None = None
            suggested_caption_candidates: list[str] | None = None
            if caption_element_id is None and element_id is not None:
                try:
                    candidates = await self._find_all_caption_candidates(
                        document_id, element_id
                    )
                except Exception:  # noqa: BLE001 — suggestion is advisory only
                    candidates = []
                if len(candidates) == 1:
                    suggested_caption_element_id = candidates[0]
                elif len(candidates) > 1:
                    suggested_caption_candidates = candidates

            await self._db.execute(
                "INSERT INTO doc_figures "
                "(id, document_id, element_id, ordinal, file_path, caption, "
                "normalized_caption, semantic_label, file_exists, "
                "caption_element_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (fig_id, document_id, element_id, ordinal, file_path, caption,
                 normalized_caption, semantic_label, exists,
                 caption_element_id, now),
            )
            row: dict[str, Any] = {
                "id": fig_id, "document_id": document_id, "element_id": element_id,
                "ordinal": ordinal, "file_path": file_path, "caption": caption,
                "normalized_caption": normalized_caption,
                "semantic_label": semantic_label, "file_exists": exists,
                "caption_element_id": caption_element_id,
                "created_at": now,
            }
            if suggested_caption_element_id is not None:
                row["suggested_caption_element_id"] = suggested_caption_element_id
            if suggested_caption_candidates is not None:
                row["suggested_caption_candidates"] = suggested_caption_candidates
            inserted.append(row)
            pool.append(row)

            if exists == 0:
                missing_files.append({"figure_id": fig_id, "file_path": file_path})

            if best_match is not None and best_score >= dedup_threshold:
                near_duplicates.append({
                    "figure_id": fig_id,
                    "matched_id": best_match.get("id"),
                    "matched_caption": best_match.get("normalized_caption"),
                    "score": round(best_score, 4),
                })

        if inserted:
            await self._db.commit()
        return {
            "inserted": inserted,
            "near_duplicates": near_duplicates,
            "missing_files": missing_files,
        }

    async def add_figure(
        self,
        document_id: str,
        file_path: str | None,
        *,
        caption: str | None = None,
        semantic_label: str | None = None,
        element_id: str | None = None,
        caption_element_id: str | None = None,
    ) -> dict[str, Any]:
        """Index ONE figure -- the ``index_figure`` MCP tool's primitive.

        Inserts a single figure (with the same advisory near-duplicate +
        missing-file surfacing as :meth:`put_figures`). Returns
        ``{"figure": <inserted row, or None if not inserted>,
        "near_duplicates": [...], "missing_files": [...]}``.

        0ff8b982 — ``caption_element_id`` (optional): the doc_elements id of the
        caption paragraph (kind='figure' SEQ-field element) that is DURABLY linked
        to this figure. When not given, an advisory suggestion is surfaced on the
        returned figure row (``suggested_caption_element_id`` or
        ``suggested_caption_candidates`` for the ambiguous-multi-candidate case).
        """
        fig: dict[str, Any] = {
            "file_path": file_path.strip() if isinstance(file_path, str) else file_path,
            "caption": caption,
            "semantic_label": semantic_label,
            "element_id": element_id,
            "caption_element_id": caption_element_id,
        }
        result = await self.put_figures(document_id, [fig])
        return {
            "figure": result["inserted"][0] if result["inserted"] else None,
            "near_duplicates": result["near_duplicates"],
            "missing_files": result["missing_files"],
        }

    async def set_figure_caption_link(
        self,
        figure_id: str,
        caption_element_id: str,
    ) -> dict[str, Any] | None:
        """Durably set the ``caption_element_id`` on an existing doc_figures row.

        0ff8b982 — the write primitive for the ``link_figure_caption`` MCP tool:
        confirms (or corrects) the durable linkage from a figure to its caption
        paragraph element, identified by the stable ``doc_elements.id``. This is
        the backfill mechanism for figures already indexed before the
        ``caption_element_id`` column existed, and the confirmation primitive
        when the advisory suggestion from :meth:`put_figures` surfaces multiple
        candidates (the "Figure 3b used twice" ambiguity scenario).

        Returns the updated figure row as a dict, or ``None`` when ``figure_id``
        does not resolve to any stored figure.
        """
        if not isinstance(figure_id, str) or not figure_id.strip():
            return None
        async with self._db.execute(
            "SELECT * FROM doc_figures WHERE id = ?", (figure_id.strip(),)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        await self._db.execute(
            "UPDATE doc_figures SET caption_element_id = ? WHERE id = ?",
            (caption_element_id, figure_id.strip()),
        )
        await self._db.commit()
        async with self._db.execute(
            "SELECT * FROM doc_figures WHERE id = ?", (figure_id.strip(),)
        ) as cur:
            updated_row = await cur.fetchone()
        return _row_to_dict(updated_row, _FIGURE_COLUMNS)

    # -- tables (semantic index) — 2622182d ----------------------------------

    async def get_tables(self, document_id: str) -> list[dict[str, Any]]:
        """Return every indexed table for a document, ordered by ``ordinal``."""
        async with self._db.execute(
            "SELECT * FROM doc_tables WHERE document_id = ? ORDER BY ordinal ASC",
            (document_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_dict(r, _TABLE_COLUMNS) for r in rows]

    async def find_similar_tables(
        self,
        document_id: str,
        description: str,
        *,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Fuzzy-match a free-text description against this document's indexed
        tables; returns every stored table carrying a ``score`` (difflib ratio
        against its ``normalized_caption``), best match first, capped at
        ``limit``. Mirrors :meth:`find_similar_figures`. Never raises; an
        empty/unknown document yields ``[]``."""
        query = (description or "").strip()
        norm = normalize_table_caption(query)
        existing = await self.get_tables(document_id)
        scored: list[dict[str, Any]] = []
        for tbl in existing:
            cap_score = _table_similarity(norm, tbl.get("normalized_caption") or "")
            scored.append({**tbl, "score": round(cap_score, 4)})
        scored.sort(key=lambda t: t["score"], reverse=True)
        lim = limit if isinstance(limit, int) and limit > 0 else 5
        return scored[:lim]

    # -- flag-state links (8ca89e8f) -----------------------------------------

    async def link_flag_state(
        self,
        project_id: str,
        document_id: str,
        element_id: str,
        flag_name: str,
        *,
        value: Any = None,
        default: Any = None,
        source_file: str | None = None,
        source_line: int | None = None,
    ) -> dict[str, Any]:
        """Durably record that ``element_id``'s underlying numbers were
        produced with ``flag_name`` set to ``value`` -- the ``link_flag_to_section``
        MCP tool's write primitive (8ca89e8f, the unbuilt half of workspace
        proposal 8d8bbe63).

        ``element_id`` is a ``doc_elements.id`` -- a section heading,
        paragraph, or (since figures/tables are ALSO doc_elements rows, see
        :func:`elements_from_docx_content_tree`) a figure or table. One
        mechanism covers all four anchor kinds the item asks for, exactly
        like the existing figure-caption linkage anchors to the same id space
        rather than inventing a parallel one.

        ``default`` should be the flag's default AS RECORDED by
        :func:`meridian.flag_registry.get_flag_registry` at link time (the
        caller typically just ran that scan to find ``flag_name`` in the
        first place). It is what a later drift check compares the CURRENT
        scanned default against -- ``diff_flag_links`` in
        :mod:`meridian.flag_registry`. ``source_file``/``source_line``
        optionally pin the exact call site scanned, so a same-named flag read
        elsewhere in the codebase can never falsely trigger/suppress drift for
        this link (see that function's docstring).

        Insert-only (NOT an upsert): re-linking the same (element_id,
        flag_name) pair after a re-verification adds a new row rather than
        overwriting the old one -- an append-only provenance trail, mirroring
        the repo's convention elsewhere (task_log, DECISIONS.md). Callers
        collapse to the latest link per pair via
        :func:`meridian.flag_registry.dedupe_flag_links` before diffing.

        Returns the inserted row as a dict (JSON-decoded ``recorded_value``/
        ``recorded_default``). Never raises on a bad ``default``/``value``
        (any non-JSON-safe value is stringified rather than failing the
        write -- see :func:`_encode_flag_json`).
        """
        row_id = uuid.uuid4().hex
        now = _now_iso()
        # Grabbed synchronously, before the awaited INSERT below, so sequential
        # ``await link_flag_state(...)`` calls get seq values in true call order
        # even when their ``created_at`` collides (see the seq schema comment).
        seq = _next_flag_link_seq()
        line = (
            int(source_line)
            if isinstance(source_line, (int, float)) and not isinstance(source_line, bool)
            else None
        )
        await self._db.execute(
            "INSERT INTO doc_flag_links "
            "(id, project_id, document_id, element_id, flag_name, "
            "recorded_value, recorded_default, source_file, source_line, "
            "created_at, seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row_id, project_id, document_id, element_id, flag_name,
                _encode_flag_json(value), _encode_flag_json(default),
                source_file, line, now, seq,
            ),
        )
        await self._db.commit()
        async with self._db.execute(
            "SELECT * FROM doc_flag_links WHERE id = ?", (row_id,)
        ) as cur:
            row = await cur.fetchone()
        return _decode_flag_link(_row_to_dict(row, _FLAG_LINK_COLUMNS))

    async def get_flag_links(
        self,
        project_id: str,
        *,
        element_id: str | None = None,
        document_id: str | None = None,
        flag_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return recorded flag-state links for a project, newest first.

        Filters (all optional, ANDed): ``element_id`` (one specific anchor),
        ``document_id`` (every link in one document), ``flag_name`` (the
        REVERSE query the item asks for -- "flag X changed, which sections
        does it touch"). With no filters, returns the project's full link
        history. Mirrors :meth:`get_edges`'s filter shape. Never raises; an
        unknown project / no links yields ``[]``.

        Ordered by ``created_at DESC, seq DESC`` -- ``seq`` (not ``id``) is the
        tiebreaker because two links recorded back-to-back can share an
        identical ``created_at`` (timestamp resolution/OS clock granularity),
        and ``id`` is a random UUID with no relationship to insertion order;
        ``seq`` is assigned synchronously in call order (see
        :meth:`link_flag_state`), so it resolves the tie correctly.
        """
        clauses = ["project_id = ?"]
        params: list[Any] = [project_id]
        if element_id is not None:
            clauses.append("element_id = ?")
            params.append(element_id)
        if document_id is not None:
            clauses.append("document_id = ?")
            params.append(document_id)
        if flag_name is not None:
            clauses.append("flag_name = ?")
            params.append(flag_name)
        sql = (
            "SELECT * FROM doc_flag_links WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC, seq DESC"
        )
        async with self._db.execute(sql, tuple(params)) as cur:
            rows = await cur.fetchall()
        return [
            _decode_flag_link(_row_to_dict(r, _FLAG_LINK_COLUMNS)) for r in rows
        ]

    async def _find_paired_figure_suggestion(
        self, document_id: str, element_id: str | None
    ) -> str | None:
        """Best-effort: find a nearby figure in the same structural section.

        When a table's ``paired_figure_id`` is not given, look for a
        ``kind='figure'`` doc_elements row in the same document that shares the
        same ``parent_id`` (section) as the table element (identified by
        ``element_id``). Returns the first matching figure element's ``id``, or
        ``None`` when no element_id is given / the section has no figures.
        This is ADVISORY -- a suggestion, never auto-applied.
        """
        if not isinstance(element_id, str) or not element_id.strip():
            return None
        # Find the table element's parent section.
        async with self._db.execute(
            "SELECT parent_id FROM doc_elements WHERE id = ?",
            (element_id.strip(),),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        parent_id = _row_get(row, "parent_id")
        # Find figures in the same section (same parent_id, same document).
        async with self._db.execute(
            "SELECT id FROM doc_elements "
            "WHERE document_id = ? AND kind = 'figure' AND parent_id IS ? "
            "ORDER BY ordinal ASC LIMIT 1",
            (document_id, parent_id),
        ) as cur:
            fig_row = await cur.fetchone()
        if fig_row is None:
            return None
        return _row_get(fig_row, "id")

    async def put_tables(
        self,
        document_id: str,
        tables: list[dict[str, Any]],
        *,
        dedup_threshold: float = _TABLE_DEDUP_THRESHOLD,
    ) -> dict[str, Any]:
        """Insert a batch of tables for ``document_id``.

        The table analogue of :meth:`put_figures` -- same ADVISORY (never
        blocking) dedup contract: every table is inserted, and a fuzzy-matched
        near-duplicate (same-document normalized-caption difflib ratio ``>=
        dedup_threshold``, against already-stored tables AND earlier tables in
        this same batch) is SURFACED via ``near_duplicates`` rather than silently
        dropped or silently piled up.

        Each ``tables`` item is ``{caption?, table_index?, semantic_label?,
        element_id?, ordinal?, paired_figure_id?}``. No file-exists check (tables
        have no image asset).

        NEW vs put_figures: when ``paired_figure_id`` is not given, the nearest
        already-indexed figure in the same structural section (via doc_elements
        kind='figure' parent matching) is surfaced as a SUGGESTION in
        ``suggested_figure_id`` on the inserted row (NOT auto-applied --
        advisory only, same pattern as near_duplicates).

        Returns ``{"inserted": [...], "near_duplicates": [...]}``;
        a ``near_duplicates`` entry is ``{table_id, matched_id, matched_caption,
        score}``.
        """
        existing = await self.get_tables(document_id)
        next_ordinal = (max((t.get("ordinal") or 0) for t in existing) + 1) if existing else 0
        inserted: list[dict[str, Any]] = []
        near_duplicates: list[dict[str, Any]] = []
        pool = list(existing)

        for tbl in tables or []:
            caption = tbl.get("caption")
            normalized_caption = normalize_table_caption(caption)

            best_score = 0.0
            best_match: dict[str, Any] | None = None
            if normalized_caption:
                for cand in pool:
                    score = _table_similarity(
                        normalized_caption, cand.get("normalized_caption") or ""
                    )
                    if score > best_score:
                        best_score, best_match = score, cand

            tbl_id = tbl.get("id") or uuid.uuid4().hex
            ordinal = tbl.get("ordinal") if isinstance(tbl.get("ordinal"), int) else next_ordinal
            next_ordinal = max(next_ordinal, ordinal + 1)
            now = _now_iso()
            semantic_label = tbl.get("semantic_label")
            element_id = tbl.get("element_id")
            table_index = tbl.get("table_index")
            paired_figure_id = tbl.get("paired_figure_id")

            # Advisory suggestion: when no paired_figure_id is given, find the
            # nearest figure in the same structural section (never auto-applied).
            suggested_figure_id: str | None = None
            if paired_figure_id is None and element_id is not None:
                try:
                    suggested_figure_id = await self._find_paired_figure_suggestion(
                        document_id, element_id
                    )
                except Exception:  # noqa: BLE001 — suggestion is advisory only
                    suggested_figure_id = None

            await self._db.execute(
                "INSERT INTO doc_tables "
                "(id, document_id, element_id, ordinal, table_index, caption, "
                "normalized_caption, semantic_label, paired_figure_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tbl_id, document_id, element_id, ordinal, table_index, caption,
                 normalized_caption, semantic_label, paired_figure_id, now),
            )
            row: dict[str, Any] = {
                "id": tbl_id, "document_id": document_id, "element_id": element_id,
                "ordinal": ordinal, "table_index": table_index, "caption": caption,
                "normalized_caption": normalized_caption,
                "semantic_label": semantic_label,
                "paired_figure_id": paired_figure_id,
                # 42d398a5 — always present (None until confirmed via
                # set_table_caption_link) so this manually-constructed row
                # matches the shape of a row re-fetched from the DB (which
                # always carries every _TABLE_COLUMNS key).
                "caption_element_id": None,
                "created_at": now,
            }
            if suggested_figure_id is not None:
                row["suggested_figure_id"] = suggested_figure_id
            inserted.append(row)
            pool.append(row)

            if best_match is not None and best_score >= dedup_threshold:
                near_duplicates.append({
                    "table_id": tbl_id,
                    "matched_id": best_match.get("id"),
                    "matched_caption": best_match.get("normalized_caption"),
                    "score": round(best_score, 4),
                })

        if inserted:
            await self._db.commit()
        return {"inserted": inserted, "near_duplicates": near_duplicates}

    async def add_table(
        self,
        document_id: str,
        table_index: int | None,
        *,
        caption: str | None = None,
        semantic_label: str | None = None,
        paired_figure_id: str | None = None,
        element_id: str | None = None,
    ) -> dict[str, Any]:
        """Index ONE table -- the ``index_table`` MCP tool's primitive.

        Inserts a single table (with the same advisory near-duplicate surfacing
        as :meth:`put_tables`). Returns
        ``{"table": <inserted row, or None if not inserted>,
        "near_duplicates": [...]}``.
        """
        tbl: dict[str, Any] = {
            "table_index": table_index,
            "caption": caption,
            "semantic_label": semantic_label,
            "paired_figure_id": paired_figure_id,
            "element_id": element_id,
        }
        result = await self.put_tables(document_id, [tbl])
        return {
            "table": result["inserted"][0] if result["inserted"] else None,
            "near_duplicates": result["near_duplicates"],
        }

    async def set_table_caption_link(
        self,
        table_id: str,
        caption_element_id: str,
    ) -> dict[str, Any] | None:
        """Durably set the ``caption_element_id`` on an existing doc_tables row.

        42d398a5 — the write primitive for the ``link_table_caption`` MCP tool,
        the table analogue of :meth:`set_figure_caption_link`: confirms (or
        corrects) the durable linkage from a table to its caption paragraph
        element, identified by the stable ``doc_elements.id``. This is the
        backfill mechanism for tables already indexed before the
        ``caption_element_id`` column existed, and the confirmation primitive
        when the advisory suggestion from :meth:`put_tables` surfaces multiple
        candidates.

        Returns the updated table row as a dict, or ``None`` when ``table_id``
        does not resolve to any stored table.
        """
        if not isinstance(table_id, str) or not table_id.strip():
            return None
        async with self._db.execute(
            "SELECT * FROM doc_tables WHERE id = ?", (table_id.strip(),)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        await self._db.execute(
            "UPDATE doc_tables SET caption_element_id = ? WHERE id = ?",
            (caption_element_id, table_id.strip()),
        )
        await self._db.commit()
        async with self._db.execute(
            "SELECT * FROM doc_tables WHERE id = ?", (table_id.strip(),)
        ) as cur:
            updated_row = await cur.fetchone()
        return _row_to_dict(updated_row, _TABLE_COLUMNS)

    # -- orchestrator — 06df6ab3 ---------------------------------------------

    async def reindex_document(
        self,
        project_id: str,
        file_path: str | bytes | bytearray,
        *,
        source: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        """One entry point tying the docx outline, figure/table, and equation
        passes together: outline+figures/tables -> ONE :meth:`put_document` call,
        equations -> a separate :meth:`put_equations` walk (06df6ab3).
        """
        from .docs_intel import document_content_tree  # noqa: PLC0415 — lazy, optional

        tree = document_content_tree(file_path)
        elements = elements_from_docx_content_tree(tree)
        stored_source = source
        if stored_source is None and isinstance(file_path, str):
            stored_source = file_path
        doc = await self.put_document(
            project_id, "docx", elements, source=stored_source, title=title,
        )
        raw_equations = parse_docx_equations(file_path)
        eq_batch = [
            {
                "element_id": eq.get("element_id"),
                "ordinal": eq.get("ordinal"),
                "omml_raw": eq.get("omml_raw"),
                "latex_normalized": _omml_flatten_text(eq.get("omml_raw")),
            }
            for eq in raw_equations
        ]
        equations = await self.put_equations(doc["id"], eq_batch)
        return {
            "document": doc,
            "elements_count": len(elements),
            "equations": equations,
        }

    # -- ID-addressable docx write (f978e588) --------------------------------

    async def _resync_element_text(
        self, document_id: str, para_id: str, new_text: str
    ) -> int:
        """Refresh the ``text`` of any stored element whose ``ref`` == ``para_id``.

        ``doc_elements`` addresses a docx paragraph by its ``w14:paraId`` in the
        ``ref`` column (headings, and figures/tables carry a caption ref instead
        — those never match a paragraph paraId). Only persisted paragraphs
        (headings) have a row; a plain body paragraph has none, so a zero return
        is EXPECTED and honest, not a failure. Returns the number of rows updated.
        """
        async with self._db.execute(
            "SELECT id FROM doc_elements WHERE document_id = ? AND ref = ?",
            (document_id, para_id),
        ) as cur:
            rows = await cur.fetchall()
        ids = [_row_get(r, "id") for r in rows]
        updated = 0
        for el_id in ids:
            if not el_id:
                continue
            await self._db.execute(
                "UPDATE doc_elements SET text = ? WHERE id = ?",
                (new_text, el_id),
            )
            updated += 1
        if updated:
            await self._db.commit()
        return updated

    async def update_paragraph(
        self,
        project_id: str,
        source: str,
        para_id: str,
        new_text_or_runs: str | list[Any],
        *,
        expected_content_hash: str | None = None,
        draft_output_path: str | None = None,
        wave_run_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """ID-addressable docx WRITE — the write counterpart of ``get_element_by_id``.

        Resolves the stored document row for ``(project_id, source)``, opens the
        source .docx (its ``source`` column IS the on-disk path), finds the
        paragraph whose ``w14:paraId`` (``p{index}`` fallback) equals ``para_id``,
        replaces its runs with ``new_text_or_runs``, writes ``word/document.xml``
        back into the ZIP, and re-syncs that element's ``doc_elements`` row so the
        index matches the new text. Targets the paragraph by id ONLY — never by
        text match (fragile text-matching is exactly what this replaces).

        ``new_text_or_runs`` is either a plain string (a single unformatted run)
        OR a list of runs, each a bare string or ``{text, bold?, italic?,
        underline?}`` (basic run formatting is set when provided; the original
        runs' formatting is otherwise dropped — replacement, not merge).

        5988a5bb — three new, entirely OPT-IN keyword parameters; every
        existing caller that omits all of them gets byte-identical behavior
        to before this change:

        * ``expected_content_hash`` — a fail-closed precondition gate. When
          given, it is compared against the source .docx's CURRENT on-disk
          content hash (the exact same hash :func:`_docx_staleness_check`
          already computes) BEFORE anything is read/mutated. A mismatch
          raises ``ValueError`` and ``source_path`` is never touched. This is
          the hardened counterpart of the advisory-only staleness warning
          below: that warning fires AFTER the fact and never blocks; this
          gate blocks BEFORE the fact, for a caller that explicitly opted in
          by supplying its own expected hash. Omitted (the default): behavior
          is exactly the pre-5988a5bb advisory-only path.
        * ``draft_output_path`` / ``wave_run_id`` (both-or-neither; see
          :func:`_resolve_docx_draft_dest`) — when given, the write targets
          the isolated ``draft_output_path`` instead of ``source_path``;
          ``source_path`` is only ever READ, never mutated, in this mode.
          Registers the draft and claims ``para_id`` as this wave's anchor
          via ``meridian.db.docx_merge.open_merge_manifest`` /
          ``declare_merge_anchors`` — real DB-backed coordination, not an
          opaque passthrough — so a second session's draft targeting the
          SAME paragraph in the SAME wave is rejected here rather than
          silently racing. The ``doc_elements`` index is NOT resynced in
          this mode (it tracks the CANONICAL file, which this mode never
          touches) — see :meth:`merge_paragraph_draft` for the counterpart
          that actually promotes a draft's anchor into the canonical file
          and performs that resync.
        * ``session_id`` — required together with the two draft-mode
          parameters (the isolated draft's manifest/anchor rows are keyed on
          a real session identity); unused otherwise.

        Mandatory post-write verification (5988a5bb, part A) now runs after
        every promoted write, draft or direct: the written file is re-read
        FRESH FROM DISK (never the in-memory ``root`` just serialized) and
        the target paragraph's text is confirmed to match ``new_text``
        before this ever reports success. ``_write_docx_transaction``'s own
        structural manifest gate only proves the archive as a whole wasn't
        corrupted — it has no idea what THIS write was supposed to do, so a
        silent no-op or wrong-paragraph write would otherwise sail through
        undetected. The write's own promotion lock is held across this
        verify step and any resulting restore (not just the promotion
        itself, 5988a5bb finding 1), and a verification failure is restored
        from its ``.bak`` backup ONLY when a compare-and-swap check confirms
        no OTHER writer has promoted something newer to the same destination
        in the meantime — see :func:`_safe_restore_after_verification_failure`.
        When that check passes, :class:`DocxPostWriteVerificationError` is
        raised after the (successful or best-effort) restore; when it does
        NOT pass (a genuine concurrent write landed),
        :class:`DocxConcurrentWriteConflictError` is raised instead WITHOUT
        touching the file further, since restoring would destroy the other
        writer's completed work. Neither ever fabricates a silent no-op
        success.

        Returns ``{document_id, para_id, new_text, elements_resynced,
        source_path, manifest_hash, pre_counts, post_counts}`` (draft mode
        additionally carries ``draft_path``, ``wave_run_id``, ``is_draft:
        True`` and omits ``elements_resynced``). Raises ``ValueError`` for an
        unknown document, an unresolvable/missing source path, a stale
        ``expected_content_hash``, a rejected draft claim, or a ``para_id``
        not present in the document; raises
        :class:`AmbiguousParagraphIdError` (827b6bdc, a ``ValueError``
        subclass) when ``para_id`` matches MORE than one paragraph — a
        duplicated native ``w14:paraId`` in the source .docx — instead of
        silently writing whichever match the resolver reached first; raises
        :class:`DocxPostWriteVerificationError` when the promoted write
        cannot be confirmed on disk and it was safe to restore, or
        :class:`DocxConcurrentWriteConflictError` when it could not be
        safely auto-corrected because a different writer's promotion landed
        after this one's — never fabricates a silent no-op success.
        """
        src = source.strip() if isinstance(source, str) else ""
        if not src:
            raise ValueError("source is required")
        if not isinstance(para_id, str) or not para_id.strip():
            raise ValueError("para_id is required")
        para_id = para_id.strip()

        doc_row = await self.get_document(project_id, src)
        if doc_row is None:
            raise ValueError(
                f"no stored document for source={src!r} — ingest_document or "
                "reindex_document it first"
            )
        # 14015718 — an independent document is a standalone snapshot with no live
        # file to write back to. Refuse loudly with a DISTINCT message (never the
        # generic "not found on disk"), before touching the filesystem, so it's
        # never mistaken for a live doc whose file is temporarily missing.
        if _normalize_link_status(doc_row.get("link_status")) == _LINK_STATUS_INDEPENDENT:
            raise ValueError(
                f"document {doc_row['id']} is marked independent (no write-back): "
                "it is a standalone captured snapshot with no live source file — "
                "update_paragraph cannot write into it"
            )
        source_path = doc_row.get("source")
        if not isinstance(source_path, str) or not source_path.strip():
            raise ValueError("stored document has no resolvable source path")
        source_path = source_path.strip()
        if not os.path.isfile(source_path):
            raise ValueError(f"source docx not found on disk: {source_path}")

        # 5988a5bb, part C — both-or-neither draft-mode validation, BEFORE any
        # hashing/reading/mutation. Raises ValueError on any invalid combination.
        draft_dest = _resolve_docx_draft_dest(
            source_path, draft_output_path, wave_run_id, session_id
        )

        # 5988a5bb, part B — opt-in fail-closed precondition-hash gate, BEFORE
        # source_path is read/mutated. Unlike the advisory staleness check
        # below, a failure to even COMPUTE the current hash here is treated as
        # inability to verify the precondition the caller explicitly asked
        # for, so it also fails closed (raises) rather than degrading silently.
        current_content_hash: str | None = None
        if expected_content_hash is not None:
            if not isinstance(expected_content_hash, str) or not expected_content_hash.strip():
                raise ValueError("expected_content_hash must be a non-empty string when provided")
            expected_content_hash = expected_content_hash.strip()
            try:
                current_content_hash = _docx_current_content_hash(source_path)
            except Exception as exc:  # noqa: BLE001 — fail closed on an opted-in gate
                raise ValueError(
                    f"could not verify expected_content_hash against {source_path}: "
                    f"{exc} — refusing to write (fail closed)"
                ) from exc
            if current_content_hash != expected_content_hash:
                raise ValueError(
                    "expected_content_hash mismatch: the source .docx has changed "
                    "on disk since the caller computed its expected hash "
                    f"(expected={expected_content_hash!r}, current={current_content_hash!r}) "
                    f"— refusing to write; {source_path} is untouched"
                )

        # eab6930a — advisory staleness check, computed against the PRE-write
        # file (before _load_docx_xml/_save_docx_xml touch it below). Reuses
        # current_content_hash when part B's gate already computed it.
        stale_warning = await _docx_staleness_check(
            doc_row, source_path, current_hash=current_content_hash
        )

        try:
            raw, root = _load_docx_xml(source_path)
        except KeyError as exc:  # missing word/document.xml part
            raise ValueError(f"not a valid .docx (no document part): {exc}") from exc
        except zipfile.BadZipFile as exc:
            raise ValueError(f"source is not a valid .docx (bad zip): {exc}") from exc

        # Canonical _find_paragraph_by_id returns the bare <w:p> element (or None);
        # update_paragraph only needs the element, not its body-order index.
        p = _find_paragraph_by_id(root, para_id)
        if p is None:
            raise ValueError(f"no paragraph with para_id={para_id!r} in {source_path}")

        runs = _normalize_runs(new_text_or_runs)
        _set_paragraph_runs(p, runs)
        new_text = "".join(r.get("text") or "" for r in runs)

        write_dest = source_path
        if draft_dest is not None:
            from . import db as db_module  # noqa: PLC0415 — local: avoid import cycle at module load

            manifest_open = await db_module.open_merge_manifest(
                self._db, wave_run_id, source_path, session_id, draft_path=draft_dest,
            )
            if not manifest_open.get("opened"):
                raise ValueError(
                    "could not open draft merge manifest for wave "
                    f"{wave_run_id!r} on {source_path!r}: "
                    f"{manifest_open.get('message') or manifest_open.get('reason')}"
                )
            anchors_declared = await db_module.declare_merge_anchors(
                self._db, wave_run_id, source_path, session_id, [para_id],
            )
            if not anchors_declared.get("declared"):
                raise ValueError(
                    f"could not claim para_id={para_id!r} for this wave's draft "
                    f"(wave_run_id={wave_run_id!r}): {anchors_declared.get('conflicts')}"
                )
            write_dest = draft_dest

        # 5988a5bb (finding 1) — hold the SAME dest's promotion lock across
        # stage+promote (_save_docx_xml, which reentrantly acquires it
        # internally) THROUGH the post-write verify and any conditional
        # restore below. This closes the SAME-PROCESS window between
        # promotion and verify/restore completely; see _docx_promotion_lock's
        # module-level comment for why it cannot (and does not need to)
        # close the cross-process window by itself — that's what the
        # compare-and-swap check just below is for.
        with _docx_promotion_lock(write_dest):
            # Canonical _save_docx_xml serializes ``root`` and rewrites only
            # the document part into a copy of the original ZIP (``raw``) at
            # ``write_dest``.
            transaction = _save_docx_xml(raw, root, write_dest)

            # 5988a5bb, part A — mandatory post-write verification: re-reads
            # write_dest FRESH FROM DISK and confirms the target paragraph's
            # text actually landed. A structural-manifest pass alone (above)
            # says nothing about whether THIS edit happened.
            verification_error = _verify_paragraph_write(write_dest, para_id, new_text)
            if verification_error is not None:
                # 5988a5bb (finding 1) — do NOT blindly restore. Check
                # whether write_dest still holds exactly what THIS writer
                # promoted before deciding a restore is safe: a different
                # (cross-process) writer may have already promoted something
                # newer to write_dest since our own promotion, in which case
                # this verification "failure" is a false positive and
                # restoring from OUR backup would destroy THEIR completed,
                # already-promoted write.
                safe_to_restore, restored = _safe_restore_after_verification_failure(
                    write_dest, transaction.get("promoted_sha256"),
                )
                if not safe_to_restore:
                    raise DocxConcurrentWriteConflictError(
                        f"post-write verification failed for para_id={para_id!r} in "
                        f"{write_dest}: {verification_error} — AND a different "
                        "writer's promotion has landed on this file since ours, so "
                        "this verification failure could not be safely "
                        "auto-corrected: restoring from our own backup would "
                        f"destroy that writer's already-promoted work. {write_dest} "
                        "was left untouched, exactly as that other writer left it — "
                        "investigate manually.",
                        manifest={**transaction, "restored": False, "concurrent_write_detected": True},
                    )
                raise DocxPostWriteVerificationError(
                    f"post-write verification failed for para_id={para_id!r} in "
                    f"{write_dest}: {verification_error}"
                    + (
                        " — restored from backup, the file reflects its PRE-write state"
                        if restored
                        else " — WARNING: could not restore from backup (no .bak found "
                        "or restore failed); the file may be left in an unverified state"
                    ),
                    manifest={**transaction, "restored": restored},
                )

        result = {
            "document_id": doc_row["id"],
            "para_id": para_id,
            "new_text": new_text,
            "source_path": source_path,
            # dccc2311 — deterministic identifier of exactly what this write
            # transaction changed (see _docx_manifest_hash).
            "manifest_hash": transaction.get("manifest_hash"),
            # 5988a5bb, part D — surface the full write manifest (previously
            # computed by _write_docx_transaction but discarded here).
            "pre_counts": transaction.get("pre_counts"),
            "post_counts": transaction.get("post_counts"),
        }
        if draft_dest is not None:
            # Draft mode: source_path (canonical) was never touched, so the
            # doc_elements index — which reflects the CANONICAL file — must
            # NOT be resynced against an edit that hasn't been merged yet.
            result["draft_path"] = draft_dest
            result["wave_run_id"] = wave_run_id
            result["is_draft"] = True
        else:
            resynced = await self._resync_element_text(doc_row["id"], para_id, new_text)
            result["elements_resynced"] = resynced
        if stale_warning is not None:
            result["stale_warning"] = stale_warning
        return result

    async def merge_paragraph_draft(
        self,
        project_id: str,
        source: str,
        para_id: str,
        draft_output_path: str,
        wave_run_id: str,
        session_id: str,
        *,
        expected_base_revision: str | None = None,
    ) -> dict[str, Any]:
        """Promote ONE anchor from an isolated wave-scoped draft into the
        canonical .docx (5988a5bb) — the counterpart of ``update_paragraph``'s
        draft-write path.

        Where ``update_paragraph(draft_output_path=..., wave_run_id=...)``
        only ever registers/writes an ISOLATED draft (the document's real
        ``source_path`` stays read-only throughout), this is the ONLY path
        that mutates ``source_path`` for a wave-scoped edit — and only after
        ``meridian.db.docx_merge.check_merge_stale_or_overlap`` confirms the
        caller (a) already holds the wave's serialized merge-owner role
        (``claim_merge_owner`` — this method does NOT claim ownership
        implicitly; it must already be held, matching docx_merge's own
        documented contract that only the merge owner may write into the
        canonical file) and (b) isn't racing a stale revision or an anchor
        another session already merged. Any block raises ``ValueError`` with
        the blocking reason; ``source_path`` is untouched.

        The actual promotion reuses the exact same primitives every other
        doc_store.py docx write goes through: the draft's ``document.xml``
        part is read (via :func:`_load_docx_xml`) and written into
        ``source_path`` via :func:`_save_docx_xml` (whose ``raw`` baseline is
        ``source_path``'s OWN current bytes, freshly read — so any of
        ``source_path``'s OTHER parts that legitimately changed since the
        draft was opened are preserved, only ``document.xml`` is replaced by
        the draft's version) — the same stage → verify → promote transaction,
        the same structural-manifest gate, the same ``.bak`` backup, held
        under ``source_path``'s promotion lock end-to-end (stage+promote
        through verify and any conditional restore, 5988a5bb finding 1).
        Mandatory post-write verification (:func:`_verify_paragraph_write`,
        part A) then re-reads ``source_path`` FRESH FROM DISK and confirms
        ``para_id``'s text now matches the draft's; a mismatch is restored
        from ``source_path``'s own ``.bak`` ONLY when a compare-and-swap
        check (:func:`_safe_restore_after_verification_failure`) confirms no
        other writer has promoted something newer to ``source_path`` since
        this merge's own promotion — when it has,
        :class:`DocxConcurrentWriteConflictError` is raised WITHOUT
        restoring (restoring would destroy that other writer's completed
        work) instead of :class:`DocxPostWriteVerificationError`. Neither
        ever fabricates a false success.

        On success, calls ``record_merge_result`` (idempotent — merging the
        same anchor twice from the same session is a no-op; a genuine race
        for the same anchor by a different session can only ever have one
        winner, and would already have been rejected above by
        ``check_merge_stale_or_overlap``) and resyncs the ``doc_elements``
        index — unlike ``update_paragraph``'s draft-write path, the canonical
        file DID just change.

        Returns ``{document_id, para_id, new_text, elements_resynced,
        source_path, draft_path, manifest_hash, pre_counts, post_counts,
        merge_result}``. Raises :class:`AmbiguousParagraphIdError` (827b6bdc,
        a ``ValueError`` subclass) when ``para_id`` matches more than one
        paragraph in the draft — see :func:`_find_paragraph_by_id`.
        """
        src = source.strip() if isinstance(source, str) else ""
        if not src:
            raise ValueError("source is required")
        if not isinstance(para_id, str) or not para_id.strip():
            raise ValueError("para_id is required")
        para_id = para_id.strip()
        draft_path = (draft_output_path or "").strip()
        if not draft_path:
            raise ValueError("draft_output_path is required")
        wave_id = (wave_run_id or "").strip()
        if not wave_id:
            raise ValueError("wave_run_id is required")
        merge_session_id = (session_id or "").strip()
        if not merge_session_id:
            raise ValueError("session_id is required")

        doc_row = await self.get_document(project_id, src)
        if doc_row is None:
            raise ValueError(
                f"no stored document for source={src!r} — ingest_document or "
                "reindex_document it first"
            )
        if _normalize_link_status(doc_row.get("link_status")) == _LINK_STATUS_INDEPENDENT:
            raise ValueError(
                f"document {doc_row['id']} is marked independent (no write-back): "
                "merge_paragraph_draft cannot write into it"
            )
        source_path = doc_row.get("source")
        if not isinstance(source_path, str) or not source_path.strip():
            raise ValueError("stored document has no resolvable source path")
        source_path = source_path.strip()
        if not os.path.isfile(source_path):
            raise ValueError(f"source docx not found on disk: {source_path}")
        if not os.path.isfile(draft_path):
            raise ValueError(f"draft_output_path not found on disk: {draft_path}")

        from . import db as db_module  # noqa: PLC0415 — local: avoid import cycle at module load

        blocked = await db_module.check_merge_stale_or_overlap(
            self._db, wave_id, source_path, merge_session_id, para_id,
            expected_base_revision=expected_base_revision,
        )
        if blocked is not None:
            raise ValueError(
                f"merge rejected for para_id={para_id!r} on {source_path!r} "
                f"(wave_run_id={wave_id!r}): reason={blocked.get('reason')} "
                f"message={blocked.get('message')}"
            )

        try:
            _, draft_root = _load_docx_xml(draft_path)
        except KeyError as exc:
            raise ValueError(f"draft is not a valid .docx (no document part): {exc}") from exc
        except zipfile.BadZipFile as exc:
            raise ValueError(f"draft is not a valid .docx (bad zip): {exc}") from exc
        draft_p = _find_paragraph_by_id(draft_root, para_id)
        if draft_p is None:
            raise ValueError(f"no paragraph with para_id={para_id!r} in draft {draft_path!r}")
        draft_text = _paragraph_plain_text(draft_p)

        try:
            dest_raw, _ = _load_docx_xml(source_path)
        except KeyError as exc:
            raise ValueError(f"not a valid .docx (no document part): {exc}") from exc
        except zipfile.BadZipFile as exc:
            raise ValueError(f"source is not a valid .docx (bad zip): {exc}") from exc

        # 5988a5bb (finding 1) — same widened-lock treatment as
        # update_paragraph: hold source_path's promotion lock across
        # stage+promote THROUGH verify and any conditional restore, and use
        # the compare-and-swap check before ever restoring — see
        # update_paragraph's identical comment for the full rationale.
        with _docx_promotion_lock(source_path):
            transaction = _save_docx_xml(dest_raw, draft_root, source_path)

            verification_error = _verify_paragraph_write(source_path, para_id, draft_text)
            if verification_error is not None:
                safe_to_restore, restored = _safe_restore_after_verification_failure(
                    source_path, transaction.get("promoted_sha256"),
                )
                if not safe_to_restore:
                    raise DocxConcurrentWriteConflictError(
                        f"post-write verification failed merging para_id={para_id!r} "
                        f"from draft {draft_path!r} into {source_path}: "
                        f"{verification_error} — AND a different writer's promotion "
                        "has landed on this file since ours, so this verification "
                        "failure could not be safely auto-corrected: restoring from "
                        "our own backup would destroy that writer's already-promoted "
                        f"work. {source_path} was left untouched, exactly as that "
                        "other writer left it — investigate manually.",
                        manifest={**transaction, "restored": False, "concurrent_write_detected": True},
                    )
                raise DocxPostWriteVerificationError(
                    f"post-write verification failed merging para_id={para_id!r} "
                    f"from draft {draft_path!r} into {source_path}: {verification_error}"
                    + (
                        " — restored from backup, the file reflects its PRE-merge state"
                        if restored
                        else " — WARNING: could not restore from backup (no .bak found "
                        "or restore failed); the file may be left in an unverified state"
                    ),
                    manifest={**transaction, "restored": restored},
                )

        merge_result = await db_module.record_merge_result(
            self._db, wave_id, source_path, merge_session_id, para_id,
            canonical_revision_after=transaction.get("manifest_hash"),
        )
        resynced = await self._resync_element_text(doc_row["id"], para_id, draft_text)
        return {
            "document_id": doc_row["id"],
            "para_id": para_id,
            "new_text": draft_text,
            "elements_resynced": resynced,
            "source_path": source_path,
            "draft_path": draft_path,
            "manifest_hash": transaction.get("manifest_hash"),
            "pre_counts": transaction.get("pre_counts"),
            "post_counts": transaction.get("post_counts"),
            "merge_result": merge_result,
        }

    async def close(self) -> None:
        """Close the underlying connection (best-effort)."""
        try:
            await self._db.close()
        except Exception:  # noqa: BLE001 — shutdown best-effort
            pass


# ---------------------------------------------------------------------------
# Tier-based backend resolution
# ---------------------------------------------------------------------------

# env override — a full url_or_path that always wins (ops escape hatch / tests).
_DOC_STORE_URL_ENV = "MERIDIAN_DOC_STORE_URL"

# Local sidecar filename, next to the main DB in the data dir.
_SIDECAR_FILENAME = "doc_structure.db"

# Plans that qualify for the cloud-Postgres tier (when a pg url is available).
_CLOUD_PLANS = frozenset({"pro", "admin"})


def _is_pg_url(url: str | None) -> bool:
    return bool(url) and str(url).startswith(("postgresql://", "postgres://"))


def resolve_doc_store_target(
    plan: str | None,
    hosted: bool,
    data_dir: str,
    tenant_pg_url: str | None,
    override_url: str | None = None,
) -> tuple[str, str]:
    """Pick the store backend target. Pure — does not open anything.

    Returns ``(url_or_path, backend_label)`` where ``backend_label`` is one of
    ``"override"``, ``"cloud_pg"``, or ``"local_sqlite"``.

    Rules (in order):

    1. ``override_url`` (``MERIDIAN_DOC_STORE_URL``) always wins.
    2. HOSTED mode AND a ``pro`` / ``admin`` plan AND a cloud Postgres url is
       available → that pg url (structure tables live in the tenant's own
       Postgres). The ``hosted`` gate matters: self-hosted has no billing tier,
       so a self-hosted instance never routes to the "Pro" cloud backend even if
       it somehow presents a pro plan + a pg url — it uses the sidecar (or an
       explicit ``MERIDIAN_DOC_STORE_URL`` override).
    3. otherwise (free / standard, self-hosted, or no pg url) → the local SQLite
       sidecar ``{data_dir}/doc_structure.db``.
    """
    if override_url and str(override_url).strip():
        return override_url.strip(), "override"

    normalized_plan = (plan or "").strip().lower()
    if hosted and normalized_plan in _CLOUD_PLANS and _is_pg_url(tenant_pg_url):
        return tenant_pg_url, "cloud_pg"  # type: ignore[return-value]

    sidecar = os.path.join(data_dir, _SIDECAR_FILENAME)
    return sidecar, "local_sqlite"


# Module-level cache keyed by the resolved target so repeated calls reuse ONE
# connection/store (mirrors _deps._tenant_db_cache). Keyed on the resolved
# url_or_path — distinct tenants/pg urls and the local sidecar never collide.
_doc_store_cache: dict[str, DocStructureStore] = {}

# Per-target provisioning lock — closes the SAME check-then-act race
# _deps._open_tenant_db_by_id had (confirmed live in production: orphaned
# AsyncConnectionPool instances leaking up to 10 Postgres connections each,
# "Task was destroyed but it is pending!" in server logs, pool counter
# climbing over days). Without this, two concurrent open_doc_store_for()
# calls for the SAME not-yet-cached target (a tenant's Postgres URL, for
# pro/admin plans) both pass the cache-miss check below, both reach
# `await db_module.init_db(target)`, and each opens its own connection/pool.
# Whichever finishes last wins the cache write; the other is orphaned with
# no reference anywhere and never closed. Mirrors the fix already applied to
# _deps._tenant_db_cache and the pre-existing pattern in
# routes/tunnel.py's _tunnel_mcp_session_locks.
_doc_store_locks: dict[str, asyncio.Lock] = {}


def _reset_doc_store_cache() -> None:
    """Clear the module-level store cache WITHOUT closing (used by tests)."""
    _doc_store_cache.clear()


async def close_all_doc_stores() -> None:
    """Close every cached store and clear the cache.

    Called at server shutdown. Clearing the cache after closing prevents a
    subsequent startup (same process, e.g. the test suite spinning up multiple
    apps) from handing out an already-closed connection for the same target.
    """
    stores = list(_doc_store_cache.values())
    _doc_store_cache.clear()
    for store in stores:
        try:
            await store.close()
        except Exception:  # noqa: BLE001 — shutdown best-effort
            pass


async def open_doc_store_for(
    *,
    plan: str | None,
    hosted: bool,
    data_dir: str,
    tenant_pg_url: str | None,
    override_url: str | None = None,
) -> DocStructureStore:
    """Resolve the tier, open (or reuse) the backend, return a ready store.

    Opens the connection via :func:`meridian.db.init_db`, wraps it in a
    :class:`DocStructureStore`, ensures the schema, and caches it by resolved
    target so subsequent calls reuse the same connection.
    """
    target, _label = resolve_doc_store_target(
        plan=plan,
        hosted=hosted,
        data_dir=data_dir,
        tenant_pg_url=tenant_pg_url,
        override_url=override_url,
    )
    cached = _doc_store_cache.get(target)
    if cached is not None:
        return cached

    lock = _doc_store_locks.get(target)
    if lock is None:
        lock = asyncio.Lock()
        _doc_store_locks[target] = lock
    async with lock:
        # Double-checked: another waiter may have already provisioned this
        # target's store while we were blocked on the lock above.
        cached = _doc_store_cache.get(target)
        if cached is not None:
            return cached

        from . import db as db_module  # local import: avoid import cycle at module load

        conn = await db_module.init_db(target)
        store = DocStructureStore(conn)
        await store.ensure_schema()
        _doc_store_cache[target] = store
        return store
