"""Helpers for executor session defaults and rendering."""

from __future__ import annotations

from typing import Any

EXECUTOR_CONFIG_KEYS = (
    "repo_path",
    "repo_paths",
    "filesystem_roots",
    "hostnames",
    "env_file",
    "test_cmd",
    "test_min",
    "deploy_cmd",
    "shell_type",
    "branch",
    "context_threshold",
    "isolation",
    "max_turns",  # d2c47f43 — /goal "Stop after N turns" ceiling (default 200)
    "loop_enabled",  # 76cf8bda — per-project /loop override: "workspace"|True|False
    "checkpoint_turns",  # 76cf8bda — checkpoint() cadence hint (ceiling matches max_turns)
    "serena_repo_path",  # b970fe07 — dashboard-configurable Serena default --project (extract slot)
    "codebase_code_dirs",  # b970fe07 — dashboard-configurable code-intel index dirs (code slot)
    "outputs_dirs",  # e2688fc1 — meridian-outputs indexing dirs surfaced by the codeintel vtab
    "timezone",  # 3d7b7aca — IANA zone (e.g. "America/Denver") for the session current_time block
    "max_planning_turns",  # 75ac1c8e — execution-policy override: turns allowed before the required first action
    "parallelism_target",  # 99c0c1be — configured PARALLELISM target (<=16); see resolve_parallelism below
)

EXECUTOR_CREDENTIALS_RULE = (
    "Read secrets from env_file only, never remote shell."
)

# ---------------------------------------------------------------------------
# 75ac1c8e — configurable immediate-execution POLICY.
#
# The existing project-level ``execution_mode`` (db.normalize_execution_mode /
# server._EXECUTION_MODE_DIRECTIVES: 'autonomous' | 'interactive') already
# selects advisory PROSE — a directive line telling the session to act now vs.
# ask first. That prose has no teeth: nothing hard-bounds how many
# planning/tool-free turns an executor burns before it actually does
# something, and there is no single machine-readable field a receiving
# session/orchestrator can check to learn what the FIRST required action is.
#
# ``build_execution_policy`` closes that gap with one deterministic contract,
# derived from (not a replacement for) the existing execution_mode posture:
#
#   'autonomous' -> policy mode 'immediate' — claim and run now, a hard
#                   planning-turn ceiling, no confirmation, parallel-safe
#                   waves permitted.
#   'interactive' -> policy mode 'relaxed' — the explicit planning/ask-first
#                   mode; still bounded (never unlimited) but far more
#                   permissive, and it does not authorize unsupervised
#                   parallel fan-out.
#
# Every field except ``max_planning_turns`` is fully determined by the mode —
# not independently configurable — so the contract can't be silently
# weakened by a partial/malicious executor_config. ``max_planning_turns`` is
# the one override accepted from executor_config, and it is clamped/rejected
# with the SAME fail-safe convention normalize_executor_config already uses
# (an invalid value falls back to the mode default rather than raising).
# ---------------------------------------------------------------------------

EXECUTION_POLICY_MODES = ("immediate", "relaxed")
DEFAULT_EXECUTION_POLICY_MODE = "immediate"

# Maps the existing project posture vocabulary onto the policy vocabulary so
# callers can pass either 'autonomous'/'interactive' (what db.normalize_
# execution_mode already returns) or 'immediate'/'relaxed' directly.
_POSTURE_TO_POLICY_MODE = {
    "autonomous": "immediate",
    "interactive": "relaxed",
}

# 'immediate': at most 1 turn is allowed before the required first action —
# i.e. the very next turn must make the required tool call, no idle
# "let me think about this" turn first.
DEFAULT_MAX_PLANNING_TURNS_IMMEDIATE = 1
# 'relaxed': generous but still bounded — "planning mode" is not "unbounded
# mode"; a misconfigured relaxed project must never be able to stall forever.
DEFAULT_MAX_PLANNING_TURNS_RELAXED = 10
# Hard safety ceiling regardless of mode or executor_config override.
MAX_PLANNING_TURNS_CEILING = 50

REQUIRED_FIRST_ACTION_IMMEDIATE = "claim_sprint_item"
REQUIRED_FIRST_ACTION_RELAXED = "get_sprint_items"

GENUINE_BLOCKER_ESCALATION_RULE = (
    "Escalate via request_hitl ONLY for a genuine blocker -- a missing "
    "credential, a materially ambiguous scope, or a destructive/irreversible "
    "action -- never for routine planning, confirmation, or \"what should I "
    "start with\"."
)


