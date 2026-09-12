"""Shared validation and pure script-building for the durable Remote Task
primitive v1 (W1-E, item 32d3d5de).

Mirrors :mod:`meridian.research_run` / :mod:`meridian.experiment`'s structure
and conventions exactly (read either first): pure validation/normalization
and deterministic string-building, no DB, no network, no subprocess. See
:mod:`meridian.db.remote_tasks` for persistence and :mod:`meridian.remote_ssh`
for the actual SSH transport this module's scripts are ultimately run over.

PROBLEM THIS SOLVES
--------------------------------------------------------------------------
A session driving a remote host over SSH to launch a long-running job (a
rented GPU pod is the canonical case) hits three recurring failures:

1. A plain ``ssh host command`` is a single live connection end-to-end -- if
   it drops for any reason the remote process gets SIGHUP and dies with it,
   unless the caller remembers to hand-wrap every command in a
   nohup+disown-shaped incantation, every single time.
2. Even done correctly, the launching call can exceed a local tool's own
   timeout, so a second, separate round-trip is always needed just to
   confirm the job actually started.
3. Nothing durably ties a piece of Meridian work to a specific remote job's
   real status, so a session restart or handoff loses track of what is still
   running on a remote box.

:func:`build_launch_script` is this module's answer to (1) and (2): a single
POSIX shell one-liner that backgrounds the caller's command fully detached
(``nohup`` + ``setsid``, not just one or the other -- see that function's
docstring) and echoes the launched PID back over the SAME short-lived
connection used to start it, so the launch call returns in a few hundred ms
with a real PID, and the underlying job survives that connection closing
immediately afterward. :func:`build_status_check_script` /
:func:`parse_status_check_output` / :func:`map_check_marker_to_status`
answer (3): a second, always-fresh connection can cheaply ask "is this job
done, running, or dead" without ever depending on the connection that
launched it.

SCOPE (v1 -- see the sprint item for the full v2 wishlist this deliberately
does not build): no ``stop_remote_task``, no log streaming, no auto-retry,
no background poller, no cost/time tracking, no host registry/alias system.
``cost_estimate`` is still a persisted column (see db/remote_tasks.py) so a
later item can populate it without a schema migration -- nothing in this v1
ever writes to it.

DEVIATIONS FROM THE SPRINT-ITEM BRIEF (documented, not silently resolved)
--------------------------------------------------------------------------
1. ``ttl_seconds`` is accepted and validated here (:func:`validate_ttl_seconds`)
   but is deliberately NOT a persisted column on ``remote_tasks`` -- the
   brief's own column list for the DB layer does not include it, and with no
   background poller (explicitly out of scope) nothing would ever act on a
   stored expiry anyway. A future item adding TTL *enforcement* is the
   natural place to add the column alongside it.
2. The closed status vocabulary's ``connection-lost-but-possibly-still-running``
   value IS a real, persistable ``remote_tasks.status`` value (not merely a
   transient response field) -- see
   :mod:`meridian.db.remote_tasks`.get_remote_task_status for the exact rule
   governing when a failed status check is allowed to write it (never
   downgrading an already-terminal job).
"""
from __future__ import annotations

import re
import shlex
from typing import Any

from meridian.secret_redaction import check_for_secrets

# a5343387 -- reuse the existing canonical ISO-timestamp helper rather than
# duplicating it, exactly like meridian.research_run does. Re-exported under
# the same name so callers of this module don't need to know it actually
# lives in external_job_register.
from meridian.external_job_register import utcnow_iso  # noqa: F401

REMOTE_TASK_STATUSES = frozenset(
    {
        "running",
        "completed-success",
        "completed-failure",
        "terminated-unexpectedly",
        "connection-lost-but-possibly-still-running",
        "unknown",
    }
)
# A job in one of these states is done for good -- no future status check can
# ever move it back to "running". "connection-lost..." and "unknown" are
# deliberately NOT terminal: both mean "we don't currently know", not "it's
# over".
REMOTE_TASK_TERMINAL_STATUSES = frozenset(
    {"completed-success", "completed-failure", "terminated-unexpectedly"}
)

MAX_HOST_CHARS = 255
MAX_COMMAND_CHARS = 20_000
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 30 * 24 * 3_600  # 30 days -- generous for a long GPU run
MAX_LOG_TAIL_BYTES = 4_000  # "last 4KB", per the sprint item's own field spec

