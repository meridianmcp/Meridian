"""627187b8 -- multi-transport exposure of meridian.db.batch_management.

``meridian.db.batch_management.execute_batch`` (86e4ae44) is the shared
transactional/idempotent batch-write ENGINE. That module's own docstring is
explicit that wiring it up as new MCP tool schemas across ``mcp_tools.py``,
``mcp/handler.py``, ``mcp/stdio_handler.py``, and HTTP routes was deliberately
left out of scope for that item and handed to THIS one (627187b8). This module
is the single shared layer every transport funnels through, so the schemas and
semantics can never drift out of sync with each other -- exactly the failure
mode this item exists to prevent.

Stable operation names
-----------------------
``execute_batch``'s own ``entry_kind`` parameter ("sprint_item",
"sprint_item_pointer", "sprint_note") conflates TWO different actions under
one kind: a ``sprint_item`` entry can independently be a create or an update
(picked per-entry via its own ``"action"`` field). Callers reasoning about a
whole BATCH ("I am submitting 20 new sprint items" vs "I am patching 20
existing ones") benefit from that distinction being visible at the batch
level, not buried per-entry -- so this layer defines four stable, transport-
facing operation names instead of exposing the raw entry_kind:

    "sprint_items"  -> entry_kind "sprint_item",         every entry action=create
    "item_updates"  -> entry_kind "sprint_item",         every entry action=update
    "pointers"      -> entry_kind "sprint_item_pointer"
    "notes"         -> entry_kind "sprint_note"

``sprint_items``/``item_updates`` are two names for the same underlying
entry_kind, split by forced action -- an entry whose own ``action`` disagrees
with the chosen operation is rejected with a clear, actionable message (use
the other operation name) rather than silently doing the wrong thing.
``BATCH_OPERATIONS`` is the canonical, importable mapping; every transport
handler resolves ``operation`` through it (never re-derives entry_kind by
hand) so a fifth operation added here automatically becomes available
everywhere without touching handler.py/stdio_handler.py logic (only their
schemas need the new enum value).

Required ``mode`` and ``idempotency_key``
------------------------------------------
``execute_batch`` itself defaults ``mode="all_or_nothing"`` and
``idempotency_key=None`` for convenience when called directly from Python
(tests, internal callers). The multi-transport surface is deliberately
STRICTER: per this item's acceptance criteria, every transport must require
both fields explicitly in the request -- no silent default mode, and no
silent "no idempotency protection" via simple omission. A caller who
genuinely wants no idempotency protection must say so by passing
``idempotency_key: null`` (or ``""``) -- an explicit, auditable choice --
rather than that being what happens when they forget the field.
``validate_batch_request_shape`` is the one place this is enforced; every
transport calls it (or, for the two transports that share one dispatch
function, calls it exactly once via that shared function) before doing
anything else.

Response shape
---------------
``execute_batch_operation`` returns ``BatchResult.to_dict()`` (see
``batch_management``: status/mode/entry_kind/project_id/idempotency_key/
idempotent_replay/created_count/error_count/results[]) with one extra key,
``"operation"``, echoing the caller's own stable operation name back --
every other field is untouched so a client that already understands
``execute_batch``'s response contract needs zero new parsing logic.
"""
from __future__ import annotations

from typing import Any

from .db import batch_management

#: Stable, transport-facing operation name -> underlying engine entry_kind.
#: See the module docstring's "Stable operation names" section.
BATCH_OPERATIONS: dict[str, str] = {
    "sprint_items": "sprint_item",
    "item_updates": "sprint_item",
    "pointers": "sprint_item_pointer",
    "notes": "sprint_note",
}

#: For the two operations that share entry_kind "sprint_item", the action
#: every entry MUST have (and gets stamped with if omitted).
_OPERATION_FORCED_ACTION: dict[str, str] = {
    "sprint_items": "create",
    "item_updates": "update",
}

#: Re-exported for transport convenience so callers don't need a second
#: import of meridian.db.batch_management just for these constants.
BATCH_MODES = batch_management.BATCH_MODES
DEFAULT_MAX_BATCH_ENTRIES = batch_management.DEFAULT_MAX_BATCH_ENTRIES


class BatchRequestError(ValueError):
    """A transport-level request malformation.

    Distinct from :class:`batch_management.BatchEngineError` only in WHEN it
    fires: before entry_kind/mode even reach the engine (missing/unknown
    operation, missing mode, missing idempotency_key key, or a per-entry
    action that disagrees with the chosen operation). Both are ``ValueError``
    subclasses so a transport that wants one broad ``except ValueError:`` to
    mean "malformed request, return {error}" can do that; a transport that
    wants to distinguish "your request shape was wrong" from "the engine
    rejected your batch contents" can catch each separately.
    """