def normalize_execution_policy_mode(mode: str | None) -> str:
    """Coerce a value to a valid execution POLICY mode ('immediate'|'relaxed').

    Accepts either the policy vocabulary directly or the underlying project
    posture terms ('autonomous'/'interactive') so callers don't have to
    translate first. Anything else (None, unknown string, wrong type) falls
    back to the default ('immediate') — never raises, matching every other
    normalize_* helper's fail-safe convention in this module/db.py.
    """
    if isinstance(mode, str):
        candidate = mode.strip().lower()
        if candidate in EXECUTION_POLICY_MODES:
            return candidate
        if candidate in _POSTURE_TO_POLICY_MODE:
            return _POSTURE_TO_POLICY_MODE[candidate]
    return DEFAULT_EXECUTION_POLICY_MODE


def _normalize_max_planning_turns(raw: Any, *, policy_mode: str) -> int:
    """Clamp an executor_config.max_planning_turns override to a safe range.

    Missing/non-numeric/non-positive values fall back to the mode's default
    (1 for immediate, 10 for relaxed) rather than raising. Positive values are
    clamped to MAX_PLANNING_TURNS_CEILING so a relaxed project can never
    configure genuinely unbounded planning.
    """
    default = (
        DEFAULT_MAX_PLANNING_TURNS_IMMEDIATE
        if policy_mode == "immediate"
        else DEFAULT_MAX_PLANNING_TURNS_RELAXED
    )
    try:
        turns = int(raw)
    except (TypeError, ValueError):
        return default
    if turns <= 0:
        return default
    return min(MAX_PLANNING_TURNS_CEILING, turns)


def build_execution_policy(
    raw_executor_config: dict[str, Any] | None,
    *,
    execution_mode: str | None = None,
) -> dict[str, Any]:
    """Build the canonical, machine-readable execution policy contract.

    ``execution_mode`` is the project's posture — pass the result of
    ``db.normalize_execution_mode`` ('autonomous'/'interactive'), or the
    policy vocabulary directly ('immediate'/'relaxed'); anything else defaults
    to 'immediate' (see ``normalize_execution_policy_mode``).

    Returns a flat dict of plain scalars/bools/strings — deterministic and
    identical in shape everywhere it's emitted (start_session's
    ``execution_policy`` field and every generate_handoff mode's embedded
    ``<execution_policy>`` /goal tag — see
    ``handoff._build_execution_policy_clause``) so a receiver can act on it
    without interpreting prose:

    * ``execution_mode`` — 'immediate' | 'relaxed'.
    * ``max_planning_turns`` — turns allowed before ``required_first_action``
      must happen. executor_config-overridable within a hard ceiling.
    * ``required_first_action`` — the literal tool name a receiver must call
      first: 'claim_sprint_item' (immediate) or 'get_sprint_items' (relaxed).
    * ``no_confirmation`` — True in immediate mode: do not wait for human
      confirmation before starting. False in relaxed mode.
    * ``permitted_parallel_wave`` — True in immediate mode: resource-
      conflict-free batches may be fanned out per the existing
      get_parallelizable_groups()/wave logic. False in relaxed mode:
      no unsupervised fan-out while still in ask-first posture.
    * ``claim_before_edit`` — always True; claim-before-edit is a
      non-negotiable rule regardless of mode (see AGENTS.md's claim
      sequence), not something a config can turn off.
    * ``genuine_blocker_escalation`` — the escalation rule text: when
      request_hitl is (and is not) appropriate.
    """
    policy_mode = normalize_execution_policy_mode(execution_mode)
    is_immediate = policy_mode == "immediate"
    cfg = raw_executor_config if isinstance(raw_executor_config, dict) else {}
    max_planning_turns = _normalize_max_planning_turns(
        cfg.get("max_planning_turns"), policy_mode=policy_mode
    )
    return {
        "execution_mode": policy_mode,
        "max_planning_turns": max_planning_turns,
        "required_first_action": (
            REQUIRED_FIRST_ACTION_IMMEDIATE if is_immediate else REQUIRED_FIRST_ACTION_RELAXED
        ),
        "no_confirmation": is_immediate,
        "permitted_parallel_wave": is_immediate,
        "claim_before_edit": True,
        "genuine_blocker_escalation": GENUINE_BLOCKER_ESCALATION_RULE,
    }


