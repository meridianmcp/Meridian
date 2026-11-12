"""InvenTree write operations: preview, execute, and the approved-plan runner.

A write is never sent by the call that proposes it. `preview` validates an operation,
reads the live state through the read client, and describes the change without writing.
The preview also stores the exact method, path and body the operation will send.
Plans are stored and approved by `plans.PlanStore`.

`run_plan` reloads the plan from disk and does not trust the caller's copy. It runs only if:
- the stored plan is approved, carries the owner's approval, and its hash matches both
  plan_hash and the approved hash;
- every operation validates;
- the InvenTree version is inside the supported range, the same check propose uses;
- the audit log can be read, so the client keys that already succeeded are known;
- the plan moves from approved to running under the plan lock.

Only after all of that does the first request go out. An operation whose client_key already
has an op_done event in the audit log is skipped. The request rebuilt from each operation must
equal the request the preview stored. Immediately before each write, the live state the operation
changes is read again and must match the before values stored at approval; otherwise the write is
refused with state_changed and nothing is sent. Each step appends an audit event before and after.
The run stops at the first failure or unknown outcome and records the result with the plan store.
A plan runs once.

An unknown outcome is a write whose request may have reached InvenTree while no usable answer came
back: a timeout, an unreachable error after the send, a 502, 503 or 504 answer, a 2xx answer too
large or too deeply nested for the client to read, a 2xx answer with no JSON body, or a 2xx answer
with no usable primary key. It is recorded as op_unknown, not op_failed, and the plan moves to
needs_reconcile. Nothing retries it. Other 5xx answers are ordinary failures.
Only the owner can settle it, by checking InvenTree and the audit log by hand.

If an operation succeeds but its op_done event cannot be appended, the plan moves to needs_reconcile
and the run stops. The write is not retried, and the key is not in the audit log as succeeded.

`execute` performs the POST or PATCH. It refuses to run anywhere except inside `run_plan`.
MCP tools must call `preview` and never `execute`.

Body field names are not confirmed against a live instance in this build. Each unconfirmed
field carries a VERIFY-LIVE comment.
"""

from __future__ import annotations

import contextvars
import hmac
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Literal, Protocol, TypedDict

from meridian_inventree.audit import OP_DONE, OP_FAILED, OP_SKIPPED, OP_STARTED, OP_UNKNOWN
from meridian_inventree.client import InvenTreeClient, InvenTreeError
from meridian_inventree.plans import (
    APPROVED,
    EXECUTED,
    FAILED,
    NEEDS_RECONCILE,
    OWNER,
    RUNNING,
    PlanError,
    PlanStore,
    compute_plan_hash,
)
from meridian_inventree.write_params import (
    WriteValidationError,
    as_client_key,
    as_flag,
    as_mapping,
    as_name,
    as_non_negative,
    as_nonzero,
    as_number,
    as_pk,
    as_positive,
    as_text,
    as_wire,
    drop_none,
    invalid,
    optional,
    reject_unknown,
    required,
)

MAX_OPERATIONS: Final[int] = 50
AUDIT_AFTER_NOTE: Final[str] = "audit_failed_after"
SKIPPED_NOTE: Final[str] = "client_key already succeeded in the audit log"
KEY_UNRESOLVED_NOTE: Final[str] = "client_key has an unsettled earlier write; nothing was sent for it"
# Client error codes that mean no usable answer came back for a request that may have been sent. The
# client reports every connection error as "unreachable", including one raised before the request left,
# and it cannot tell the two apart, so a write is recorded as an unknown outcome. "bad_response" means the
# status was 2xx but the body was too large or nested too deeply for the client to read, so the write was
# probably applied.
_UNANSWERED_CODES: Final[frozenset[str]] = frozenset({"unreachable", "timeout", "bad_response"})
# HTTP answers from a gateway in front of InvenTree. The upstream may have applied the write, so for
# a write these are unknown outcomes. Other 5xx answers stay ordinary failures.
_GATEWAY_STATUSES: Final[frozenset[int]] = frozenset({502, 503, 504})
_GATEWAY_CODE: Final[str] = "gateway_error"

OpStatus = Literal["ok", "skipped", "failed", "not_run", "unknown"]


class PlanOperation(TypedDict):
    op: str
    params: dict[str, Any]
    client_key: str


