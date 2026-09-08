"""81b5491b — SCHEMA: paper_strategy_graph — argument-layer nodes and
rhetorical edges.

Meridian's manuscript/paper editorial tooling line already has
:mod:`meridian.research_graph` (the EVIDENTIARY graph: which citation/code/
run/output backs which claim, for provenance) and ``decision_evidence`` (one
pinned engineering decision linked to one pointer). Neither one answers the
question an editor or a co-author actually asks while drafting a paper's
argument: "what is this paper's rhetorical STRATEGY — which claims support
the thesis, which objections are being pre-empted, which limitations are
being conceded, and has anyone signed off on framing it this way." This
module is the closed-vocabulary layer for that: the RHETORICAL structure of
the argument itself, not the evidence trail behind it.

This module is intentionally a LEAF (no ``aiosqlite``/DB import), mirroring
:mod:`meridian.research_graph`'s own "no opinion on how a caller obtained
data" contract. It owns:

* :data:`NODE_TYPES` — the closed set of argument-layer node kinds, loosely
  modeled on Toulmin's argumentation layout (claim / data / warrant /
  qualifier / rebuttal) adapted to how a paper's argument is actually
  discussed in an editorial pass.
* :data:`EDGE_TYPES` — the closed set of rhetorical relations connecting
  those nodes.
* :data:`EDGE_DIRECTIONALITY` — documents which way each edge kind points,
  so callers and the persistence layer (:mod:`meridian.db.paper_strategy_graph`)
  agree on meaning without re-deriving it (mirrors
  ``meridian.research_graph.EDGE_DIRECTIONALITY`` exactly).
* :data:`NODE_STATUSES` — the node lifecycle, including the human-approval
  gate (``draft`` -> ``approved``/``rejected``; any edit past ``draft``
  supersedes into a new version — see the persistence module's docstring for
  the full versioning contract).
* :func:`validate_node_type` / :func:`validate_edge_kind` — closed-set
  validation, raising ``ValueError`` on anything else (mirrors
  ``meridian.research_graph.validate_node_type``/``validate_edge_kind``
  exactly, including the case-insensitive normalization).

WHY A SEPARATE GRAPH FROM research_graph
-----------------------------------------

``research_graph`` nodes are typed by SOURCE (a citation, a run, a piece of
code) and its edges assert EVIDENTIARY relationships (``supports``,
``cites``, ``produces``) for provenance/reproducibility purposes — "what
backs this claim." ``paper_strategy_graph`` nodes are typed by RHETORICAL
ROLE (a thesis, a counter-argument, a concession) and its edges assert
ARGUMENTATIVE relationships (``rebuts``, ``concedes``, ``motivates``) for
editorial purposes — "why is the argument shaped this way, and who signed
off on framing it that way." A single manuscript's ``claim`` in
``research_graph`` and a ``claim`` node here can both exist and reference
the same prose without either graph needing to know about the other; a
node here MAY carry a free-text ``document_ref`` pointing at the section it
concerns (see the persistence module), but there is no hard foreign key
into ``research_graph`` or ``doc_store`` — keeping this schema addition
decoupled, matching ``decisions_pinned.code_anchor``'s "coarse free-text
anchor, not a typed pointer" weight class, appropriate for a bare schema
item.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------

#: The nine argument-layer node kinds this graph recognizes.
NODE_TYPES: frozenset[str] = frozenset(
    {
        "thesis",
        "claim",
        "counter_claim",
        "evidence",
        "warrant",
        "rebuttal",
        "concession",
        "motivation",
        "framing_note",
    }
)

#: The closed set of rhetorical edge kinds. See :data:`EDGE_DIRECTIONALITY`
#: for which way each one points — this set alone doesn't encode direction.
EDGE_TYPES: frozenset[str] = frozenset(
    {
        "supports",
        "rebuts",
        "concedes",
        "qualifies",
        "motivates",
        "contrasts",
        "elaborates",
        "restates",
    }
)

#: Human-readable "from -> to" meaning for every edge kind, keyed by the SAME
#: strings as :data:`EDGE_TYPES` — one source of truth so a caller (or a
#: future dashboard) can render a sensible label without hardcoding a second
#: copy of this table. Documentation only, never used to reject a write (the
#: persistence layer only checks node existence + self-loop rejection).
EDGE_DIRECTIONALITY: dict[str, str] = {
    "supports": "evidence/warrant node -> claim/thesis node (backs the target)",
    "rebuts": "rebuttal node -> counter_claim node (defeats/limits the target)",
    "concedes": "concession node -> claim/thesis node (the target concedes this limitation)",
    "qualifies": "node -> node (narrows/hedges the strength or scope of the target)",
    "motivates": "motivation node -> thesis/claim node (the gap that motivates the target)",
    "contrasts": "counter_claim node -> claim/thesis node (sets up the opposition being addressed)",
    "elaborates": "node -> node (generic: expands on / details the target)",
    "restates": "node -> thesis node (a later node loops back and restates the target)",
}

#: Node lifecycle states. ``draft`` is the only status a plain
#: :func:`meridian.db.paper_strategy_graph.create_strategy_node` call ever
#: produces; ``approved``/``rejected`` are the human-approval-gate terminal
#: decisions on a draft (see :func:`meridian.db.paper_strategy_graph.
#: approve_strategy_node` / ``reject_strategy_node``); ``superseded`` marks a
#: row retired by a later version of the same node family (see
#: :func:`meridian.db.paper_strategy_graph.supersede_strategy_node`). Nothing
#: is ever hard-deleted — every row stays queryable via
#: :func:`meridian.db.paper_strategy_graph.list_strategy_node_versions`.
NODE_STATUSES: frozenset[str] = frozenset(
    {"draft", "approved", "rejected", "superseded"}
)


def validate_node_type(raw: object) -> str:
    """Return ``raw`` stripped/lowercased if it's one of :data:`NODE_TYPES`.

    Raises ``ValueError`` naming the full closed set otherwise — mirrors
    ``meridian.research_graph.validate_node_type`` exactly (same
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
