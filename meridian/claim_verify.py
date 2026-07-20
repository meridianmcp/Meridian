"""4ef6ce5e — claim_verification_mode: catch narration/reality mismatches after
claim_sprint_item / complete_sprint_item.

Motivated directly by a 2026-07-19 incident: an executor session narrated
"3 agents are now investigating and fixing... each in an isolated worktree"
for three sprint items. Independently verified twice, moments apart, against
``get_sprint_items(status=pending)``: all three still showed
``status=pending, claimed_at=null``. The narration did not match reality.
(That specific incident was later traced to a *separate* bug — a1d75ff3,
multi-instance sprint-items cache staleness — not a genuine claim_sprint_item
failure. That doesn't make this module pointless: defense-in-depth against a
narration/reality mismatch is worthwhile regardless of what caused any one
incident, and the a1d75ff3 fix won't cover every possible future failure
mode.)

WHERE THE GAP ACTUALLY IS: ``claim_sprint_item``'s own write logic is already
race-safe — fa3e3331 routes the actual UPDATE through ``_transition_status``
with ``from_statuses`` set to the claimable statuses, so a lost race returns
None / raises ``ValueError`` rather than silently succeeding. The gap is one
layer up: whether the tool call happened at all, or the response was lost/
malformed (d0f05438's class — "dispatch errors sometimes carry no message
text"), and the calling session's own narration reported success regardless.
No amount of hardening inside claim_sprint_item fixes that; the fix has to
live at the "does downstream reasoning match live DB reality" layer,
independent of what any single call claims happened.

DESIGN — reuses two systems that already exist, doesn't invent a third:
  1. ``workspace_settings.claim_verification_mode`` (off/advisory/strict) —
     see ``meridian.db.workspace`` / ``meridian.db.migrations``
     ``_migrate_workspace_claim_verification_mode``.
  2. A PostToolUse hook registered through the existing user-creatable-hooks
     infra (``meridian/db/hooks.py`` — 273287cb), the same mechanism
     ``meridian.orphan_reaper`` uses for its Stop hook. ``seed_claim_
     verification_hook`` (in this module) registers ONE PostToolUse row per
     project; ``blocking=True`` for 'strict', ``blocking=False`` for
     'advisory' — the existing ``_render_custom_hook_files`` wrapper (see
     ``meridian/handoff.py``) already downgrades an exit-2 to exit-1 whenever
     a hook is registered non-blocking, so THIS module never needs its own
     mode branching: it always signals a mismatch the same way (exit 2), and
     which mode is active is entirely encoded in which ``blocking`` value was
     registered.

This module is the actual comparison logic invoked by the thin sh/ps1 hook
shims (``_CLAIM_VERIFY_SH`` / ``_CLAIM_VERIFY_PS1`` below), mirroring
``meridian/orphan_reaper.py``'s "one well-tested cross-platform Python module,
not duplicated shell-dialect logic" shape — the pure comparison functions
(``verify_transition``, ``response_is_declined``, ``narrated_status``,
``evaluate_hook_payload``) are unit-testable independent of any real HTTP
call or Claude Code hook invocation.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Slug this hook is registered under in custom_hooks.
HOOK_NAME = "claim_verification_guard"
HOOK_EVENT = "PostToolUse"
# Informational only today — see _render_custom_hook_files (handoff.py):
# `matcher` is stored on the row and rendered into the hook file's header
# comment, but is NOT (yet) used to auto-wire a Claude Code settings.json
# PostToolUse entry (that wiring is a documented manual/human step, same gap
# orphan_reaper's Stop hook already has). Because evaluate_hook_payload()
# below no-ops (exit 0) for any tool_name it doesn't watch, the script is
# always safe to invoke on every PostToolUse event once wired, even broadly.
HOOK_MATCHER = "mcp__meridian__claim_sprint_item|mcp__meridian__complete_sprint_item"

_WATCHED_TOOL_SUFFIXES: tuple[str, ...] = ("claim_sprint_item", "complete_sprint_item")

# The status a WATCHED tool's own transition should have produced when it
# actually succeeded (independent of what the tool's own response claims).
_EXPECTED_STATUS_BY_TOOL_SUFFIX: dict[str, str] = {
    "claim_sprint_item": "in_progress",
    "complete_sprint_item": "done",
}

# tool_response keys that mark a DELIBERATE non-transition — a structural
# gate (DEFERRED/SUPERSEDED/UNPROSPECTED/WAVE_GATE_PENDING/PROTECTED/
# CONFLICT/a caught ValueError, etc.) declined the call rather than a claimed
# success silently failing to land. Comparing live state against the
# "should have transitioned" expectation for one of THESE calls would itself
# produce a false mismatch, so they're recognized and skipped.
_DECLINED_MARKER_KEYS: tuple[str, ...] = ("error", "blocked")


def _tool_suffix(tool_name: str) -> str | None:
    """Return which watched tool this is ('claim_sprint_item' /
    'complete_sprint_item'), or None if ``tool_name`` isn't one we watch.

    Matches on suffix so the raw MCP tool name, an ``mcp__meridian__``-
    prefixed name, and a slot-prefixed display name (b4455202, e.g.
    ``"Meridian: claim_sprint_item"``) are all recognized.
    """
    name = (tool_name or "").strip()
    if not name:
        return None
    for suffix in _WATCHED_TOOL_SUFFIXES:
        if name == suffix or name.endswith(f"__{suffix}") or name.endswith(f": {suffix}"):
            return suffix
    return None


def parse_hook_payload(raw: str) -> dict[str, Any]:
    """Best-effort JSON parse of a Claude Code PostToolUse hook payload (read
    from stdin: ``{tool_name, tool_input, tool_response, session_id, ...}``).
    Returns ``{}`` on empty/malformed input — never raises."""
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def response_is_declined(tool_response: Any) -> bool:
    """True if ``tool_response`` represents a deliberate, structural
    non-transition (a gate declined the call) rather than a claimed success
    that live state should confirm. See ``_DECLINED_MARKER_KEYS`` above."""
    if not isinstance(tool_response, dict):
        return False
    if any(k in tool_response for k in _DECLINED_MARKER_KEYS):
        return True
    if tool_response.get("status") == "already_claimed":
        return True
    return False


def narrated_status(tool_response: Any) -> str | None:
    """Extract the status the tool call's own response claims happened, or
    ``None`` when ``tool_response`` is missing/malformed/carries no status —
    the d0f05438-class dispatch-error case this feature exists to catch:
    when the response itself gives us nothing to go on, we still independently
    re-check live state rather than trusting whatever the session narrates."""
    if isinstance(tool_response, dict):
        val = tool_response.get("status")
        if isinstance(val, str) and val:
            return val
    return None


def verify_transition(
    live_item: dict[str, Any] | None,
    expected_status: str,
    expected_actor: str | None = None,
) -> dict[str, Any]:
    """Pure comparison: does the live sprint-item row actually show the
    transition a claim_sprint_item / complete_sprint_item call is supposed to
    have produced? No I/O — takes an already-fetched live row (or ``None`` if
    the re-fetch itself failed or found nothing).

    Returns ``{"ok": bool, "reason": str}``. ``ok=False`` is the
    "narration and reality diverge" signal the calling hook script acts on
    (block in strict mode, log in advisory mode).
    """
    if live_item is None:
        return {
            "ok": False,
            "reason": (
                "live re-check found no such sprint item (or the re-check "
                "call itself failed) — cannot confirm the transition landed"
            ),
        }
    live_status = live_item.get("status")
    if live_status != expected_status:
        return {
            "ok": False,
            "reason": (
                f"live status is {live_status!r}, expected {expected_status!r} — "
                "the tool call's narrated outcome does not match live DB state"
            ),
        }
    if expected_actor:
        live_actor = (live_item.get("actor") or "").strip()
        if live_actor and live_actor != expected_actor:
            return {
                "ok": False,
                "reason": (
                    f"live actor is {live_actor!r}, expected {expected_actor!r} — "
                    "item transitioned but under a different claim owner than "
                    "this call believed"
                ),
            }
    return {"ok": True, "reason": "live state matches the expected transition"}


def fetch_sprint_item_live(
    base_url: str, project_id: str, item_id: str, timeout: float = 5.0
) -> dict[str, Any] | None:
    """GET the live sprint item straight from the server — never from any
    in-process cache or the calling session's own narration.
    ``GET /projects/{project_id}/sprint-items/{item_id}`` (added alongside
    this feature; see ``routes/sprint.py``). Best-effort, mirroring
    ``orphan_reaper.fetch_dead_worktree_paths``: returns ``None`` on any
    network/parse failure or a 404 — this is a PostToolUse hook and must
    degrade to "couldn't confirm" (which ``verify_transition`` already treats
    as ``ok=False``) rather than crash Claude Code's hook runner.
    """
    url = f"{base_url.rstrip('/')}/projects/{project_id}/sprint-items/{item_id}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — trusted, own MERIDIAN_URL
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def evaluate_hook_payload(
    payload: dict[str, Any],
    base_url: str,
    project_id_arg: str | None = None,
    fetch_fn: "Callable[[str, str, str], dict[str, Any] | None] | None" = None,
) -> dict[str, Any]:
    """The entry point ``main()`` calls: decide whether this PostToolUse
    invocation is one we watch, and if so, verify it.

    Returns ``{"action": "skip"|"ok"|"mismatch", "reason": str, ...}``. Never
    raises — any internal failure degrades to ``{"action": "skip"}`` (this is
    advisory tooling layered on top of a structural DB write, not itself a
    data-integrity mechanism; ``"skip"`` always maps to exit 0, so it never
    blocks a session by accident).
    """
    try:
        tool_name = payload.get("tool_name") or ""
        suffix = _tool_suffix(tool_name)
        if suffix is None:
            return {"action": "skip", "reason": f"tool {tool_name!r} not watched"}
        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            tool_input = {}
        tool_response = payload.get("tool_response")
        if response_is_declined(tool_response):
            return {
                "action": "skip",
                "reason": "tool call itself reported a declined/blocked outcome",
            }
        project_id = tool_input.get("project_id") or project_id_arg
        item_id = tool_input.get("item_id")
        if not project_id or not item_id:
            return {"action": "skip", "reason": "tool_input missing project_id/item_id"}
        expected_status = _EXPECTED_STATUS_BY_TOOL_SUFFIX[suffix]
        expected_actor = tool_input.get("actor") or tool_input.get("session_id")
        fetch = fetch_fn or fetch_sprint_item_live
        live_item = fetch(base_url, str(project_id), str(item_id))
        result = verify_transition(live_item, expected_status, expected_actor)
        if result["ok"]:
            return {"action": "ok", "reason": result["reason"]}
        return {
            "action": "mismatch",
            "reason": result["reason"],
            "tool_name": tool_name,
            "project_id": project_id,
            "item_id": item_id,
            "narrated_status": narrated_status(tool_response),
        }
    except Exception:  # noqa: BLE001 — advisory tooling must never crash the hook
        logger.warning("claim_verify: evaluate_hook_payload failed", exc_info=True)
        return {"action": "skip", "reason": "internal error during evaluation"}


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m meridian.claim_verify``, invoked from the
    thin sh/ps1 hook shims registered by ``seed_claim_verification_hook``.
    Reads the PostToolUse JSON payload from stdin (Claude Code's own hook
    contract).

    Exit code drives Claude Code's blocking semantics: 0 = no issue / tool
    not watched, 2 = live state doesn't match the narrated transition.
    Whether exit 2 actually blocks the session is decided ONE LAYER UP:
    ``handoff._render_custom_hook_files`` downgrades exit 2 -> 1 whenever the
    registered hook row has ``blocking=False`` — i.e. 'advisory' mode is
    "exit 2 that can't block", 'strict' mode is a real block. This module has
    NO mode concept of its own; the mode lives entirely in which ``blocking``
    value ``seed_claim_verification_hook`` registered for this project.
    """
    parser = argparse.ArgumentParser(
        description="Verify a claim_sprint_item/complete_sprint_item call actually landed."
    )
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--url", default=os.environ.get("MERIDIAN_URL") or "http://localhost:7878")
    args = parser.parse_args(argv)

    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001
        raw = ""
    payload = parse_hook_payload(raw)
    result = evaluate_hook_payload(payload, args.url, project_id_arg=args.project_id)
    if result["action"] == "mismatch":
        print(
            f"Meridian claim_verify: {result['reason']} "
            f"(tool={result.get('tool_name')!r} item_id={result.get('item_id')!r} "
            f"narrated_status={result.get('narrated_status')!r}). "
            "Re-check live sprint-item state before trusting this call succeeded.",
            file=sys.stderr,
        )
        return 2
    return 0


