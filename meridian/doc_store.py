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
"""
from __future__ import annotations

import os
import uuid
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable

from .zotero_client import resolve_citation_ref

_log = logging.getLogger(__name__)


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
    "CREATE INDEX IF NOT EXISTS idx_doc_documents_project_source "
    "ON doc_documents (project_id, source)",
    "CREATE INDEX IF NOT EXISTS idx_doc_elements_document_ordinal "
    "ON doc_elements (document_id, ordinal)",
    "CREATE INDEX IF NOT EXISTS idx_doc_edges_project_kind "
    "ON doc_edges (project_id, edge_kind)",
    "CREATE INDEX IF NOT EXISTS idx_doc_edges_source_element "
    "ON doc_edges (source_element_id)",
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
    "content_hash", "element_count", "created_at", "updated_at",
)
_ELEMENT_COLUMNS = (
    "id", "document_id", "parent_id", "ordinal", "level", "kind", "text", "ref",
)
_EDGE_COLUMNS = (
    "id", "project_id", "source_element_id", "edge_kind", "target_kind",
    "target_ref", "target_element_id", "target_document_id", "resolved_at",
    "created_at",
)

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
    para_id}]}``. Each heading becomes a ``kind='heading'`` element carrying its
    ``level``, ``text`` and ``ref`` (the docx ``w14:paraId``). Parent edges are
    inferred by heading-level nesting: a heading attaches under the nearest
    preceding heading of a strictly smaller level (``parent_ordinal``), else it
    is a root. Ordinals are assigned in document order.
    """
    headings = (outline or {}).get("headings") or []
    elements: list[dict[str, Any]] = []
    # stack of (ordinal, level) for the open ancestor chain.
    stack: list[tuple[int, int]] = []
    for ordinal, h in enumerate(headings):
        level = h.get("level")
        lvl = int(level) if isinstance(level, (int, float)) else 1
        while stack and stack[-1][1] >= lvl:
            stack.pop()
        parent_ordinal = stack[-1][0] if stack else None
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
        """Create the store's tables + indexes if absent (idempotent)."""
        for stmt in _SCHEMA_STATEMENTS:
            await self._db.execute(stmt)
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
            "element_count, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                doc_id, project_id, src, doc_type, title, ch,
                len(prepared), created_at, now,
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
            "element_count": len(prepared), "created_at": created_at,
            "updated_at": now,
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

    from . import db as db_module  # local import: avoid import cycle at module load

    conn = await db_module.init_db(target)
    store = DocStructureStore(conn)
    await store.ensure_schema()
    _doc_store_cache[target] = store
    return store
