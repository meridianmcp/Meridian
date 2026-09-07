"""SCHEMA: paper_contract (7c96d41b) — the first-class versioned editorial
intent document for Meridian's manuscript/paper editorial tooling line.

Meridian's sprint-board machinery answers "what should an executor build
next"; a manuscript needs the adjacent, distinct question answered FIRST:
"what is this paper actually trying to say, to whom, and what may an
editorial session not change without a human looking at it." That is the
``paper_contract`` — not a sprint item (not claimable/completable work) and
not a pinned decision (not a single append-only fact) but a small, versioned
document with its own approval lifecycle, closer in shape to
``meridian.profile_contract``'s layered/versioned config or
``meridian.db.profile_layers``'s content-hash + revision counter than to
anything on the sprint board.

This module is the LEAF half of that contract: closed vocabularies and pure
helpers with no DB import — mirroring the split already established by
``meridian.experiment_model`` (pure) vs. ``meridian.db.experiment_model``
(persistence) and ``meridian.research_graph`` (pure) vs.
``meridian.db.research_graph`` (persistence). See
``meridian.db.paper_contract`` for the two-table persistence layer built on
top of this one, and ``meridian.models`` for the Pydantic wire-format
classes (``PaperContract``, ``PaperContractContent``,
``PaperContractRevision``, ...).

VERSIONING / PINNING (the title's "versioned" half)
----------------------------------------------------
A ``paper_contract`` row is a stable identity (``project_id`` +
``paper_key``) that never itself holds editorial content. Every edit to the
editorial intent — scope, audience, required sections, style guide, word
limit, whatever the manuscript needs — mints a brand-new, immutable
``paper_contract_revisions`` row (``revision_number`` = previous max + 1,
never reused, never mutated after creation except for
``superseded_by_revision_id``). This is the exact "immutable revision
ledger" shape already proven by ``meridian.db.profile_layers``
(``profile_layer_revisions``) and the design-only
``TemplateRevisionSnapshot`` in ``meridian.models`` — not a new pattern.

HUMAN APPROVAL GATE (the title's "editorial intent" half)
-----------------------------------------------------------
A freshly created revision starts ``approval_status="pending"`` — proposing
a change to editorial intent (very plausibly from an AI drafting session)
does not make it binding. Only an explicit human approval
(``meridian.db.paper_contract.approve_paper_contract_revision``, which
REQUIRES a non-empty ``approved_by_human_id``) pins that revision as the
contract's ``current_revision_id`` — the one snapshot any editorial session
should treat as authoritative "what this paper is allowed to be" right now.
Approving supersedes the previously-approved revision (if any); a
``rejected`` revision is a dead end (propose a new revision instead of
"unrejecting" one) — see :func:`validate_revision_approval_status` for the
closed vocabulary this rests on.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

#: Lifecycle of the ``paper_contracts`` container row itself. ``draft`` =
#: no revision has ever been approved yet; ``active`` = at least one
#: revision is currently approved and pinned (``current_revision_id`` is
#: set); ``archived`` = the manuscript/contract is retired — set explicitly,
#: independent of whether it ever reached ``active``.
CONTRACT_STATUSES: frozenset[str] = frozenset({"draft", "active", "archived"})

#: Lifecycle of one ``paper_contract_revisions`` row. ``pending`` is the
#: only status a freshly created revision may start in — the human-approval
#: gate is what moves a revision to ``approved`` (see module docstring).
#: ``rejected`` is terminal: propose a new revision rather than trying to
#: revive a rejected one.
REVISION_APPROVAL_STATUSES: frozenset[str] = frozenset({"pending", "approved", "rejected"})


def validate_contract_status(value: object) -> str:
    """Return ``value`` lowercased/stripped if it's one of
    :data:`CONTRACT_STATUSES`. Raises ``ValueError`` naming the full closed
    set otherwise — mirrors ``meridian.experiment_model.validate_attempt_status``'s
    exact contract."""
    status = value.strip().lower() if isinstance(value, str) else ""
    if status not in CONTRACT_STATUSES:
        raise ValueError(
            f"paper_contract status must be one of {sorted(CONTRACT_STATUSES)}, got {value!r}"
        )
    return status


def validate_revision_approval_status(value: object) -> str:
    """Return ``value`` lowercased/stripped if it's one of
    :data:`REVISION_APPROVAL_STATUSES`. Raises ``ValueError`` naming the
    full closed set otherwise."""
    status = value.strip().lower() if isinstance(value, str) else ""
    if status not in REVISION_APPROVAL_STATUSES:
        raise ValueError(
            "paper_contract revision approval_status must be one of "
            f"{sorted(REVISION_APPROVAL_STATUSES)}, got {value!r}"
        )
    return status


def content_fingerprint(content: "dict[str, Any]") -> str:
    """A deterministic ``sha256:...`` fingerprint of a revision's editorial
    content, so the SAME logical content always fingerprints identically
    regardless of caller key order — same canonical-JSON convention as
    ``meridian.experiment_model.params_fingerprint`` and
    ``meridian.db.profile_layers._content_hash``. Unlike
    ``params_fingerprint``, empty content still produces a real hash (a
    revision always has SOME editorial content — at minimum a
    ``working_title`` — so there is no meaningful "no fingerprint" case to
    special-case)."""
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
