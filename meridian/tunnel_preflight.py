"""31c7b1fc — preflight child MCP entrypoints before tunnel slot advertisement.

Standalone, cross-platform preflight check for a tunnel child MCP server's
declared launch command: resolve the effective executable, run it in
isolation with a bounded budget, and classify the outcome as healthy, a
deterministic dependency failure, or a cold-start timeout — all BEFORE the
control plane ever advertises the slot as available.

Deliberately disjoint from meridian.tunnel_client's SlotProxy/resolve_plugins:
those manage an already-spawned process over a live websocket/port and are
concurrently being edited by other in-progress tunnel-control-plane sprint
items. This module reuses tunnel_client's existing PURE classification
helpers (``_classify_launch_exception``, ``_classify_stderr_signature``,
``_cold_spawn_budget``, ``_spawn_kwargs``) as the single source of truth for
failure-signature matching, rather than duplicating that regex/logic here —
but it does not import from, call into, or modify SlotProxy, resolve_plugins,
or any other live-control-plane state. Wiring this into the real spawn path
is a deliberately separate follow-up item once the concurrent tunnel edits
land — see docs/tunnel-child-runtime-contract.md for the contract and the
integration boundary.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from meridian.tunnel_client import (
    SlotState,
    _classify_launch_exception,
    _classify_stderr_signature,
    _cold_spawn_budget,
    _spawn_kwargs,
)

# Default budget for a direct preflight_child_entrypoint() call. Callers that
# know the slot label should use preflight_for_label() instead, which derives
# a label-aware budget from tunnel_client._cold_spawn_budget.
DEFAULT_TIMEOUT_SECONDS = 20.0

# Floor applied to the label-derived budget in preflight_for_label() so a
# very small (attempts, delay) can never starve a genuinely slow cold-fetch
# child of the time it needs to import and exit.
_MIN_LABEL_TIMEOUT_SECONDS = 2.0

_OUTPUT_TAIL_CHARS = 4000


@dataclass(frozen=True)
class PreflightDiagnostic:
    """Machine-readable preflight result for one child MCP entrypoint.

    ``state`` reuses tunnel_client.SlotState so a preflight result and a
    live-spawn diagnostic speak the same vocabulary. ``recommend_quarantine``
    is advisory only — this module does not itself quarantine anything; a
    future integration item decides whether/how to act on it.
    """

    label: str
    command: tuple[str, ...]
    resolved_executable: "str | None"
    cwd: "str | None"
    healthy: bool
    state: SlotState
    reason: str
    human_reason: str
    duration_seconds: float
    exit_code: "int | None"
    stdout_tail: str
    stderr_tail: str
    recommend_quarantine: bool

    def as_dict(self) -> dict:
        """JSON-serializable form (SlotState is a str Enum, so .value round-trips)."""
        return {
            "label": self.label,
            "command": list(self.command),
            "resolved_executable": self.resolved_executable,
            "cwd": self.cwd,
            "healthy": self.healthy,
            "state": self.state.value,
            "reason": self.reason,
            "human_reason": self.human_reason,
            "duration_seconds": self.duration_seconds,
            "exit_code": self.exit_code,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "recommend_quarantine": self.recommend_quarantine,
        }


def resolve_effective_executable(command: "list[str] | tuple[str, ...]") -> "str | None":
    """Resolve ``command[0]`` to an absolute path via PATH lookup when possible.

    Returns the bare token (or None for an empty command) when it can't be
    resolved ahead of time — this is purely diagnostic context; the actual
    spawn attempt below is what authoritatively decides success/failure.
    """
    if not command:
        return None
    return shutil.which(command[0]) or command[0]


def _tail(text: "str | None", limit: int = _OUTPUT_TAIL_CHARS) -> str:
    if not text:
        return ""
    return text[-limit:]


def preflight_child_entrypoint(
    label: str,
    command: "list[str] | tuple[str, ...]",
    *,
    cwd: "str | Path | None" = None,
    env: "dict[str, str] | None" = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> PreflightDiagnostic:
    """Run *command* in isolation and classify whether the child MCP entrypoint
    is safe to advertise as a tunnel slot.

    Spawns the command with stdin piped and immediately closes it: a
    well-behaved MCP stdio server treats stdin EOF as a shutdown signal and
    exits 0, while an import-time dependency failure crashes almost
    instantly with a captured traceback on stderr — both resolve well within
    a short budget, so only a genuinely slow cold-fetch (or a hung server)
    consumes the full *timeout*.

    Three outcomes:
      - deterministic launch/dependency failure (DEPENDENCY_MISSING /
        CHILD_CRASHED) — reuses tunnel_client's existing classifiers so the
        signature stays a single source of truth with the live spawn path;
      - cold-start timeout (STARTUP_TIMEOUT) — still running when the budget
        expires; NOT treated as deterministic, so callers should retry with
        a larger budget rather than quarantine;
      - healthy (HEALTHY) — exited 0 with no deterministic-failure signature.

    Never raises: every failure mode (missing launcher, crash, timeout) is
    captured into the returned diagnostic instead.
    """
    command = tuple(command)
    cwd_str = str(cwd) if cwd is not None else None
    resolved = resolve_effective_executable(command)
    start = time.monotonic()

    try:
        proc = subprocess.Popen(
            command,
            cwd=cwd_str,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **_spawn_kwargs(),
        )
    except OSError as exc:
        duration = time.monotonic() - start
        state, detail = _classify_launch_exception(exc)
        return PreflightDiagnostic(
            label=label,
            command=command,
            resolved_executable=resolved,
            cwd=cwd_str,
            healthy=False,
            state=state,
            reason=state.value,
            human_reason=detail,
            duration_seconds=duration,
            exit_code=None,
            stdout_tail="",
            stderr_tail="",
            recommend_quarantine=True,
        )

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        proc.kill()
        stdout, stderr = proc.communicate()
        return PreflightDiagnostic(
            label=label,
            command=command,
            resolved_executable=resolved,
            cwd=cwd_str,
            healthy=False,
            state=SlotState.STARTUP_TIMEOUT,
            reason=SlotState.STARTUP_TIMEOUT.value,
            human_reason=(
                f"{label}: entrypoint did not exit within {timeout:.1f}s of stdin "
                "closing -- likely a cold uvx/npx fetch or a genuinely long-running "
                "server, not a deterministic dependency failure; do not quarantine"
            ),
            duration_seconds=duration,
            exit_code=None,
            stdout_tail=_tail(stdout),
            stderr_tail=_tail(stderr),
            recommend_quarantine=False,
        )

    duration = time.monotonic() - start
    if proc.returncode == 0:
        return PreflightDiagnostic(
            label=label,
            command=command,
            resolved_executable=resolved,
            cwd=cwd_str,
            healthy=True,
            state=SlotState.HEALTHY,
            reason="ok",
            human_reason=f"{label}: entrypoint exited cleanly (code 0)",
            duration_seconds=duration,
            exit_code=0,
            stdout_tail=_tail(stdout),
            stderr_tail=_tail(stderr),
            recommend_quarantine=False,
        )

    classified = _classify_stderr_signature(stderr)
    if classified is not None:
        state, detail = classified
    elif not stderr and not stdout:
        # Fast exit, non-zero, no output at all — deterministic (mirrors the
        # fast-exit-without-signature = CHILD_CRASHED convention already used
        # on the live spawn path in tunnel_client).
        state, detail = (
            SlotState.CHILD_CRASHED,
            f"{label}: entrypoint exited with code {proc.returncode} and no output",
        )
    else:
        state, detail = (
            SlotState.CHILD_CRASHED,
            f"{label}: entrypoint exited with code {proc.returncode}",
        )

    return PreflightDiagnostic(
        label=label,
        command=command,
        resolved_executable=resolved,
        cwd=cwd_str,
        healthy=False,
        state=state,
        reason=state.value,
        human_reason=detail,
        duration_seconds=duration,
        exit_code=proc.returncode,
        stdout_tail=_tail(stdout),
        stderr_tail=_tail(stderr),
        recommend_quarantine=True,
    )


def preflight_for_label(
    label: str,
    command: "list[str] | tuple[str, ...]",
    *,
    cwd: "str | Path | None" = None,
    env: "dict[str, str] | None" = None,
) -> PreflightDiagnostic:
    """Convenience wrapper: derive the timeout budget from *label* via
    tunnel_client._cold_spawn_budget, so cold-fetch slots (dc/ppt/word/docs/
    zotero) automatically get the same larger allowance the live spawn path
    already gives them (050dcb6b) — one source of truth for cold-fetch
    awareness, not a second copy of the slot list.
    """
    attempts, delay = _cold_spawn_budget(label)
    timeout = max(_MIN_LABEL_TIMEOUT_SECONDS, attempts * delay)
    return preflight_child_entrypoint(label, command, cwd=cwd, env=env, timeout=timeout)


@dataclass(frozen=True)
class QuarantineDecision:
    """Result of feeding one PreflightDiagnostic into a PreflightQuarantineTracker."""

    quarantined: bool
    consecutive_failures: int
    reason: "str | None"


class PreflightQuarantineTracker:
    """Tracks consecutive deterministic preflight failures per slot label and
    decides when a slot should stop being retried instead of being retried
    forever.

    Deliberately NOT wired into SlotProxy/resolve_plugins in this item (see
    module docstring) — this is the standalone decision logic a future
    integration item calls. In-memory only, per-instance; callers own the
    tracker's lifetime.
    """

    def __init__(self, *, threshold: int = 3) -> None:
        if threshold < 1:
            raise ValueError("threshold must be >= 1")
        self._threshold = threshold
        self._consecutive_failures: dict[str, int] = {}

    def record(self, diagnostic: PreflightDiagnostic) -> QuarantineDecision:
        """Feed one diagnostic in. Only diagnostics with recommend_quarantine=True
        count toward the streak (a cold-start timeout never does); any healthy
        or non-deterministic result resets the streak for that label."""
        label = diagnostic.label
        if not diagnostic.recommend_quarantine:
            self._consecutive_failures.pop(label, None)
            return QuarantineDecision(quarantined=False, consecutive_failures=0, reason=None)

        count = self._consecutive_failures.get(label, 0) + 1
        self._consecutive_failures[label] = count
        if count >= self._threshold:
            return QuarantineDecision(
                quarantined=True,
                consecutive_failures=count,
                reason=(
                    f"{label}: {count} consecutive deterministic preflight failures "
                    f"(state={diagnostic.state.value}) -- suppressing further retries"
                ),
            )
        return QuarantineDecision(quarantined=False, consecutive_failures=count, reason=None)
