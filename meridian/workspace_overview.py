"""Local workspace overview (phase 1): a read-only join of what this machine knows.

``build_overview`` joins the local DB (projects, sessions, active worktrees and
prospect receipts), each project's ``executor_config.repo_paths``, the
codebase-memory snapshot, per-repo ``.claude/settings.json`` and ``.mcp.json``
(key names only), the guard directory's ``config.json`` and ``audit.log`` tail,
and the tunnel slot-claim files. Served by ``GET /workspace/overview``
(``meridian/routes/workspace_overview.py``), loopback only.

Read-only by construction: no DB writes, no ``cbm_registry.refresh``, no file
writes, no subprocess. Filesystem reads are capped per file and per request
(``SCAN_BUDGET_BYTES`` / ``SCAN_BUDGET_S``) and run in a worker thread.

Constants marked NEW have no earlier source in the repo; they are phase 1 values.
Every unknown value is ``None`` (never a fabricated ``0``, ``false`` or ``""``),
and each unknown or null-by-design field has a gap code in ``gaps``.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import socket
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import _deps
from . import cbm_registry as reg
from . import code_intel_receipt as _receipts
from . import db as db_module
from . import guard_core as gc
from . import hook_paths as hp
from . import session_brief as sb
from . import setup_bundle as _bundle
from . import toml_config
from .hook_settings_merge import CONFIG_NAME

SCHEMA = "meridian-workspace-overview/1"

# Reused constants (values unchanged).
STALE_DAYS = reg.STALE_DAYS  # cbm_registry.py:77
AUDIT_TAIL_BYTES = sb.AUDIT_TAIL_BYTES  # session_brief.py:120

# New phase 1 constants.
LIVE_WINDOW_S = 600  # NEW mirror of dashboard-utils.ts:15 (10 * 60 * 1000 ms)
LIVE_WINDOW_MS = LIVE_WINDOW_S * 1000
RECENT_WINDOW_S = 86400  # NEW; mirrors the "last 24h" comment at dashboard.ts:5195
RECEIPT_WINDOW_DAYS = 7  # NEW
MAX_RECEIPTS_PER_PROJECT = 500  # NEW
MAX_REPOS = 300  # NEW response cap
MAX_SESSIONS = 200  # NEW response cap
SETTINGS_MAX_BYTES = 1024 * 1024  # NEW
MCP_MAX_BYTES = 1024 * 1024  # NEW
CONFIG_MAX_BYTES = 1024 * 1024  # NEW
CLAIM_MAX_FILES = 64  # NEW
CLAIM_MAX_BYTES = 4096  # NEW
SNAPSHOT_MAX_BYTES = 64 * 1024 * 1024  # value of cbm_registry.load_snapshot's read limit (:1076)
SCAN_BUDGET_BYTES = 64 * 1024 * 1024  # NEW
SCAN_BUDGET_S = 2.0  # NEW
PROBLEMS_MAX = 5  # NEW
MANAGED_KEY_PREFIX = _bundle.MANAGED_PREFIX + "codebase_memory_"  # setup_bundle.py:26, :34, :70-71
GUARD_COMPONENT_NAMES = frozenset(hp.GUARD_COMPONENTS.values())  # hook_paths.py:327-331

_SOURCE_ORDER = (
    "project_repo_paths",
    "project_repo_paths_legacy",
    "active_worktree",
    "index_snapshot",
    "guard_install_record",
)
_PROJECT_REPO_SOURCES = frozenset({"project_repo_paths", "project_repo_paths_legacy"})
_RECEIPT_REPO_SOURCES = frozenset({"explicit_root_dir", "session_worktree"})
_PROJECT_STATUSES = frozenset({"active", "parked", "archived"})
_SESSION_STATES_SCOPED = frozenset({"active", "idle"})

_UNKNOWN_CLIENT_REASON = "tunnel process not detected"
_FOUND_CLIENT_REASON = "tunnel process found"
_HOSTED_CONNECTION_REASON = "The local server cannot see the hosted tunnel connection."
_AUDIT_ABSENT = "no audit.log in the guard dir"
_AUDIT_UNREADABLE = "unknown: audit.log unreadable"
_AUDIT_NOT_IN_TAIL = "unknown: not in the last 256 KiB of audit.log"
_AUDIT_NO_EVENT = "no guard event recorded for this repo in audit.log"
_AUDIT_NOT_SCANNED = "unknown: scan budget reached before audit.log was read"
_AUDIT_EVENT_MALFORMED = "unknown: guard event timestamp is malformed"

# (code, always_present, fixed message, source). Spec section 2.5. Conditional
# codes are added in _gap_codes when their condition holds.
_GAP_TABLE: tuple[tuple[str, bool, str, str], ...] = (
    (
        "tunnel_hosted_connection_unknown", True,
        "Hosted tunnel connection state is not visible to the local server.",
        "meridian/routes/tunnel.py:4118-4133, meridian/tray_main.py:237",
    ),
    (
        "tunnel_claim_signal_partial", True,
        "Tunnel client detection uses slot-claim files, which exist only after slot proxies start. "
        "A missing claim does not mean no tunnel.",
        "meridian/tunnel_client.py:4076-4108",
    ),
    (
        "sessions_location_unknown", True,
        "Sessions record no working directory or hostname.",
        "meridian/db/__init__.py:412-426",
    ),
    (
        "guard_mode_server_process", True,
        "Guard mode is read from this server process's environment and sentinel files, "
        "not from the Claude Code session.",
        "meridian/routes/hooks.py:186-187, meridian/guard_core.py:2627-2654",
    ),
    (
        "guard_runtime_server_process", False,
        "Hook runtime checks (script, interpreter, PATH) run in this server process and can differ "
        "from the Claude Code session.",
        "meridian/hook_paths.py:415-417",
    ),
    (
        "guard_audit_tail_limited", False,
        "Guard events are read from the last 256 KiB of audit.log only.",
        "meridian/session_brief.py:120, meridian/session_brief.py:507-521",
    ),
    (
        "guard_event_root_is_index_root", False,
        "Audit events record the winning index root, not the session cwd.",
        "meridian/guard_core.py:1475-1476, meridian/guard_core.py:2942-2944",
    ),
    (
        "user_scope_guard_not_scanned", True,
        "User-scope hook entries in ~/.claude/settings.json are not scanned, only per-repo settings.",
        "meridian/hook_paths.py:428, meridian/hook_settings_merge.py:1358",
    ),
    (
        "snapshot_written_at_session_start", False,
        "The index snapshot is written at SessionStart or by manual refresh, so its age can be large.",
        "meridian/session_brief.py:53-57, meridian/session_brief.py:102",
    ),
    (
        "snapshot_machine_wide", False,
        "The snapshot covers one codebase-memory cache directory per machine. "
        "It is not a per-repo install list.",
        "meridian/cbm_registry.py:978-1001, meridian/cbm_registry.py:327-340",
    ),
    (
        "repo_last_use_partial", True,
        "No per-repo last-use record. Only guard non-allow events and prospect receipts are shown.",
        "meridian/guard_core.py:2932-2947, meridian/code_intel_receipt.py:539-554",
    ),
    (
        "session_repo_link_partial", True,
        "A session links to a repo only through active_worktrees. "
        "Project-level repo links are not session-level.",
        "meridian/db/__init__.py:9988-10020, meridian/executor_config.py:387-419",
    ),
    (
        "project_status_manual", True,
        "Project status is a manual flag, not derived from activity.",
        "meridian/db/__init__.py:394-395, meridian/routes/projects.py:978",
    ),
    (
        "project_activity_excludes_closed", True,
        "Project activity counts active and idle sessions only. Closed and archived sessions are excluded.",
        "meridian/workspace_overview.py (project activity rule)",
    ),
    (
        "hostname_unverified", False,
        "Recorded hostnames are short names taken from hook payloads. Their format is not verified.",
        "meridian/server.py:7262, meridian/server.py:7505, meridian/server.py:7510",
    ),
    (
        "repo_root_unresolved", False,
        "No .git was found above this path. The key is the normalized path itself.",
        "meridian/cbm_registry.py:488-530",
    ),
    (
        "index_nodes_zero_ambiguous", False,
        "A node count of 0 can mean the count was never recorded.",
        "meridian/cbm_registry.py:837, meridian/cbm_registry.py:860",
    ),
    (
        "receipts_default_root_excluded", True,
        "Receipts whose repo root came from the server's default checkout are not attributed to any repo.",
        "meridian/code_intel_receipt.py:445-450",
    ),
    (
        "receipts_window_truncated", False,
        "The receipt query hit its row limit, so the last-receipt time can be older than the true value.",
        "meridian/db/workspace.py:2643, meridian/db/workspace.py:2666",
    ),
    (
        "scan_budget_reached", False,
        "The filesystem scan stopped at its read or time budget. "
        "Repos after the stop show guard state not_scanned.",
        "meridian/workspace_overview.py (scan budget)",
    ),
    (
        "tunnel_claims_unreadable", False,
        "The tunnel slot-claim directory could not be read. Live tunnel claims are unknown.",
        "meridian/workspace_overview.py (claim directory read)",
    ),
    (
        "repo_facts_unavailable", False,
        "The facts for at least one repo could not be read. Its guard and index state is shown as unknown.",
        "meridian/workspace_overview.py (per-repo facts)",
    ),
    (
        "section_failed", False,
        "A part of this overview could not be built. The fields it covers are shown as unknown.",
        "meridian/workspace_overview.py (section guards)",
    ),
    (
        "receipt_root_compare_normalized", True,

        "Receipt roots are compared after lexical normalization, not resolved paths. "
        "A rare mismatch is possible.",
        "meridian/code_intel_receipt.py:221-252",
    ),
)
_GAP_BY_CODE = {code: (message, source) for code, _always, message, source in _GAP_TABLE}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _add_error(errors: list[tuple[str, str]], section: str, code: str) -> None:
    if (section, code) not in errors:
        errors.append((section, code))


def _iso(epoch: float | int) -> str | None:
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def _iso_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event_reason(event_iso: str | None, no_event_reason: str, malformed: bool) -> str | None:
    """``last_guard_event_reason`` (spec 3.6): null only when the timestamp is set.

    A line whose time cannot be rendered never wins the maximum (N2), so it reaches here only as
    ``malformed``. With no renderable time, a malformed line gives the fixed reason, so the reason
    is never null next to a null timestamp.
    """
    if event_iso is not None:
        return None
    return _AUDIT_EVENT_MALFORMED if malformed else no_event_reason


def _json_safe(value: Any) -> Any:
    """Replace NaN and Infinity with null at every depth, so the body is strict JSON (spec 2).

    ``json.loads`` accepts the NaN and Infinity literals, so a snapshot or audit value can carry
    one. A non-finite number is an unknown value, and unknown is null (spec 2.1).
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