# ---------------------------------------------------------------------------
# 99c0c1be — configurable PARALLELISM target, decoupled from host-enforced
# capacity.
#
# Historically the only "how many workers at once" knob was Dispatcher's
# max_in_flight (a bare hard cap, dispatcher.DEFAULT_MAX_IN_FLIGHT=4) and
# get_parallelizable_groups' conflict-graph coloring (which bounds parallel-
# SAFETY -- which items may run together -- not parallel-COUNT -- how many
# run at once). Neither distinguished four genuinely different things:
#
#   * requested_parallelism  -- how much parallelism the caller/board is
#     actually asking for right now (e.g. the size of a resource-conflict-free
#     batch of eligible items).
#   * configured_target      -- how much this project is configured to allow
#     (executor_config.parallelism_target), now permitted up to 16 -- not
#     hardcoded to a small default forever.
#   * host_limit              -- how much the host/client actually supports
#     (e.g. a vendor UI concurrency cap). Often UNKNOWN.
#   * resource_safe_capacity -- how much can run at once without two items
#     stepping on the same declared resource.
#
# resolve_parallelism() makes all four explicit and produces ONE deterministic
# effective_parallelism = min(...) plus a limiting_reason, so any caller
# (Dispatcher, get_parallelizable_groups, a handoff, a dashboard) can say WHY
# parallelism was capped instead of just reporting a bare number. Two rules
# are load-bearing:
#
#   1. A KNOWN host_limit is always respected -- effective_parallelism can
#      never exceed a known host_limit. This is the "never claim to override
#      a lower host-enforced limit" contract.
#   2. A MISSING host_limit (None -- the host reported nothing) is EXCLUDED
#      from the min(), never coerced to some conservative stand-in (e.g. 1).
#      Treating "unknown" as "one" would serialize genuinely disjoint,
#      resource-safe work for no reason other than an unrelated vendor UI cap
#      never having been reported -- exactly what this item forbids.
# ---------------------------------------------------------------------------

PARALLELISM_TARGET_CEILING = 16
# Matches dispatcher.DEFAULT_MAX_IN_FLIGHT's historical hardcoded value, so an
# unconfigured project sees byte-for-byte the same effective concurrency as
# before this feature existed.
DEFAULT_PARALLELISM_TARGET = 4

# Priority used ONLY to choose a single label when multiple candidates tie at
# the minimum -- never affects the numeric result. host_limit outranks
# everything else because it is the one constraint that must never appear
# silently overridden by a different, equally-small candidate.
_PARALLELISM_LIMIT_PRIORITY = (
    "host_limit",
    "configured_target",
    "resource_safe_capacity",
    "requested_parallelism",
)


def normalize_parallelism_target(raw: Any) -> int:
    """Clamp an executor_config.parallelism_target override to [1, 16].

    Missing/non-numeric/non-positive values fall back to
    ``DEFAULT_PARALLELISM_TARGET`` rather than raising -- same fail-safe
    convention ``_normalize_max_planning_turns`` already uses. A value above
    the ceiling is clamped down to it, never rejected outright, so "ask for
    more than 16" degrades to "get exactly 16" rather than falling back to
    the (much smaller) default.
    """
    try:
        target = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PARALLELISM_TARGET
    if target <= 0:
        return DEFAULT_PARALLELISM_TARGET
    return min(PARALLELISM_TARGET_CEILING, target)


