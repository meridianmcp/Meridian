"""SCHEMA: structural_patch -- a proposed manuscript structural edit that
sits behind a human approval gate (sprint item 6d109127).

Meridian's manuscript/paper editorial tooling (meridian.doc_store's
``doc_documents``/``doc_elements`` tables) needs a durable, versioned record
of a PROPOSED edit to a document's structure -- insert/delete/move/replace/
reorder a section, paragraph, figure, or table -- that a human must approve
before anything is actually changed. This module is the LEAF half of that
contract: closed vocabularies and pure transition rules, no DB import --
mirroring :mod:`meridian.experiment_model`'s own split from
:mod:`meridian.db.experiment_model` (the persistence layer built on top of
this one, in ``meridian/db/structural_patch.py``).

STATE MACHINE
--------------

::

    proposed --> approved --> applied   (terminal)
       |            |
       |            +-------> rejected   (terminal)
       |            |
       |            +-------> superseded (terminal)
       |
       +-----------------> rejected      (terminal)
       +-----------------> withdrawn     (terminal)
       +-----------------> superseded    (terminal)

* ``proposed`` -- the initial state. Something (an executor, an editorial
  tool) has computed a candidate structural edit and is asking a human to
  approve it. Nothing has been changed in the document yet.
* ``approved`` -- a human has signed off. The edit has NOT been applied yet
  -- application is a distinct, separate act (``applied_at`` on the
  persisted row) deliberately out of scope for this schema-only item. An
  approved patch can still be rejected (the human changes their mind before
  it's applied) or superseded (a newer edit obsoletes it).
* ``rejected`` -- a human declined it. Terminal.
* ``withdrawn`` -- the PROPOSER retracted it before a human decided (e.g. a
  newer analysis made it moot). Terminal.
* ``superseded`` -- a revised patch (linking back to this one via its own
  ``supersedes_patch_id``) replaces it. Reachable from ``proposed`` OR
  ``approved`` -- approving a patch does not freeze the document against
  further proposals. Terminal.
* ``applied`` -- the approved edit was actually executed against the
  document. Only reachable from ``approved`` (an edit must be approved
  before it can be applied -- there is no path that skips the gate).
  Terminal.

A transition to the SAME status is always legal (idempotent no-op --
mirrors :mod:`meridian.experiment_model`'s identical convention so a caller
retrying a call after a network blip never sees a spurious ``ValueError``).

VERSION PINNING
----------------

A structural patch is computed against one specific snapshot of the target
document, recorded as ``base_content_hash`` -- the SAME sha256 hexdigest
:func:`meridian.doc_store.compute_content_hash` / ``doc_documents.content_hash``
already use. This reuses the exact "pin what you saw, verify it before
acting" contract :mod:`meridian.doc_store`'s ``update_paragraph`` already
applies via its ``expected_content_hash`` precondition gate -- the only
difference is that a structural_patch OUTLIVES a single call, so the pin has
to be a stored column rather than a request parameter. Persisting it lets a
future apply-time check compare it against the document's CURRENT
content_hash and refuse (or re-flag for re-review) a patch that has gone
stale because the document changed after it was proposed. Computing and
enforcing that comparison is intentionally NOT this schema-only item's job
(no apply pathway is wired here) -- the column exists so a later item can.
"""
from __future__ import annotations

from typing import Any

from meridian.secret_redaction import check_for_secrets

# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------

#: The kind of structural edit a patch proposes. Deliberately small and
#: generic (not one entry per doc_elements ``kind``) -- the SHAPE of the edit
#: (insert/delete/move/replace/reorder), not the element kind it targets,
#: which lives in ``payload`` instead.
PATCH_OPERATIONS: frozenset[str] = frozenset(
    {"insert", "delete", "move", "replace", "reorder"}
)

#: Every state a structural_patch can be in. See the module docstring's
#: state-machine diagram.
PATCH_STATUSES: frozenset[str] = frozenset(
    {"proposed", "approved", "rejected", "withdrawn", "superseded", "applied"}
)

#: Statuses that will never transition again on their own -- reached only via
#: an explicit transition and never left. Mirrors
#: ``meridian.experiment_model.ATTEMPT_TERMINAL_STATUSES``'s exact contract.
PATCH_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"rejected", "withdrawn", "superseded", "applied"}
)

#: Legal ``from_status -> {to_status, ...}`` transitions. A transition to the
#: SAME status is always included (idempotent no-op). Terminal statuses
#: transition only to themselves.
_ALLOWED_PATCH_TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset(
        {"proposed", "approved", "rejected", "withdrawn", "superseded"}
    ),
    "approved": frozenset({"approved", "applied", "rejected", "superseded"}),
    "rejected": frozenset({"rejected"}),
    "withdrawn": frozenset({"withdrawn"}),
    "superseded": frozenset({"superseded"}),
    "applied": frozenset({"applied"}),
}