# Base directory (on the REMOTE host, in the launched shell's own $HOME) each
# job's private log/pid/exit-code files live under. Deliberately not
# caller-configurable in v1 -- a host registry/alias system is explicitly out
# of scope. Left as ``~/...`` (not ``$HOME/...``) because it is interpolated
# UNQUOTED into the generated script (see build_job_dir's docstring) so shell
# tilde-expansion actually happens on the remote end.
DEFAULT_REMOTE_BASE_DIR = "~/.meridian_remote_tasks"

_JOB_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
_STATUS_LINE_RE = re.compile(r"^MERIDIAN_STATUS=(\w+)\s*$", re.MULTILINE)
_EXIT_CODE_LINE_RE = re.compile(r"^MERIDIAN_EXIT_CODE=(-?\d+)\s*$", re.MULTILINE)


class RemoteExecError(ValueError):
    """Raised when remote-task input fails schema or safety validation."""


def validate_status(value: object) -> str:
    """Normalize and validate a remote-task status against the closed
    vocabulary (running | completed-success | completed-failure |
    terminated-unexpectedly | connection-lost-but-possibly-still-running |
    unknown)."""
    status = value.strip().lower() if isinstance(value, str) else ""
    if status not in REMOTE_TASK_STATUSES:
        raise RemoteExecError(
            f"remote task status must be one of {sorted(REMOTE_TASK_STATUSES)}, got {value!r}"
        )
    return status


def validate_host(value: object) -> str:
    """Validate the SSH destination (``host``, ``user@host``, or a configured
    ssh-config alias). Rejects anything that would be parsed as an ssh
    OPTION rather than a destination (a leading ``-``) -- this is the one
    field passed through to the ``ssh`` argv, so this is real argument-
    injection defense, not just cosmetic input hygiene."""
    if not isinstance(value, str):
        raise RemoteExecError("host must be a string")
    host = value.strip()
    if not host:
        raise RemoteExecError("host is required")
    if len(host) > MAX_HOST_CHARS:
        raise RemoteExecError(f"host exceeds {MAX_HOST_CHARS} characters")
    if host.startswith("-"):
        raise RemoteExecError(
            "host must not start with '-' -- it would be parsed as an ssh option, not a destination"
        )
    if "\n" in host or "\r" in host:
        raise RemoteExecError("host must not contain newlines")
    check_for_secrets(host, context="remote task host")
    return host


def validate_command(value: object) -> str:
    """Validate the shell command to run on the remote host. This is
    intentionally an opaque string (pipes, redirects, env assignments, and
    subshells are all valid remote shell syntax) -- it is embedded as a
    single quoted argument to ``bash -c`` by :func:`build_launch_script`,
    never interpolated unquoted."""
    if not isinstance(value, str):
        raise RemoteExecError("command must be a string")
    command = value.strip()
    if not command:
        raise RemoteExecError("command is required")
    if len(command) > MAX_COMMAND_CHARS:
        raise RemoteExecError(f"command exceeds {MAX_COMMAND_CHARS} characters")
    # check_for_secrets raises ValueError (not RemoteExecError) on a match --
    # let it propagate as-is, matching external_job_register's and
    # research_run's own convention of reusing this exact fail-closed gate
    # unmodified.
    check_for_secrets(command, context="remote task command")
    return command


def validate_ttl_seconds(value: object) -> "int | None":
    """Validate the optional caller-declared time-to-live, in seconds.

    Unlike :func:`meridian.research_run.validate_ttl_seconds`, ``None`` stays
    ``None`` here (no default is manufactured) -- see this module's
    docstring, deviation 1: v1 has nothing that would ever read/enforce a
    computed default, so inventing one would be misleading."""
    if value is None:
        return None
    try:
        ttl = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise RemoteExecError("ttl_seconds must be an integer") from None
    if isinstance(value, bool):
        raise RemoteExecError("ttl_seconds must be an integer, not a bool")
    if ttl < MIN_TTL_SECONDS or ttl > MAX_TTL_SECONDS:
        raise RemoteExecError(
            f"ttl_seconds must be between {MIN_TTL_SECONDS} and {MAX_TTL_SECONDS}, got {ttl}"
        )
    return ttl


def validate_start_fields(
    *, host: object, command: object, ttl_seconds: object = None
) -> dict[str, Any]:
    """Validate the full field set needed to start a new remote task."""
    return {
        "host": validate_host(host),
        "command": validate_command(command),
        "ttl_seconds": validate_ttl_seconds(ttl_seconds),
    }


