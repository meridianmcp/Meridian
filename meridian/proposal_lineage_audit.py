"""RECONCILE (4eedeef8) -- legacy proposal-predecessor-reference audit and
opt-in migration planning: pure classification logic, no DB access.

Before ``meridian.db.proposal_lineage`` (5a744f81) existed, the ONLY way a
newer proposal could reference an older one was baking a short id into free
text -- most commonly a sentence like "This is a successor proposal to:
<uuid>" in the ``body`` field (the real production example that motivated
this audit is proposal ``1ee11ede-06f2-4455-ae72-0586a92ca360``, whose body
names predecessor ``bacf5c87-ed67-4472-89ab-afb009d826f0`` -- confirmed via
``get_proposal_lineage(bacf5c87-...)`` returning zero links/ancestors/
successors, i.e. the prose reference was never mirrored into the structured
table). Those free-text references are real information, but they are
UNSTRUCTURED and were never validated the way ``link_proposal_lineage``
validates a real edge (existence, tenant match, cycle safety) -- silently
"inferring" them into real ``proposal_lineage`` rows would risk manufacturing
edges the original author never actually asserted with that precision, or
edges that would violate invariants (a cross-tenant link, a cycle) the
typed table exists specifically to prevent.

This module never does that. It only ever CLASSIFIES: given already-fetched
proposal rows (and, optionally, already-fetched existing lineage/evidence
link rows), it reports what a legacy reference LOOKS LIKE it might mean, and
sorts every candidate into an explicit bucket -- never a bare "found N
matches" list a caller might be tempted to bulk-apply. Actually writing a
migrated relation is a SEPARATE, explicit, opt-in step
(``meridian.db.proposal_reconciliation.migrate_legacy_proposal_lineage``)
that requires the caller to hand back the exact ``candidate_key`` of each
item it has reviewed and approved -- an unreviewed candidate is never
applied merely because ``dry_run=False`` was passed. This mirrors the
``dry_run=True``-by-default discipline already established by
``meridian.db.sprint_items.reconcile_stale_claims`` and
``meridian.provenance_authority``'s classify_* functions, but goes one step
further: those two callers apply everything they classify as safe once
``dry_run=False`` (their classifications are deterministic/structural, not
inferred from prose), whereas an inferred predecessor reference is
inherently fuzzy, so this module additionally requires a per-item
``accept`` allow-list before anything from :func:`audit_legacy_proposal_lineage`
can ever be written -- see :func:`plan_legacy_migration`.

Two independent legacy-reference shapes are covered, mirroring
``meridian.provenance_authority.classify_legacy_provenance_sources``'s
"one classifier per source, one combined wrapper" structure:

  * :func:`audit_legacy_proposal_lineage` -- free-text PREDECESSOR
    references (a lineage keyword near a UUID in ``body``/``tags``) not yet
    present in ``proposal_lineage``. Genuinely fuzzy/inferred.
  * :func:`audit_promotion_evidence_backlinks` -- proposals whose
    ``promoted_to_sprint_item_id`` column (a real, structural,
    already-explicit relation -- see ``db.workspace.promote_workspace_proposal``)
    has no matching ``proposal_evidence_links`` row. This is NOT an
    inference: ``promoted_to_sprint_item_id`` already names the exact
    relation; ``link_proposal_evidence`` swallows its own failures during
    promotion (never blocks promotion on a linking hiccup -- see that
    function's docstring), so a proposal promoted before 6cdc5df3 shipped,
    or one whose auto-link call failed, has a real gap here. Because this is
    a backfill of an already-declared fact rather than an inference, it is
    intentionally held to a lighter bar than proposal-lineage below (see
    that function's own docstring for the exact distinction).

Neither classifier reads or writes ``family_id``, ``proposal_events``, or
the ``promoted_to_sprint_item_id`` column itself -- family_id is a plain
compatibility grouping field (two proposals sharing one family_id are NOT,
by that fact alone, evidence of a predecessor relation -- treating "shares a
family_id" as lineage would be exactly the kind of silent inference this
item exists to prevent), and proposal_events / promoted_to_sprint_item_id
keep their exact existing behaviour untouched (mirrors
``proposal_lineage``'s own module docstring on this point).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

RECONCILE_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Part A -- free-text predecessor-reference detection.
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

# Lineage-keyword phrase -> the meridian.db.proposal_lineage.VALID_RELATION_TYPES
# value it most plausibly encodes. This module intentionally does NOT import
# VALID_RELATION_TYPES (it would be the only DB-adjacent import in an
# otherwise DB-free leaf module) -- tests/test_proposal_lineage_migration.py
# cross-checks that every value produced here is a member of that real enum,
# the same "kept in lockstep, cross-checked by tests" convention
# meridian.provenance_authority documents for its own literal mirrors.
#
# Order matters: the FIRST pattern that matches within the context window
# wins (deterministic, documented precedence -- see detect_predecessor_references),
# so a more specific phrase is listed before a more generic one that could
# also match the same text (e.g. "successor ... to" before a bare "to").
_RELATION_KEYWORD_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"duplicates?\s+of", "duplicates"),
    (r"duplicate\s+report\s+of", "duplicates"),
    (r"responds?\s+to", "responds_to"),
    (r"in\s+response\s+to", "responds_to"),
    (r"successor\s+(?:proposal\s+)?to", "supersedes"),
    (r"supersedes?", "supersedes"),
    (r"refines?", "refines"),
    (r"forked?\s+from", "forks"),
    (r"continuation\s+of", "continues"),
    (r"continues?", "continues"),
    (r"based\s+on", "continues"),
)

# How many characters BEFORE a matched UUID to scan for a lineage keyword.
# Generous enough for the real production shape "This is a successor
# proposal to:\n- <uuid>" (keyword precedes the id, separated by a colon and
# a line break). Every keyword phrase in _RELATION_KEYWORD_PATTERNS is a
# PRE-modifier of the id it governs ("supersedes <uuid>", "successor to
# <uuid>", never the reverse) -- searching only backward, and never past an
# earlier UUID match in the same text, is what keeps two nearby id mentions
# from cross-contaminating each other's keyword (see
# detect_predecessor_references's docstring).
_CONTEXT_WINDOW = 160

# Safety bound mirroring proposal_lineage._MAX_LINEAGE_HOPS -- a real
# migration-candidate cycle check only ever needs a handful of hops; this
# only guards against a pathological/corrupted edge set.
_MAX_CYCLE_HOPS = 2000


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def detect_predecessor_references(text: str) -> list[dict[str, Any]]:
    """Scan free text for UUID-shaped ids paired with PRECEDING lineage
    language.

    Returns one entry per (uuid, relation_type) pair actually found --
    ``{"target_id", "relation_type", "matched_keyword", "snippet"}``. A bare
    UUID with NO preceding lineage keyword is never returned: an id mention
    alone is not evidence of a predecessor relation (a proposal can cite
    another id for many reasons -- "see also", an unrelated cross-reference,
    a pasted log line). Keyword immediately or nearly preceding an id, within
    :data:`_CONTEXT_WINDOW` characters, is the only signal this function
    treats as a candidate -- this is the literal "never silently infer"
    boundary.

    Every UUID's look-back window is bounded on BOTH sides: at most
    :data:`_CONTEXT_WINDOW` characters back, and never past the END of the
    PRECEDING UUID match (if any) in the same text -- so two id mentions
    close together can never have their keywords cross-contaminate each
    other (a keyword governing an EARLIER id is never attributed to a LATER
    one just because both fall inside one large window). When more than one
    keyword phrase matches within one UUID's own window, the match whose END
    is CLOSEST to the id wins -- the nearest preceding phrase, not simply the
    first pattern checked -- since a nearer phrase is the more specific,
    intentional modifier of that particular id.

    Never raises: an empty or non-string ``text`` returns ``[]``.
    """
    if not text or not isinstance(text, str):
        return []
    found: list[dict[str, Any]] = []
    prev_end = 0
    for m in _UUID_RE.finditer(text):
        uid = m.group(0).lower()
        window_start = max(prev_end, m.start() - _CONTEXT_WINDOW, 0)
        window = text[window_start:m.start()]
        best_end = -1
        best_pattern: str | None = None
        best_relation: str | None = None
        for pattern, relation_type in _RELATION_KEYWORD_PATTERNS:
            for km in re.finditer(pattern, window, re.IGNORECASE):
                if km.end() > best_end:
                    best_end = km.end()
                    best_pattern = pattern
                    best_relation = relation_type
        if best_relation is not None:
            snippet_start = window_start
            snippet_end = min(len(text), m.end() + 40)
            found.append({
                "target_id": uid,
                "relation_type": best_relation,
                "matched_keyword": best_pattern,
                "snippet": " ".join(text[snippet_start:snippet_end].split())[:300],
            })
        prev_end = m.end()
    return found


def _edges_reach(
    adjacency: dict[str, set[str]], start_id: str, target_id: str,
) -> bool:
    """BFS over an already-built ``from_id -> {to_id, ...}`` adjacency map:
    can ``start_id`` reach ``target_id``? Mirrors
    ``meridian.db.proposal_lineage._lineage_reaches`` exactly (same
    direction/semantics), operating on a plain in-memory graph instead of a
    DB connection so this module stays DB-free."""
    if start_id == target_id:
        return True
    visited = {start_id}
    frontier = [start_id]
    hops = 0
    while frontier and hops < _MAX_CYCLE_HOPS:
        hops += 1
        next_frontier: list[str] = []
        for node in frontier:
            for nxt in adjacency.get(node, ()):
                if nxt == target_id:
                    return True
                if nxt not in visited:
                    visited.add(nxt)
                    next_frontier.append(nxt)
        frontier = next_frontier
    return False


def audit_legacy_proposal_lineage(
    proposals: "list[dict[str, Any]] | None",
    existing_lineage_links_by_proposal: "dict[str, list[dict[str, Any]]] | None" = None,
    *,
    dry_run: bool = True,
) -> "dict[str, Any]":
    """Classify every proposal's ``body``/``tags`` for legacy predecessor
    references not yet present in ``proposal_lineage``.

    ``proposals`` -- already-fetched ``workspace_proposals`` rows (any
    scope/status; the caller decides what set to scan). Each must carry at
    least ``id``; ``tenant_id``/``project_id`` are used for the cross-tenant
    check below when present.

    ``existing_lineage_links_by_proposal`` -- optional map of
    ``proposal_id -> get_proposal_lineage_links(...)``-shaped rows (raw
    ``proposal_lineage`` rows touching that proposal, either endpoint) for
    every proposal in ``proposals``. Used both to skip a reference that is
    already linked, and to build the existing-edge graph the cycle check
    walks. Omitting it (``None``) is equivalent to "no existing lineage
    known" -- every candidate is then checked only against OTHER candidates
    discovered in this same call, never silently treated as cycle-free
    against edges that exist but weren't supplied.

    Every candidate is sorted into exactly one bucket:

      * ``would_migrate`` -- a genuine, validated candidate: keyword+id
        found, target exists among ``proposals``, same tenant, not a
        self-reference, not already linked, and would not close a cycle.
        Each entry carries a ``candidate_key`` (``"{from}:{to}:{relation_type}"``)
        -- the ONLY token :func:`plan_legacy_migration` accepts back.
      * ``already_linked`` -- the exact ``(from, to, relation_type)`` triple
        already exists in ``proposal_lineage`` -- nothing to do.
      * ``blocked_cycle`` -- adding this edge would let the graph reach back
        on itself through EXISTING edges (of any relation type) -- exactly
        what ``link_proposal_lineage`` would reject; never proposed.
      * ``ambiguous`` -- cross-tenant candidate (``link_proposal_lineage``
        would reject it outright) or any other case parseable but unsafe.
      * ``out_of_scope`` -- a self-reference, or a referenced id that does
        not match any proposal in ``proposals`` (could be a different kind
        of entity entirely -- a sprint item, a commit short-hash-style
        reference, a typo -- never assumed to be a proposal id "close
        enough" to migrate blind).
      * ``skipped_unclassifiable`` -- a malformed row (not a dict, or
        missing ``id``).
      * ``errors`` -- one bad row's classification blew up; never aborts
        the rest of the scan.

    A duplicate mention of the same ``(from, to, relation_type)`` triple
    within one proposal's own text (or across ``body`` and ``tags``) is
    de-duplicated to a single candidate.

    Returns ``{"schema_version", "source", "dry_run", "scanned",
    "would_migrate", "already_linked", "blocked_cycle", "ambiguous",
    "out_of_scope", "skipped_unclassifiable", "errors", "generated_at"}``.
    Never raises. An empty/``None`` ``proposals`` returns a valid,
    all-zero/all-empty report -- the "no-op completion when no safe
    migration exists" case is just this report with every bucket empty,
    never an error.
    """
    proposals = proposals or []
    existing_lineage_links_by_proposal = existing_lineage_links_by_proposal or {}

    known_ids = {
        p.get("id") for p in proposals if isinstance(p, dict) and p.get("id")
    }
    proposals_by_id = {
        p.get("id"): p for p in proposals if isinstance(p, dict) and p.get("id")
    }

    # Existing-edge adjacency (from_proposal_id -> {to_proposal_id, ...}),
    # pooled across every relation_type -- mirrors
    # proposal_lineage._lineage_reaches treating all relation types as one
    # graph for cycle purposes.
    adjacency: dict[str, set[str]] = {}
    existing_triples: set[tuple[str, str, str]] = set()
    for links in existing_lineage_links_by_proposal.values():
        for link in links or []:
            frm = link.get("from_proposal_id")
            to = link.get("to_proposal_id")
            rel = link.get("relation_type")
            if frm and to:
                adjacency.setdefault(frm, set()).add(to)
            if frm and to and rel:
                existing_triples.add((frm, to, rel))

    scanned = 0
    would_migrate: list[dict[str, Any]] = []
    already_linked: list[dict[str, Any]] = []
    blocked_cycle: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    out_of_scope: list[dict[str, Any]] = []
    skipped_unclassifiable: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()

    for proposal in proposals:
        scanned += 1
        try:
            if not isinstance(proposal, dict):
                skipped_unclassifiable.append(
                    {"proposal": proposal, "reason": "row is not an object"}
                )
                continue
            pid = proposal.get("id")
            if not pid:
                skipped_unclassifiable.append(
                    {"proposal": proposal, "reason": "missing required id"}
                )
                continue
            text = "\n".join(
                str(proposal.get(field) or "") for field in ("body", "tags")
            )
            candidates = detect_predecessor_references(text)
            if not candidates:
                continue
            proposal_tenant = proposal.get("tenant_id")
            for cand in candidates:
                target_id = cand["target_id"]
                relation_type = cand["relation_type"]
                key = (pid, target_id, relation_type)
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                if target_id == pid:
                    out_of_scope.append({
                        "proposal_id": pid,
                        "target_id": target_id,
                        "relation_type": relation_type,
                        "reason": (
                            "self-reference -- a proposal cannot have a "
                            "lineage relation to itself"
                        ),
                    })
                    continue
                if key in existing_triples:
                    already_linked.append({
                        "proposal_id": pid,
                        "target_id": target_id,
                        "relation_type": relation_type,
                    })
                    continue
                if target_id not in known_ids:
                    out_of_scope.append({
                        "proposal_id": pid,
                        "target_id": target_id,
                        "relation_type": relation_type,
                        "matched_keyword": cand["matched_keyword"],
                        "reason": (
                            "referenced id does not match any proposal in "
                            "this scan -- may be a different kind of entity, "
                            "a proposal outside this scan's scope, or a "
                            "typo; never migrated on a guess"
                        ),
                    })
                    continue
                target_tenant = proposals_by_id[target_id].get("tenant_id")
                if proposal_tenant != target_tenant:
                    ambiguous.append({
                        "proposal_id": pid,
                        "target_id": target_id,
                        "relation_type": relation_type,
                        "reason": (
                            f"cross-tenant candidate ({proposal_tenant!r} vs "
                            f"{target_tenant!r}) -- link_proposal_lineage "
                            "would reject this outright; never proposed"
                        ),
                    })
                    continue
                if _edges_reach(adjacency, target_id, pid):
                    blocked_cycle.append({
                        "proposal_id": pid,
                        "target_id": target_id,
                        "relation_type": relation_type,
                        "reason": (
                            f"'{target_id}' can already reach '{pid}' "
                            "through existing proposal_lineage edges -- "
                            "adding this edge would close a cycle"
                        ),
                    })
                    continue
                candidate_key = f"{pid}:{target_id}:{relation_type}"
                would_migrate.append({
                    "candidate_key": candidate_key,
                    "from_proposal_id": pid,
                    "to_proposal_id": target_id,
                    "relation_type": relation_type,
                    "matched_keyword": cand["matched_keyword"],
                    "snippet": cand["snippet"],
                })
                # A newly-proposed (not-yet-applied) candidate also counts
                # toward the in-memory adjacency so a SECOND candidate
                # discovered later in this same scan that would close a
                # cycle THROUGH this one is caught too, even though neither
                # has been written yet.
                adjacency.setdefault(pid, set()).add(target_id)
        except Exception as exc:  # noqa: BLE001 -- one bad row must never break the scan
            errors.append({"proposal": proposal, "reason": str(exc)})

    return {
        "schema_version": RECONCILE_SCHEMA_VERSION,
        "source": "workspace_proposals_lineage_text",
        "dry_run": dry_run,
        "scanned": scanned,
        "would_migrate": would_migrate,
        "already_linked": already_linked,
        "blocked_cycle": blocked_cycle,
        "ambiguous": ambiguous,
        "out_of_scope": out_of_scope,
        "skipped_unclassifiable": skipped_unclassifiable,
        "errors": errors,
        "generated_at": _utcnow_iso(),
    }


# ---------------------------------------------------------------------------
# Part B -- promotion evidence backlink audit (structural, not inferred).
# ---------------------------------------------------------------------------


def audit_promotion_evidence_backlinks(
    proposals: "list[dict[str, Any]] | None",
    existing_links_by_proposal: "dict[str, list[dict[str, Any]]] | None" = None,
    *,
    dry_run: bool = True,
) -> "dict[str, Any]":
    """Find promoted proposals whose ``promoted_to_sprint_item_id`` has no
    matching ``proposal_evidence_links`` row.

    Unlike :func:`audit_legacy_proposal_lineage`, this is NOT an inference:
    ``promoted_to_sprint_item_id`` already names the exact relation
    explicitly (a proposal was promoted to exactly this sprint item) --
    ``db.workspace.promote_workspace_proposal`` writes the matching
    ``proposal_evidence_links`` row automatically on every promotion, but
    wraps that call in a bare ``try/except`` that swallows ANY failure so a
    linking hiccup never blocks the promotion itself. A proposal promoted
    before 6cdc5df3 shipped that auto-link, or one whose auto-link call
    failed for any reason, is left with the relation recorded in exactly
    ONE place (the single-valued column) instead of two.

    ``proposals`` -- already-fetched ``workspace_proposals`` rows. Only rows
    with a non-empty ``promoted_to_sprint_item_id`` are candidates; every
    other row is skipped without being counted in any bucket (it was never
    a candidate in the first place, not an out-of-scope one).

    ``existing_links_by_proposal`` -- optional map of ``proposal_id ->
    get_proposal_links(...)``-shaped rows (raw ``proposal_evidence_links``
    rows for that proposal) for every candidate proposal.

    Buckets: ``would_migrate`` (candidate_key ``"{proposal_id}:sprint_item:
    {sprint_item_id}"``), ``already_linked``, ``out_of_scope`` (promoted but
    missing ``project_id`` -- ``link_proposal_evidence`` requires one, so
    this cannot be safely backfilled), ``skipped_unclassifiable``, ``errors``.
    Same shape/discipline as Part A otherwise (never raises, no-op on an
    empty input is a valid empty report).
    """
    proposals = proposals or []
    existing_links_by_proposal = existing_links_by_proposal or {}

    scanned = 0
    would_migrate: list[dict[str, Any]] = []
    already_linked: list[dict[str, Any]] = []
    out_of_scope: list[dict[str, Any]] = []
    skipped_unclassifiable: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for proposal in proposals:
        try:
            if not isinstance(proposal, dict):
                skipped_unclassifiable.append(
                    {"proposal": proposal, "reason": "row is not an object"}
                )
                continue
            pid = proposal.get("id")
            if not pid:
                skipped_unclassifiable.append(
                    {"proposal": proposal, "reason": "missing required id"}
                )
                continue
            si_id = proposal.get("promoted_to_sprint_item_id")
            if not si_id:
                continue  # not promoted -- not a candidate, not counted
            scanned += 1
            project_id = proposal.get("project_id")
            if not project_id:
                out_of_scope.append({
                    "proposal_id": pid,
                    "sprint_item_id": si_id,
                    "reason": (
                        "promoted proposal has no project_id recorded -- "
                        "link_proposal_evidence requires one; cannot be "
                        "safely backfilled without guessing a project"
                    ),
                })
                continue
            existing = existing_links_by_proposal.get(pid) or []
            already = any(
                isinstance(link, dict)
                and link.get("entity_type") == "sprint_item"
                and link.get("entity_id") == si_id
                for link in existing
            )
            if already:
                already_linked.append({
                    "proposal_id": pid, "sprint_item_id": si_id,
                })
                continue
            would_migrate.append({
                "candidate_key": f"{pid}:sprint_item:{si_id}",
                "proposal_id": pid,
                "project_id": project_id,
                "entity_type": "sprint_item",
                "entity_id": si_id,
                "rationale": (
                    "promoted_to_sprint_item_id is set but no matching "
                    "proposal_evidence_links row exists -- either promoted "
                    "before 6cdc5df3 shipped the auto-link, or the "
                    "auto-link call failed silently at promotion time"
                ),
            })
        except Exception as exc:  # noqa: BLE001
            errors.append({"proposal": proposal, "reason": str(exc)})

    return {
        "schema_version": RECONCILE_SCHEMA_VERSION,
        "source": "workspace_proposals_promotion_backlink",
        "dry_run": dry_run,
        "scanned": scanned,
        "would_migrate": would_migrate,
        "already_linked": already_linked,
        "out_of_scope": out_of_scope,
        "skipped_unclassifiable": skipped_unclassifiable,
        "errors": errors,
        "generated_at": _utcnow_iso(),
    }


# ---------------------------------------------------------------------------
# Opt-in migration planning -- shared by both audits above.
# ---------------------------------------------------------------------------


def plan_legacy_migration(
    report: "dict[str, Any]", accept: "list[str] | None",
) -> "dict[str, Any]":
    """Resolve an explicit, caller-reviewed ``accept`` list of
    ``candidate_key`` strings against a FRESH audit ``report``'s
    ``would_migrate`` bucket.

    This is the enforcement point for "never silently infer relations":
    ``accept=None`` or ``[]`` plans NOTHING (the safe default -- migration
    is opt-in per item, never "accept everything found"). Every key in
    ``accept`` that matches a live ``would_migrate`` candidate is planned,
    in the order it was found in ``report`` (not the order given in
    ``accept``, so a duplicate/reordered ``accept`` list can't produce
    duplicate or reordered writes). Every key in ``accept`` that does NOT
    match -- because it was never a real candidate, or because a fresh
    audit no longer classifies it as ``would_migrate`` (already applied by
    a concurrent caller, or superseded by a newer edge) -- is reported
    under ``rejected_keys``, never silently applied and never silently
    dropped without being named.

    Returns ``{"to_apply": [...], "rejected_keys": [...],
    "available_candidate_keys": [...]}``. Never raises.
    """
    would_migrate = report.get("would_migrate") or []
    by_key = {c.get("candidate_key"): c for c in would_migrate if c.get("candidate_key")}
    accept_set = set(accept or [])
    to_apply = [c for c in would_migrate if c.get("candidate_key") in accept_set]
    rejected_keys = [k for k in (accept or []) if k not in by_key]
    return {
        "to_apply": to_apply,
        "rejected_keys": rejected_keys,
        "available_candidate_keys": list(by_key.keys()),
    }