class AuditSink(Protocol):
    """Structural type for the audit log. audit.AuditLog implements both methods."""

    def append(self, event: dict[str, Any]) -> Any: ...

    def succeeded_keys(self) -> set[str]: ...

    def unresolved_keys(self) -> set[str]: ...


class PlanRefused(Exception):
    """run_plan or execute refused to run. `code` is stable and safe to show."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        self.message = message or code
        super().__init__(self.message)


class WriteOutcomeUnknown(InvenTreeError):
    """A write request may have reached InvenTree, but no usable answer came back. Never retried.

    `code` is the client code that showed the answer was lost, such as "unreachable".
    """


@dataclass(frozen=True)
class OpOutcome:
    index: int
    op: str
    client_key: str
    status: OpStatus
    pk: int | None = None
    error_code: str | None = None
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "op": self.op,
            "client_key": self.client_key,
            "status": self.status,
            "pk": self.pk,
            "error_code": self.error_code,
            "note": self.note,
        }


def _outcome_clean(outcome: OpOutcome) -> bool:
    return outcome.status in ("ok", "skipped") and outcome.note != AUDIT_AFTER_NOTE


@dataclass(frozen=True)
class RunReport:
    plan_id: str
    outcomes: tuple[OpOutcome, ...]

    @property
    def complete(self) -> bool:
        return all(_outcome_clean(outcome) for outcome in self.outcomes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "complete": self.complete,
            "outcomes": [outcome.as_dict() for outcome in self.outcomes],
        }


# ---------------------------------------------------------------- shared reads


def _read_object(client: InvenTreeClient, path: str, label: str) -> dict[str, Any]:
    """GET one object. A missing object refuses the write; it is not a server fault."""
    try:
        data = client.get(path)
    except InvenTreeError as exc:
        if exc.code == "not_found":
            raise WriteValidationError("missing_reference", f"{label} does not exist") from None
        raise
    if not isinstance(data, dict):
        raise InvenTreeError("bad_response", f"GET {path} did not return an object")
    return data


def _check_category(pk: int | None, client: InvenTreeClient) -> None:
    if pk is not None:
        _read_object(client, f"/api/part/category/{pk}/", f"category {pk}")  # VERIFY-LIVE: detail path


def _check_location(pk: int | None, client: InvenTreeClient) -> None:
    if pk is not None:
        _read_object(client, f"/api/stock/location/{pk}/", f"location {pk}")  # VERIFY-LIVE: detail path


def _stock_quantity(stock: Mapping[str, Any], item: int) -> Decimal:
    try:
        return as_number(stock.get("quantity"), "quantity")  # VERIFY-LIVE: field name and string or number
    except WriteValidationError:
        raise InvenTreeError("bad_response", f"stock item {item} has no readable quantity") from None


# ---------------------------------------------------------------- parts


_PART_TEXT: Final[frozenset[str]] = frozenset({"description", "IPN", "keywords", "units", "link", "revision"})
_PART_FIELDS: Final[frozenset[str]] = _PART_TEXT | {"name", "category", "active"}


def _part_fields(params: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize the part fields that create and update share. Only the keys given are returned."""
    reject_unknown(params, _PART_FIELDS)
    fields: dict[str, Any] = {}
    for field, value in params.items():
        if field == "name":
            fields[field] = as_name(value)
        elif field == "category":
            fields[field] = None if value is None else as_pk(value, field)  # None clears it on update
        elif field == "active":
            fields[field] = as_flag(value, field)
        else:
            fields[field] = as_text(value, field)
    return fields


def _norm_create_part(params: Mapping[str, Any]) -> dict[str, Any]:
    if "name" not in params:
        raise invalid("missing_name", "name is required")
    fields = drop_none(_part_fields(params))
    fields.setdefault("active", True)  # VERIFY-LIVE: InvenTree default for active
    return {"fields": fields}


def _norm_update_part(params: Mapping[str, Any]) -> dict[str, Any]:
    pk = required(params, "pk", as_pk)
    fields = _part_fields({key: value for key, value in params.items() if key != "pk"})
    if not fields:
        raise invalid("no_changes", "update_part needs at least one field to change")
    return {"pk": pk, "fields": fields}


def _preview_create_part(norm: dict[str, Any], client: InvenTreeClient) -> dict[str, Any]:
    _check_category(norm["fields"].get("category"), client)
    return {"before": {}, "after": dict(norm["fields"])}  # nothing exists yet, so no prior values