def _short_host(value: str) -> str:
    """Short host name: first DNS label, lower-cased, not truncated (spec 5.4)."""
    return value.strip().split(".")[0].lower()


def _recorded_host(raw: Any) -> str | None:
    """A recorded hook hostname, or None when it is empty or the 'unknown' placeholder."""
    if not isinstance(raw, str):
        return None
    short = _short_host(raw)
    return short if short and short != "unknown" else None


def parse_last_seen(value: Any) -> datetime | None:
    """Parse a session ``last_seen`` value to an aware UTC datetime, or None (spec 3.4).

    A datetime without tzinfo is UTC. A string with ``T`` is ISO; a trailing ``Z`` is
    read as ``+00:00``. A space-form string becomes ``T`` plus ``Z``, matching
    ``dashboard-utils.ts:92``. A zone-less ``T`` form is UTC here (open question 14).
    """
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if "T" not in text:
        text = text.replace(" ", "T", 1) + "Z"
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_ms(parsed: datetime | None, now_dt: datetime) -> float | None:
    if parsed is None:
        return None
    return (now_dt - parsed).total_seconds() * 1000.0


def _raw_last_seen(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _exec_config(raw: Any) -> dict[str, Any]:
    """Decode ``executor_config`` the way ``get_project_settings`` does (db/__init__.py:2196-2215)."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _local_hostname() -> str | None:
    """This machine's short host name (``socket.gethostname``, first label, lower-cased)."""
    try:
        raw = socket.gethostname()
    except OSError:
        return None
    short = _short_host(raw or "")
    return short or None


def _connection_name() -> str | None:
    """Display name of the active DB profile. The URL half is discarded (spec 2.1)."""
    try:
        name = toml_config.get_active_db_url()[1]
    except Exception:  # noqa: BLE001 - a config read problem must not fail the overview
        return None
    return name if isinstance(name, str) else None


# ---------------------------------------------------------------------------
# Read budget and capped file reads (spec 3.7, 3.8)
# ---------------------------------------------------------------------------


class _Budget:
    """Bytes and wall time for one request's filesystem scan (spec 3.8)."""

    def __init__(self, limit_bytes: int, deadline: float) -> None:
        self.limit_bytes = limit_bytes
        self.deadline = deadline
        self.bytes_read = 0
        self.stopped: str | None = None

    def charge(self, count: int) -> None:
        self.bytes_read += max(0, int(count))

    def stop_reason(self) -> str | None:
        if self.bytes_read >= self.limit_bytes:
            return "bytes"
        if time.monotonic() >= self.deadline:
            return "time"
        return None


def _may_read(budget: _Budget) -> bool:
    """True while the request budget has room. The first stop is recorded and sticks (spec 3.8)."""
    if budget.stopped is None:
        budget.stopped = budget.stop_reason()
    return budget.stopped is None


def _read_capped(path: str, cap: int, budget: _Budget) -> tuple[str, bytes | None]:
    """Read at most ``cap + 1`` bytes. Returns ``(status, data)``.

    ``status`` is ``ok``, ``missing`` (FileNotFoundError), ``over`` (larger than ``cap``)
    or ``unreadable`` (any other OSError, including a directory at that path).
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read(cap + 1)
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "unreadable", None
    budget.charge(len(data))
    if len(data) > cap:
        return "over", None
    return "ok", data


# ---------------------------------------------------------------------------
# Snapshot, config, audit and claim reads (run in the worker thread)
# ---------------------------------------------------------------------------


def _read_snapshot(path: str | None, budget: _Budget, errors: list[tuple[str, str]]) -> dict[str, Any]:
    """Classify the codebase-memory snapshot (spec 3.7). Never reuses cbm_registry._read_json."""
    out: dict[str, Any] = {
        "state": "missing", "present": False, "path": path, "schema": None,
        "built_at": None, "stats": None, "valid": None,
    }
    if not path:
        return out

    def _bad(state: str, code: str) -> dict[str, Any]:
        out["state"] = state
        out["present"] = True
        _add_error(errors, "repos", code)
        return out

    try:
        fh = open(path, "rb")
    except FileNotFoundError:
        return out
    except OSError:
        return _bad("unreadable", "snapshot_unreadable")
    with fh:
        try:
            data = fh.read(SNAPSHOT_MAX_BYTES + 1)
        except OSError:
            return _bad("unreadable", "snapshot_unreadable")
    budget.charge(len(data))
    out["present"] = True
    if len(data) > SNAPSHOT_MAX_BYTES:
        return _bad("unreadable", "snapshot_unreadable")
    try:
        parsed = json.loads(data.decode("utf-8"))
    except ValueError:
        return _bad("corrupt", "snapshot_unreadable")
    if not isinstance(parsed, dict):
        return _bad("corrupt", "snapshot_unreadable")
    out["schema"] = parsed.get("schema") if isinstance(parsed.get("schema"), str) else None
    if parsed.get("schema") != reg.SNAPSHOT_SCHEMA or not isinstance(parsed.get("rows"), list):
        return _bad("corrupt", "snapshot_unreadable")
    valid = reg.valid_snapshot(parsed)
    if valid is None:
        return _bad("corrupt", "snapshot_unreadable")
    out["state"] = "ok"
    out["valid"] = valid
    out["built_at"] = _int_or_none(parsed.get("built_at"))
    stats = parsed.get("stats")
    if isinstance(stats, dict):
        out["stats"] = {k: v for k, v in stats.items() if isinstance(k, str) and _int_or_none(v) is not None}
    return out


def _read_config(gdir: str | None, budget: _Budget, errors: list[tuple[str, str]]) -> dict[str, Any]:
    """The guard dir ``config.json``: install records and the user-scope flag (spec 2.1, 3.5)."""
    out: dict[str, Any] = {
        "state": "absent", "installs": {}, "install_paths": [], "user_scope": None, "over_cap": False,
    }
    if not gdir:
        return out
    status, data = _read_capped(gdir.rstrip("/") + "/" + CONFIG_NAME, CONFIG_MAX_BYTES, budget)
    if status == "missing":
        return out
    if status == "over":
        out["over_cap"] = True
    if status != "ok":
        out["state"] = "unreadable"
        _add_error(errors, "runtime", "config_unreadable")
        return out
    try:
        parsed = json.loads(data.decode("utf-8"))
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        out["state"] = "unreadable"
        _add_error(errors, "runtime", "config_unreadable")
        return out
    out["state"] = "ok"
    installs = parsed.get("installs")
    if not isinstance(installs, dict):
        installs = {}
    out["user_scope"] = "user" in installs
    for key, record in installs.items():
        if not isinstance(key, str) or not key.startswith("project:") or not isinstance(record, dict):
            continue
        repo = reg.norm_path(key[len("project:"):], None, msys=True)
        if not repo:
            continue
        out["installs"][reg.path_key(repo)] = {
            "installed_at": _str_or_none(record.get("installed_at")),
            "mode": _str_or_none(record.get("mode")),
        }
        out["install_paths"].append(repo)
    return out


def _read_audit(gdir: str | None, fs: Any, budget: _Budget, errors: list[tuple[str, str]]) -> dict[str, Any]:
    """Latest guard event time per root key, from the last AUDIT_TAIL_BYTES of audit.log."""
    out: dict[str, Any] = {"state": "absent", "limited": False, "events": {}, "malformed": set()}
    if not gdir:
        return out
    path = gdir.rstrip("/") + "/audit.log"
    try:
        size = os.stat(path).st_size
    except FileNotFoundError:
        return out
    except OSError:
        out["state"] = "unreadable"
        _add_error(errors, "repos", "audit_unreadable")
        return out
    out["limited"] = size > AUDIT_TAIL_BYTES
    budget.charge(min(size, AUDIT_TAIL_BYTES))
    text = sb._audit_tail(fs, path)
    if text is None:
        out["state"] = "unreadable"
        _add_error(errors, "repos", "audit_unreadable")
        return out
    out["state"] = "ok"
    events: dict[str, float] = {}
    malformed: set[str] = set()  # keys with a line whose time cannot render (N2)
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        root = record.get("root")
        ts = record.get("ts")
        if not isinstance(root, str) or not root:
            continue
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            continue
        if isinstance(ts, float) and not math.isfinite(ts):
            continue  # NaN and Infinity are malformed timestamps, skipped like the lines above
        key = reg.path_key(reg.norm_path(root, None, msys=True))
        if key is None:
            continue
        if _iso(ts) is None:
            malformed.add(key)  # counted for the reason only: it never wins the maximum (N2)
            continue
        if key not in events or ts > events[key]:
            events[key] = ts
    out["events"] = events
    out["malformed"] = malformed
    return out


def _claims_not_scanned() -> dict[str, Any]:
    return {"client_process": "unknown", "live_slot_claims": None, "unreadable": False}


def _read_claims(budget: _Budget, errors: list[tuple[str, str]]) -> dict[str, Any]:
    """Tunnel client liveness from slot-claim files (spec 5.2). Never returns a pid or port.

    The directory is listed with ``os.scandir``, which raises on a listing failure. The
    earlier ``Path.glob`` form could report zero claims instead, because pathlib swallows
    PermissionError inside its wildcard selector (review M1).
    """
    out: dict[str, Any] = {"client_process": "unknown", "live_slot_claims": None, "unreadable": False}

    def _unreadable() -> dict[str, Any]:
        out["unreadable"] = True
        _add_error(errors, "runtime", "tunnel_claims_unreadable")
        return out

    try:
        claim_dir = str(Path.home() / ".meridian" / "slot_claims")
    except (OSError, RuntimeError, KeyError):
        return _unreadable()
    try:
        with os.scandir(claim_dir) as entries:
            names = sorted(entry.name for entry in entries if entry.name.endswith(".json"))
    except (FileNotFoundError, NotADirectoryError):
        names = []  # no claim directory, or a file where it should be: no claims
    except OSError:
        return _unreadable()
    files = [os.path.join(claim_dir, name) for name in names[:CLAIM_MAX_FILES]]
    if not files:
        out["live_slot_claims"] = 0
        return out
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        _add_error(errors, "runtime", "psutil_missing")
        return out
    live = 0
    for claim_path in files:
        if not _may_read(budget):
            return out  # a partial count is not reported: live_slot_claims stays null
        status, data = _read_capped(claim_path, CLAIM_MAX_BYTES, budget)
        if status != "ok" or data is None:
            continue
        try:
            claim = json.loads(data.decode("utf-8"))
        except ValueError:
            continue
        if not isinstance(claim, dict):
            continue
        pid = claim.get("tunnel_pid")
        if isinstance(pid, bool) or not isinstance(pid, int):
            continue
        create_time = claim.get("create_time")
        try:
            if create_time is not None:
                alive = abs(psutil.Process(pid).create_time() - float(create_time)) < 1.0
            else:
                alive = bool(psutil.pid_exists(pid))
        except Exception:  # noqa: BLE001 - no such pid, bad value or psutil error: not live
            alive = False
        if alive:
            live += 1
    out["live_slot_claims"] = live
    out["client_process"] = "running" if live else "unknown"
    return out


# ---------------------------------------------------------------------------
# Repo identity (spec 4)
# ---------------------------------------------------------------------------


def _identity(norm: str, fs: Any) -> dict[str, Any]:
    """Git root first, then the repo key (spec 4.1 and 4.3). Reads only small .git files."""
    try:
        worktree_root = reg.git_roots(norm, fs)[0]
    except Exception:  # noqa: BLE001 - git_roots is documented never to raise; stay defensive
        worktree_root = None
    root = worktree_root or norm
    key = reg.path_key(reg.norm_path(root, None, msys=True) or root)
    return {"root": root, "key": key, "unresolved": worktree_root is None}


def _identity_skipped(norm: str) -> dict[str, Any]:
    """Identity once the scan budget has stopped, with no git-file reads (spec 3.8).

    The limit, stated exactly: once the budget has stopped, identity is not resolved. A path keys by
    its normalized path, so a subdirectory of a git root that was resolved before the stop can show as
    its own row instead of joining that root's row. A nested git checkout or linked worktree (one with
    its own ``.git``) always gets its own row, because its key is its own normalized path.
    ``unresolved`` is None rather than True, because no git root was looked for, so nothing claims that
    a ``.git`` is missing.
    """
    return {"root": norm, "key": reg.path_key(reg.norm_path(norm, None, msys=True) or norm), "unresolved": None}


def _identify_candidates(
    inputs: list[tuple[str, Any, str | None, Any]],
    valid_snapshot: dict[str, Any] | None,
    install_paths: list[str],
    fs: Any,
    budget: _Budget,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Map every candidate path to its repo key. Returns (candidates, key by normalized path).

    A path is resolved to its git root only while the budget has room (spec 3.8). A path after
    the stop keys by its normalized form, with no root resolved (see ``_identity_skipped``).
    """
    cands: list[dict[str, Any]] = []
    key_by_norm: dict[str, str] = {}
    cache: dict[str, dict[str, Any]] = {}

    def add(source: str, raw: Any, project_id: str | None, hostname: str | None) -> None:
        norm = reg.norm_path(raw, None, msys=True) if isinstance(raw, str) else None
        if not norm:
            return
        ident = cache.get(norm)
        if ident is None:
            if _may_read(budget):
                ident = _identity(norm, fs)
            else:
                ident = _identity_skipped(norm)
            cache[norm] = ident
        key_by_norm[norm] = ident["key"]
        cands.append({
            "source": source, "project_id": project_id, "hostname": hostname, "norm": norm,
            "root": ident["root"], "key": ident["key"], "unresolved": ident["unresolved"],
        })

    for source, raw, project_id, host in inputs:
        add(source, raw, project_id, _recorded_host(host))
    rows = (valid_snapshot or {}).get("rows") or []
    for row in rows:
        add("index_snapshot", row.get("root"), None, None)
    for repo in install_paths:
        add("guard_install_record", repo, None, None)
    return cands, key_by_norm