#: Statuses that record a human decision -- see
#: ``meridian.db.structural_patch.transition_structural_patch``'s
#: ``decided_by_human_id`` requirement, the concrete enforcement of "human
#: approval gate".
DECISION_STATUSES: frozenset[str] = frozenset({"approved", "rejected"})

MAX_RATIONALE_CHARS = 4_000
MAX_CONTENT_HASH_CHARS = 128
MAX_PAYLOAD_BYTES = 50_000


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def validate_operation(raw: object) -> str:
    """Return ``raw`` lowercased/stripped if it's one of
    :data:`PATCH_OPERATIONS`. Raises ``ValueError`` naming the full closed
    set otherwise -- mirrors
    ``meridian.experiment_model.validate_attempt_status``'s exact contract.
    """
    value = raw.strip().lower() if isinstance(raw, str) else ""
    if value not in PATCH_OPERATIONS:
        raise ValueError(
            f"operation must be one of {sorted(PATCH_OPERATIONS)}, got {raw!r}"
        )
    return value


def validate_status(raw: object) -> str:
    """Return ``raw`` lowercased/stripped if it's one of
    :data:`PATCH_STATUSES`. Raises ``ValueError`` naming the full closed set
    otherwise.
    """
    value = raw.strip().lower() if isinstance(raw, str) else ""
    if value not in PATCH_STATUSES:
        raise ValueError(
            f"status must be one of {sorted(PATCH_STATUSES)}, got {raw!r}"
        )
    return value


def validate_transition(current: str, new_status: str) -> str:
    """Validate that ``current -> new_status`` is a legal structural_patch
    transition (see the module docstring's state-machine diagram).

    Returns the normalized ``new_status`` on success. Raises ``ValueError``
    (naming both states and the allowed destinations) on an illegal jump --
    e.g. ``applied -> rejected`` (an applied edit cannot un-happen) or
    ``rejected -> approved`` (a declined patch must be re-proposed, not
    flipped).
    """
    current = validate_status(current)
    new_status = validate_status(new_status)
    allowed = _ALLOWED_PATCH_TRANSITIONS.get(current, frozenset())
    if new_status not in allowed:
        raise ValueError(
            f"illegal structural-patch transition {current!r} -> {new_status!r}; "
            f"from {current!r} only {sorted(allowed)} is allowed"
        )
    return new_status


def is_terminal_status(status: str) -> bool:
    """True when ``status`` is one of :data:`PATCH_TERMINAL_STATUSES`."""
    return validate_status(status) in PATCH_TERMINAL_STATUSES


def _validate_text(
    value: object,
    *,
    field: str,
    required: bool = False,
    max_chars: int = MAX_RATIONALE_CHARS,
) -> "str | None":
    if value is None:
        if required:
            raise ValueError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = value.strip()
    if required and not text:
        raise ValueError(f"{field} is required")
    if len(text) > max_chars:
        raise ValueError(f"{field} exceeds the {max_chars}-character limit")
    check_for_secrets(text, context=f"structural patch {field}")
    return text or None


def validate_rationale(value: object) -> "str | None":
    """Bounded, secret-checked free text explaining why a patch is proposed
    (or why a decision was made, when reused for ``decision_note``)."""
    return _validate_text(value, field="rationale", max_chars=MAX_RATIONALE_CHARS)


def validate_content_hash(value: object) -> "str | None":
    """Bounded, secret-checked ``base_content_hash`` pin.

    Deliberately lenient on exact format (no fixed-length hex check): the
    value is whatever :func:`meridian.doc_store.compute_content_hash`
    produces today, and pinning this validator to sha256's exact hex length
    would make a future hash-algorithm change here a breaking schema change
    for no real safety benefit -- the actual invariant this validator
    enforces (bounded length, no embedded secret) does not depend on the
    hash algorithm.
    """
    return _validate_text(
        value, field="base_content_hash", max_chars=MAX_CONTENT_HASH_CHARS
    )


def validate_payload(value: object) -> dict[str, Any]:
    """Validate the operation-specific JSON payload: JSON-serializable,
    bounded size, and free of secrets/machine-local content in any string
    leaf. Mirrors
    ``meridian.external_job_register.validate_metadata``'s exact contract
    (recursive visit + size cap), reused here rather than reinvented.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("structural patch payload must be an object")

    def visit(node: Any, path: str = "payload") -> None:
        if isinstance(node, str):
            check_for_secrets(node, context=f"structural patch {path}")
        elif isinstance(node, dict):
            for key, child in node.items():
                if not isinstance(key, str):
                    raise ValueError("structural patch payload keys must be strings")
                visit(child, f"{path}.{key}")
        elif isinstance(node, (list, tuple)):
            for index, child in enumerate(node):
                visit(child, f"{path}[{index}]")
        elif node is not None and not isinstance(node, (bool, int, float)):
            raise ValueError(f"structural patch {path} contains a non-JSON value")

    visit(value)
    import json  # noqa: PLC0415 -- only needed for the serialize/size check below

    try:
        encoded = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("structural patch payload must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"structural patch payload exceeds {MAX_PAYLOAD_BYTES} bytes")
    return dict(value)