# ---------------------------------------------------------------------------
# Hook script bodies — registered via seed_claim_verification_hook (273287cb infra)
# ---------------------------------------------------------------------------

_CLAIM_VERIFY_SH = """# 4ef6ce5e — Meridian claim/complete verification guard (auto-registered via
# add_custom_hook / 273287cb custom-hooks infra). PostToolUse hook: after every
# claim_sprint_item / complete_sprint_item call, re-checks the item's LIVE
# status against what the call's own response narrated, catching a silent
# dispatch failure (d0f05438's class) narrated as success. Reads the hook
# JSON payload from stdin (Claude Code's PostToolUse contract) and forwards
# it unchanged to `python -m meridian.claim_verify`, which does the actual
# comparison and sets the exit code (0 = no issue / not a watched tool,
# 2 = live state does not match the narrated transition). Whether exit 2
# actually blocks depends on THIS hook row's own `blocking` flag (registered
# per claim_verification_mode: strict=blocking, advisory=non-blocking) --
# this script has no mode logic of its own.
set -uo pipefail
if [ -n "$CLAUDE_PROJECT_DIR" ]; then cd "$CLAUDE_PROJECT_DIR" 2>/dev/null || true; fi
MERIDIAN_HOOK_URL="${MERIDIAN_URL:-__URL__}"
payload="$(cat 2>/dev/null || true)"
if command -v pixi >/dev/null 2>&1; then
  printf '%s' "$payload" | pixi run python -m meridian.claim_verify --project-id "__PROJECT_ID__" --url "$MERIDIAN_HOOK_URL"
  rc=$?
elif command -v python3 >/dev/null 2>&1; then
  printf '%s' "$payload" | python3 -m meridian.claim_verify --project-id "__PROJECT_ID__" --url "$MERIDIAN_HOOK_URL"
  rc=$?
elif command -v python >/dev/null 2>&1; then
  printf '%s' "$payload" | python -m meridian.claim_verify --project-id "__PROJECT_ID__" --url "$MERIDIAN_HOOK_URL"
  rc=$?
else
  rc=0
fi
exit $rc
"""