# ---------------------------------------------------------------------------
# Per-repo facts (spec 2.2, 3.3 inputs, 3.5, 3.6)
# ---------------------------------------------------------------------------


def _read_settings(root: str, budget: _Budget, errors: list[tuple[str, str]]) -> tuple[str, list[tuple[str, str]]]:
    """``.claude/settings.json`` as ``(settings_state, [(event, command)])``."""
    status, data = _read_capped(root.rstrip("/") + "/.claude/settings.json", SETTINGS_MAX_BYTES, budget)
    if status == "missing":
        return "absent", []
    if status != "ok" or data is None:
        _add_error(errors, "repos", "settings_unreadable")
        return "unreadable", []
    try:
        settings = json.loads(data.decode("utf-8"))
        if not isinstance(settings, dict):
            raise ValueError("settings is not an object")
        pairs = hp.parse_hook_commands(settings)
    except (ValueError, AttributeError, TypeError):
        _add_error(errors, "repos", "settings_unreadable")
        return "unreadable", []
    return "present", [(event, cmd) for event, cmd in pairs if isinstance(cmd, str)]


def _read_mcp(root: str, budget: _Budget, errors: list[tuple[str, str]]) -> bool | None:
    """Whether ``.mcp.json`` has a managed codebase-memory key (key names only, spec 3.5)."""
    status, data = _read_capped(root.rstrip("/") + "/.mcp.json", MCP_MAX_BYTES, budget)
    if status == "missing":
        return False
    if status != "ok" or data is None:
        _add_error(errors, "repos", "mcp_unreadable")
        return None
    try:
        cfg = json.loads(data.decode("utf-8"))
    except ValueError:
        _add_error(errors, "repos", "mcp_unreadable")
        return None
    if not isinstance(cfg, dict):
        _add_error(errors, "repos", "mcp_unreadable")
        return None
    servers = cfg.get("mcpServers")
    if not isinstance(servers, dict):
        return False
    return any(isinstance(key, str) and key.startswith(MANAGED_KEY_PREFIX) for key in servers)