def truncate_log_tail(text: object) -> str:
    """Bound a log blob to the last :data:`MAX_LOG_TAIL_BYTES` bytes (UTF-8),
    matching the sprint item's "bounded, e.g. last 4KB" field spec. Trims from
    the FRONT (keeps the tail) since the most recent output is what a caller
    checking status actually wants."""
    if not isinstance(text, str) or not text:
        return ""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_LOG_TAIL_BYTES:
        return text
    # A byte-level slice from the end can land mid-codepoint; errors="ignore"
    # on decode drops the resulting partial leading bytes rather than raising.
    return encoded[-MAX_LOG_TAIL_BYTES:].decode("utf-8", errors="ignore")


def build_job_dir(job_id: str, base_dir: str = DEFAULT_REMOTE_BASE_DIR) -> str:
    """Return the deterministic remote directory for ``job_id``.

    Deliberately NOT persisted as its own DB column -- it is always
    reconstructable from ``job_id`` alone (plus the process-wide constant
    ``base_dir``), so :mod:`meridian.db.remote_tasks` never needs to store or
    round-trip it.

    ``job_id`` must already be a real UUID4 string (server-generated, never
    caller-supplied) -- this is enforced here as defense in depth, since the
    result is interpolated UNQUOTED into the generated shell scripts below
    (quoting it would also quote-and-defeat ``base_dir``'s ``~`` tilde
    expansion if the two were quoted together; see build_launch_script).
    """
    if not isinstance(job_id, str) or not _JOB_ID_RE.match(job_id):
        raise RemoteExecError("job_id must be a UUID4 string")
    return f"{base_dir}/{job_id}"


def build_launch_script(
    job_id: str, command: str, *, base_dir: str = DEFAULT_REMOTE_BASE_DIR
) -> str:
    """Build the single POSIX shell command passed as the SSH remote command
    to launch ``command`` fully detached from the launching connection.

    Structure:
      1. ``mkdir -p`` the job directory and ``cd`` into it (unquoted: see
         :func:`build_job_dir`'s docstring on why -- ``base_dir`` and
         ``job_id`` are both trusted/generated, never raw caller input).
      2. Run the caller's ``command`` inside ``nohup setsid bash -c ...``,
         backgrounded, with stdin closed and stdout/stderr redirected to log
         files. BOTH ``nohup`` (ignores SIGHUP delivered directly to the
         process) AND ``setsid`` (starts a brand-new session, fully detached
         from the SSH pty/session) are used together -- either alone covers
         most but not all of the ways a dropped SSH connection can still
         reap a naively-backgrounded child; using both is the standard
         belt-and-suspenders idiom for this exact problem.
      3. ``$!`` (the PID of the just-backgrounded process) is captured
         immediately in the SAME shell invocation and both written to
         ``pid.txt`` AND echoed to stdout (via ``tee``) -- the launch call's
         own SSH stdout is exactly this PID, so the caller gets it back on
         the first round-trip with no extra work.
      4. On completion (success OR failure) the wrapped command's real exit
         code is written to ``exit_code.txt``. This -- not "the PID is gone"
         -- is the primary "is it actually done, and how" signal: a PID
         disappearing is ambiguous (normal exit vs. OOM-killed vs. host
         reboot all look the same from the outside), while an exit-code file
         existing means the wrapper shell itself ran to completion.

    ``command`` is the only untrusted input here and is embedded as ONE
    ``shlex.quote``-d argument to ``bash -c`` -- never string-concatenated
    unquoted into the outer script.
    """
    job_dir = build_job_dir(job_id, base_dir)
    inner = f"{{ {command} ; }} > stdout.log 2> stderr.log < /dev/null; echo $? > exit_code.txt"
    quoted_inner = shlex.quote(inner)
    return (
        f"mkdir -p {job_dir} && cd {job_dir} && "
        f"{{ nohup setsid bash -c {quoted_inner} & }} && "
        f"echo $! | tee pid.txt"
    )


