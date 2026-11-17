"""Plan store for proposed InvenTree writes.

One plan is one JSON file under the store's root folder, named by its plan_id
(uuid4 hex). A plan moves through these states:

    proposed -> approved -> running -> executed
                                    -> failed
                                    -> needs_reconcile

`approve` moves proposed to approved. `mark_running` moves approved to running, under the
plan lock, before the first write. `mark_executed` moves running to executed or failed.
`mark_needs_reconcile` moves running to needs_reconcile, when a run stopped on an operation
whose outcome is unknown: the request may have reached InvenTree and its answer was lost. Such a
plan is never retried, and nothing moves it on until the owner has checked InvenTree and the
audit log by hand. No other move exists, so a running plan is never started again. If the
final record cannot be written, the plan stays running. Nothing in this module talks to InvenTree.

On Windows, os.replace can fail with PermissionError while another process has the plan file
open. `_write` retries it 5 times, 50 ms apart, and then raises plan_file_locked. The plan file is
left as it was.

Integrity: plan_hash is the sha256 of the canonical JSON of operations and previews. Every
read that matters (approve, mark_running, mark_executed, get) recomputes that hash from disk
and refuses a file whose contents no longer match, so an edited plan cannot be approved or run.

Client keys: propose and approve read the audit log and refuse any operation whose client_key has
an unresolved op_unknown event (key_unresolved). Such a key may have reached InvenTree already. A
store built without an audit log refuses both, so none is proposed or approved unchecked.

Limit: the hash covers operations and previews, not the status, running or approval blocks.
Someone who can write the plan folder can still edit the approval block by hand, and so can
any process running as the same OS user. The guard against that is the gate, not the file:
approve() must be reached only from approve_cli, which needs a human at a terminal, and the
MCP server must not expose it. Approval is a control against accidental or drifting writes.
It is not a barrier against a hostile process running as the owner.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import tempfile
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Protocol

ALLOWED_OPS: Final[frozenset[str]] = frozenset(
    {
        "create_part",
        "update_part",
        "create_location",
        "create_stock_item",
        "transfer_stock",
        "adjust_stock_quantity",
    }
)
MAX_OPERATIONS: Final[int] = 50
MAX_CLIENT_KEY_LENGTH: Final[int] = 200
OWNER: Final[str] = "owner"

PROPOSED: Final[str] = "proposed"
APPROVED: Final[str] = "approved"
RUNNING: Final[str] = "running"
EXECUTED: Final[str] = "executed"
FAILED: Final[str] = "failed"
NEEDS_RECONCILE: Final[str] = "needs_reconcile"
_TERMINAL: Final[frozenset[str]] = frozenset({EXECUTED, FAILED})

_OPERATION_KEYS: Final[frozenset[str]] = frozenset({"op", "client_key", "params"})
_PLAN_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}")
_LOCK_WAIT_SECONDS: Final[float] = 5.0
_LOCK_POLL_SECONDS: Final[float] = 0.05
_STALE_LOCK_SECONDS: Final[float] = 300.0
_REPLACE_RETRIES: Final[int] = 5  # retries after the first attempt, so six attempts in all
_REPLACE_PAUSE_SECONDS: Final[float] = 0.05
_RECONCILE_MESSAGE: Final[str] = (
    "an operation in this plan has an unknown outcome; the owner must check InvenTree and the audit "
    "log and record the outcome by hand before any further plan runs"
)


class PlanError(Exception):
    """A refused plan operation. `code` is stable. `message` never echoes params or tokens."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class KeyLedger(Protocol):
    """Reads the client_keys whose earlier write has an unknown outcome. audit.AuditLog implements it."""

    def unresolved_keys(self) -> set[str]: ...


def compute_plan_hash(operations: list[Any], previews: list[Any]) -> str:
    """Return the sha256 hex digest of the canonical JSON of operations and previews."""
    canonical = json.dumps(
        {"operations": operations, "previews": previews},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _same(left: object, right: object) -> bool:
    """Constant-time string equality. Non-strings never match."""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _plain(value: Any, code: str, message: str) -> Any:
    """Round-trip through JSON so every stored value is a plain JSON value."""
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError):
        raise PlanError(code, message) from None