def _guard_fields(settings_state: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Guard summary for one repo from its guard hook entries (spec 2.2, hook_paths.summarize_guard).

    ``components`` is empty whenever ``settings_state`` is not ``present``; read it
    together with ``settings_state`` and ``registered``.
    """
    out: dict[str, Any] = {
        "settings_state": settings_state, "registered": None, "components": [], "complete": None,
        "installed_mode": None, "scope": None, "runtime_ok": None, "problems": [],
    }
    if settings_state == "absent":
        out.update(registered=False, complete=False)
        return out
    if settings_state != "present":
        return out
    summary = hp.summarize_guard(entries)
    out["registered"] = summary["registered"]
    out["components"] = sorted(c for c in summary["components"] if c in GUARD_COMPONENT_NAMES)
    out["complete"] = summary["complete"]
    if summary["registered"]:
        modes = {e["guard"].get("installed_mode") for e in entries}
        scopes = {e["guard"].get("scope") for e in entries}
        out["installed_mode"] = "advisory" if "advisory" in modes else "enforce"
        out["scope"] = "user" if "user" in scopes else "project"
        out["runtime_ok"] = summary["runtime_ok"]
        out["problems"] = list(summary["problems"][:PROBLEMS_MAX])
    return out


def _index_facts(
    root: str, valid: dict[str, Any] | None, fs: Any, env: dict[str, Any], now: float, snapshot_state: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Index facts for one repo, from ``cbm_registry.resolve`` (spec 3.5, not re-derived)."""
    res = reg.resolve(root, valid, fs, env)
    mode = res.get("mode") or "none"
    winner = res.get("winner")
    index: dict[str, Any] = {
        "mode": mode, "why": res.get("why"), "pinned": mode == "pin", "project": None,
        "index_root": None, "shadowed": sorted(res.get("shadowed") or []), "partial": None,
        "present": None, "fresh": None, "age_days": None, "last_index_write": None,
        "nodes": None, "files": None, "db_bytes": None, "covers_repo_root": None,
        "slug_match": None, "snapshot_state": snapshot_state,
    }
    if not isinstance(winner, dict):
        return index, None
    fr = reg.freshness(winner, fs, now)
    sig = winner.get("sig")
    covered = winner.get("covered_dirs")
    index.update(
        project=_str_or_none(winner.get("name")),
        index_root=_str_or_none(winner.get("root")),
        partial=winner["partial"] if isinstance(winner.get("partial"), bool) else None,
        present=fr["present"],
        fresh=fr["fresh"],
        age_days=fr["age_days"],
        last_index_write=_iso(fr["last_activity"]) if fr["last_activity"] else None,
        nodes=_int_or_none(winner.get("nodes")),
        files=_int_or_none(winner.get("files")),
        db_bytes=_int_or_none(sig[0]) if isinstance(sig, list) and sig else None,
        covers_repo_root=None if not isinstance(covered, list) else bool(covered),
        slug_match=winner["slug_match"] if isinstance(winner.get("slug_match"), bool) else None,
    )
    return index, winner


def _audit_reason(audit: dict[str, Any]) -> str:
    if audit["state"] == "not_scanned":
        return _AUDIT_NOT_SCANNED
    if audit["state"] == "unreadable":
        return _AUDIT_UNREADABLE
    if audit["state"] == "absent":
        return _AUDIT_ABSENT
    if audit["limited"]:
        return _AUDIT_NOT_IN_TAIL
    return _AUDIT_NO_EVENT


def _index_unknown(snapshot_state: str, why: str) -> dict[str, Any]:
    """Index facts that were not read because the scan budget stopped first.

    Every probe is null, and so is ``mode``: an unread index is unknown, never ``"none"``.
    """
    # R3 (decision): index facts are null once the budget stops, which supersedes spec r2 3.8 for
    # index facts. resolve() calls git_roots (cbm_registry.py:670), which probes .git at :507 and
    # :511, and freshness() stats the .db (:733, :735), so the snapshot row alone cannot supply them.
    return {
        "mode": None, "why": why, "pinned": None, "project": None, "index_root": None,
        "shadowed": [], "partial": None, "present": None, "fresh": None, "age_days": None,
        "last_index_write": None, "nodes": None, "files": None, "db_bytes": None,
        "covers_repo_root": None, "slug_match": None, "snapshot_state": snapshot_state,
    }


def _canonical_empty() -> dict[str, Any]:
    return {"worktree_root": None, "canonical_root": None, "linked": None, "canonical_key": None}


def _canonical(root: str, unresolved: bool | None, fs: Any) -> dict[str, Any]:
    """Linked-worktree facts from the git files (spec 2.2, cbm_registry.git_roots)."""
    if unresolved:
        return _canonical_empty()
    try:
        worktree_root, canonical_root, linked = reg.git_roots(root, fs)
    except Exception:  # noqa: BLE001 - defensive; see _identity
        return _canonical_empty()
    if worktree_root is None:
        return _canonical_empty()
    return {
        "worktree_root": worktree_root,
        "canonical_root": canonical_root,
        "linked": linked,
        "canonical_key": reg.path_key(canonical_root) if canonical_root else None,
    }


def _config_not_scanned() -> dict[str, Any]:
    return {"state": "not_scanned", "installs": {}, "install_paths": [], "user_scope": None, "over_cap": False}


def _audit_not_scanned() -> dict[str, Any]:
    return {"state": "not_scanned", "limited": False, "events": {}, "malformed": set()}


def _repo_facts(key: str, root: str, unresolved: bool | None, ctx: dict[str, Any]) -> dict[str, Any]:
    """Everything about one repo that comes from the filesystem (spec 3.8: budget-gated steps).

    Each read, git-file probe and index probe runs only while the budget has room. A skipped
    step gives its unknown value, never a fabricated ``0``, ``false`` or ``""``.
    """
    budget: _Budget = ctx["budget"]
    errors: list[tuple[str, str]] = ctx["errors"]
    if _may_read(budget):
        settings_state, commands = _read_settings(root, budget, errors)
    else:
        settings_state, commands = "not_scanned", []
    managed = _read_mcp(root, budget, errors) if _may_read(budget) else None
    entries: list[dict[str, Any]] = []
    if settings_state == "present":
        for event, command in commands:
            if hp.guard_script_token(command) is None:
                continue
            diag = hp.diagnose_guard_command(command, Path(root), guard_dir=ctx["diag_gdir"], env=ctx["env"])
            entries.append({"event": event, "guard": diag})
    guard = _guard_fields(settings_state, entries)
    record = ctx["installs"].get(key)
    guard["record"] = dict(record) if record else None
    installed = guard["installed_mode"]
    record_mode = record.get("mode") if record else None
    guard["record_mode_matches"] = (record_mode == installed) if record_mode and installed else None

    # R3 (decision): index facts are null once the budget stops. resolve() and freshness() probe the
    # filesystem (see _index_unknown), so this supersedes spec r2 3.8 for index facts only.
    if _may_read(budget):
        index, winner = _index_facts(root, ctx["valid"], ctx["fs"], ctx["env"], ctx["now"], ctx["snapshot_state"])
    else:
        index, winner = _index_unknown(ctx["snapshot_state"], "not read: scan budget reached"), None
    canonical = _canonical(root, unresolved, ctx["fs"]) if _may_read(budget) else _canonical_empty()
    index_root_key = reg.path_key(reg.norm_path(winner["root"], None, msys=True)) if winner else None
    seen = [ts for ts in (ctx["events"].get(key), ctx["events"].get(index_root_key)) if ts is not None]
    last_event = max(seen) if seen else None
    last_iso = _iso(last_event) if last_event is not None else None
    malformed = key in ctx["malformed"] or index_root_key in ctx["malformed"]
    return {
        "path": root,
        "name": _basename(root),
        "unresolved": unresolved,
        "canonical": canonical,
        "guard": guard,
        "codebase_memory": {"managed_entry": managed, "index": index},
        "last_guard_event_at": last_iso,
        "last_guard_event_reason": _event_reason(last_iso, ctx["audit_reason"], malformed),
    }


def _degraded_repo_facts(key: str, root: str, unresolved: bool | None, ctx: dict[str, Any]) -> dict[str, Any]:
    """A repo whose facts raised while being built (review L1). It stays in ``items`` so totals reconcile.

    Its guard reads ``settings_state: unreadable``, which is the state the settings read reports,
    and its index is unknown. The per-repo error and the ``repo_facts_unavailable`` gap say why.
    """
    guard = _guard_fields("unreadable", [])
    guard.update(record=None, record_mode_matches=None)
    seen = ctx["events"].get(key)
    seen_iso = _iso(seen) if seen is not None else None
    return {
        "path": root,
        "name": _basename(root),
        "unresolved": unresolved,
        "canonical": _canonical_empty(),
        "guard": guard,
        "codebase_memory": {
            "managed_entry": None,
            "index": _index_unknown(ctx["snapshot_state"], "not read: repo facts unavailable"),
        },
        "last_guard_event_at": seen_iso,
        "last_guard_event_reason": _event_reason(seen_iso, "unknown: repo facts unavailable", key in ctx["malformed"]),
    }


def _scan_filesystem(
    *,
    env: dict[str, Any],
    fs: Any,
    now: float,
    gdir: str | None,
    snapshot_path: str | None,
    candidates_in: list[tuple[str, Any, str | None, Any]],
) -> dict[str, Any]:
    """The whole filesystem side, as one synchronous function run in a worker thread (spec 8.1).

    The snapshot is always read first. Each later read, identity probe and repo step checks
    the budget before it runs (spec 3.8, review M2). A skipped step records its unknown value,
    and ``repos.scan`` reports the stop reason.
    """
    start = time.monotonic()
    budget = _Budget(SCAN_BUDGET_BYTES, start + SCAN_BUDGET_S)
    errors: list[tuple[str, str]] = []
    snap = _read_snapshot(snapshot_path, budget, errors)
    cfg = _read_config(gdir, budget, errors) if _may_read(budget) else _config_not_scanned()
    audit = _read_audit(gdir, fs, budget, errors) if _may_read(budget) else _audit_not_scanned()
    claims = _read_claims(budget, errors) if _may_read(budget) else _claims_not_scanned()
    cands, key_by_norm = _identify_candidates(candidates_in, snap["valid"], cfg["install_paths"], fs, budget)

    root_by_key: dict[str, str] = {}
    unresolved_by_key: dict[str, bool | None] = {}
    for cand in cands:
        key = cand["key"]
        if key not in root_by_key or cand["root"] < root_by_key[key]:
            root_by_key[key] = cand["root"]
        unresolved_by_key[key] = unresolved_by_key.get(key, False) or cand["unresolved"]
    keys_all = sorted(root_by_key)
    ctx: dict[str, Any] = {
        "budget": budget,
        "errors": errors,
        "env": env,
        "fs": fs,
        "now": now,
        "valid": snap["valid"],
        "snapshot_state": snap["state"],
        "installs": cfg["installs"],
        "events": audit["events"],
        "malformed": audit["malformed"],
        "audit_reason": _audit_reason(audit),
        "diag_gdir": None if cfg["over_cap"] else gdir,
    }
    facts: dict[str, dict[str, Any]] = {}
    failed: list[str] = []
    for key in keys_all[:MAX_REPOS]:
        try:
            facts[key] = _repo_facts(key, root_by_key[key], unresolved_by_key[key], ctx)
        except Exception:  # noqa: BLE001 - one repo must not fail the overview (review L1)
            _add_error(errors, "repos", "settings_unreadable")
            failed.append(key)
            facts[key] = _degraded_repo_facts(key, root_by_key[key], unresolved_by_key[key], ctx)
    try:
        guard_mode = gc.guard_mode(env, fs)
        disabled = sorted(gc.disabled_rules(env))
    except Exception:  # noqa: BLE001
        guard_mode, disabled = None, []
    return {
        "snapshot": snap,
        "config": cfg,
        "audit": audit,
        "claims": claims,
        "candidates": cands,
        "key_by_norm": key_by_norm,
        "keys_total": len(keys_all),
        "facts": facts,
        "failed": failed,
        "guard_mode": guard_mode,
        "disabled_rules": disabled,
        "hostname": _local_hostname(),
        "connection_name": _connection_name(),
        "scan": {
            "complete": budget.stopped is None,
            "bytes_read": budget.bytes_read,
            "budget_bytes": SCAN_BUDGET_BYTES,
            "elapsed_s": round(time.monotonic() - start, 1),
            "stopped_reason": budget.stopped,
        },
        "errors": errors,
    }


def _empty_scan(snapshot_path: str | None) -> dict[str, Any]:
    """The shape of a scan that could not run at all (review M3). Every value is unknown or empty."""
    return {
        "snapshot": {
            "state": "unreadable", "present": None, "path": snapshot_path, "schema": None,
            "built_at": None, "stats": None, "valid": None,
        },
        "config": _config_not_scanned(),
        "audit": _audit_not_scanned(),
        "claims": _claims_not_scanned(),
        "candidates": [],
        "key_by_norm": {},
        "keys_total": 0,
        "facts": {},
        "failed": [],
        "guard_mode": None,
        "disabled_rules": [],
        "hostname": None,
        "connection_name": None,
        "scan": {
            "complete": False, "bytes_read": 0, "budget_bytes": SCAN_BUDGET_BYTES,
            "elapsed_s": 0.0, "stopped_reason": None,
        },
        "errors": [],
    }


# ---------------------------------------------------------------------------
# DB reads (run on the event loop; no SQL here, existing helpers only)
# ---------------------------------------------------------------------------


async def _load_db(db: Any, now: float, errors: list[tuple[str, str]]) -> dict[str, Any]:
    state: dict[str, Any] = {
        "projects": None,
        "sessions": {},
        "sessions_failed": set(),
        "worktrees": {},
        "receipts": {},
        "receipt_truncated": False,
    }
    try:
        rows = await db_module.list_projects(db)
    except Exception:  # noqa: BLE001 - reported as db_error, the overview still returns
        _add_error(errors, "projects", "db_error")
        return state
    projects = [row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)]
    state["projects"] = projects
    since = (datetime.fromtimestamp(now, tz=timezone.utc)
             - timedelta(days=RECEIPT_WINDOW_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    for project in projects:
        pid = project["id"]
        try:
            state["sessions"][pid] = await db_module.get_sessions(db, pid, active_only=False)
        except Exception:  # noqa: BLE001
            _add_error(errors, "sessions", "db_error")
            state["sessions_failed"].add(pid)
        try:
            state["worktrees"][pid] = await db_module.list_active_worktrees(db, pid)
        except Exception:  # noqa: BLE001
            _add_error(errors, "repos", "db_error")
        try:
            receipt_rows = await db_module.get_action_audit_log(
                db, project_id=pid, event_type=_receipts.RECEIPT_EVENT_TYPE,
                since=since, limit=MAX_RECEIPTS_PER_PROJECT,
            )
        except Exception:  # noqa: BLE001
            _add_error(errors, "repos", "receipts_unavailable")
            continue
        state["receipts"][pid] = receipt_rows
        if len(receipt_rows) >= MAX_RECEIPTS_PER_PROJECT:
            state["receipt_truncated"] = True
    return state


def _candidate_inputs(db_state: dict[str, Any]) -> list[tuple[str, Any, str | None, Any]]:
    """DB-derived candidate paths: repo_paths, the legacy repo_path, and active worktrees."""
    inputs: list[tuple[str, Any, str | None, Any]] = []
    for project in db_state["projects"] or []:
        pid = project["id"]
        config = _exec_config(project.get("executor_config"))
        repo_paths = config.get("repo_paths")
        if isinstance(repo_paths, list):
            for entry in repo_paths:
                if not isinstance(entry, dict):
                    continue
                cwd = entry.get("cwd")
                if isinstance(cwd, str) and cwd.strip():
                    inputs.append(("project_repo_paths", cwd, pid, entry.get("hostname")))
        else:
            legacy = config.get("repo_path")
            if isinstance(legacy, str) and legacy.strip():
                inputs.append(("project_repo_paths_legacy", legacy, pid, "unknown"))
    for pid, worktrees in db_state["worktrees"].items():
        for worktree in worktrees:
            path = worktree.get("path")
            if isinstance(path, str) and path.strip():
                inputs.append(("active_worktree", path, pid, None))
    return inputs


def _receipt_index(db_state: dict[str, Any], now: float) -> dict[str, datetime]:
    """Latest prospect receipt per repo key (spec 3.6). Only explicit or session-worktree roots count."""
    floor = datetime.fromtimestamp(now, tz=timezone.utc) - timedelta(days=RECEIPT_WINDOW_DAYS)
    latest: dict[str, datetime] = {}
    for rows in db_state["receipts"].values():
        for row in rows:
            if not isinstance(row, dict):
                continue
            detail = row.get("detail")
            try:
                payload = json.loads(detail) if isinstance(detail, str) else detail
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict) or payload.get("repo_source") not in _RECEIPT_REPO_SOURCES:
                continue
            root = payload.get("repo_root")
            when = parse_last_seen(row.get("created_at"))
            if not isinstance(root, str) or not root or when is None or when < floor:
                continue
            key = reg.path_key(reg.norm_path(root, None, msys=True))
            if key is not None and (key not in latest or when > latest[key]):
                latest[key] = when
    return latest


# ---------------------------------------------------------------------------
# Sessions, projects and assembly (pure, on the event loop)
# ---------------------------------------------------------------------------


def _worktree_view(worktree: dict[str, Any], key_by_norm: dict[str, str]) -> dict[str, Any]:
    path = _str_or_none(worktree.get("path"))
    norm = reg.norm_path(path, None, msys=True) if path else None
    return {
        "path": path,
        "branch": _str_or_none(worktree.get("branch")),
        "repo_key": key_by_norm.get(norm) if norm else None,
    }


def _session_view(
    row: dict[str, Any],
    project_id: str,
    project_name: str | None,
    now_dt: datetime,
    worktree: dict[str, Any] | None,
    key_by_norm: dict[str, str],
) -> dict[str, Any]:
    status = _str_or_none(row.get("status"))
    parsed = parse_last_seen(row.get("last_seen"))
    age_ms = _age_ms(parsed, now_dt)
    live = status == "active" and age_ms is not None and 0 <= age_ms <= LIVE_WINDOW_MS
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "project_id": project_id,
        "project_name": project_name,
        "human_id": row.get("human_id"),
        "client_type": row.get("client_type"),
        "agent_framework": row.get("agent_framework"),
        "session_type": row.get("session_type"),
        "status": status,
        "sprint_version": row.get("sprint_version"),
        "last_seen": _raw_last_seen(row.get("last_seen")),
        "last_seen_utc": _iso_dt(parsed),
        "age_s": round(age_ms / 1000.0, 1) if age_ms is not None else None,
        "live": live,
        "location": "unknown",
        "worktree": _worktree_view(worktree, key_by_norm) if worktree else None,
        "_parsed": parsed,
        "_age_ms": age_ms,
    }