def _preview_update_part(norm: dict[str, Any], client: InvenTreeClient) -> dict[str, Any]:
    pk, fields = norm["pk"], norm["fields"]
    current = _read_object(client, f"/api/part/{pk}/", f"part {pk}")  # VERIFY-LIVE: detail path
    _check_category(fields.get("category"), client)
    return {"before": {field: current.get(field) for field in fields}, "after": dict(fields)}


def _request_create_part(norm: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    return "POST", "/api/part/", dict(norm["fields"])  # VERIFY-LIVE: body field names


def _request_update_part(norm: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    return "PATCH", f"/api/part/{norm['pk']}/", dict(norm["fields"])  # VERIFY-LIVE: body field names


# ---------------------------------------------------------------- locations


_LOCATION_FIELDS: Final[frozenset[str]] = frozenset({"name", "parent", "description", "structural", "external"})


def _norm_create_location(params: Mapping[str, Any]) -> dict[str, Any]:
    reject_unknown(params, _LOCATION_FIELDS)
    if "name" not in params:
        raise invalid("missing_name", "name is required")
    fields: dict[str, Any] = {
        "name": as_name(params["name"]),
        "parent": optional(params, "parent", as_pk),  # VERIFY-LIVE
        "description": optional(params, "description", as_text),  # VERIFY-LIVE
        "structural": optional(params, "structural", as_flag),  # VERIFY-LIVE
        "external": optional(params, "external", as_flag),  # VERIFY-LIVE
    }
    return {"fields": drop_none(fields)}


def _preview_create_location(norm: dict[str, Any], client: InvenTreeClient) -> dict[str, Any]:
    _check_location(norm["fields"].get("parent"), client)
    return {"before": {}, "after": dict(norm["fields"])}


def _request_create_location(norm: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    return "POST", "/api/stock/location/", dict(norm["fields"])  # VERIFY-LIVE: body field names


# ---------------------------------------------------------------- stock


_STOCK_CREATE_FIELDS: Final[frozenset[str]] = frozenset({"part", "quantity", "location", "batch", "notes"})


def _norm_create_stock_item(params: Mapping[str, Any]) -> dict[str, Any]:
    reject_unknown(params, _STOCK_CREATE_FIELDS)
    return {
        "part": required(params, "part", as_pk),
        "quantity": required(params, "quantity", as_non_negative),
        "location": optional(params, "location", as_pk),
        "batch": optional(params, "batch", as_text),  # VERIFY-LIVE
        "notes": optional(params, "notes", as_text),  # VERIFY-LIVE
    }


def _stock_create_body(norm: dict[str, Any]) -> dict[str, Any]:
    return {
        "part": norm["part"],  # VERIFY-LIVE
        "quantity": as_wire(norm["quantity"]),  # VERIFY-LIVE: decimal string
        **drop_none({"location": norm["location"], "batch": norm["batch"], "notes": norm["notes"]}),
    }


def _preview_create_stock_item(norm: dict[str, Any], client: InvenTreeClient) -> dict[str, Any]:
    part = norm["part"]
    _read_object(client, f"/api/part/{part}/", f"part {part}")  # VERIFY-LIVE: detail path
    _check_location(norm["location"], client)
    return {"before": {}, "after": _stock_create_body(norm)}


def _request_create_stock_item(norm: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    return "POST", "/api/stock/", _stock_create_body(norm)


_TRANSFER_FIELDS: Final[frozenset[str]] = frozenset({"item", "quantity", "destination", "notes"})


def _norm_transfer(params: Mapping[str, Any]) -> dict[str, Any]:
    reject_unknown(params, _TRANSFER_FIELDS)
    return {
        "item": required(params, "item", as_pk),
        "quantity": required(params, "quantity", as_positive),
        "destination": required(params, "destination", as_pk, missing_code="missing_destination"),
        "notes": optional(params, "notes", as_text),  # VERIFY-LIVE
    }


def _preview_transfer(norm: dict[str, Any], client: InvenTreeClient) -> dict[str, Any]:
    item, quantity = norm["item"], norm["quantity"]
    stock = _read_object(client, f"/api/stock/{item}/", f"stock item {item}")  # VERIFY-LIVE: detail path
    available = _stock_quantity(stock, item)
    if quantity > available:
        raise WriteValidationError(
            "insufficient_stock",
            f"stock item {item} holds {as_wire(available)}, fewer than the {as_wire(quantity)} to move",
        )
    _check_location(norm["destination"], client)
    return {
        "before": {"location": stock.get("location"), "quantity": as_wire(available)},  # VERIFY-LIVE
        "after": {
            "location": norm["destination"],
            "moved_quantity": as_wire(quantity),
            "source_quantity_after": as_wire(available - quantity),
        },
    }


def _request_transfer(norm: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    body: dict[str, Any] = {
        "items": [{"pk": norm["item"], "quantity": as_wire(norm["quantity"])}],  # VERIFY-LIVE
        "location": norm["destination"],  # VERIFY-LIVE
        **drop_none({"notes": norm["notes"]}),
    }
    return "POST", "/api/stock/transfer/", body


_ADJUST_FIELDS: Final[frozenset[str]] = frozenset({"item", "delta", "notes"})


def _norm_adjust(params: Mapping[str, Any]) -> dict[str, Any]:
    reject_unknown(params, _ADJUST_FIELDS)
    return {
        "item": required(params, "item", as_pk),
        "delta": required(params, "delta", as_nonzero),
        "notes": optional(params, "notes", as_text),  # VERIFY-LIVE
    }


def _preview_adjust(norm: dict[str, Any], client: InvenTreeClient) -> dict[str, Any]:
    item, delta = norm["item"], norm["delta"]
    stock = _read_object(client, f"/api/stock/{item}/", f"stock item {item}")  # VERIFY-LIVE: detail path
    current = _stock_quantity(stock, item)
    result = current + delta
    if result < 0:
        raise WriteValidationError(
            "would_go_negative",
            f"stock item {item} holds {as_wire(current)}; this change would take it below zero",
        )
    return {"before": {"quantity": as_wire(current)}, "after": {"quantity": as_wire(result)}}


def _request_adjust(norm: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    delta = norm["delta"]
    path = "/api/stock/add/" if delta > 0 else "/api/stock/remove/"  # VERIFY-LIVE: endpoint choice by sign
    body: dict[str, Any] = {
        "items": [{"pk": norm["item"], "quantity": as_wire(abs(delta))}],  # VERIFY-LIVE
        **drop_none({"notes": norm["notes"]}),
    }
    return "POST", path, body


# ---------------------------------------------------------------- registry


@dataclass(frozen=True)
class _Spec:
    normalize: Callable[[Mapping[str, Any]], dict[str, Any]]
    preview: Callable[[dict[str, Any], InvenTreeClient], dict[str, Any]]
    request: Callable[[dict[str, Any]], tuple[str, str, dict[str, Any]]]


_SPECS: Final[dict[str, _Spec]] = {
    "create_part": _Spec(_norm_create_part, _preview_create_part, _request_create_part),
    "update_part": _Spec(_norm_update_part, _preview_update_part, _request_update_part),
    "create_location": _Spec(_norm_create_location, _preview_create_location, _request_create_location),
    "create_stock_item": _Spec(_norm_create_stock_item, _preview_create_stock_item, _request_create_stock_item),
    "transfer_stock": _Spec(_norm_transfer, _preview_transfer, _request_transfer),
    "adjust_stock_quantity": _Spec(_norm_adjust, _preview_adjust, _request_adjust),
}
OPERATION_NAMES: Final[frozenset[str]] = frozenset(_SPECS)


def _spec(op: object) -> _Spec:
    if not isinstance(op, str) or op not in _SPECS:
        raise invalid("unknown_op", "unknown write operation")
    return _SPECS[op]


def _normalize(op: object, params: object) -> tuple[_Spec, dict[str, Any]]:
    spec = _spec(op)
    return spec, spec.normalize(as_mapping(params))


# ---------------------------------------------------------------- version gate (shared with propose)


def version_supported(reported: object, prefixes: Sequence[str]) -> bool:
    """True when the reported version starts with an allowed prefix. No prefixes allows nothing."""
    if not isinstance(reported, str) or not prefixes:
        return False
    return any(reported.startswith(prefix) for prefix in prefixes)


def reported_version(data: Mapping[str, Any]) -> str | None:
    value = data.get("version")
    return value[:64] if isinstance(value, str) else None


def check_write_version(client: InvenTreeClient, prefixes: Sequence[str]) -> None:
    """Refuse unless the live InvenTree version is inside the supported range. Sends one GET /api/.

    With no range set, every version is refused before any request, so no GET /api/ is sent.
    propose and run_plan both call this, so the two cannot disagree about the range.
    """
    if not prefixes:
        # Nothing can be supported, so there is no version to compare. Refuse without reading InvenTree.
        raise PlanRefused(
            "version_unsupported",
            "No supported InvenTree version range is set, so writes are refused. No version was read.",
        )
    try:
        data = client.check_version()
    except InvenTreeError as exc:
        raise PlanRefused(
            "version_unverified",
            f"could not read the InvenTree version ({exc.code}), so writes are refused",
        ) from None
    reported = reported_version(data)
    if not version_supported(reported, prefixes):
        raise PlanRefused(
            "version_unsupported",
            f"InvenTree reports version {reported or 'unknown'}, which is not in the supported range, "
            "so writes are refused",
        )


# ---------------------------------------------------------------- public: preview and execute


def preview(op: str, params: Mapping[str, Any], client: InvenTreeClient) -> dict[str, Any]:
    """Validate, read the live state, and describe the change. Sends no POST or PATCH.

    The result is plain JSON, so it can be passed straight to PlanStore.propose as one preview.
    method, path and body are the exact request the operation will send. run_plan checks
    the rebuilt request against them before it writes anything.
    """
    spec, norm = _normalize(op, params)
    state = spec.preview(norm, client)
    method, path, body = spec.request(norm)
    return {
        "op": op,
        "method": method,
        "path": path,
        "body": body,
        "before": state["before"],
        "after": state["after"],
    }


_EXECUTOR_ACTIVE: Final[contextvars.ContextVar[bool]] = contextvars.ContextVar(
    "meridian_inventree_executor_active", default=False
)


def _canonical_json(value: object) -> str | None:
    """Canonical JSON text of a value, or None when it cannot be serialized."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return None


def _request_digest(method: object, path: object, body: object) -> str | None:
    """Canonical JSON of one request, or None when it cannot be serialized."""
    return _canonical_json({"method": method, "path": path, "body": body})


def _refuse_changed_state(
    spec: _Spec,
    norm: dict[str, Any],
    stored_request: Mapping[str, Any],
    client: InvenTreeClient,
) -> None:
    """Re-read what the operation changes. Refuse unless it still matches the before values stored at approval.

    A check that now fails is also a changed state: it passed when the plan was proposed. Nothing is
    sent when this refuses.
    """
    try:
        live = spec.preview(norm, client)
    except WriteValidationError as exc:
        raise PlanRefused(
            "state_changed",
            f"the live state no longer passes this operation's check ({exc.code}), so nothing was sent",
        ) from None
    live_before = _canonical_json(live.get("before"))
    stored_before = _canonical_json(stored_request.get("before"))
    if live_before is None or live_before != stored_before:
        raise PlanRefused(
            "state_changed",
            "the live state differs from the before values stored at approval, so nothing was sent",
        )


def execute(
    op: str,
    params: Mapping[str, Any],
    client: InvenTreeClient,
    client_key: str,
    stored_request: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Perform one write. Only run_plan may call this.

    stored_request is the preview that proposal stored for this operation. The request rebuilt
    here must match its method, path and body exactly, and the live state must still match its
    before values, or nothing is sent.
    """
    if not _EXECUTOR_ACTIVE.get():
        raise PlanRefused("execute_outside_plan", "writes run only through an approved plan")
    key = as_client_key(client_key)
    if not isinstance(stored_request, Mapping):
        raise PlanRefused("request_not_stored", "the plan does not store the request for this operation")
    spec, norm = _normalize(op, params)
    method, path, body = spec.request(norm)
    rebuilt = _request_digest(method, path, body)
    stored = _request_digest(stored_request.get("method"), stored_request.get("path"), stored_request.get("body"))
    if rebuilt is None or rebuilt != stored:
        raise PlanRefused(
            "request_mismatch",
            "the request rebuilt from this operation differs from the request stored at proposal",
        )
    _refuse_changed_state(spec, norm, stored_request, client)  # re-read just before the write
    try:
        if method == "PATCH":
            response = client.patch(path, body)
        else:
            response = client.post(path, body)
    except InvenTreeError as exc:
        # Only failures after the write was sent can leave an unknown outcome. The reads above
        # raise the same codes, but they send no write, so they stay ordinary failures.
        if exc.code in _UNANSWERED_CODES:
            raise WriteOutcomeUnknown(
                exc.code,
                f"the write request may have reached InvenTree but no answer was read ({exc.code}); "
                "its outcome is unknown",
            ) from None
        if exc.status in _GATEWAY_STATUSES:
            raise WriteOutcomeUnknown(
                _GATEWAY_CODE,
                f"InvenTree sits behind a gateway that answered the write with HTTP {exc.status}; "
                "the upstream may have applied it, so its outcome is unknown",
            ) from None
        raise
    pk = _response_pk(response)
    if pk is None:
        # A 2xx answer is success only when it names the object it changed. No JSON body, or no
        # usable primary key, means the outcome cannot be confirmed, so nothing is retried.
        raise WriteOutcomeUnknown(
            "bad_response",
            "InvenTree answered the write without a usable primary key, so its outcome is unknown",
        )
    return {"op": op, "client_key": key, "method": method, "path": path, "pk": pk}


def _response_pk(response: object) -> int | None:
    """The primary key of the object the write named, or None when the answer does not carry a usable one."""
    if isinstance(response, dict):
        pk = response.get("pk")
        if isinstance(pk, int) and not isinstance(pk, bool) and pk >= 1:
            return pk
    return None


# ---------------------------------------------------------------- the runner


def _same_digest(left: object, right: object) -> bool:
    """Constant-time comparison of two hex digests. Non-strings never match."""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _load_for_run(plan: object, store: PlanStore) -> dict[str, Any]:
    """Reload the plan from disk. Refuse unless it is owner-approved, unchanged and not yet started.

    Only the plan_id from the caller is used. Operations, previews, status and approval all come
    from disk, so an edited or stale copy has no effect.
    """
    plan_id = plan.get("plan_id") if isinstance(plan, Mapping) else None
    if not isinstance(plan_id, str):
        raise PlanRefused("malformed_plan", "the plan record has no plan id")
    try:
        stored = store.get(plan_id)
    except PlanError as exc:
        raise PlanRefused(exc.code, exc.message) from None
    status = stored.get("status")
    if status == RUNNING:
        raise PlanRefused(
            "already_running",
            "this plan is running or its run was never recorded; inspect the audit log and InvenTree first",
        )
    if status == NEEDS_RECONCILE:
        raise PlanRefused(
            "needs_reconcile",
            "an operation in this plan has an unknown outcome, so nothing is retried; the owner must check "
            "InvenTree and the audit log and record the outcome by hand before any further plan runs",
        )
    if status in (EXECUTED, FAILED):
        raise PlanRefused("already_executed", "this plan has already run; remaining work needs a new plan")
    if status != APPROVED:
        raise PlanRefused("not_approved", "the plan has no owner approval, so nothing runs")
    approval = stored.get("approval")
    if not isinstance(approval, Mapping) or approval.get("approved_by") != OWNER:
        raise PlanRefused("not_approved", "the plan has no owner approval, so nothing runs")
    operations, previews = stored.get("operations"), stored.get("previews")
    if not isinstance(operations, list) or not isinstance(previews, list):
        raise PlanRefused("malformed_plan", "the plan record is missing its operations or previews")
    if not 1 <= len(operations) <= MAX_OPERATIONS:
        raise PlanRefused("bad_plan_size", f"a plan holds 1 to {MAX_OPERATIONS} operations")
    try:
        current = compute_plan_hash(operations, previews)
    except (TypeError, ValueError):
        raise PlanRefused("unhashable_plan", "the plan cannot be hashed, so it cannot be checked") from None
    if not (_same_digest(current, stored.get("plan_hash")) and _same_digest(current, approval.get("approved_hash"))):
        raise PlanRefused("hash_mismatch", "the plan changed after approval")
    return stored


def _preflight(operations: Sequence[Mapping[str, Any]]) -> None:
    """Validate every operation before any runs, so a bad one cannot stop a plan half way."""
    for index, item in enumerate(operations):
        try:
            as_client_key(item["client_key"])
            _normalize(item["op"], item["params"])
        except WriteValidationError as exc:
            raise PlanRefused("invalid_operation", f"operation {index} refused: {exc.code}") from None
        except (KeyError, TypeError):
            raise PlanRefused("invalid_operation", f"operation {index} is malformed") from None


def _read_succeeded(audit: AuditSink) -> set[str]:
    """Client keys that already have an op_done event. An unreadable log refuses the run."""
    try:
        return set(audit.succeeded_keys())
    except Exception:  # noqa: BLE001 - an unreadable log must stop the run before any write (fail closed)
        raise PlanRefused("audit_unreadable", "the audit log cannot be read, so no write is sent") from None


def _read_unresolved(audit: AuditSink) -> set[str]:
    """Client keys whose last write may have reached InvenTree and was never settled. Unreadable logs refuse the run."""
    try:
        return set(audit.unresolved_keys())
    except Exception:  # noqa: BLE001 - fail closed: an unreadable log must stop the run before any write
        raise PlanRefused("audit_unreadable", "the audit log cannot be read, so no write is sent") from None


def _start(store: PlanStore, plan_id: str) -> dict[str, Any]:
    """Move the plan from approved to running. This is the last check before the first write."""
    try:
        return store.mark_running(plan_id)
    except PlanError as exc:
        raise PlanRefused(exc.code, exc.message) from None


def _event_name(phase: str, status: str | None) -> str:
    if phase == "started":
        return OP_STARTED
    if phase == "skipped":
        return OP_SKIPPED
    if status == "unknown":
        return OP_UNKNOWN
    return OP_DONE if status == "ok" else OP_FAILED


def _event(
    plan_id: str,
    index: int,
    op: str,
    key: str,
    phase: str,
    *,
    status: str | None = None,
    error_code: str | None = None,
    pk: int | None = None,
    params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event": _event_name(phase, status),
        "plan_id": plan_id,
        "index": index,
        "op": op,
        "client_key": key,
        "phase": phase,
        "status": status,
        "error_code": error_code,
        "pk": pk,
        "at": datetime.now(UTC).isoformat(),
    }
    if params is not None:
        event["params"] = dict(params)
    return event


def _record(audit: AuditSink, event: dict[str, Any]) -> bool:
    """Append one audit event. False means the sink failed, and the caller must stop."""
    try:
        audit.append(event)
    except Exception:  # noqa: BLE001 - any audit failure stops the run (fail closed)
        return False
    return True


def _run_one(
    plan_id: str,
    index: int,
    item: PlanOperation,
    stored_preview: Mapping[str, Any],
    client: InvenTreeClient,
    audit: AuditSink,
) -> OpOutcome:
    op, key, params = item["op"], item["client_key"], item["params"]
    if not _record(audit, _event(plan_id, index, op, key, "started", params=params)):
        return OpOutcome(index, op, key, "failed", error_code="audit_failed")
    try:
        result = execute(op, params, client, key, stored_request=stored_preview)
    except PlanRefused as exc:
        outcome = OpOutcome(index, op, key, "failed", error_code=exc.code)
    except WriteOutcomeUnknown as exc:  # before InvenTreeError: this is a subclass of it
        outcome = OpOutcome(index, op, key, "unknown", error_code=exc.code)
    except InvenTreeError as exc:
        outcome = OpOutcome(index, op, key, "failed", error_code=exc.code)
    except ValueError:
        outcome = OpOutcome(index, op, key, "failed", error_code="invalid")
    except Exception:  # noqa: BLE001 - record any other failure, then stop the plan
        outcome = OpOutcome(index, op, key, "failed", error_code="unexpected_error")
    else:
        outcome = OpOutcome(index, op, key, "ok", pk=result["pk"])
    finished = _event(
        plan_id,
        index,
        op,
        key,
        "finished",
        status=outcome.status,
        error_code=outcome.error_code,
        pk=outcome.pk,
    )
    if not _record(audit, finished):
        return replace(outcome, note=AUDIT_AFTER_NOTE)
    return outcome


def _stops(outcome: OpOutcome) -> bool:
    """A run stops at the first failure or unknown outcome, and after an audit failure that followed a write."""
    return outcome.status not in ("ok", "skipped") or outcome.note == AUDIT_AFTER_NOTE


def _owner_must_reconcile(outcomes: Sequence[OpOutcome]) -> bool:
    """True when a write's outcome is unknown, or a write succeeded but its op_done could not be recorded.

    Either way the audit log does not say the key succeeded, so a retry could repeat the write.
    Such a plan goes to needs_reconcile, never to failed.
    """
    return any(outcome.status == "unknown" or outcome.note == AUDIT_AFTER_NOTE for outcome in outcomes)


def run_plan(
    plan: Mapping[str, Any],
    client: InvenTreeClient,
    audit: AuditSink,
    *,
    store: PlanStore,
    supported_prefixes: Sequence[str] = (),
) -> RunReport:
    """Run one approved plan in order. Stops at the first failure and records what happened.

    `plan` supplies only its plan_id. The plan is reloaded and verified from disk, so an
    edited or stale copy has no effect. `store` and `supported_prefixes` are keyword-only.
    Pass meridian_inventree.server.SUPPORTED_VERSION_PREFIXES, the same range propose uses.
    The default is empty, which refuses every run.

    Before the first request, and with nothing changed until the last step:
    1. reload from disk and verify status, approval and hash;
    2. validate every operation;
    3. check the InvenTree version (one GET /api/, or no request when no range is set);
    4. read the client keys that already succeeded in the audit log;
    5. move the plan from approved to running, under the plan lock.

    Then each operation runs in order. An operation whose client_key already succeeded is
    skipped. Every other operation writes an audit event before and after its request. The run
    stops at the first failure or unknown outcome, and the rest are reported as not_run.

    Each operation re-reads the live state it changes just before its write. A difference from the
    before values stored at approval refuses that operation with state_changed and stops the run.

    The outcome is recorded once. With no unknown outcome, and no write whose op_done could not be
    recorded, it goes to mark_executed (executed, or failed). Otherwise it goes to
    mark_needs_reconcile. If the record cannot be written, the plan stays running and
    PlanRefused("result_not_recorded") is raised.

    Known limits:
    - A crash after a POST reaches InvenTree and before its response is read leaves that
      operation with an op_started event and no op_done. The plan stays running.
    - An exception that escapes the loop also leaves the plan running.
    - Nothing stops a different, later plan from repeating an unknown write until the owner has
      recorded its outcome by hand. propose and approve refuse such a key, and the README recovery
      section says what to do first.
    """
    stored = _load_for_run(plan, store)
    plan_id = str(stored["plan_id"])
    _preflight(stored["operations"])
    check_write_version(client, supported_prefixes)
    succeeded = _read_succeeded(audit)
    unresolved = _read_unresolved(audit)
    running = _start(store, plan_id)
    operations, previews = running["operations"], running["previews"]
    outcomes: list[OpOutcome] = []
    stopped = False
    token = _EXECUTOR_ACTIVE.set(True)
    try:
        for index, item in enumerate(operations):
            op, key = item["op"], item["client_key"]
            if stopped:
                outcomes.append(OpOutcome(index, op, key, "not_run"))
                continue
            if key in unresolved and key not in succeeded:
                # Nothing is sent for this key. An earlier write with it may already have reached InvenTree.
                # The event is "skipped", which unresolved_keys ignores, so it does not settle the key. An
                # automatic op_failed would settle it and let a later plan reuse the key.
                outcome = OpOutcome(index, op, key, "failed", error_code="key_unresolved", note=KEY_UNRESOLVED_NOTE)
                if not _record(audit, _event(plan_id, index, op, key, "skipped", error_code="key_unresolved")):
                    outcome = OpOutcome(index, op, key, "failed", error_code="audit_failed")
                outcomes.append(outcome)
                stopped = _stops(outcome)
                continue
            if key in succeeded:
                outcome = OpOutcome(index, op, key, "skipped", note=SKIPPED_NOTE)
                if not _record(audit, _event(plan_id, index, op, key, "skipped")):
                    outcome = OpOutcome(index, op, key, "failed", error_code="audit_failed")
                outcomes.append(outcome)
                stopped = _stops(outcome)
                continue
            outcome = _run_one(plan_id, index, item, previews[index], client, audit)
            outcomes.append(outcome)
            stopped = _stops(outcome)
    finally:
        _EXECUTOR_ACTIVE.reset(token)
    report = RunReport(plan_id, tuple(outcomes))
    rows = [outcome.as_dict() for outcome in outcomes]
    try:
        if _owner_must_reconcile(outcomes):
            store.mark_needs_reconcile(plan_id, rows)
        else:
            store.mark_executed(plan_id, rows, failed=not report.complete)
    except (PlanError, OSError) as exc:
        raise PlanRefused(
            "result_not_recorded",
            "the run finished but its result could not be written; the plan stays running. "
            "Inspect the audit log and InvenTree before any manual change.",
        ) from exc
    return report