def _checked_operations(operations: Any) -> list[dict[str, Any]]:
    if not isinstance(operations, list):
        raise PlanError("invalid_operation", "operations must be a list")
    if not operations:
        raise PlanError("empty_plan", "a plan needs at least one operation")
    if len(operations) > MAX_OPERATIONS:
        raise PlanError(
            "too_many_operations",
            f"a plan holds at most {MAX_OPERATIONS} operations; split the work into more plans",
        )
    plain: list[Any] = _plain(operations, "invalid_operation", "operations must be plain JSON values")
    seen: set[str] = set()
    for index, op in enumerate(plain):
        if not isinstance(op, dict) or set(op) != _OPERATION_KEYS:
            raise PlanError(
                "invalid_operation",
                f"operation {index} must have exactly the keys op, client_key, params",
            )
        name = op["op"]
        if not isinstance(name, str) or name not in ALLOWED_OPS:
            raise PlanError("unknown_operation", f"operation {index} is not an allowed write operation")
        key = op["client_key"]
        if not isinstance(key, str) or not key.strip() or len(key) > MAX_CLIENT_KEY_LENGTH:
            raise PlanError("invalid_operation", f"operation {index} needs a non-empty client_key")
        if key in seen:
            raise PlanError(
                "duplicate_client_key",
                f"operation {index} repeats a client_key already used in this plan",
            )
        seen.add(key)
        if not isinstance(op["params"], dict):
            raise PlanError("invalid_operation", f"operation {index} params must be an object")
    return plain


def _checked_previews(previews: Any, operation_count: int) -> list[dict[str, Any]]:
    if not isinstance(previews, list) or len(previews) != operation_count:
        raise PlanError("preview_mismatch", "there must be exactly one preview per operation")
    plain: list[Any] = _plain(previews, "invalid_preview", "previews must be plain JSON values")
    for index, preview in enumerate(plain):
        if (
            not isinstance(preview, dict)
            or not isinstance(preview.get("before"), dict)
            or not isinstance(preview.get("after"), dict)
        ):
            raise PlanError("invalid_preview", f"preview {index} needs object values for before and after")
    return plain


def _plan_file(root: Path, plan_id: object) -> Path:
    if not isinstance(plan_id, str) or _PLAN_ID_PATTERN.fullmatch(plan_id) is None:
        raise PlanError("invalid_plan_id", "plan_id must be 32 lowercase hex characters")
    return root / f"{plan_id}.json"


def _lock_is_stale(lock_path: Path) -> bool:
    try:
        age = time.time() - os.path.getmtime(lock_path)
    except OSError:
        return False
    return age > _STALE_LOCK_SECONDS


def _replace_file(source: str, target: Path) -> None:
    """os.replace, retried while Windows holds the target open.

    Another process with the plan file open makes os.replace raise PermissionError. The retry
    waits _REPLACE_PAUSE_SECONDS between attempts, up to _REPLACE_RETRIES times. If the file is
    still locked after that, the change is refused. The caller then removes the temp file.
    """
    for attempt in range(_REPLACE_RETRIES + 1):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == _REPLACE_RETRIES:
                break
            time.sleep(_REPLACE_PAUSE_SECONDS)
    raise PlanError(
        "plan_file_locked",
        "the plan file is held open by another process, so the change was not saved; try again",
    )


def _check_approval(plan: dict[str, Any]) -> None:
    """The approval must name the owner and carry the hash that the plan still has."""
    approval = plan.get("approval")
    if (
        not isinstance(approval, dict)
        or approval.get("approved_by") != OWNER
        or not _same(approval.get("approved_hash"), plan["plan_hash"])
    ):
        raise PlanError("approval_mismatch", "the approval does not match this plan")