def _public_session(view: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in view.items() if not k.startswith("_")}


def _session_counts(views: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(views),
        "live": sum(1 for v in views if v["live"]),
        "status_active": sum(1 for v in views if v["status"] == "active"),
        "status_idle": sum(1 for v in views if v["status"] == "idle"),
    }


def _project_activity(views: list[dict[str, Any]]) -> tuple[str, datetime | None]:
    """Project activity over active and idle sessions only (spec 3.4). Returns (activity, last seen)."""
    scoped = [v for v in views if v["status"] in _SESSION_STATES_SCOPED]
    if not scoped:
        return "none", None
    stamps = [v["_parsed"] for v in scoped if v["_parsed"] is not None]
    last = max(stamps) if stamps else None
    if any(v["live"] for v in scoped):
        return "live", last
    if any(v["_age_ms"] is not None and 0 <= v["_age_ms"] <= RECENT_WINDOW_S * 1000 for v in scoped):
        return "recent", last
    return "idle", last


def _snapshot_out(snap: dict[str, Any], now: float) -> dict[str, Any]:
    state = snap["state"]
    valid = snap["valid"]
    built_at = snap["built_at"] if valid is not None else None
    return {
        "path": snap["path"],
        "present": snap["present"],
        "error": None if state == "ok" else state,
        "schema": snap["schema"],
        "built_at": built_at,
        "built_age_s": round(now - built_at, 1) if built_at is not None else None,
        "stats": snap["stats"] if valid is not None else None,
        "rows_total": len(valid["rows"]) if valid is not None else None,
        "stale_after_days": STALE_DAYS,
    }


