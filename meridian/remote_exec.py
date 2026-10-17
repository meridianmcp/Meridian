"""Shared validation and pure script-building for the durable Remote Task
primitive v1 (W1-E, item 32d3d5de).

Mirrors :mod:`meridian.research_run` / :mod:`meridian.experiment`'s structure
and conventions exactly (read either first): pure validation/normalization
and deterministic string-building (the one exception: build_launch_script draws
a fresh per-launch nonce), no DB, no network, no subprocess. See
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
connection used to start it, and the underlying job survives that connection
closing immediately afterward. A normal launch returns in seconds with a real
PID. A launch that cannot confirm within about 15 seconds (the launch timeout
in :mod:`meridian.remote_ssh`) is recorded as ``launch_unconfirmed`` and is
confirmed later by a status check. :func:`build_status_check_script` /
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
import secrets
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
        "launch_unconfirmed",
        "unknown",
    }
)
# A job in one of these states is done for good -- no future status check can
# ever move it back to "running". "connection-lost...", "launch_unconfirmed"
# and "unknown" are deliberately NOT terminal: each means "we don't currently
# know", not "it's over".
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
_PID_LINE_RE = re.compile(r"^MERIDIAN_PID=(\d+)\s*$", re.MULTILINE)
# Per-file log-tail blocks emitted by build_status_check_script. Each block's
# markers carry this job's nonce: ``MERIDIAN_STDOUT_BEGIN_<nonce>`` and so on.
# build_launch_script writes the nonce to ``nonce.txt`` in the job directory,
# and the status script echoes it back on a ``MERIDIAN_NONCE=`` header line.
# parse_status_check_output accepts a block only when its markers carry the
# nonce from that header line. Job output that contains the fixed strings
# (``MERIDIAN_STDOUT_END`` and so on) therefore cannot move a tail boundary.
# The unsuffixed prefixes below only locate the end of the header: the real
# first block begins before any job output, so the first occurrence of a prefix
# is always the real one.
#
# Limit: the job runs with the job directory as its working directory, so a job
# that deliberately reads or rewrites nonce.txt can still forge markers. The
# same holds for exit_code.txt and pid.txt, which a job can also write. The
# nonce guards against marker text that shows up in job output by accident or
# by copying another job's log. It is not a sandbox.
_NONCE_LINE_RE = re.compile(r"^MERIDIAN_NONCE=([0-9a-f]{16})\s*$", re.MULTILINE)
_STDOUT_BEGIN = "MERIDIAN_STDOUT_BEGIN"
_STDOUT_END = "MERIDIAN_STDOUT_END"
_STDERR_BEGIN = "MERIDIAN_STDERR_BEGIN"
_STDERR_END = "MERIDIAN_STDERR_END"


class RemoteExecError(ValueError):
    """Raised when remote-task input fails schema or safety validation."""


def validate_status(value: object) -> str:
    """Normalize and validate a remote-task status against the closed
    vocabulary (running | completed-success | completed-failure |
    terminated-unexpectedly | connection-lost-but-possibly-still-running |
    launch_unconfirmed | unknown)."""
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


def new_nonce() -> str:
    """Return a fresh per-launch nonce: 16 lowercase hex characters from
    :func:`secrets.token_hex`. It frames the log-tail blocks of one job's
    status checks (see the constants above)."""
    return secrets.token_hex(8)


def build_launch_script(
    job_id: str, command: str, *, base_dir: str = DEFAULT_REMOTE_BASE_DIR
) -> str:
    """Build the single POSIX shell command passed as the SSH remote command
    to launch ``command`` fully detached from the launching connection.

    Structure:
      1. ``mkdir -p`` the job directory and ``cd`` into it (unquoted: see
         :func:`build_job_dir`'s docstring on why -- ``base_dir`` and
         ``job_id`` are both trusted/generated, never raw caller input). Then
         write a fresh nonce to ``nonce.txt`` (:func:`new_nonce`). The status
         check frames its log tails with it, so job output cannot forge the
         block boundaries (see the constants above).
      2. Start the inner script ``bash -c ...`` under ``nohup setsid``,
         backgrounded. BOTH ``nohup`` (ignores SIGHUP delivered directly to the
         process) AND ``setsid`` (starts a brand-new session, fully detached
         from the SSH pty/session) are used together -- either alone covers
         most but not all of the ways a dropped SSH connection can still
         reap a naively-backgrounded child; using both is the standard
         belt-and-suspenders idiom for this exact problem.
         The OUTER backgrounded job (the ``nohup setsid`` process) has its
         stdin and stdout redirected to ``/dev/null`` and its stderr to
         ``launch.err`` in the job directory, so it holds no file descriptor on
         the SSH channel. This matters: sshd keeps the session open until every
         process holding the channel's stdout/stderr closes it, so an inherited
         channel would keep the launching ssh call open for the whole life of
         the job. ``launch.err`` keeps the outer job's own errors (for example
         ``nohup: failed to run command 'setsid'``), which ``/dev/null`` would
         discard. The inner script itself redirects the caller's command to
         ``stdout.log`` and ``stderr.log`` (and gives it ``/dev/null`` as stdin).
      3. ``$!`` (the PID of the just-backgrounded process) is captured
         immediately in the SAME shell invocation and both written to
         ``pid.txt`` AND echoed to stdout (via ``tee``) -- the launch call's
         own SSH stdout is exactly this PID, so the caller gets it back on
         the first round-trip with no extra work. The script then sleeps one
         second and checks that the job is still there: ``kill -0`` on that PID,
         or an existing ``exit_code.txt``, which the wrapper writes just before it
         exits, so a command that finished inside that second also passes. Only
         then does the launch exit 0 with the PID. Otherwise the outer job died
         in its first second. The script prints the tail of ``launch.err`` to
         stderr and exits 1, so the caller sees the reason in the launch result.
         The synchronous steps fail with the shell's own error for mkdir, cd and
         the nonce write, because ``launch.err`` does not exist yet at those
         points. Only a failed pid write, which runs after the job has started,
         prints the tail of ``launch.err``. A detached job that dies after
         that second is not reported by the launch. The first status check
         reports it (see :func:`build_status_check_script`). Until that check,
         the row is stored as ``running`` with a PID that is no longer alive.
      4. When the wrapped command finishes (including by calling ``exit N``),
         the inner script writes that command's own exit status to
         ``exit_code.txt``. The command runs in a subshell ``( command )``, so
         the recorded value is the command's ``$?``, not the status of the
         wrapper shell. A command killed by a signal records ``128 + signal
         number``. This -- not "the PID is gone" -- is the primary "is it
         actually done, and how" signal: a PID disappearing is ambiguous
         (normal exit vs. OOM-killed vs. host reboot all look the same from
         the outside), while an existing ``exit_code.txt`` means the command
         finished and this is its status. A job whose wrapper dies before it
         can write the file leaves no exit-code file and is reported as
         terminated-unexpectedly.

    ``command`` is the only untrusted input here and is embedded as ONE
    ``shlex.quote``-d argument to ``bash -c`` -- never string-concatenated
    unquoted into the outer script.
    """
    job_dir = build_job_dir(job_id, base_dir)
    nonce = new_nonce()
    # The command runs in a SUBSHELL, not a brace group: a bare ``exit N`` inside
    # the command must end only the subshell, so the ``echo $?`` record still
    # runs. In a brace group that ``exit`` would end the wrapper first and the
    # job would be reported as terminated-unexpectedly. The newline before ``)``
    # keeps a trailing shell comment in the command from swallowing the paren.
    inner = f"( {command}\n) > stdout.log 2> stderr.log < /dev/null; echo $? > exit_code.txt"
    quoted_inner = shlex.quote(inner)
    return (
        f"mkdir -p {job_dir} && cd {job_dir} && "
        f"echo {nonce} > nonce.txt && "
        f"{{ nohup setsid bash -c {quoted_inner} < /dev/null > /dev/null 2> launch.err & }} && "
        f"echo $! | tee pid.txt && "
        f"sleep 1 && "
        f'{{ kill -0 "$(cat pid.txt)" 2>/dev/null || [ -f exit_code.txt ]; }} || '
        f"{{ tail -c {MAX_LOG_TAIL_BYTES} launch.err >&2 2>/dev/null; exit 1; }}"
    )


def build_status_check_script(
    job_id: str,
    *,
    base_dir: str = DEFAULT_REMOTE_BASE_DIR,
    log_tail_bytes: int = MAX_LOG_TAIL_BYTES,
) -> str:
    """Build the single POSIX shell command that determines a job's status,
    in the exact priority order the sprint item specifies:

      1. ``exit_code.txt`` exists -> the wrapped command finished; read the
         exit status it recorded (see :func:`build_launch_script` step 4).
      2. Else the recorded PID is still alive (``kill -0``) -> running.
      3. Else a PID was recorded but is gone with no exit code -> the job
         was launched but died without the wrapper ever completing
         (``terminated-unexpectedly`` -- most often OOM-kill or a host
         reboot/reset).
      4. Else (no ``pid.txt`` at all, or the job directory itself is
         missing) -> genuinely unknown, distinct from every case above.

    The header lines come first: ``MERIDIAN_STATUS=...``, then
    ``MERIDIAN_EXIT_CODE=...`` (only when DONE; the value is the wrapped
    command's own exit status, see :func:`build_launch_script` step 4) and
    ``MERIDIAN_PID=...`` (whenever ``pid.txt`` exists, so a caller can persist
    the PID and log path even when the status itself is not RUNNING).

    A ``MERIDIAN_NONCE=<nonce>`` line (the nonce from ``nonce.txt``) follows
    the header, then two independent log-tail blocks, one per file. Each
    block's markers carry that nonce: ``MERIDIAN_STDOUT_BEGIN_<nonce>`` ..
    ``MERIDIAN_STDOUT_END_<nonce>`` holds the last ``log_tail_bytes`` of
    ``stdout.log``, and ``MERIDIAN_STDERR_BEGIN_<nonce>`` ..
    ``MERIDIAN_STDERR_END_<nonce>`` holds the last ``log_tail_bytes`` of
    ``stderr.log``. Each file gets its own ``tail -c`` call so a large stderr
    cannot crowd stdout out of the result. When ``stderr.log`` is absent the
    job never started, so the stderr block holds ``launch.err`` instead, and a
    launch that failed still reports why. With no ``nonce.txt`` (the NODIR path,
    or a job directory from before nonces existed) no blocks are emitted at all,
    and the parser returns empty tails.

    Emits a small, line-oriented, greppable marker format rather than JSON --
    no guarantee a bare POSIX ``sh`` on an arbitrary remote host has a JSON
    encoder on hand, but every one of these is a plain ``echo``.
    :func:`parse_status_check_output` is this format's one reader.
    """
    job_dir = build_job_dir(job_id, base_dir)
    tail_bytes = int(log_tail_bytes)
    # Blocks are emitted only when nonce.txt exists and is non-empty. On the
    # NODIR path the cwd is NOT the job directory, so there is no nonce and no
    # blocks, which also keeps a stray stdout.log in $HOME from being shown.
    # The nonce is read into N and every marker is built from it. The echo
    # arguments are double-quoted, so a malformed nonce file cannot split or
    # glob them. The parser rejects any nonce that is not 16 hex characters.
    tail_blocks = (
        "if [ -s nonce.txt ]; then N=$(cat nonce.txt); "
        'echo "MERIDIAN_NONCE=$N"; '
        f'echo "{_STDOUT_BEGIN}_$N"; tail -c {tail_bytes} stdout.log 2>/dev/null; echo; '
        f'echo "{_STDOUT_END}_$N"; '
        f'echo "{_STDERR_BEGIN}_$N"; '
        f"{{ tail -c {tail_bytes} stderr.log 2>/dev/null || "
        f"tail -c {tail_bytes} launch.err 2>/dev/null; }}; echo; "
        f'echo "{_STDERR_END}_$N"; '
        "fi"
    )
    return (
        f"cd {job_dir} 2>/dev/null || {{ echo MERIDIAN_STATUS=NODIR; exit 0; }}; "
        "if [ -f exit_code.txt ]; then "
        "echo MERIDIAN_STATUS=DONE; echo MERIDIAN_EXIT_CODE=$(cat exit_code.txt); "
        "elif [ -f pid.txt ] && kill -0 \"$(cat pid.txt)\" 2>/dev/null; then "
        "echo MERIDIAN_STATUS=RUNNING; "
        "elif [ -f pid.txt ]; then "
        "echo MERIDIAN_STATUS=DIED; "
        "else echo MERIDIAN_STATUS=NOPID; fi; "
        "if [ -f pid.txt ]; then echo MERIDIAN_PID=$(cat pid.txt); fi; "
        f"{tail_blocks}"
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


def _extract_block(text: str, begin: str, end: str, start: int = 0) -> tuple[str, int]:
    """Return ``(body, cursor)`` for the first ``begin``..``end`` block at or
    after ``start``, where ``cursor`` is the offset just past the ``end``
    marker. A missing ``begin`` yields ``("", start)``; a missing ``end``
    (truncated transport output) takes the rest of the text."""
    begin_at = text.find(begin, start)
    if begin_at < 0:
        return "", start
    body_start = begin_at + len(begin)
    end_at = text.find(end, body_start)
    if end_at < 0:
        return text[body_start:].strip("\n"), len(text)
    return text[body_start:end_at].strip("\n"), end_at + len(end)


def parse_status_check_output(stdout: object) -> dict[str, Any]:
    """Parse :func:`build_status_check_script`'s marker-format output into
    ``{"marker": str, "exit_code": int | None, "pid": int | None,
    "stdout_tail": str, "stderr_tail": str}``.

    ``marker`` is one of DONE/RUNNING/DIED/NOPID/NODIR, or "NOPID" as a safe
    default when the expected marker line is missing entirely (a
    should-never-happen case -- a genuinely broken remote shell -- that
    still needs to map to SOMETHING rather than raising).

    ``marker``, ``exit_code`` and ``pid`` are read ONLY from the header, the
    part before the first log block. Log content is job output, which is
    untrusted, so a log line that happens to look like ``MERIDIAN_EXIT_CODE=``
    must never be taken as the real exit status. Each tail is read only from a
    block whose markers carry the nonce declared in the header (see the
    constants above), so job output that contains the fixed marker strings
    cannot move a tail boundary. Each tail is truncated to
    :data:`MAX_LOG_TAIL_BYTES` on its own. Without a nonce header, both tails
    are empty.
    """
    text = stdout if isinstance(stdout, str) else ""
    # Job output appears only inside blocks, and the real first block begins
    # before any of it, so the first occurrence of the unsuffixed prefix is
    # always the real one. Everything before it is the header.
    first_block = text.find(_STDOUT_BEGIN)
    header = text if first_block < 0 else text[:first_block]
    marker_match = _STATUS_LINE_RE.search(header)
    marker = marker_match.group(1) if marker_match else "NOPID"
    exit_match = _EXIT_CODE_LINE_RE.search(header)
    exit_code = int(exit_match.group(1)) if exit_match else None
    pid_match = _PID_LINE_RE.search(header)
    pid = int(pid_match.group(1)) if pid_match else None
    # Without a valid nonce line the tails stay empty. That fails closed.
    stdout_tail = stderr_tail = ""
    nonce_match = _NONCE_LINE_RE.search(header)
    if nonce_match:
        nonce = nonce_match.group(1)
        stdout_tail, cursor = _extract_block(
            text, f"{_STDOUT_BEGIN}_{nonce}", f"{_STDOUT_END}_{nonce}",
        )
        stderr_tail, _ = _extract_block(
            text, f"{_STDERR_BEGIN}_{nonce}", f"{_STDERR_END}_{nonce}", cursor,
        )
    return {
        "marker": marker,
        "exit_code": exit_code,
        "pid": pid,
        "stdout_tail": truncate_log_tail(stdout_tail),
        "stderr_tail": truncate_log_tail(stderr_tail),
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