def _positive_int_or_none(value: Any) -> int | None:
    """Coerce to a positive int, or None for missing/invalid/non-positive.

    None is a meaningful, distinct return value (not an error): it means
    "this candidate does not participate in the min()" -- see the module
    docstring above on why a missing host_limit must never become 1.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def resolve_parallelism(
    requested_parallelism: Any,
    *,
    configured_target: Any = None,
    host_limit: Any = None,
    resource_safe_capacity: Any = None,
) -> dict[str, Any]:
    """Deterministically resolve how much parallelism is actually safe/allowed.

    Returns a flat dict with every input surfaced SEPARATELY (never collapsed
    into just a final number) so a caller can see why the effective value is
    what it is:

    * ``requested_parallelism`` -- what was asked for (coerced to >=1; a
      missing/invalid value defaults to 1, never 0 -- "no request" still
      means "at least one thing can run").
    * ``configured_target`` -- this project's configured ceiling, normalized
      via :func:`normalize_parallelism_target` (falls back to
      ``DEFAULT_PARALLELISM_TARGET``, clamped to ``PARALLELISM_TARGET_CEILING``
      == 16).
    * ``host_limit`` -- the host/client-reported cap, or ``None`` when the
      host did not report one. Never coerced into a number.
    * ``resource_safe_capacity`` -- the largest batch provably safe to run at
      once given declared resource conflicts (typically the size of
      ``get_parallelizable_groups()``'s first group), or ``None`` when not
      supplied.
    * ``effective_parallelism`` -- ``min()`` of every non-``None`` candidate
      above. Always >= 1.
    * ``limiting_reason`` -- which candidate the ``min()`` came from: one of
      ``"host_limit"``, ``"configured_target"``, ``"resource_safe_capacity"``,
      ``"requested_parallelism"``. When several candidates tie at the
      minimum, ``host_limit`` wins the label (see module docstring), then
      ``configured_target``, then ``resource_safe_capacity``, then
      ``requested_parallelism``.
    """
    requested = _positive_int_or_none(requested_parallelism) or 1
    target = normalize_parallelism_target(configured_target)
    candidates: dict[str, int] = {
        "requested_parallelism": requested,
        "configured_target": target,
    }
    host_limit_value = _positive_int_or_none(host_limit)
    if host_limit_value is not None:
        candidates["host_limit"] = host_limit_value
    resource_capacity_value = _positive_int_or_none(resource_safe_capacity)
    if resource_capacity_value is not None:
        candidates["resource_safe_capacity"] = resource_capacity_value

    effective = min(candidates.values())
    limiting_reason = next(
        key for key in _PARALLELISM_LIMIT_PRIORITY if candidates.get(key) == effective
    )
    return {
        "requested_parallelism": requested,
        "configured_target": target,
        "host_limit": host_limit_value,
        "resource_safe_capacity": resource_capacity_value,
        "effective_parallelism": effective,
        "limiting_reason": limiting_reason,
    }


def merge_repo_paths(
    existing: Any, new: Any
) -> list[dict[str, str]]:
    """Merge two ``repo_paths`` lists of ``{cwd, hostname}`` entries.

    Dedupes by ``(cwd, hostname)`` and preserves order (existing entries first,
    then new ones). Entries are normalized to ``{"cwd", "hostname"}`` with
    stripped strings; anything without a ``cwd`` is dropped. Used so a manual
    path entry (dashboard / set_executor_config) coexists with hook-registered
    entries instead of overwriting them.
    """
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add(entries: Any) -> None:
        if not isinstance(entries, (list, tuple)):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            cwd = str(entry.get("cwd") or "").strip()
            if not cwd:
                continue
            hostname = str(entry.get("hostname") or "").strip()
            key = (cwd, hostname)
            if key in seen:
                continue
            seen.add(key)
            out.append({"cwd": cwd, "hostname": hostname})

    _add(existing)
    _add(new)
    return out


def normalize_executor_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only the supported executor_config keys."""
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, Any] = {}
    for key in EXECUTOR_CONFIG_KEYS:
        value = raw.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        normalized[key] = value
    return normalized


def executor_config_for_output(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Return normalized config plus the always-injected credentials rule."""
    return {
        **normalize_executor_config(raw),
        "credentials_rule": EXECUTOR_CREDENTIALS_RULE,
    }


def has_executor_config(raw: dict[str, Any] | None) -> bool:
    """Return True when any persisted executor setting is present."""
    return bool(normalize_executor_config(raw))


def build_executor_config_block(raw: dict[str, Any] | None) -> str:
    """Render a compact starter/handoff block for executor sessions."""
    config = executor_config_for_output(raw)
    labels = {
        "repo_path": "repo_path",
        "env_file": "env_file",
        "test_cmd": "test_cmd",
        "test_min": "test_min",
        "deploy_cmd": "deploy_cmd",
        "shell_type": "shell_type",
        "branch": "branch",
        "context_threshold": "context_threshold (turns before warning)",
        "credentials_rule": "credentials_rule",
    }
    lines = ["# Executor Config"]
    for key in (
        "repo_path",
        "env_file",
        "test_cmd",
        "test_min",
        "deploy_cmd",
        "shell_type",
        "branch",
        "context_threshold",
        "credentials_rule",
    ):
        value = config.get(key)
        if value is None or value == "":
            continue
        lines.append(f"{labels[key]}: {value}")
    return "\n".join(lines)