def _match_location(runtime_host: str | None, hostnames: list[str]) -> str:
    if runtime_host is None or not hostnames:
        return "unknown"
    return "same_machine" if runtime_host in hostnames else "other_machine"


def _gap_codes(scan: dict[str, Any], repo_items: list[dict[str, Any]], receipt_truncated: bool) -> list[str]:
    snap_present = scan["snapshot"]["present"]
    cands = scan["candidates"]
    wanted = {
        "guard_runtime_server_process": any(r["guard"]["registered"] is True for r in repo_items),
        "guard_audit_tail_limited": scan["audit"]["limited"],
        "guard_event_root_is_index_root": any(r["activity"]["last_guard_event_at"] for r in repo_items),
        "snapshot_written_at_session_start": snap_present,
        "snapshot_machine_wide": snap_present,
        "hostname_unverified": any(r["hostnames"] for r in repo_items),
        "repo_root_unresolved": any(c["unresolved"] for c in cands),
        "index_nodes_zero_ambiguous": any(r["codebase_memory"]["index"]["nodes"] == 0 for r in repo_items),
        "receipts_window_truncated": receipt_truncated,
        # A stop reason is set only when the budget actually stopped a step (the scan is then incomplete).
        "scan_budget_reached": scan["scan"]["stopped_reason"] is not None,
        "tunnel_claims_unreadable": bool(scan["claims"].get("unreadable")),
        "repo_facts_unavailable": bool(scan["failed"]),
    }
    codes: list[str] = []
    for code, always, _message, _source in _GAP_TABLE:
        if always or wanted.get(code):
            codes.append(code)
    return codes