_CLAIM_VERIFY_PS1 = """# 4ef6ce5e — Meridian claim/complete verification guard (auto-registered via
# add_custom_hook / 273287cb custom-hooks infra). PostToolUse hook: after every
# claim_sprint_item / complete_sprint_item call, re-checks the item's LIVE
# status against what the call's own response narrated, catching a silent
# dispatch failure (d0f05438's class) narrated as success. Forwards the hook
# JSON payload (stdin) unchanged to `python -m meridian.claim_verify`, which
# sets the exit code (0 = no issue, 2 = live state doesn't match). Whether
# exit 2 actually blocks depends on THIS hook row's own `blocking` flag.
$ErrorActionPreference = 'SilentlyContinue'
if ($env:CLAUDE_PROJECT_DIR) { Set-Location $env:CLAUDE_PROJECT_DIR }
$ProjectId = '__PROJECT_ID__'
$Url = if ($env:MERIDIAN_URL) { $env:MERIDIAN_URL } else { '__URL__' }
$payload = [Console]::In.ReadToEnd()
if (Get-Command pixi -ErrorAction SilentlyContinue) {
    $payload | pixi run python -m meridian.claim_verify --project-id $ProjectId --url $Url
    exit $LASTEXITCODE
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $payload | python -m meridian.claim_verify --project-id $ProjectId --url $Url
    exit $LASTEXITCODE
} else {
    exit 0
}
"""