def build_status_check_script(
    job_id: str,
    *,
    base_dir: str = DEFAULT_REMOTE_BASE_DIR,
    log_tail_bytes: int = MAX_LOG_TAIL_BYTES,
) -> str:
    """Build the single POSIX shell command that determines a job's status,
    in the exact priority order the sprint item specifies:

      1. ``exit_code.txt`` exists -> the wrapper ran to completion; read the
         real exit code.
      2. Else the recorded PID is still alive (``kill -0``) -> running.
      3. Else a PID was recorded but is gone with no exit code -> the job
         was launched but died without the wrapper ever completing
         (``terminated-unexpectedly`` -- most often OOM-kill or a host
         reboot/reset).
      4. Else (no ``pid.txt`` at all, or the job directory itself is
         missing) -> genuinely unknown, distinct from every case above.

    Emits a small, line-oriented, greppable marker format
    (``MERIDIAN_STATUS=...`` / ``MERIDIAN_EXIT_CODE=...`` /
    ``MERIDIAN_LOG_BEGIN``..``MERIDIAN_LOG_END``) rather than JSON -- no
    guarantee a bare POSIX ``sh`` on an arbitrary remote host has a JSON
    encoder on hand, but every one of these is a plain ``echo``.
    :func:`parse_status_check_output` is this format's one reader.
    """
    job_dir = build_job_dir(job_id, base_dir)
    return (
        f"cd {job_dir} 2>/dev/null || {{ echo MERIDIAN_STATUS=NODIR; echo MERIDIAN_LOG_BEGIN; echo MERIDIAN_LOG_END; exit 0; }}; "
        "if [ -f exit_code.txt ]; then "
        "echo MERIDIAN_STATUS=DONE; echo MERIDIAN_EXIT_CODE=$(cat exit_code.txt); "
        "elif [ -f pid.txt ] && kill -0 \"$(cat pid.txt)\" 2>/dev/null; then "
        "echo MERIDIAN_STATUS=RUNNING; "
        "elif [ -f pid.txt ]; then "
        "echo MERIDIAN_STATUS=DIED; "
        "else echo MERIDIAN_STATUS=NOPID; fi; "
        "echo MERIDIAN_LOG_BEGIN; "
        f"tail -c {int(log_tail_bytes)} stdout.log stderr.log 2>/dev/null; "
        "echo MERIDIAN_LOG_END"
    )


def parse_launch_pid(stdout: object) -> "int | None":
    """Extract the PID :func:`build_launch_script` echoes on its last line.
    Tolerant of extra blank lines/whitespace; returns None if nothing
    digit-shaped is found (the launch may have failed before reaching the
    ``tee`` -- the caller inspects the SSH result's returncode for that)."""
    if not isinstance(stdout, str):
        return None
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None


def parse_status_check_output(stdout: object) -> dict[str, Any]:
    """Parse :func:`build_status_check_script`'s marker-format output into
    ``{"marker": str, "exit_code": int | None, "log_tail": str}``.

    ``marker`` is one of DONE/RUNNING/DIED/NOPID/NODIR, or "NOPID" as a safe
    default when the expected marker line is missing entirely (a
    should-never-happen case -- a genuinely broken remote shell -- that
    still needs to map to SOMETHING rather than raising).
    """
    text = stdout if isinstance(stdout, str) else ""
    marker_match = _STATUS_LINE_RE.search(text)
    marker = marker_match.group(1) if marker_match else "NOPID"
    exit_match = _EXIT_CODE_LINE_RE.search(text)
    exit_code = int(exit_match.group(1)) if exit_match else None
    log_tail = ""
    if "MERIDIAN_LOG_BEGIN" in text:
        after = text.split("MERIDIAN_LOG_BEGIN", 1)[1]
        log_tail = after.split("MERIDIAN_LOG_END", 1)[0].strip("\n")
    return {
        "marker": marker,
        "exit_code": exit_code,
        "log_tail": truncate_log_tail(log_tail),
    }


def map_check_marker_to_status(marker: object, exit_code: "int | None") -> str:
    """Map a parsed status-check marker (+ optional exit code) to a member
    of :data:`REMOTE_TASK_STATUSES`. The ONLY inputs that can ever produce
    ``connection-lost-but-possibly-still-running`` live outside this
    function entirely -- see :mod:`meridian.db.remote_tasks` -- because that
    status describes the SSH connection attempt failing, which this
    function (fed only a successful check's stdout) never sees."""
    marker = marker if isinstance(marker, str) else ""
    if marker == "DONE":
        if exit_code is None:
            return "unknown"
        return "completed-success" if exit_code == 0 else "completed-failure"
    if marker == "RUNNING":
        return "running"
    if marker == "DIED":
        return "terminated-unexpectedly"
    # NOPID, NODIR, or any unrecognized marker: genuinely don't know.
    return "unknown"