def _assemble(
    *,
    now: float,
    scan: dict[str, Any],
    db_state: dict[str, Any],
    errors: list[tuple[str, str]],
) -> dict[str, Any]:
    now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
    key_by_norm = scan["key_by_norm"]
    cands = scan["candidates"]
    projects: list[dict[str, Any]] = db_state["projects"] or []
    project_names = {p["id"]: _str_or_none(p.get("name")) for p in projects}

    worktree_by_session: dict[str, dict[str, Any]] = {}
    for worktrees in db_state["worktrees"].values():
        for worktree in worktrees:
            sid = worktree.get("session_id")
            if isinstance(sid, str) and sid not in worktree_by_session:
                worktree_by_session[sid] = worktree

    views_by_pid: dict[str, list[dict[str, Any]]] = {}
    all_views: list[dict[str, Any]] = []
    for project in projects:
        pid = project["id"]
        if pid not in db_state["sessions"]:
            continue
        views = [
            _session_view(
                row, pid, project_names.get(pid), now_dt,
                worktree_by_session.get(row.get("id")), key_by_norm,
            )
            for row in db_state["sessions"][pid] if isinstance(row, dict)
        ]
        views_by_pid[pid] = views
        all_views.extend(views)

    scoped_items = [
        v for v in all_views
        if v["status"] in _SESSION_STATES_SCOPED or (v["status"] in ("closed", "archived") and v["worktree"])
    ]
    scoped_items.sort(key=lambda v: (v["_parsed"] is None, -v["_parsed"].timestamp() if v["_parsed"] else 0.0))
    session_items = [_public_session(v) for v in scoped_items[:MAX_SESSIONS]]
    sessions_known = db_state["projects"] is not None

    # Projects and their repo keys.
    keys_by_pid: dict[str, set[str]] = {}
    for cand in cands:
        if cand["source"] in _PROJECT_REPO_SOURCES and cand["project_id"]:
            keys_by_pid.setdefault(cand["project_id"], set()).add(cand["key"])
    project_rows: list[dict[str, Any]] = []
    last_seen_by_pid: dict[str, datetime] = {}
    for project in sorted(projects, key=lambda p: ((_str_or_none(p.get("name")) or "").lower(), p["id"])):
        pid = project["id"]
        status = _str_or_none(project.get("status"))
        known = pid in views_by_pid
        views = views_by_pid.get(pid, [])
        activity, last = _project_activity(views) if known else (None, None)
        if last is not None:
            last_seen_by_pid[pid] = last
        project_rows.append({
            "id": pid,
            "name": _str_or_none(project.get("name")),
            "priority": _str_or_none(project.get("priority")),
            "github_repo": _str_or_none(project.get("github_repo")),
            "status": status if status in _PROJECT_STATUSES else None,
            "repo_keys": sorted(keys_by_pid.get(pid, set())),
            "session_counts": _session_counts(views) if known else None,
            "last_session_seen_at": _iso_dt(last) if known else None,
            "activity": activity,
            "activity_source": "sessions.last_seen",
        })

    # Repos (only the facts shown: the first MAX_REPOS keys, sorted).
    members_by_key: dict[str, list[dict[str, Any]]] = {}
    for cand in cands:
        members_by_key.setdefault(cand["key"], []).append(cand)
    receipt_by_key = _receipt_index(db_state, now)
    host = scan["hostname"]
    repo_items: list[dict[str, Any]] = []
    for key in sorted(scan["facts"]):
        facts = scan["facts"][key]
        members = members_by_key.get(key, [])
        hostnames = sorted({m["hostname"] for m in members if m["source"] == "project_repo_paths" and m["hostname"]})
        project_ids = sorted(
            {m["project_id"] for m in members if m["source"] in _PROJECT_REPO_SOURCES and m["project_id"]},
            key=lambda p: ((project_names.get(p) or "").lower(), p),
        )
        last_session = max(
            (last_seen_by_pid[p] for p in project_ids if p in last_seen_by_pid), default=None,
        )
        repo_items.append({
            "key": key,
            "path": facts["path"],
            "name": facts["name"],
            "sources": [s for s in _SOURCE_ORDER if any(m["source"] == s for m in members)],
            "source_paths": sorted({m["norm"] for m in members}),
            "hostnames": hostnames,
            "location": {"match": _match_location(host, hostnames), "hostnames": hostnames},
            "projects": [{"id": p, "name": project_names.get(p)} for p in project_ids],
            "canonical": facts["canonical"],
            "guard": facts["guard"],
            "codebase_memory": facts["codebase_memory"],
            "activity": {
                "last_guard_event_at": facts["last_guard_event_at"],
                "last_guard_event_reason": facts["last_guard_event_reason"],
                "last_prospect_receipt_at": _iso_dt(receipt_by_key.get(key)),
                "last_session_seen_at": _iso_dt(last_session),
                "receipt_window_days": RECEIPT_WINDOW_DAYS,
            },
        })

    session_total = len(scoped_items) if sessions_known else None
    sessions_out = {
        "live_window_s": LIVE_WINDOW_S,
        "items": session_items,
        "total": session_total,
        "truncated": bool(session_total is not None and session_total > MAX_SESSIONS),
        "totals": _session_counts(all_views) if sessions_known else None,
    }
    projects_out = {"items": project_rows, "recent_window_s": RECENT_WINDOW_S}

    snap = scan["snapshot"]
    repos_total = scan["keys_total"]
    repos_out = {
        "snapshot": _snapshot_out(snap, now),
        "items": repo_items,
        "total": repos_total,
        "truncated": repos_total > MAX_REPOS,
        "limit": MAX_REPOS,
        "scan": scan["scan"],
    }
    receipt_truncated = db_state["receipt_truncated"]
    gaps = [
        {"code": code, "message": _GAP_BY_CODE[code][0], "source": _GAP_BY_CODE[code][1]}
        for code in _gap_codes(scan, repo_items, receipt_truncated)
    ]
    return {
        "repos": repos_out,
        "sessions": sessions_out,
        "projects": projects_out,
        "gaps": gaps,
        "errors": [{"section": section, "code": code} for section, code in errors],
    }