async def seed_claim_verification_hook(
    db: Any, project_id: str, mode: str, url: str | None = None
) -> dict[str, Any] | None:
    """Idempotently register (or remove) the claim-verification PostToolUse
    hook for ``project_id`` via the 273287cb custom-hooks infra
    (``db.hooks.add_custom_hook`` / ``update_custom_hook`` /
    ``delete_custom_hook``) — this is the ONLY place the hook gets wired in.

    * ``mode == "off"`` — removes any existing ``claim_verification_guard``
      hook row for this project (a no-op if none exists) and returns
      ``None``. Matches the "off does nothing" recommendation: no stray
      registered-but-inert hook left behind.
    * ``mode == "advisory"`` — registers/updates with ``blocking=False``
      (mismatch logs a warning, per ``_render_custom_hook_files``'s existing
      exit-2->1 downgrade for non-blocking hooks; never blocks the session).
    * ``mode == "strict"`` — registers/updates with ``blocking=True``
      (mismatch is a real block — Claude Code's own PostToolUse exit-code-2
      semantics take over).

    Safe to call repeatedly (e.g. once per ``generate_handoff``, mirroring
    ``orphan_reaper.seed_orphan_reaper_hook``): an existing row's script
    bodies / blocking flag are refreshed in place rather than erroring on the
    duplicate-slug guard in ``add_custom_hook``. Returns the resulting
    ``custom_hooks`` row (or ``None`` for ``mode == "off"``).

    Raises ``ValueError`` for any ``mode`` other than off/advisory/strict —
    same validation ``update_workspace_settings`` already applies before this
    is ever called, kept here too since this is a public entry point in its
    own right.
    """
    from . import db as db_module  # noqa: PLC0415 — avoid import cycle at module load

    if mode not in ("off", "advisory", "strict"):
        raise ValueError(f"invalid claim_verification_mode: {mode!r}")

    existing_hooks = await db_module.get_custom_hooks(db, project_id, event=HOOK_EVENT)
    existing = next((h for h in existing_hooks if h.get("slug") == HOOK_NAME), None)

    if mode == "off":
        if existing is not None:
            await db_module.delete_custom_hook(db, project_id, existing["id"])
        return None

    base_url = url or os.environ.get("MERIDIAN_URL") or "http://localhost:7878"
    script_sh = _CLAIM_VERIFY_SH.replace("__PROJECT_ID__", project_id).replace("__URL__", base_url)
    script_ps1 = _CLAIM_VERIFY_PS1.replace("__PROJECT_ID__", project_id).replace("__URL__", base_url)
    blocking = mode == "strict"

    if existing is not None:
        return await db_module.update_custom_hook(
            db, project_id, existing["id"],
            script_sh=script_sh, script_ps1=script_ps1, matcher=HOOK_MATCHER,
            blocking=blocking, enabled=True,
        )
    return await db_module.add_custom_hook(
        db, project_id,
        name=HOOK_NAME,
        event=HOOK_EVENT,
        script_sh=script_sh,
        script_ps1=script_ps1,
        matcher=HOOK_MATCHER,
        blocking=blocking,
        enabled=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