def validate_batch_request_shape(payload: dict[str, Any]) -> None:
    """Enforce the identical-across-every-transport request-shape rules.

    Checks (raises :class:`BatchRequestError` with a client-safe message on
    the first failure):

    * ``operation`` is present and is one of :data:`BATCH_OPERATIONS`.
    * ``mode`` is present and is one of ``batch_management.BATCH_MODES``.
    * ``idempotency_key`` is a key that exists in ``payload`` -- its VALUE may
      be ``None``/``""`` (an explicit opt-out), but the key itself must not be
      simply absent.

    Deliberately does NOT check ``project_id``/``entries`` here: those get the
    same wording every other sprint tool already uses
    ("project_id is required (or pass project_name)") at the handler level,
    so error text stays consistent with the rest of the sprint-management
    tool surface instead of introducing a second phrasing for the same
    condition.
    """
    operation = payload.get("operation")
    if not operation:
        raise BatchRequestError(
            f"operation is required and must be one of {tuple(BATCH_OPERATIONS)}"
        )
    if operation not in BATCH_OPERATIONS:
        raise BatchRequestError(
            f"operation must be one of {tuple(BATCH_OPERATIONS)}, got {operation!r}"
        )
    mode = payload.get("mode")
    if not mode:
        raise BatchRequestError(
            f"mode is required and must be one of {BATCH_MODES} -- every caller "
            "must explicitly choose all_or_nothing or best_effort; there is no "
            "default at this layer"
        )
    if mode not in BATCH_MODES:
        raise BatchRequestError(f"mode must be one of {BATCH_MODES}, got {mode!r}")
    if "idempotency_key" not in payload:
        raise BatchRequestError(
            "idempotency_key is required -- pass a real key to make retries "
            "safe, or explicitly pass null (or an empty string) to acknowledge "
            "you are opting out of idempotency protection for this call"
        )


def _normalize_entries_for_operation(
    operation: str, entries: Any,
) -> Any:
    """Stamp/validate the forced ``action`` for the two split sprint_item ops.

    Returns ``entries`` unchanged for "pointers"/"notes" (no forced action)
    and for anything that isn't a list (batch_management's own call-level
    validation rejects a non-list ``entries`` with its standard message --
    duplicating that check here would just be a second, differently-worded
    error path for the same input). A non-dict individual entry is likewise
    passed through unchanged so it hits the engine's own per-entry
    "entry must be an object" validation error rather than a confusing
    AttributeError from ``.get()`` here.
    """
    forced_action = _OPERATION_FORCED_ACTION.get(operation)
    if forced_action is None or not isinstance(entries, list):
        return entries
    normalized: list[Any] = []
    other_op = "item_updates" if forced_action == "create" else "sprint_items"
    for i, raw in enumerate(entries):
        if not isinstance(raw, dict):
            normalized.append(raw)
            continue
        # Missing 'action' defaults to whatever THIS operation forces, not a
        # hardcoded "create" -- an item_updates entry that omits 'action'
        # (the common case: callers pass item_id + fields, never action) must
        # default to "update", matching the operation it was submitted under.
        action = raw.get("action") or forced_action
        if action != forced_action:
            raise BatchRequestError(
                f"operation {operation!r} requires every entry's action to be "
                f"{forced_action!r}; entry at index {i} has action={action!r}. "
                f"Use operation={other_op!r} for that entry instead, or split "
                "this call into two batch calls."
            )
        entry = dict(raw)
        entry["action"] = forced_action
        normalized.append(entry)
    return normalized


async def execute_batch_operation(
    db: Any,
    *,
    project_id: str,
    operation: str,
    entries: list[dict[str, Any]],
    mode: str,
    idempotency_key: str | None,
    tenant_id: str | None = None,
    actor: str | None = None,
    session_id: str | None = None,
    max_entries: int = DEFAULT_MAX_BATCH_ENTRIES,
) -> dict[str, Any]:
    """The ONE function every transport (MCP handler/stdio, HTTP route) calls.

    Translates ``operation`` into ``batch_management``'s ``entry_kind``,
    normalizes/validates the forced ``action`` for the two split
    "sprint_item" operations, calls :func:`batch_management.execute_batch`,
    and returns a plain JSON-serializable dict: ``BatchResult.to_dict()``
    plus ``"operation"`` echoing the caller's own stable operation name back.
    Every transport's response is therefore byte-identical modulo its own
    envelope (MCP tool result / HTTP JSON body / stdio TextContent).

    Raises :class:`BatchRequestError` for an unknown operation or an
    action/operation mismatch, and :class:`batch_management.BatchEngineError`
    for every other call-level contract violation (bad mode, empty/oversized
    ``entries``). Both are ``ValueError`` subclasses. Per-entry failures are
    NEVER raised here either -- see ``execute_batch``'s own docstring; they
    come back inside ``results[]`` in the returned dict.

    Does NOT call :func:`validate_batch_request_shape` itself -- callers are
    expected to call that first (it needs the raw, not-yet-typed request
    payload, which callers already have as a dict before they unpack it into
    this function's keyword arguments).
    """
    if operation not in BATCH_OPERATIONS:
        raise BatchRequestError(
            f"operation must be one of {tuple(BATCH_OPERATIONS)}, got {operation!r}"
        )
    entry_kind = BATCH_OPERATIONS[operation]
    normalized_entries = _normalize_entries_for_operation(operation, entries)
    result = await batch_management.execute_batch(
        db,
        project_id=project_id,
        entry_kind=entry_kind,
        entries=normalized_entries,
        mode=mode,
        idempotency_key=idempotency_key or None,
        tenant_id=tenant_id,
        actor=actor,
        session_id=session_id,
        max_entries=max_entries,
    )
    out = result.to_dict()
    out["operation"] = operation
    return out