def _runtime(scan: dict[str, Any], env: dict[str, Any], gdir: str | None, version: str) -> dict[str, Any]:
    backend = (
        "postgres" if env.get("MERIDIAN_DB_URL")
        else "memory" if env.get("MERIDIAN_DB") == ":memory:"
        else "sqlite"
    )
    claims = scan["claims"]
    client = claims["client_process"]
    return {
        "version": version,
        "hostname": scan["hostname"],
        "location": "local",
        "deployment": "self_hosted",
        "database": {"backend": backend, "connection_name": scan["connection_name"]},
        "guard": {
            "mode": scan["guard_mode"],
            "mode_scope": "server_process",
            "disabled_rules": scan["disabled_rules"],
            "user_scope_installed": scan["config"]["user_scope"],
            "guard_dir": gdir,
        },
        "tunnel": {
            "client_process": client,
            "client_process_reason": _FOUND_CLIENT_REASON if client == "running" else _UNKNOWN_CLIENT_REASON,
            "live_slot_claims": claims["live_slot_claims"],
            "hosted_connection": "unknown",
            "hosted_connection_reason": _HOSTED_CONNECTION_REASON,
        },
    }


def _runtime_unknown(gdir: str | None, version: str) -> dict[str, Any]:
    """The runtime section when it could not be built (review M3). Every derived value is null."""
    return {
        "version": version,
        "hostname": None,
        "location": "local",
        "deployment": "self_hosted",
        "database": {"backend": None, "connection_name": None},
        "guard": {
            "mode": None, "mode_scope": "server_process", "disabled_rules": None,
            "user_scope_installed": None, "guard_dir": gdir,
        },
        "tunnel": {
            "client_process": "unknown", "client_process_reason": _UNKNOWN_CLIENT_REASON,
            "live_slot_claims": None, "hosted_connection": "unknown",
            "hosted_connection_reason": _HOSTED_CONNECTION_REASON,
        },
    }


def _gap_rows(codes: list[str]) -> list[dict[str, str]]:
    return [{"code": code, "message": _GAP_BY_CODE[code][0], "source": _GAP_BY_CODE[code][1]} for code in codes]


def _always_codes() -> list[str]:
    return [code for code, always, _message, _source in _GAP_TABLE if always]


def _section_failed(errors: list[tuple[str, str]], failed: list[str], section: str) -> None:
    """Record one section whose builder raised: an error entry and the section_failed gap (review M3)."""
    _add_error(errors, section, "section_failed")
    if section not in failed:
        failed.append(section)


def _empty_db_state() -> dict[str, Any]:
    return {
        "projects": None,
        "sessions": {},
        "sessions_failed": set(),
        "worktrees": {},
        "receipts": {},
        "receipt_truncated": False,
    }


def _assembled_fallback(scan: dict[str, Any], now: float) -> dict[str, Any]:
    """Repos, sessions and projects when their assembly raised (review M3). Lists are empty."""
    return {
        "repos": {
            "snapshot": _snapshot_out(scan["snapshot"], now),
            "items": [],
            "total": scan["keys_total"],
            "truncated": scan["keys_total"] > MAX_REPOS,
            "limit": MAX_REPOS,
            "scan": scan["scan"],
        },
        "sessions": {"live_window_s": LIVE_WINDOW_S, "items": [], "total": None, "truncated": False, "totals": None},
        "projects": {"items": [], "recent_window_s": RECENT_WINDOW_S},
        "gaps": _gap_rows(_always_codes()),
    }


async def build_overview(
    db: Any,
    *,
    env: dict[str, Any],
    now: float,
    fs: Any = None,
    guard_dir: str | None = None,
) -> dict[str, Any]:
    """Return the ``meridian-workspace-overview/1`` document. Never raises (spec 2).

    Each section runs inside its own guard (review M3). A section whose builder raises gets
    ``{section, "section_failed"}`` in ``errors`` and the ``section_failed`` gap, and the
    other sections are still returned. Error text is never echoed.

    ``env`` is read, never written, except that ``guard_dir`` (when given) is applied as
    ``MERIDIAN_GUARD_DIR`` on a private copy. ``now`` is epoch seconds. The filesystem scan
    runs in a worker thread so it does not block the event loop.
    """
    working_env: dict[str, Any] = dict(env or {})
    if guard_dir is not None:
        working_env["MERIDIAN_GUARD_DIR"] = guard_dir
    probe = fs if fs is not None else reg.RealFS()
    now = float(now)
    errors: list[tuple[str, str]] = []
    failed: list[str] = []
    try:
        gdir = reg.guard_dir(working_env)
        snapshot_path = gdir.rstrip("/") + "/snapshot.json" if gdir else None
    except Exception:  # noqa: BLE001 - the guard dir lookup must not fail the overview
        _section_failed(errors, failed, "runtime")
        gdir, snapshot_path = None, None

    try:
        db_state = await _load_db(db, now, errors)
    except Exception:  # noqa: BLE001
        _section_failed(errors, failed, "projects")
        db_state = _empty_db_state()
    try:
        candidates_in = _candidate_inputs(db_state)
    except Exception:  # noqa: BLE001
        _section_failed(errors, failed, "repos")
        candidates_in = []
    try:
        scan = await asyncio.to_thread(
            _scan_filesystem,
            env=working_env,
            fs=probe,
            now=now,
            gdir=gdir,
            snapshot_path=snapshot_path,
            candidates_in=candidates_in,
        )
    except Exception:  # noqa: BLE001
        _section_failed(errors, failed, "repos")
        scan = _empty_scan(snapshot_path)
    for section, code in scan["errors"]:
        _add_error(errors, section, code)
    try:
        assembled = _assemble(now=now, scan=scan, db_state=db_state, errors=errors)
    except Exception:  # noqa: BLE001
        for section in ("repos", "sessions", "projects"):
            _section_failed(errors, failed, section)
        assembled = _assembled_fallback(scan, now)
    try:
        runtime = _runtime(scan, working_env, gdir, _deps._VERSION)
    except Exception:  # noqa: BLE001
        _section_failed(errors, failed, "runtime")
        runtime = _runtime_unknown(gdir, _deps._VERSION)
    gaps = list(assembled["gaps"])
    if failed:
        gaps.extend(_gap_rows(["section_failed"]))
    return _json_safe({
        "schema": SCHEMA,
        "generated_at": _iso(now),
        "runtime": runtime,
        "repos": assembled["repos"],
        "sessions": assembled["sessions"],
        "projects": assembled["projects"],
        "gaps": gaps,
        "errors": [{"section": section, "code": code} for section, code in errors],
    })
