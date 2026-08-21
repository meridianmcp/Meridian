"""b558892a — the research artifact graph: typed nodes/edges linking claims,
citations, code, runs, outputs, documents, and executor decisions.

Meridian already has several NARROW, single-domain graphs: ``doc_store``'s
``doc_edges`` (intra-document citation edges, one DOCX/LaTeX source at a
time), ``proposal_lineage`` (proposal-to-proposal supersession), and
``decision_evidence`` (one pinned decision to one pointer). None of them
answer the cross-cutting question a research project actually needs:
"what evidence supports this claim, and what document did this output end up
in" — spanning code, registered outputs, DOCX/XML document elements,
citations, and pinned decisions all at once. This module is the durable,
project-scoped, dual-backend (SQLite + Postgres) graph that closes that gap.

This module is intentionally a LEAF: no ``aiosqlite``/DB import, mirroring
``meridian.dependency_graph``'s own "no opinion on how a caller obtained
data" contract. It owns:

* :data:`NODE_TYPES` / :data:`EDGE_TYPES` — the two closed vocabularies.
* :data:`EDGE_DIRECTIONALITY` — documents which way each edge kind points
  (e.g. ``supports`` runs evidence -> claim, never the reverse) so callers
  and the persistence layer (:mod:`meridian.db.research_graph`) agree on
  meaning without re-deriving it.
* :func:`validate_node_type` / :func:`validate_edge_kind` — closed-set
  validation, raising ``ValueError`` on anything else (mirrors
  ``proposal_lineage``'s ``relation_type`` validation).
* Identity-key builders (:func:`code_identity_key`,
  :func:`output_identity_key`, :func:`document_identity_key`,
  :func:`citation_identity_key`, :func:`run_identity_key`,
  :func:`decision_identity_key`, :func:`claim_identity_key`) — one
  canonical, deterministic string per node type so the SAME external thing
  (a file+symbol, a registered output, a DOCX element, a citation key, an
  executor run, a pinned decision, a researcher's claim) always resolves to
  the SAME identity regardless of which caller/extension produced it. This
  is the "preserve source identity" half of the sprint item's acceptance
  criteria; :mod:`meridian.db.research_graph`'s ``revision`` column is the
  "and revision" half — the same identity can have many revision rows over
  time, each an append-only fact, never overwritten in place.

Node identity vs. revision, briefly (full contract lives in
``meridian.db.research_graph``'s module docstring): a node's
``identity_key`` is STABLE across time (e.g. the same file path, the same
registered output, the same DOCX element) while its ``revision`` (a content
hash, a git SHA, a version string — whatever the source type's natural
freshness proof is) changes as the underlying source changes. Two rows can
share an ``identity_key`` at different ``revision`` values; the persistence
layer's ``create_node`` is a pure append (both rows coexist, an audit
trail), while ``replace_node_revision`` is the explicit, atomic
"transactionally replaceable" operation that retires the old row and
activates the new one in one commit — see that module for the full
contract and the pinned decision recorded for this sprint item.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------

#: The seven typed node kinds this graph recognizes — matches the sprint
#: item's acceptance criteria verbatim ("claims, citations, code, runs,
#: outputs, and documents" plus "executor decisions").
NODE_TYPES: frozenset[str] = frozenset(
    {"claim", "citation", "code", "run", "output", "document", "decision"}
)

#: The closed set of typed edge kinds. See :data:`EDGE_DIRECTIONALITY` for
#: which way each one points — this set alone doesn't encode direction.
EDGE_TYPES: frozenset[str] = frozenset(
    {
        "supports",
        "contradicts",
        "evidences",
        "cites",
        "produces",
        "derived_from",
        "documents",
        "implements",
        "references",
    }
)

#: Human-readable "from -> to" meaning for every edge kind, keyed by the
#: SAME strings as :data:`EDGE_TYPES` — kept as one source of truth so a
#: caller (or a future dashboard) can render a sensible label without
#: hardcoding a second copy of this table. Never used to reject a write
#: (the persistence layer only checks node_type/edge_kind membership); this
#: is documentation, not an enforced schema.
EDGE_DIRECTIONALITY: dict[str, str] = {
    "supports": "evidence node -> claim node (the evidence supports the claim)",
    "contradicts": "evidence node -> claim node (the evidence contradicts the claim)",
    "evidences": "evidence node -> claim node (polarity-neutral: bears on the claim)",
    "cites": "citing node (claim/code/document) -> citation node",
    "produces": "run node -> output node (the run produced this output)",
    "derived_from": "node -> source node (generic lineage: this was derived from that)",
    "documents": "artifact node (code/output/run) -> document node (written up / embedded there)",
    "implements": "code node -> claim/decision node (this code implements/addresses it)",
    "references": "generic catch-all: node -> node, no more specific kind applies",
}

#: The edge kinds :func:`meridian.db.research_graph.get_claim_evidence`
#: follows by default — evidence-bearing edges that terminate at a claim.
CLAIM_EVIDENCE_EDGE_KINDS: tuple[str, ...] = ("supports", "contradicts", "evidences")

#: The edge kinds :func:`meridian.db.research_graph.get_artifact_document_lineage`
#: follows by default — the production/embedding chain from an artifact
#: (code/run/output) forward to the document(s) it ends up documented in.
ARTIFACT_DOCUMENT_LINEAGE_EDGE_KINDS: tuple[str, ...] = ("produces", "derived_from", "documents")


def validate_node_type(raw: object) -> str:
    """Return ``raw`` stripped/lowercased if it's one of :data:`NODE_TYPES`.

    Raises ``ValueError`` naming the full closed set otherwise — mirrors
    ``proposal_lineage``'s ``relation_type`` validation exactly (same
    error-message shape, same "reject before any write" contract).
    """
    value = (raw or "").strip().lower() if isinstance(raw, str) else ""
    if value not in NODE_TYPES:
        raise ValueError(
            f"node_type must be one of {sorted(NODE_TYPES)}, got {raw!r}"
        )
    return value


def validate_edge_kind(raw: object) -> str:
    """Return ``raw`` stripped/lowercased if it's one of :data:`EDGE_TYPES`.

    Raises ``ValueError`` naming the full closed set otherwise.
    """
    value = (raw or "").strip().lower() if isinstance(raw, str) else ""
    if value not in EDGE_TYPES:
        raise ValueError(
            f"edge_kind must be one of {sorted(EDGE_TYPES)}, got {raw!r}"
        )
    return value


# ---------------------------------------------------------------------------
# Identity-key builders — one canonical, deterministic string per node type.
#
# These are conveniences, not a hard requirement: meridian.db.research_graph's
# create_node/create_edge accept any non-empty identity_key string. Using the
# builder for a given node_type just means two different callers (e.g. the
# core executor and the meridian-outputs extension) describing the SAME
# underlying source land on the SAME identity_key, so their nodes merge into
# one revision history instead of silently forking into two.
# ---------------------------------------------------------------------------


def code_identity_key(file_path: str, symbol: str | None = None) -> str:
    """``code`` identity: a file path, optionally narrowed to one symbol
    within it (``qualified_name``, matching the ``symbol`` selector type in
    :mod:`meridian.pointers`)."""
    file_path = (file_path or "").strip()
    if not file_path:
        raise ValueError("code_identity_key requires a non-empty file_path")
    symbol = (symbol or "").strip() if isinstance(symbol, str) else ""
    return f"{file_path}::{symbol}" if symbol else file_path


def output_identity_key(*, path: str | None = None, sha256: str | None = None) -> str:
    """``output`` identity: prefer a content fingerprint (``sha256``) when
    given — a registered output is often moved/renamed but its content
    identity is what matters for provenance — falling back to its path.

    Raises ``ValueError`` when neither is given.
    """
    sha256 = (sha256 or "").strip() if isinstance(sha256, str) else ""
    if sha256:
        return f"sha256:{sha256}"
    path = (path or "").strip() if isinstance(path, str) else ""
    if path:
        return path
    raise ValueError("output_identity_key requires at least one of path or sha256")


def document_identity_key(source: str, element_id: str | None = None) -> str:
    """``document`` identity: a document's ``source`` (path/uri), optionally
    narrowed to one structural element within it (a ``doc_store`` element
    id — the ``node_id`` selector type in :mod:`meridian.pointers`)."""
    source = (source or "").strip()
    if not source:
        raise ValueError("document_identity_key requires a non-empty source")
    element_id = (element_id or "").strip() if isinstance(element_id, str) else ""
    return f"{source}::{element_id}" if element_id else source


def citation_identity_key(
    *, zotero_key: str | None = None, doi: str | None = None, raw: str | None = None
) -> str:
    """``citation`` identity: prefer a Zotero key, then a DOI, then a raw
    citation string — the same preference order :mod:`meridian.pointers`'s
    ``zotero_key`` selector implies (a durable reference-manager id beats a
    DOI beats free text). Raises ``ValueError`` when none is given."""
    zotero_key = (zotero_key or "").strip() if isinstance(zotero_key, str) else ""
    if zotero_key:
        return f"zotero:{zotero_key}"
    doi = (doi or "").strip() if isinstance(doi, str) else ""
    if doi:
        return f"doi:{doi}"
    raw = (raw or "").strip() if isinstance(raw, str) else ""
    if raw:
        return raw
    raise ValueError("citation_identity_key requires one of zotero_key, doi, or raw")


def run_identity_key(run_id: str) -> str:
    """``run`` identity: an executor/verification run id, used verbatim."""
    run_id = (run_id or "").strip()
    if not run_id:
        raise ValueError("run_identity_key requires a non-empty run_id")
    return run_id


def decision_identity_key(decision_id: str) -> str:
    """``decision`` identity: a ``decisions_pinned`` row id, used verbatim."""
    decision_id = (decision_id or "").strip()
    if not decision_id:
        raise ValueError("decision_identity_key requires a non-empty decision_id")
    return decision_id


def claim_identity_key(claim_id: str) -> str:
    """``claim`` identity: a caller-supplied stable id/slug for a researcher's
    claim, used verbatim (claims have no pre-existing store of their own to
    derive an identity from — the caller mints one, e.g. a slug or a fresh
    uuid, and reuses it across revisions of the same claim)."""
    claim_id = (claim_id or "").strip()
    if not claim_id:
        raise ValueError("claim_identity_key requires a non-empty claim_id")
    return claim_id
