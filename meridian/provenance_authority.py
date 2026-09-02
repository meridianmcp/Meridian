"""PROV-CANONICAL (7d9b8251) -- authority matrix, machine-readable
provenance-contract receipt, and legacy-source classification for
Meridian's provenance/lineage systems.

Why this module exists
-----------------------
A discovery pass for this sprint item inventoried at least ten distinct
provenance-adjacent systems already live in this codebase (event audit log,
typed lineage graph, per-file provenance ledger, composed per-file status,
the canonical typed envelope, artifact identity registry, per-write
classification, run-level receipts, the DOCX integrity gate, and the
code-intel prospecting receipt) and found:

  * At least six mutually-incompatible status vocabularies, two of which
    (``artifact_registry`` vs ``provenance.py``, both in the SAME package)
    directly disagree on their value sets.
  * Inconsistent scope fields across systems (some carry tenant_id, some
    don't; some carry project_id only).
  * No shared, named "operation/idempotency key" concept.
  * No first-class "derived from" pointer usable uniformly.
  * Three unrelated meanings all calling themselves "schema_version" (an
    int gate-schema version, a module-private int, and an envelope-FORMAT
    semver string).
  * No migration/backfill classifier covering ``action_audit_log`` or
    ``research_nodes``/``research_edges`` (only the outputs-ledger ->
    artifact-registry path had one, via
    ``meridian_outputs.artifact_registry.reconcile_legacy_outputs``).
  * No machine-readable statement of WHICH system is authoritative for
    WHICH question -- only inferable from reading module docstrings.

This module closes the LAST TWO of those gaps directly (the authority
matrix and the migration/backfill classifiers) and provides the
machine-readable "provenance contract receipt" the acceptance criteria
calls for, following the same ``schema_version``/``executable``/
``executable_reasons`` shape ``meridian.docx_integrity_gate`` already
established for exactly this kind of composed, capability-gated verdict.
The other gaps (status vocabulary, scope fields, operation key,
derivation pointer, one canonical schema_version meaning) are closed
directly on ``meridian_outputs.research_evidence`` itself (see that
module's own PROV-CANONICAL-tagged docstrings/fields) -- this module
does not re-derive or duplicate any of that here.

Package-boundary note (mirrors ``meridian.docx_integrity_gate`` and
``meridian.capability_contract``'s own documented pattern): ``research_evidence``
lives in ``extensions/meridian-outputs``, an independently installable
extension that is NOT a guaranteed-importable dependency of core
``meridian`` (see that package's own module docstrings: its own tests
import it straight off ``sys.path``, not via a real package install, even
in this repo's own dev environment). This module therefore never imports
``meridian_outputs`` directly -- every status/kind name below is a PLAIN
STRING LITERAL kept in lockstep with ``research_evidence.ResolverStatus``/
``EvidenceKind``'s own ``.value`` strings by convention and cross-checked
by ``tests/test_provenance_authority.py`` (which DOES import
``research_evidence`` off ``sys.path``, the same way the extension's own
test suite does, and asserts the literal sets here match the real enums
whenever that import succeeds).

Everything in this module is a pure function over caller-supplied data
(rows already fetched, capability lists already loaded) -- it never opens
a DB connection, never imports ``aiosqlite``/``psycopg``, and never
queries ``action_audit_log``/``research_nodes``/``research_edges`` itself.
This mirrors ``meridian.research_graph``'s own "intentionally a LEAF"
contract and keeps this module trivially unit-testable without a DB
fixture. Wiring a live self-fetching MCP tool or a ``generate_handoff``
integration point on top of these pure functions is a documented,
deliberately deferred follow-up (see ``docs/`` for this item's own
write-up) -- NOT because it's out of scope, but because
``meridian/handoff.py``, ``meridian/db/research_graph.py``, and
``meridian_outputs/annotate.py`` were all under active, live write-locked
development by a concurrent session at the time this item was
implemented (see this item's own commit message / task log for the
file-claim conflict this discovery surfaced) -- editing them here would
have manufactured a near-certain merge collision on the exact same
"unify ... provenance" surface, for a wiring step that carries only
integration risk and no independent value without this module's
underlying logic existing first.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# The ONE canonical name/meaning for "schema_version" this module contributes
# (gap #6 in this item's discovery brief): an int, matching
# docx_integrity_gate.GATE_SCHEMA_VERSION's and run_manifest's own
# (module-private) _SCHEMA_VERSION's existing convention -- distinct from
# research_evidence.ProvenanceEnvelope.version, which is an envelope-FORMAT
# semver STRING, not a payload schema version.
# ---------------------------------------------------------------------------

PROVENANCE_CONTRACT_SCHEMA_VERSION = 1
AUTHORITY_MATRIX_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Plain-string mirrors of research_evidence.ResolverStatus / EvidenceKind's
# real .value strings -- see module docstring for why these are literals,
# not an import, and how they're kept honest (test_provenance_authority.py).
# ---------------------------------------------------------------------------

RESOLVER_STATUS_VALUES: frozenset[str] = frozenset({
    "verified", "stale", "held", "ambiguous", "unavailable", "degraded",
    "pending_retry", "failed",
})

EVIDENCE_KIND_VALUES: frozenset[str] = frozenset({
    "claim", "source", "citation", "dataset", "code", "run", "output",
    "figure", "table", "document", "review",
})


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Authority matrix -- machine-readable "who is authoritative for what"
# (discovery-brief gap #9: today this is only inferable from reading module
# docstrings). Each entry is deliberately small and flat so it can be
# rendered, diffed, or queried without a schema of its own.
# ---------------------------------------------------------------------------

AUTHORITY_MATRIX: "tuple[dict[str, str], ...]" = (
    {
        "question": "What did Meridian actually DO, in what order, ever?",
        "authoritative_system": "meridian.db (action_audit_log table)",
        "rationale": (
            "Append-only; no UPDATE/DELETE helper exists anywhere in the "
            "codebase for this table -- the literal, unedited event history."
        ),
    },
    {
        "question": (
            "What is the CURRENT state of one identified research node/edge, "
            "including its full revision history?"
        ),
        "authoritative_system": "meridian.db.research_graph (research_nodes/research_edges)",
        "rationale": (
            "Identity-key + monotonic seq with explicit active/superseded "
            "revisioning; replace_node_revision is the only atomic supersede "
            "path -- both rows coexist as an audit trail, never overwritten."
        ),
    },
    {
        "question": "What is the LATEST per-path provenance record Outputs itself knows about?",
        "authoritative_system": "meridian_outputs.annotate (provenance_ledger.json)",
        "rationale": (
            "Path-keyed, intentionally overwrite-on-write by design -- the "
            "CURRENT-only view, never a history. Not authoritative for "
            "\"what did this path look like before\"."
        ),
    },
    {
        "question": "Given a path, what SINGLE composed provenance verdict should a caller read?",
        "authoritative_system": "meridian_outputs.provenance_status.get_provenance_status",
        "rationale": (
            "Already ranks annotate + outputs_local + fingerprint staleness "
            "into one 7-way status -- never re-derive this ranking elsewhere."
        ),
    },
    {
        "question": (
            "What durable, content-hash-anchored identity does one artifact "
            "have, independent of where it currently lives on disk?"
        ),
        "authoritative_system": "meridian_outputs.artifact_registry",
        "rationale": (
            "The only durable, relocation-safe identity store; "
            "research_evidence's own module docstring names this module as "
            "owning identity resolution, not itself."
        ),
    },
    {
        "question": (
            "What is the canonical, lossless, typed representation of one "
            "evidence graph (records+links) for JSON/XML/Markdown projection?"
        ),
        "authoritative_system": "meridian_outputs.research_evidence.ProvenanceEnvelope",
        "rationale": (
            "The only lossless, round-trippable typed model in this "
            "codebase; Markdown is a read-only PROJECTION of it, never "
            "parsed back -- it can never become the source of truth."
        ),
    },
    {
        "question": "Did one Outputs run finish cleanly, and what did it produce?",
        "authoritative_system": "meridian_outputs.run_manifest",
        "rationale": (
            "The run-scoped, idempotent receipt (manifest_hash identity); "
            "composes (never reimplements) fingerprint/artifact_registry/"
            "outputs_local/research_evidence."
        ),
    },
    {
        "question": "Is a given DOCX artifact/handoff structurally/render-safe to treat as complete?",
        "authoritative_system": "meridian.docx_integrity_gate",
        "rationale": (
            "Composes render/equation-style/provenance-manifest checks into "
            "one executable/executable_reasons verdict; this module's own "
            "receipt shape is modeled directly on it."
        ),
    },
    {
        "question": "Did a code-intel prospecting tool actually get called before this edit?",
        "authoritative_system": (
            "meridian.code_intel_receipt (action_audit_log rows keyed by "
            "RECEIPT_EVENT_TYPE='code_intel_prospect_receipt')"
        ),
        "rationale": (
            "The one durable, server-side receipt mechanism for "
            "prospecting; complete_sprint_item checks for its presence only "
            "for a project that has opted in via a capability manifest."
        ),
    },
    {
        "question": (
            "What did a specific WRITE's per-write classification resolve "
            "to (RESOLVED/ORPHANED/HASH_MISMATCH/UNRESOLVED)?"
        ),
        "authoritative_system": "meridian_outputs.provenance.bind_artifact_provenance",
        "rationale": (
            "A narrower, per-write, path-first resolver -- NOT the same "
            "status set as artifact_registry's own RESOLVED/AMBIGUOUS/"
            "HASH_MISMATCH/UNRESOLVED/ORPHANED despite living in the same "
            "package; see this item's discovery brief gap #1 for the full "
            "disagreement. Authoritative only for the per-write question, "
            "never for durable artifact identity (that's artifact_registry)."
        ),
    },
)


def authoritative_system_for(question: str) -> "str | None":
    """Exact-match lookup: the ``authoritative_system`` string for
    ``question`` if it appears verbatim in :data:`AUTHORITY_MATRIX`, else
    ``None``. Exact-match only (never fuzzy) -- a caller needing a new
    question answered should add a new, explicit row rather than rely on
    this function guessing which existing row is "close enough"."""
    for row in AUTHORITY_MATRIX:
        if row["question"] == question:
            return row["authoritative_system"]
    return None


# ---------------------------------------------------------------------------
# Provenance-contract capability evaluation -- the concrete, FIRST
# implementation of the generic "is this executable" fallback-chain contract
# AGENTS.md documents (649e095f) as a "design contract, ahead of
# implementation" for meridian.capability_manifest, applied here to this
# item's own opt-in capability. Mirrors code_intel_prospecting's own
# established rollout convention exactly (AGENTS.md a8c0f3b7): a project
# with NO matching capability entry declared sees ZERO behavior change.
# ---------------------------------------------------------------------------

#: The well-known capability id a project opts into to make this item's
#: receipt meaningfully enforced (vs. purely advisory). Passed as the ``id``
#: field of one entry in a project's capability manifest
#: (``meridian.capability_manifest`` -- that module needs no changes to
#: support this: capability ids are free-form strings, validated generically,
#: not a hardcoded enum).
PROVENANCE_CONTRACT_CAPABILITY_ID = "provenance_contract_receipt"


def _first_available_tool(
    candidates: "list[str]", available: "set[str] | None",
) -> "str | None":
    """First entry of ``candidates`` present in ``available``, honoring
    ``fallback_chain`` ORDER (AGENTS.md: "Fallbacks are tried in
    fallback_chain order; only exhausting the chain ... counts as no
    available tool"). ``available=None`` means "no live tool-inventory
    signal at all" -- returns ``None`` unconditionally (fail-closed: an
    unknown availability state is never treated as "assume available")."""
    if available is None:
        return None
    for candidate in candidates:
        if candidate in available:
            return candidate
    return None


def evaluate_provenance_contract_capability(
    capabilities: "list[dict[str, Any]] | None",
    *,
    available_tools: "set[str] | None" = None,
    capability_id: str = PROVENANCE_CONTRACT_CAPABILITY_ID,
) -> "dict[str, Any]":
    """Evaluate whether the canonical provenance-contract receipt is
    currently satisfiable for a project's declared capability manifest.

    ``capabilities`` -- a project's normalized capability list (the shape
    ``meridian.capability_manifest.normalize_manifest`` returns / a
    ``get_capability_manifest`` response's ``manifest`` field). ``None`` or
    ``[]`` (a project that never declared this capability) returns
    ``configured=False, executable=True`` -- the "zero behavior change for
    an ordinary project" contract every capability-gated feature in this
    codebase follows.

    ``available_tools`` -- the live tool-inventory signal (a set of tool/
    server names known to be reachable right now). ``None`` means "caller
    has no live signal" -- fails closed to "not satisfied" for a
    ``required`` capability (never optimistically assumed available).

    Returns a dict with ``configured``, ``capability_id``,
    ``availability_policy``, ``satisfied``, ``tool_used``, ``executable``,
    and ``executable_reasons`` -- ``executable``/``executable_reasons``
    match the exact field names/semantics
    ``meridian.docx_integrity_gate.build_docx_integrity_gate`` and
    ``meridian.capability_contract.build_capability_contract`` already use
    for this soft ready/executable pattern, so a caller composing multiple
    gates can treat them uniformly.
    """
    cap = None
    for entry in capabilities or []:
        if isinstance(entry, dict) and entry.get("id") == capability_id:
            cap = entry
            break

    if cap is None:
        return {
            "configured": False,
            "capability_id": capability_id,
            "availability_policy": None,
            "satisfied": None,
            "tool_used": None,
            "executable": True,
            "executable_reasons": [],
        }

    required_tools = list(cap.get("required_tools") or [])
    fallback_chain = list(cap.get("fallback_chain") or [])
    policy = (cap.get("availability_policy") or "required").strip().lower()

    tool_used = _first_available_tool(required_tools + fallback_chain, available_tools)
    satisfied = tool_used is not None

    executable = True
    reasons: "list[str]" = []
    if not satisfied:
        if policy == "required":
            executable = False
            reasons.append(
                f"provenance-contract capability {capability_id!r} is "
                f"required but none of {required_tools + fallback_chain} are "
                "available (fallback_chain exhausted, or no live tool "
                "inventory signal was supplied)"
            )
        else:
            reasons.append(
                f"provenance-contract capability {capability_id!r} is "
                f"{policy!r} and currently unavailable -- degrading, not "
                "blocking (availability_policy is optional/degraded_ok)"
            )

    return {
        "configured": True,
        "capability_id": capability_id,
        "availability_policy": policy,
        "satisfied": satisfied,
        "tool_used": tool_used,
        "executable": executable,
        "executable_reasons": reasons,
    }


def build_provenance_contract_receipt(
    *,
    capabilities: "list[dict[str, Any]] | None" = None,
    available_tools: "set[str] | None" = None,
    evidence_summaries: "list[dict[str, Any]] | None" = None,
) -> "dict[str, Any]":
    """Build the machine-readable provenance-contract receipt: the
    acceptance-criteria "receipt" this item asks for, following
    ``docx_integrity_gate.build_docx_integrity_gate``'s exact precedent
    (``schema_version``/``executable``/``executable_reasons`` plus a
    per-item detail list) rather than inventing a new shape.

    ``evidence_summaries`` -- zero or more
    ``research_evidence.evidence_status_summary(envelope)``-shaped dicts
    (plain dicts already -- no import needed here; see module docstring).
    Each summary's ``status_counts`` is inspected for ``failed``/
    ``pending_retry`` counts, surfaced for visibility. A non-zero
    ``failed`` count is reported in ``executable_reasons`` but does NOT by
    itself flip ``executable`` to ``False`` -- mirroring
    ``docx_integrity_gate``'s own "unresolved is informational unless the
    covering item/capability requires it" discipline; a caller wanting a
    hard fail on any FAILED record should check
    ``failed_record_count`` itself.

    Never raises: a malformed entry in ``evidence_summaries`` is skipped,
    never allowed to break receipt construction (matches this codebase's
    "an enrichment convenience must never break the mandatory path"
    convention -- see ``docx_integrity_gate``'s own docstring for the same
    rule stated explicitly).
    """
    capability_eval = evaluate_provenance_contract_capability(
        capabilities, available_tools=available_tools,
    )

    checked_envelopes: "list[dict[str, Any]]" = []
    failed_total = 0
    pending_retry_total = 0
    for summary in evidence_summaries or []:
        if not isinstance(summary, dict):
            continue
        counts = summary.get("status_counts")
        counts = counts if isinstance(counts, dict) else {}
        failed = int(counts.get("failed", 0) or 0)
        pending_retry = int(counts.get("pending_retry", 0) or 0)
        failed_total += failed
        pending_retry_total += pending_retry
        checked_envelopes.append({
            "envelope_id": summary.get("envelope_id"),
            "failed": failed,
            "pending_retry": pending_retry,
            "record_count": summary.get("record_count"),
            "authoritative_record_count": summary.get("authoritative_record_count"),
        })

    executable = capability_eval["executable"]
    executable_reasons = list(capability_eval["executable_reasons"])
    if failed_total:
        executable_reasons.append(
            f"{failed_total} evidence record(s) across {len(checked_envelopes)} "
            "envelope(s) carry ResolverStatus.FAILED"
        )

    return {
        "schema_version": PROVENANCE_CONTRACT_SCHEMA_VERSION,
        "capability": capability_eval,
        "envelopes_checked": len(checked_envelopes),
        "checked_envelopes": checked_envelopes,
        "failed_record_count": failed_total,
        "pending_retry_record_count": pending_retry_total,
        "executable": executable,
        "executable_reasons": executable_reasons,
        "generated_at": _utcnow_iso(),
    }


# ---------------------------------------------------------------------------
# Legacy-source classification (discovery-brief gap #8): generalizes
# meridian_outputs.artifact_registry.reconcile_legacy_outputs's own report
# shape (scanned / would-register / ambiguous / errors / skipped) to the two
# sources that had NO such classifier at all: action_audit_log rows and
# research_graph nodes. Classification only -- there is no destination
# canonical STORE to write into yet (unlike reconcile_legacy_outputs, which
# writes into a real artifact_registry), so every result here reports what
# the mapping WOULD be; wiring an actual write-path once a canonical event/
# lineage store exists is deliberately out of THIS item's scope (see module
# docstring's "deliberately deferred follow-up" note).
# ---------------------------------------------------------------------------

#: Real, grounded ``action_audit_log.event_type`` values (found via a
#: repo-wide search of every ``record_action_audit_event(...)`` call site --
#: see this item's own commit for the exact grep) that represent a
#: completed-or-overridden VERIFICATION RECEIPT against a research
#: artifact -- i.e. genuinely IN SCOPE for this provenance/evidence
#: classifier. Every other real event_type this codebase writes today
#: (``cross_project_quarantine``, ``manual_issue_screening_enabled``,
#: ``sprint_item_stale_claim_reconciled``, ``velocity_anomaly``,
#: ``release_transaction_state``, etc.) is a GOVERNANCE/OPERATIONAL event,
#: not evidence of an artifact's correctness -- classifying those as
#: research evidence would manufacture a false fit, so they are correctly
#: reported under ``out_of_scope`` below, never forced into
#: ``would_migrate``/``ambiguous``.
_ACTION_AUDIT_EVIDENCE_EVENT_MAP: "dict[str, tuple[str, str, str]]" = {
    # event_type -> (candidate EvidenceKind value, candidate ResolverStatus
    # value, rationale)
    "code_intel_prospect_receipt": (
        "code", "verified",
        "a completed, hash-bound code-intel prospecting receipt "
        "(meridian.code_intel_receipt.record_prospect_receipt)",
    ),
    "code_intel_receipt_override": (
        "code", "degraded",
        "an explicit, audited override of a missing/failed code-intel "
        "receipt check -- usable but confirmed imperfect, not a clean pass",
    ),
    "resource_lock_granularity_receipt": (
        "code", "verified",
        "a completed resource-lock-granularity receipt",
    ),
    "sprint_item_test_run_receipt_override": (
        "run", "degraded",
        "an explicit, audited override of a missing/failed test-run receipt check",
    ),
    "sprint_item_strict_evidence_override": (
        "claim", "degraded",
        "an explicit, audited override of the strict sprint-item evidence gate",
    ),
    "discovery_scope_override": (
        "code", "degraded",
        "an explicit, audited override of a code-discovery scope check",
    ),
    "sprint_item_merge_approval_override": (
        "run", "degraded",
        "an explicit, audited override of a merge-approval gate",
    ),
}


def classify_action_audit_log_rows(
    rows: "list[dict[str, Any]] | None", *, dry_run: bool = True,
) -> "dict[str, Any]":
    """Classify ``action_audit_log`` rows against the canonical evidence
    model. Pure function over already-fetched rows (see module docstring
    for why this module never queries the DB itself).

    Each ``row`` is expected to carry at least ``id``/``event_type``
    (matching the real ``action_audit_log`` schema in
    ``meridian.db.migrations._migrate_action_audit_log_table``); a row
    missing either goes to ``skipped_unclassifiable``.

    Returns ``{"source", "dry_run", "scanned", "would_migrate",
    "already_canonical", "ambiguous", "out_of_scope",
    "skipped_unclassifiable", "errors"}`` -- mirrors
    ``reconcile_legacy_outputs``'s field names where a direct analogue
    exists (``scanned``/``ambiguous``/``errors``); ``already_canonical``
    is always empty today (no action_audit_log row has ever been bridged
    into the canonical model before this item), kept as a field for
    forward-compatibility with a future write-path. ``out_of_scope`` is a
    field this classifier adds beyond ``reconcile_legacy_outputs``'s own
    shape: a governance/operational event_type is not a classification
    FAILURE, it is correctly not evidence at all -- conflating the two
    would make the report unable to distinguish "we don't yet know how to
    classify this" from "this was never a candidate."
    """
    scanned = 0
    would_migrate: "list[dict[str, Any]]" = []
    ambiguous: "list[dict[str, Any]]" = []
    out_of_scope: "list[dict[str, Any]]" = []
    skipped_unclassifiable: "list[dict[str, Any]]" = []
    errors: "list[dict[str, Any]]" = []

    for row in rows or []:
        scanned += 1
        try:
            if not isinstance(row, dict):
                skipped_unclassifiable.append({"row": row, "reason": "row is not an object"})
                continue
            row_id = row.get("id")
            event_type = row.get("event_type")
            if not row_id or not event_type:
                skipped_unclassifiable.append(
                    {"row": row, "reason": "missing required id/event_type"}
                )
                continue
            mapping = _ACTION_AUDIT_EVIDENCE_EVENT_MAP.get(event_type)
            if mapping is None:
                out_of_scope.append({
                    "id": row_id,
                    "event_type": event_type,
                    "reason": (
                        "not a research-artifact evidence event -- a "
                        "governance/operational action_audit_log event_type, "
                        "out of scope for this provenance/evidence classifier"
                    ),
                })
                continue
            kind, status, rationale = mapping
            would_migrate.append({
                "id": row_id,
                "event_type": event_type,
                "candidate_evidence_kind": kind,
                "candidate_resolver_status": status,
                "rationale": rationale,
            })
        except Exception as exc:  # noqa: BLE001 -- one bad row must never break the scan
            errors.append({"row": row, "reason": str(exc)})

    return {
        "source": "action_audit_log",
        "dry_run": dry_run,
        "scanned": scanned,
        "already_canonical": [],
        "would_migrate": would_migrate,
        "ambiguous": ambiguous,
        "out_of_scope": out_of_scope,
        "skipped_unclassifiable": skipped_unclassifiable,
        "errors": errors,
    }


def classify_research_graph_nodes(
    nodes: "list[dict[str, Any]] | None", *, dry_run: bool = True,
) -> "dict[str, Any]":
    """Classify ``research_nodes`` rows against the canonical evidence
    model. Pure function over already-fetched rows (typically
    ``meridian.db.research_graph``'s own row shape: ``id``,
    ``identity_key``, ``node_type``, ``status`` in
    ``{"active", "superseded"}`` -- see that module's docstring).

    Mapping rationale (see this item's discovery brief gap #1/#8):
    ``status == "superseded"`` maps CLEANLY to ``ResolverStatus.STALE`` --
    that is exactly what superseded means, no ambiguity. ``status ==
    "active"`` does NOT map cleanly to ``VERIFIED``: research_graph's
    active/superseded axis is about REVISION CURRENCY, not independent
    confirmation of correctness -- claiming VERIFIED for every active row
    would manufacture false confidence this classifier has no basis for.
    Active rows are therefore reported under ``would_migrate`` with
    ``candidate_resolver_status="ambiguous"`` and an explicit rationale,
    never silently upgraded.

    Returns the same field shape as :func:`classify_action_audit_log_rows`
    (``out_of_scope`` is always empty here -- every well-formed
    research_graph node IS in scope for this classifier, unlike
    action_audit_log's mixed governance/evidence event stream).
    """
    scanned = 0
    would_migrate: "list[dict[str, Any]]" = []
    ambiguous: "list[dict[str, Any]]" = []
    skipped_unclassifiable: "list[dict[str, Any]]" = []
    errors: "list[dict[str, Any]]" = []

    for node in nodes or []:
        scanned += 1
        try:
            if not isinstance(node, dict):
                skipped_unclassifiable.append({"node": node, "reason": "node is not an object"})
                continue
            node_id = node.get("id")
            identity_key = node.get("identity_key")
            node_type = node.get("node_type")
            status = node.get("status")
            if not node_id or not identity_key or not node_type:
                skipped_unclassifiable.append(
                    {"node": node, "reason": "missing required id/identity_key/node_type"}
                )
                continue
            if status == "superseded":
                would_migrate.append({
                    "node_id": node_id,
                    "identity_key": identity_key,
                    "node_type": node_type,
                    "candidate_resolver_status": "stale",
                    "rationale": (
                        "superseded research-graph revision -- a newer row "
                        "already replaced it"
                    ),
                })
            elif status == "active":
                would_migrate.append({
                    "node_id": node_id,
                    "identity_key": identity_key,
                    "node_type": node_type,
                    "candidate_resolver_status": "ambiguous",
                    "rationale": (
                        "active research-graph revision carries no "
                        "independent confidence signal of its own -- cannot "
                        "be classified verified without a caller-supplied "
                        "verification step"
                    ),
                })
            else:
                ambiguous.append({
                    "node": node,
                    "reason": f"unrecognized research_graph status {status!r}",
                })
        except Exception as exc:  # noqa: BLE001
            errors.append({"node": node, "reason": str(exc)})

    return {
        "source": "research_graph_nodes",
        "dry_run": dry_run,
        "scanned": scanned,
        "already_canonical": [],
        "would_migrate": would_migrate,
        "ambiguous": ambiguous,
        "out_of_scope": [],
        "skipped_unclassifiable": skipped_unclassifiable,
        "errors": errors,
    }


def classify_legacy_provenance_sources(
    *,
    action_audit_log_rows: "list[dict[str, Any]] | None" = None,
    research_graph_nodes: "list[dict[str, Any]] | None" = None,
    dry_run: bool = True,
) -> "dict[str, Any]":
    """Convenience wrapper: run both classifiers and return one combined
    report keyed by source name, plus a ``total_scanned`` roll-up. Either
    argument may be omitted (defaults to an empty scan of that source,
    never an error) -- a caller with only ONE of the two row sets on hand
    still gets a valid, partial report.
    """
    action_audit = classify_action_audit_log_rows(action_audit_log_rows, dry_run=dry_run)
    research_nodes = classify_research_graph_nodes(research_graph_nodes, dry_run=dry_run)
    return {
        "schema_version": AUTHORITY_MATRIX_SCHEMA_VERSION,
        "dry_run": dry_run,
        "action_audit_log": action_audit,
        "research_graph_nodes": research_nodes,
        "total_scanned": action_audit["scanned"] + research_nodes["scanned"],
        "generated_at": _utcnow_iso(),
    }