class PlanStore:
    """Plans as JSON files under root_dir. Every change re-verifies plan_hash first.

    audit is the log that holds unknown outcomes. propose and approve need it to refuse a
    client_key whose earlier write is unresolved. Without it they refuse, so no plan is
    proposed or approved unchecked. The other operations do not need it.
    """

    def __init__(self, root_dir: str | os.PathLike[str], *, audit: KeyLedger | None = None) -> None:
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._audit = audit

    @property
    def root_dir(self) -> Path:
        return self._root

    def _refuse_unresolved_keys(self, operations: list[dict[str, Any]]) -> None:
        """Refuse any operation whose client_key has an op_unknown that nothing has resolved.

        Fails closed: no audit log, or a log that cannot be read, refuses. A write that may have
        been applied must not be proposed again under the same key.
        """
        if self._audit is None:
            raise PlanError("audit_unavailable", "this plan store has no audit log, so client keys cannot be checked")
        try:
            unresolved = self._audit.unresolved_keys()
        except Exception:  # noqa: BLE001 - an unreadable log refuses (fail closed)
            raise PlanError(
                "audit_unreadable",
                "the audit log cannot be read, so client keys cannot be checked",
            ) from None
        for index, item in enumerate(operations):
            if item["client_key"] in unresolved:
                raise PlanError(
                    "key_unresolved",
                    f"operation {index} reuses a client_key whose earlier write has an unknown outcome; "
                    "the owner must settle that write in the audit log before the key can be used again",
                )

    def propose(self, operations: Any, previews: Any) -> dict[str, Any]:
        """Validate and store a new proposed plan. Nothing is written to InvenTree."""
        ops = _checked_operations(operations)
        prevs = _checked_previews(previews, len(ops))
        self._refuse_unresolved_keys(ops)
        plan: dict[str, Any] = {
            "plan_id": uuid.uuid4().hex,
            "created_at": _utc_now(),
            "operations": ops,
            "previews": prevs,
            "status": PROPOSED,
            "plan_hash": compute_plan_hash(ops, prevs),
            "approval": None,
            "running_at": None,
            "executed_at": None,
            "results": None,
        }
        self._write(plan)
        return plan

    def get(self, plan_id: str) -> dict[str, Any]:
        """Return the plan after verifying its hash. Raises plan_tampered for an edited file."""
        return self._load_verified(plan_id)

    def approve(self, plan_id: str, typed_hash: str, *, approved_by: str = OWNER) -> dict[str, Any]:
        """Record the owner's approval. Only approve_cli should call this.

        typed_hash must equal plan_hash exactly. The plan must be proposed. Approval refuses a
        client_key whose earlier write has an unknown outcome, the same as propose does.
        """
        if approved_by != OWNER:
            raise PlanError("not_owner", "only the owner can approve a plan")
        with self._locked(plan_id):
            plan = self._load_verified(plan_id)
            status = plan["status"]
            if status == APPROVED:
                raise PlanError("already_approved", "this plan is already approved")
            if status in _TERMINAL:
                raise PlanError("already_executed", "this plan has already run")
            if status != PROPOSED:
                raise PlanError("invalid_status", f"a {status} plan cannot be approved")
            if not _same(typed_hash, plan["plan_hash"]):
                raise PlanError("hash_mismatch", "the typed hash does not match plan_hash")
            self._refuse_unresolved_keys(plan["operations"])
            plan["status"] = APPROVED
            plan["approval"] = {
                "approved_at": _utc_now(),
                "approved_by": OWNER,
                "approved_hash": plan["plan_hash"],
            }
            self._write(plan)
        return plan

    def mark_running(self, plan_id: str) -> dict[str, Any]:
        """Move one approved plan to running, under the plan lock, before its first write.

        Only an approved plan with a matching approval moves. A running plan is never moved
        again, so a plan cannot start twice. Nothing else in the plan changes.
        """
        with self._locked(plan_id):
            plan = self._load_verified(plan_id)
            status = plan["status"]
            if status == RUNNING:
                raise PlanError(
                    "already_running",
                    "this plan is running or its run was never recorded; the owner must inspect it first",
                )
            if status in _TERMINAL:
                raise PlanError("already_executed", "this plan has already run")
            if status == NEEDS_RECONCILE:
                raise PlanError("needs_reconcile", _RECONCILE_MESSAGE)
            if status != APPROVED:
                raise PlanError("not_approved", "only an approved plan can start running")
            _check_approval(plan)
            plan["status"] = RUNNING
            plan["running_at"] = _utc_now()
            self._write(plan)
        return plan

    def mark_executed(self, plan_id: str, results: Any, failed: bool = False) -> dict[str, Any]:
        """Record the outcome of a running plan. A plan can be recorded only once.

        Refuses unless the plan is running, its approval matches plan_hash, and the file
        still matches its hash. A plan that is only approved was never started, so it is
        refused as not_running.
        """
        return self._finish(plan_id, results, FAILED if failed else EXECUTED)

    def mark_needs_reconcile(self, plan_id: str, results: Any) -> dict[str, Any]:
        """Record a run that stopped on an operation whose outcome is unknown. Same checks as mark_executed.

        The plan is neither executed nor failed. A write may or may not have reached InvenTree, so
        it cannot be retried, and it cannot be marked either way by this module. The owner settles it.
        """
        return self._finish(plan_id, results, NEEDS_RECONCILE)

    def _finish(self, plan_id: str, results: Any, status: str) -> dict[str, Any]:
        with self._locked(plan_id):
            plan = self._load_verified(plan_id)
            current = plan["status"]
            if current in _TERMINAL:
                raise PlanError("already_executed", "this plan has already run")
            if current == NEEDS_RECONCILE:
                raise PlanError("needs_reconcile", _RECONCILE_MESSAGE)
            if current == PROPOSED:
                raise PlanError("not_approved", "only an approved plan can run")
            if current == APPROVED:
                raise PlanError("not_running", "a plan must be marked running before its result is recorded")
            if current != RUNNING:
                raise PlanError("invalid_status", f"a {current} plan cannot be recorded")
            _check_approval(plan)
            stored = _plain(results, "invalid_results", "results must be plain JSON values")
            if not isinstance(stored, list):
                raise PlanError("invalid_results", "results must be a list")
            plan["status"] = status
            plan["executed_at"] = _utc_now()
            plan["results"] = stored
            self._write(plan)
        return plan

    def _load_verified(self, plan_id: str) -> dict[str, Any]:
        path = _plan_file(self._root, plan_id)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise PlanError("not_found", "no plan has this id") from None
        try:
            plan = json.loads(text)
        except json.JSONDecodeError:
            raise PlanError("corrupt_plan", "plan file is not valid JSON") from None
        if not isinstance(plan, dict):
            raise PlanError("corrupt_plan", "plan file is not a JSON object")
        try:
            ops = _checked_operations(plan.get("operations"))
            prevs = _checked_previews(plan.get("previews"), len(ops))
            recomputed = compute_plan_hash(ops, prevs)
        except PlanError:
            raise PlanError("plan_tampered", "plan contents fail validation; refusing to use it") from None
        if not _same(recomputed, plan.get("plan_hash")):
            raise PlanError("plan_tampered", "plan contents do not match plan_hash; refusing to use it")
        if plan.get("plan_id") != path.stem:
            raise PlanError("plan_tampered", "plan_id inside the file does not match its name")
        return plan

    def _write(self, plan: dict[str, Any]) -> None:
        """Replace the plan file atomically: write a temp file, then os.replace it."""
        path = _plan_file(self._root, plan["plan_id"])
        fd, tmp_name = tempfile.mkstemp(dir=self._root, prefix=".plan-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(plan, handle, indent=2, sort_keys=True, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            _replace_file(tmp_name, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise

    @contextlib.contextmanager
    def _locked(self, plan_id: str) -> Iterator[None]:
        """Hold an exclusive lock file for one plan, so two changes cannot interleave."""
        lock_path = _plan_file(self._root, plan_id).with_suffix(".lock")
        deadline = time.monotonic() + _LOCK_WAIT_SECONDS
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if _lock_is_stale(lock_path):
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(lock_path)
                    continue
                if time.monotonic() >= deadline:
                    raise PlanError(
                        "plan_busy",
                        "another change to this plan is in progress; try again",
                    ) from None
                time.sleep(_LOCK_POLL_SECONDS)
                continue
            os.close(fd)
            break
        try:
            yield
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(lock_path)
