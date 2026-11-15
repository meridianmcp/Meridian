"""Read-only GPU jobs for the GPUs tab: this computer's GPUs and their processes, the SSH hosts saved in Settings, Slurm and
LSF GPU jobs, and RunPod pods from the local Meridian server.

Pure Python, no Qt. Every outside call goes through a runner the caller passes in. A runner is ``run(argv, timeout)``
and returns a CommandResult. The default runner starts a program from an argument list with no shell, with a timeout,
and with no input, so a password prompt cannot wait. Tests pass a fake runner, so they start no process and open no
socket.

Nothing here starts, stops, submits or changes anything. The commands are fixed. The only text from the owner is a
saved SSH host name. It is checked against ALIAS_PATTERN, and it is always passed after "--", so it can never read as
an option.

What each part reads, and what it says when it cannot read:
- This computer, GPUs: nvidia-smi, two queries. Without nvidia-smi the GPUs part says so.
- Each saved host: one ssh call that runs the same nvidia-smi queries, then squeue and bjobs when they exist on that
  host. A host that fails names its reason (host key unknown, auth failed, timed out, unreachable). It never stops the
  other hosts.
- Slurm and LSF on this computer: squeue --me and bjobs. Without them the part says "not on this machine".
- RunPod: the pods the local Meridian server lists. Without that server the part says "not connected".
"""

from __future__ import annotations

import csv
import math
import os
import re
import shlex
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from meridian_desktop import resources
from meridian_desktop.resources import ResourceRow

# ----------------------------------------------------------------- limits and fixed text

MAX_HOSTS = 8
MAX_WORKERS = 4
LOCAL_TIMEOUT_SECONDS = 10.0
SSH_TIMEOUT_SECONDS = 20.0
# A remote host is read no more often than this. The local GPUs are read on every tick of the tab's timer, which is 5 s.
# The 30 s floor is the owner's to confirm, so it is one named value here.
REMOTE_REFRESH_SECONDS = 30.0
# After a failed login, a host is skipped for 1, 2, 4 ... minutes, and never longer than this.
BACKOFF_CAP_MINUTES = 10
# The stderr words that mean a login was refused, as opposed to a host that did not answer.
LOGIN_FAILURE_WORDS = (
    "permission denied",
    "host key",
    "remote host identification has changed",
    "too many authentication failures",
)
# The owner-facing text for a host whose key is not accepted, and for a changed key. Neither tells anyone to accept a key.
KEY_TYPE_TEXT = "The host offers no SSH key type this computer accepts. This needs a change on the host, not here."
# The "No <type> host key is known" line is printed for first contact, and for a key type the host does not offer. The
# text says it is not in known_hosts and never says the key changed.
FIRST_CONTACT_TEXT = (
    "This host has no key of this type in known_hosts. If you have connected to it before, its key may have changed. "
    "Check its fingerprint with the host's admin before you add any key. Do not add a fingerprint you have not verified."
)
CHANGED_KEY_TEXT = (
    "Host key changed. This may be a reinstall or an attack. "
    "Do not accept the new key until the host's admin confirms it."
)
# A saved host is an SSH alias from the owner's own ssh config: letters, digits, dot, dash, underscore and "@" for
# user@host. It may not start with a dash, so it can never be read as an option.
ALIAS_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@-]{0,62}")

LOCAL_HOST = "This computer"

SOURCE_GPU_PROCESS = "GPU process"
SOURCE_SLURM = "Slurm"
SOURCE_LSF = "LSF"
SOURCE_RUNPOD = "RunPod"

PART_GPUS = "GPUs"
PART_PROCESSES = "Processes"
PART_SLURM = "Slurm"
PART_LSF = "LSF"
PART_RUNPOD = "RunPod"

STATE_OK = "ok"
STATE_EMPTY = "empty"  # the tool answered and reported nothing
STATE_ABSENT = "absent"  # the tool is not on this machine or host
STATE_FAILED = "failed"  # the tool was there but the read failed

# The nvidia-smi fields the GPU table shows, in the order the query asks for them.
GPU_FIELDS = "index,name,uuid,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit"
APP_FIELDS = "pid,process_name,used_memory,gpu_uuid"
NVIDIA_FORMAT = "--format=csv,noheader,nounits"
NVIDIA_GPU_ARGV = ("nvidia-smi", f"--query-gpu={GPU_FIELDS}", NVIDIA_FORMAT)
NVIDIA_APPS_ARGV = ("nvidia-smi", f"--query-compute-apps={APP_FIELDS}", NVIDIA_FORMAT)
# squeue: job id, user, name, state, elapsed, nodes, gres. A job name may hold "|", so the name is what sits between
# the user and the state, and the fixed fields are counted from the end.
SQUEUE_FORMAT = "%i|%u|%j|%T|%M|%D|%b"
SQUEUE_ARGV = ("squeue", "--me", "--noheader", f"--format={SQUEUE_FORMAT}")
# bjobs: the GPU column is gpu_alloc. Its name on an LSF host is not verified here, so an unknown name fails the read
# in plain words rather than showing a wrong job.
LSF_FIELDS = "jobid stat user run_time gpu_alloc job_name"
BJOBS_ARGV = ("bjobs", "-noheader", "-o", LSF_FIELDS)

# The remote script. Each section starts with its marker line and ends with a status line that carries the section's own
# exit code, so a failed step is never hidden behind a later step that succeeded. A section that never prints its status
# line is read as failed. The script ends with exit 0, so the only 255 that reaches the reader is ssh's own.
GPU_MARKER = "---GPU---"
APPS_MARKER = "---APPS---"
SLURM_MARKER = "---SLURM---"
LSF_MARKER = "---LSF---"
RES_MARKER = "---RES---"
# Printed by the script's EXIT trap, after its final exit 0. Its presence is how the reader knows the script finished.
END_MARKER = "---END---"
SECTION_MARKERS = {GPU_MARKER: "GPU", APPS_MARKER: "APPS", SLURM_MARKER: "SLURM", LSF_MARKER: "LSF", RES_MARKER: "RES"}
_STATUS_LINE = re.compile(r"---(GPU|APPS|SLURM|LSF|RES)-RC=(\d+)---")
REMOTE_SCRIPT = "; ".join(
    [
        f"trap 'echo {END_MARKER}' EXIT",
        f"echo {GPU_MARKER}",
        f"nvidia-smi --query-gpu={GPU_FIELDS} {NVIDIA_FORMAT}",
        'echo "---GPU-RC=$?---"',
        f"echo {APPS_MARKER}",
        f"nvidia-smi --query-compute-apps={APP_FIELDS} {NVIDIA_FORMAT}",
        'echo "---APPS-RC=$?---"',
        f'if command -v squeue >/dev/null 2>&1; then echo {SLURM_MARKER}; squeue --me --noheader "--format={SQUEUE_FORMAT}"; echo "---SLURM-RC=$?---"; fi',
        f'if command -v bjobs >/dev/null 2>&1; then echo {LSF_MARKER}; bjobs -noheader -o "{LSF_FIELDS}"; echo "---LSF-RC=$?---"; fi',
        # CPU and memory: the core count, the two memory totals, and two /proc/stat samples one second apart.
        f"echo {RES_MARKER}",
        "nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null",
        'grep -E "^(MemTotal|MemAvailable):" /proc/meminfo 2>/dev/null',
        "head -n 1 /proc/stat 2>/dev/null",
        "sleep 1",
        "head -n 1 /proc/stat 2>/dev/null",
        'echo "---RES-RC=$?---"',
        "exit 0",
    ]
)
# The login shell on the host is not assumed to be POSIX, so the script goes to sh -c as one quoted argument.
REMOTE_COMMAND = "sh -c " + shlex.quote(REMOTE_SCRIPT)

# nvidia-smi writes these where a value does not exist or the process owner cannot be read.
UNREPORTED_VALUES = frozenset({"", "[N/A]", "N/A", "[Not Supported]", "[Unknown Error]"})
NAME_HIDDEN_PREFIX = "["  # nvidia-smi writes a process name in brackets when it cannot read it, for example [Insufficient Permissions]

RUNPOD_NOT_CONNECTED_TEXT = "Not connected. The local Meridian server is not answering, so RunPod pods are not listed."
FAILED_READ_TEXT = "The GPU read failed. Press Refresh to try again."
RUNPOD_NOT_LISTED_TEXT = "Not connected. The local Meridian server did not list RunPod pods."
RUNPOD_NO_ROUTE_TEXT = "The local Meridian server has no RunPod pods route yet (HTTP 404). Update Meridian to a build that includes it."
RUNPOD_REFUSED_TEXT = "The local Meridian server refused the RunPod request (HTTP 403)."
RUNPOD_SERVER_ERROR_TEXT = "The local Meridian server could not list RunPod pods (HTTP 500)."
# The local client's error codes (local_client.LocalClientError.code) and the text each one gets. A connection that
# never opened is code "not_running", which is "not answering".
RUNPOD_TEXT_BY_CODE = {
    "http_404": RUNPOD_NO_ROUTE_TEXT,
    "http_403": RUNPOD_REFUSED_TEXT,
    "http_500": RUNPOD_SERVER_ERROR_TEXT,
    "not_running": RUNPOD_NOT_CONNECTED_TEXT,
}

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ----------------------------------------------------------------- data


@dataclass(frozen=True)
class CommandResult:
    """What one program call returned. ``returncode`` is None when the program never finished."""

    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    missing: bool = False  # the program is not on this machine
    timed_out: bool = False
    failed_to_start: bool = False
    # Set when the program is on PATH only as a script (.cmd or .bat). It is the text the owner sees, and nothing runs.
    refusal: str = ""


Runner = Callable[[Sequence[str], float], CommandResult]


@dataclass(frozen=True)
class GpuRow:
    host: str
    index: str
    name: str
    uuid: str
    utilization: int | None
    memory_used_mib: int | None
    memory_total_mib: int | None
    temperature_c: int | None
    power_w: float | None
    power_limit_w: float | None


@dataclass(frozen=True)
class GpuJob:
    """One GPU job or process. ``gpu`` is the GPU index, the GPU's text, or the job's GPU request."""

    source: str
    host: str
    job_id: str
    user: str
    name: str
    state: str
    gpu: str
    gpu_memory_mib: int | None


@dataclass(frozen=True)
class SourceStatus:
    """One line of the host status list: where it was read, which part, whether it worked, and what to say."""

    where: str
    part: str
    state: str
    text: str


@dataclass(frozen=True)
class GpuSnapshot:
    checked_at: datetime
    gpus: tuple[GpuRow, ...] = ()
    jobs: tuple[GpuJob, ...] = ()
    statuses: tuple[SourceStatus, ...] = ()
    resources: tuple[ResourceRow, ...] = ()


@dataclass(frozen=True)
class Reading:
    gpus: tuple[GpuRow, ...] = ()
    jobs: tuple[GpuJob, ...] = ()
    statuses: tuple[SourceStatus, ...] = ()
    resources: tuple[ResourceRow, ...] = ()


# ----------------------------------------------------------------- the default runner


# Where each program is looked for before PATH. A bare name is never run from the current directory, because on Windows
# a bare name is searched there first. Only absolute folders are searched on PATH, and each answer is kept.
PROGRAM_CANDIDATES = {
    "nvidia-smi": (
        r"%SystemRoot%\System32\nvidia-smi.exe",
        r"%ProgramFiles%\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
    ),
    "ssh": (r"%SystemRoot%\System32\OpenSSH\ssh.exe",),
    "squeue": (),
    "bjobs": (),
}
_RESOLVED: dict[str, str | None] = {}
_RESOLVE_LOCK = threading.Lock()


# Only a real program runs. On Windows that is an .exe; a .cmd or .bat is a script and is refused by name.
PROGRAM_EXTENSION = ".exe" if os.name == "nt" else ""
REFUSED_SCRIPT_EXTENSIONS = (".cmd", ".bat")


def _extensions() -> tuple[str, ...]:
    return (PROGRAM_EXTENSION,)


def _absolute_path_folders() -> list[str]:
    return [folder for folder in os.environ.get("PATH", "").split(os.pathsep) if folder and os.path.isabs(folder)]


def program_refusal(name: str) -> str | None:
    """The owner-facing text when a script of this name is on an absolute PATH folder and no .exe is found."""
    if os.name != "nt":
        return None
    for folder in _absolute_path_folders():
        for extension in REFUSED_SCRIPT_EXTENSIONS:
            script = os.path.join(folder, name + extension)
            if os.path.isfile(script):
                return (
                    f"{name}{extension} is on PATH, but it is a script, and only .exe programs are run. "
                    f"Put {name}.exe on PATH instead."
                )
    return None


def resolve_program(name: str) -> str | None:
    """The absolute path of one program: a standard location first, then an absolute folder on PATH. None when it is
    not found. The answer, found or not, is kept, so each program is resolved once per run of the app."""
    with _RESOLVE_LOCK:
        if name in _RESOLVED:
            return _RESOLVED[name]
        found = None
        for candidate in PROGRAM_CANDIDATES.get(name, ()):
            path = os.path.expandvars(candidate)
            if os.path.isabs(path) and os.path.isfile(path):
                found = path
                break
        if found is None:
            for folder in os.environ.get("PATH", "").split(os.pathsep):
                if not folder or not os.path.isabs(folder):
                    continue
                for extension in _extensions():
                    path = os.path.join(folder, name + extension)
                    if os.path.isfile(path):
                        found = os.path.abspath(path)
                        break
                if found is not None:
                    break
        _RESOLVED[name] = found
        return found


def clear_program_cache() -> None:
    """Forget the resolved programs, so the next call looks again. For tests only."""
    with _RESOLVE_LOCK:
        _RESOLVED.clear()


def run_command(argv: Sequence[str], timeout: float) -> CommandResult:
    """Start one program from an argument list. No shell, no input, no window, and a timeout. Never raises.

    argv[0] is the program's bare name. It is started by its resolved absolute path, and a program that cannot be
    resolved is reported as missing without anything being started.
    """
    if not argv:
        return CommandResult(returncode=None, missing=True)
    program = resolve_program(argv[0])
    if program is None:
        return CommandResult(returncode=None, missing=True, refusal=program_refusal(argv[0]) or "")
    kwargs: dict[str, Any] = {}
    if _NO_WINDOW:
        kwargs["creationflags"] = _NO_WINDOW
    try:
        done = subprocess.run(
            [program, *argv[1:]],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            check=False,
            stdin=subprocess.DEVNULL,
            **kwargs,
        )
    except FileNotFoundError:
        return CommandResult(returncode=None, missing=True)
    except subprocess.TimeoutExpired:
        return CommandResult(returncode=None, timed_out=True)
    except OSError:
        return CommandResult(returncode=None, failed_to_start=True)
    return CommandResult(returncode=done.returncode, stdout=done.stdout, stderr=done.stderr)


def ssh_argv(alias: str) -> tuple[str, ...]:
    """The ssh call for one saved host. Batch mode asks for no password, and an unknown host key is refused, not
    accepted. The alias comes after "--" and has already passed ALIAS_PATTERN."""
    return (
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "StrictHostKeyChecking=yes",
        "--",
        alias,
        REMOTE_COMMAND,
    )


# ----------------------------------------------------------------- host names


def check_alias(text: str) -> bool:
    """True when the text is a safe SSH host name for a saved host."""
    # fullmatch, not match with "$": a "$" also matches before a trailing newline, which would let "gpu-box\n" through.
    return isinstance(text, str) and ALIAS_PATTERN.fullmatch(text) is not None


def parse_host_list(text: str) -> tuple[tuple[str, ...], list[str]]:
    """Split a comma-separated line of SSH host names. Returns the valid names, in order and without repeats, and the
    plain problems with the rest. An empty line means no hosts and no problem."""
    hosts: list[str] = []
    problems: list[str] = []
    for raw in text.split(","):
        item = raw.strip()
        if not item:
            continue
        if not check_alias(item):
            problems.append(f'"{item[:40]}" is not a valid SSH host name. Use letters, digits, dot, dash or underscore.')
            continue
        if item not in hosts:
            hosts.append(item)
    # Too many names is a refusal, not a silent cut: the caller saves nothing and the owner removes the extra names.
    if len(hosts) > MAX_HOSTS:
        problems.append(f"Save at most {MAX_HOSTS} SSH hosts. Remove {len(hosts) - MAX_HOSTS} to save.")
    return tuple(hosts), problems


# ----------------------------------------------------------------- pure parsers


def _number(text: str) -> float | None:
    cleaned = text.strip()
    if cleaned in UNREPORTED_VALUES:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _whole(text: str) -> int | None:
    value = _number(text)
    return None if value is None else int(value)


def _csv_fields(line: str) -> list[str]:
    return [field.strip() for field in next(csv.reader([line], skipinitialspace=True))]


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def parse_gpus(text: str, host: str) -> tuple[tuple[GpuRow, ...], int]:
    """Parse the GPU query. Returns the rows and how many non-empty lines did not match the nine fields."""
    rows: list[GpuRow] = []
    unmatched = 0
    for line in _lines(text):
        fields = _csv_fields(line)
        if len(fields) != 9:
            unmatched += 1
            continue
        rows.append(
            GpuRow(
                host=host,
                index=fields[0],
                name=fields[1],
                uuid=fields[2],
                utilization=_whole(fields[3]),
                memory_used_mib=_whole(fields[4]),
                memory_total_mib=_whole(fields[5]),
                temperature_c=_whole(fields[6]),
                power_w=_number(fields[7]),
                power_limit_w=_number(fields[8]),
            )
        )
    return tuple(rows), unmatched


def _process_name(raw: str) -> tuple[str, bool]:
    """A short name for a process: the file name of its program. A name nvidia-smi could not read is said so, and
    reported as hidden, so the status line can explain it."""
    cleaned = raw.strip()
    if cleaned.startswith(NAME_HIDDEN_PREFIX) and cleaned.endswith("]"):
        return f"(not readable: {cleaned[1:-1]})", True
    base = re.split(r"[\\/]", cleaned)[-1] if cleaned else ""
    return base or "(no name)", False


def parse_apps(text: str, host: str, gpus: Sequence[GpuRow]) -> tuple[tuple[GpuJob, ...], int, bool]:
    """Parse the compute-apps query. Returns the jobs, the unmatched line count, and whether any name was hidden."""
    index_by_uuid = {row.uuid: row.index for row in gpus if row.uuid}
    jobs: list[GpuJob] = []
    unmatched = 0
    hidden = False
    for line in _lines(text):
        fields = _csv_fields(line)
        if len(fields) != 4:
            unmatched += 1
            continue
        pid, raw_name, used, uuid = fields
        name, was_hidden = _process_name(raw_name)
        hidden = hidden or was_hidden
        jobs.append(
            GpuJob(
                source=SOURCE_GPU_PROCESS,
                host=host,
                job_id=pid,
                user="",
                name=name,
                state="running",
                gpu=index_by_uuid.get(uuid, uuid),
                gpu_memory_mib=_whole(used),
            )
        )
    return tuple(jobs), unmatched, hidden


def parse_squeue(text: str, host: str) -> tuple[tuple[GpuJob, ...], int]:
    """Parse squeue output in SQUEUE_FORMAT. Only jobs that request a GPU (a gres with "gpu") are returned."""
    jobs: list[GpuJob] = []
    unmatched = 0
    for line in _lines(text):
        parts = line.split("|")
        if len(parts) < 7:
            unmatched += 1
            continue
        gres = parts[-1].strip()
        if "gpu" not in gres.lower():
            continue
        jobs.append(
            GpuJob(
                source=SOURCE_SLURM,
                host=host,
                job_id=parts[0].strip(),
                user=parts[1].strip(),
                name="|".join(parts[2:-4]).strip(),
                state=parts[-4].strip(),
                gpu=gres,
                gpu_memory_mib=None,
            )
        )
    return tuple(jobs), unmatched


def parse_bjobs(text: str, host: str) -> tuple[tuple[GpuJob, ...], int]:
    """Parse bjobs output in LSF_FIELDS. Only jobs with a GPU in gpu_alloc are returned. A dash means no GPU."""
    jobs: list[GpuJob] = []
    unmatched = 0
    for line in _lines(text):
        tokens = line.split(None, 5)
        if len(tokens) < 6:
            unmatched += 1
            continue
        job_id, state, user, _elapsed, gpu_alloc, name = tokens
        if gpu_alloc in ("-", "") or not any(char.isdigit() for char in gpu_alloc):
            continue
        jobs.append(
            GpuJob(
                source=SOURCE_LSF,
                host=host,
                job_id=job_id,
                user=user,
                name=name.strip(),
                state=state,
                gpu=gpu_alloc,
                gpu_memory_mib=None,
            )
        )
    return tuple(jobs), unmatched


def parse_runpod_pods(payload: Mapping[str, Any]) -> tuple[GpuJob, ...]:
    """Pods from the local server's RunPod list. A pod with no readable id is skipped."""
    raw = payload.get("pods")
    if not isinstance(raw, list):
        return ()
    jobs: list[GpuJob] = []
    for pod in raw:
        if not isinstance(pod, dict) or not pod.get("id"):
            continue
        gpu = pod.get("gpu")
        gpu_text = ""
        if isinstance(gpu, dict) and gpu.get("id") is not None:
            gpu_text = str(gpu["id"]) if gpu.get("count") is None else f"{gpu['id']} x{gpu['count']}"
        jobs.append(
            GpuJob(
                source=SOURCE_RUNPOD,
                host=SOURCE_RUNPOD,
                job_id=str(pod["id"]),
                user="",
                name=str(pod.get("name") or pod["id"]),
                state=str(pod.get("status") or ""),
                gpu=gpu_text,
                gpu_memory_mib=None,
            )
        )
    return tuple(jobs)


# ----------------------------------------------------------------- reading one place


def _nvidia_gpu_status(where: str, result: CommandResult, *, remote: bool) -> tuple[tuple[GpuRow, ...], SourceStatus]:
    """The GPU rows and the status line for one nvidia-smi GPU query."""
    if result.missing:
        if result.refusal:
            return (), SourceStatus(where, PART_GPUS, STATE_FAILED, result.refusal)
        text = (
            "nvidia-smi is not on this host, so its GPUs cannot be read."
            if remote
            else "nvidia-smi is not on this computer, so its GPUs cannot be read."
        )
        return (), SourceStatus(where, PART_GPUS, STATE_ABSENT, text)
    if result.timed_out:
        return (), SourceStatus(where, PART_GPUS, STATE_FAILED, "nvidia-smi timed out.")
    if result.returncode != 0:
        return (), SourceStatus(
            where, PART_GPUS, STATE_FAILED, f"nvidia-smi could not read the GPUs (exit code {result.returncode})."
        )
    rows, unmatched = parse_gpus(result.stdout, where)
    if not rows:
        if unmatched:
            return (), SourceStatus(where, PART_GPUS, STATE_FAILED, "nvidia-smi printed output this desktop does not read.")
        return (), SourceStatus(where, PART_GPUS, STATE_EMPTY, "No GPUs reported.")
    noun = "GPU" if len(rows) == 1 else "GPUs"
    return rows, SourceStatus(where, PART_GPUS, STATE_OK, f"{len(rows)} {noun} read.")


def _apps_status(where: str, result: CommandResult, gpus: Sequence[GpuRow]) -> tuple[tuple[GpuJob, ...], SourceStatus]:
    """The GPU process rows and the status line for one nvidia-smi process query."""
    if result.timed_out:
        return (), SourceStatus(where, PART_PROCESSES, STATE_FAILED, "GPU processes timed out.")
    if result.missing or result.returncode != 0:
        text = result.refusal or "GPU processes could not be read."
        return (), SourceStatus(where, PART_PROCESSES, STATE_FAILED, text)
    jobs, unmatched, hidden = parse_apps(result.stdout, where, gpus)
    if unmatched and not jobs:
        return (), SourceStatus(where, PART_PROCESSES, STATE_FAILED, "GPU processes printed output this desktop does not read.")
    if not jobs:
        return (), SourceStatus(where, PART_PROCESSES, STATE_EMPTY, "No GPU processes are running.")
    noun = "process" if len(jobs) == 1 else "processes"
    text = f"{len(jobs)} GPU {noun} listed."
    if hidden:
        text += " Some names need admin rights to read."
    return jobs, SourceStatus(where, PART_PROCESSES, STATE_OK, text)


def _scheduler_status(
    where: str, part: str, result: CommandResult | None, parser: Callable[[str, str], tuple[tuple[GpuJob, ...], int]]
) -> tuple[tuple[GpuJob, ...], SourceStatus]:
    """Slurm or LSF on one place. ``result`` is None when the tool is not there (a remote host with no marker)."""
    if result is not None and result.refusal:
        return (), SourceStatus(where, part, STATE_FAILED, result.refusal)
    if result is None or result.missing:
        text = f"{part}: not on this machine." if where == LOCAL_HOST else f"{part}: not on this host."
        return (), SourceStatus(where, part, STATE_ABSENT, text)
    if result.timed_out or result.returncode != 0:
        return (), SourceStatus(where, part, STATE_FAILED, f"{part} could not be read.")
    jobs, unmatched = parser(result.stdout, where)
    if not jobs:
        text = f"No {part} GPU jobs."
        if unmatched:
            text = f"{part} printed output this desktop does not read."
            return (), SourceStatus(where, part, STATE_FAILED, text)
        return (), SourceStatus(where, part, STATE_EMPTY, text)
    noun = "job" if len(jobs) == 1 else "jobs"
    return jobs, SourceStatus(where, part, STATE_OK, f"{len(jobs)} GPU {noun}.")


def _read_local(run: Runner) -> Reading:
    gpu_result = run(NVIDIA_GPU_ARGV, LOCAL_TIMEOUT_SECONDS)
    gpus, gpu_status = _nvidia_gpu_status(LOCAL_HOST, gpu_result, remote=False)
    apps_result = run(NVIDIA_APPS_ARGV, LOCAL_TIMEOUT_SECONDS)
    apps, apps_status = _apps_status(LOCAL_HOST, apps_result, gpus)
    slurm_result = run(SQUEUE_ARGV, LOCAL_TIMEOUT_SECONDS)
    slurm, slurm_status = _scheduler_status(
        LOCAL_HOST, PART_SLURM, slurm_result, lambda text, host: parse_squeue(text, host)
    )
    lsf_result = run(BJOBS_ARGV, LOCAL_TIMEOUT_SECONDS)
    lsf, lsf_status = _scheduler_status(LOCAL_HOST, PART_LSF, lsf_result, lambda text, host: parse_bjobs(text, host))
    return Reading(
        gpus=gpus,
        jobs=apps + slurm + lsf,
        statuses=(gpu_status, apps_status, slurm_status, lsf_status),
    )


def _ssh_failure(where: str, result: CommandResult) -> SourceStatus:
    """Name the reason an ssh call failed. The text is fixed: the raw error is never shown."""
    if result.missing:
        return SourceStatus(where, PART_GPUS, STATE_ABSENT, "ssh is not on this computer, so this host cannot be read.")
    if result.failed_to_start:
        return SourceStatus(where, PART_GPUS, STATE_FAILED, "ssh could not be started.")
    if result.timed_out:
        return SourceStatus(where, PART_GPUS, STATE_FAILED, "Timed out. The host did not answer in time.")
    stderr = result.stderr.lower()
    # A changed key prints the identification banner, and ssh then reports the same bare "Host key verification
    # failed." line as an unknown key. So the banner is checked first, and the new key is never offered for acceptance.
    if "remote host identification has changed" in stderr:
        reason = CHANGED_KEY_TEXT
    elif "no matching host key type" in stderr:
        reason = KEY_TYPE_TEXT
    elif "host key is known" in stderr:
        # ssh's strict-checking line for a host with no known key: first contact, or a key type it has not recorded.
        reason = FIRST_CONTACT_TEXT
    elif "host key verification failed" in stderr:
        reason = "Host key not accepted. Check the host's key in a terminal before trying again."
    elif "permission denied" in stderr:
        reason = "Auth failed. The key for this host is not accepted without a password."
    elif "timed out" in stderr:
        reason = "Timed out. The host did not answer in time."
    else:
        reason = "Unreachable. The host name or the network did not answer."
    return SourceStatus(where, PART_GPUS, STATE_FAILED, reason)


@dataclass(frozen=True)
class _Section:
    """One section of the remote reply. ``status`` is None when the section never printed its status line."""

    started: bool
    status: int | None
    text: str


def _sections(text: str) -> tuple[dict[str, _Section], bool]:
    """Split the remote reply into its sections. The second value is True only when the end marker is there, which the
    script's EXIT trap prints after its final exit 0. Without it the script did not finish."""
    started: set[str] = set()
    statuses: dict[str, int] = {}
    lines: dict[str, list[str]] = {name: [] for name in SECTION_MARKERS.values()}
    current: str | None = None
    finished = False
    for line in text.splitlines():
        if line == END_MARKER:
            finished = True
            current = None
            continue
        if line in SECTION_MARKERS:
            current = SECTION_MARKERS[line]
            started.add(current)
            continue
        status = _STATUS_LINE.fullmatch(line)
        if status is not None:
            statuses[status.group(1)] = int(status.group(2))
            current = None
            continue
        if current is not None:
            lines[current].append(line)
    sections = {
        name: _Section(name in started, statuses.get(name), "\n".join(lines[name])) for name in SECTION_MARKERS.values()
    }
    return sections, finished


def _remote_gpus(alias: str, section: _Section) -> tuple[tuple[GpuRow, ...], SourceStatus]:
    # A section that never started, after a script that finished, means nvidia-smi is not on the host. A started one
    # that did not report a status failed, and so does a non-zero status, 127 included.
    if not section.started:
        return (), SourceStatus(alias, PART_GPUS, STATE_ABSENT, "nvidia-smi is not on this host, so its GPUs cannot be read.")
    if section.status is None:
        return (), SourceStatus(alias, PART_GPUS, STATE_FAILED, "nvidia-smi could not be read on this host.")
    return _nvidia_gpu_status(alias, CommandResult(returncode=section.status, stdout=section.text), remote=True)


def _remote_apps(alias: str, section: _Section, gpus: Sequence[GpuRow]) -> tuple[tuple[GpuJob, ...], SourceStatus]:
    if not section.started:
        return (), SourceStatus(alias, PART_PROCESSES, STATE_ABSENT, "GPU processes are not on this host.")
    if section.status is None:
        return (), SourceStatus(alias, PART_PROCESSES, STATE_FAILED, "GPU processes could not be read.")
    return _apps_status(alias, CommandResult(returncode=section.status, stdout=section.text), gpus)


def _remote_scheduler(
    alias: str,
    part: str,
    section: _Section,
    parser: Callable[[str, str], tuple[tuple[GpuJob, ...], int]],
) -> tuple[tuple[GpuJob, ...], SourceStatus]:
    """Slurm or LSF on one host. Not started after a finished script means the tool is not there. Started and failed,
    127 included, means it could not be read."""
    if not section.started:
        return (), SourceStatus(alias, part, STATE_ABSENT, f"{part}: not on this host.")
    if section.status is None:
        return (), SourceStatus(alias, part, STATE_FAILED, f"{part} could not be read.")
    return _scheduler_status(alias, part, CommandResult(returncode=section.status, stdout=section.text), parser)


def _remote_resources(alias: str, section: _Section) -> ResourceRow:
    """This host's cores, CPU use and memory, from its RES section. A section that never started printed none, and a
    section that failed says so."""
    if not section.started:
        return resources.absent_row(alias, resources.SECTION_ABSENT_NOTE)
    if section.status is None or section.status != 0:
        return resources.failed_row(alias, resources.SECTION_FAILED_NOTE)
    return resources.parse_section(alias, section.text)


def _login_failed(result: CommandResult) -> bool:
    """True for a failed login that a retry will not fix soon: a refused key or a host key that is not accepted."""
    if result.returncode != 255:
        return False
    stderr = result.stderr.lower()
    # A key-type mismatch is a setting on the host, not a refused login, so it does not back the host off.
    if "no matching host key type" in stderr:
        return False
    return any(word in stderr for word in LOGIN_FAILURE_WORDS)


def _read_remote(run: Runner, alias: str, backoff: HostBackoff | None) -> Reading:
    if backoff is not None:
        wait = backoff.remaining(alias)
        if wait > 0:
            minutes = max(1, math.ceil(wait / 60))
            text = f"Skipped for {minutes} min after a failed login."
            return Reading(statuses=(SourceStatus(alias, PART_GPUS, STATE_FAILED, text),))
    result = run(ssh_argv(alias), SSH_TIMEOUT_SECONDS)
    # 255 is ssh's own failure. It is read before any section, so it is never mistaken for a remote command's status.
    if result.missing or result.timed_out or result.failed_to_start or result.returncode == 255:
        if backoff is not None and _login_failed(result):
            backoff.failed(alias)
        return Reading(statuses=(_ssh_failure(alias, result),))
    if backoff is not None:
        backoff.succeeded(alias)
    sections, finished = _sections(result.stdout)
    if not finished:
        # The script never reached its end: the shell could not run it, or it was cut off. No part is called absent.
        return Reading(
            statuses=(
                SourceStatus(alias, PART_GPUS, STATE_FAILED, "nvidia-smi could not be read on this host."),
                SourceStatus(alias, PART_PROCESSES, STATE_FAILED, "GPU processes could not be read."),
                SourceStatus(alias, PART_SLURM, STATE_FAILED, "Slurm could not be read."),
                SourceStatus(alias, PART_LSF, STATE_FAILED, "LSF could not be read."),
            ),
            resources=(resources.failed_row(alias, resources.SECTION_FAILED_NOTE),),
        )
    gpus, gpu_status = _remote_gpus(alias, sections["GPU"])
    apps, apps_status = _remote_apps(alias, sections["APPS"], gpus)
    slurm, slurm_status = _remote_scheduler(alias, PART_SLURM, sections["SLURM"], parse_squeue)
    lsf, lsf_status = _remote_scheduler(alias, PART_LSF, sections["LSF"], parse_bjobs)
    return Reading(
        gpus=gpus,
        jobs=apps + slurm + lsf,
        statuses=(gpu_status, apps_status, slurm_status, lsf_status),
        resources=(_remote_resources(alias, sections["RES"]),),
    )


def _runpod_failure(exc: Exception) -> SourceStatus:
    """The status for a RunPod list that raised. A local-client error names its code; anything else means the server
    was not answering."""
    code = getattr(exc, "code", None)
    if not isinstance(code, str):
        return SourceStatus(SOURCE_RUNPOD, PART_RUNPOD, STATE_ABSENT, RUNPOD_NOT_CONNECTED_TEXT)
    text = RUNPOD_TEXT_BY_CODE.get(code, RUNPOD_NOT_LISTED_TEXT)
    state = STATE_ABSENT if text == RUNPOD_NOT_CONNECTED_TEXT else STATE_FAILED
    return SourceStatus(SOURCE_RUNPOD, PART_RUNPOD, state, text)


def _runpod_reading(runpod: Callable[[], Mapping[str, Any]] | None) -> Reading:
    if runpod is None:
        return Reading(statuses=(SourceStatus(SOURCE_RUNPOD, PART_RUNPOD, STATE_ABSENT, RUNPOD_NOT_CONNECTED_TEXT),))
    try:
        payload = runpod()
    except Exception as exc:  # noqa: BLE001 - the failure is named in plain words, never by class or raw value
        return Reading(statuses=(_runpod_failure(exc),))
    if not isinstance(payload, Mapping) or payload.get("ok") is False:
        return Reading(statuses=(SourceStatus(SOURCE_RUNPOD, PART_RUNPOD, STATE_FAILED, RUNPOD_NOT_LISTED_TEXT),))
    pods = parse_runpod_pods(payload)
    if not pods:
        return Reading(statuses=(SourceStatus(SOURCE_RUNPOD, PART_RUNPOD, STATE_EMPTY, "No RunPod pods."),))
    noun = "pod" if len(pods) == 1 else "pods"
    # The pod list carries no CPU or memory, so each pod's row says so instead of showing a number.
    pod_rows = tuple(resources.not_reported_row(f"RunPod: {job.name}") for job in pods)
    return Reading(
        jobs=pods,
        statuses=(SourceStatus(SOURCE_RUNPOD, PART_RUNPOD, STATE_OK, f"{len(pods)} RunPod {noun}."),),
        resources=pod_rows,
    )


def _failed_reading(where: str) -> Reading:
    return Reading(statuses=(SourceStatus(where, PART_GPUS, STATE_FAILED, "This could not be read."),))


def read_local(
    run: Runner,
    runpod: Callable[[], Mapping[str, Any]] | None = None,
    local_resources: Callable[[], ResourceRow] | None = None,
) -> Reading:
    """This computer's part: its GPUs, processes, schedulers, CPU and memory, and the RunPod pods from the local server.

    Never raises. ``local_resources`` is None when the caller does not read this computer's CPU and memory.
    """
    local = _read_local(run)
    pods = _runpod_reading(runpod)
    own = (local_resources(),) if local_resources is not None else ()
    return Reading(
        gpus=local.gpus,
        jobs=local.jobs + pods.jobs,
        statuses=local.statuses + pods.statuses,
        resources=own + pods.resources,
    )


def read_host(run: Runner, alias: str, backoff: HostBackoff | None = None) -> Reading:
    """One saved host's part. It never raises, and a skipped host says so in its status line."""
    return _guarded_remote(run, alias, backoff)


def assemble(checked_at: datetime, readings: Sequence[Reading]) -> GpuSnapshot:
    """One snapshot from the parts, in the order given."""
    gpus: list[GpuRow] = []
    jobs: list[GpuJob] = []
    statuses: list[SourceStatus] = []
    rows: list[ResourceRow] = []
    for reading in readings:
        gpus.extend(reading.gpus)
        jobs.extend(reading.jobs)
        statuses.extend(reading.statuses)
        rows.extend(reading.resources)
    return GpuSnapshot(
        checked_at=checked_at,
        gpus=tuple(gpus),
        jobs=tuple(jobs),
        statuses=tuple(statuses),
        resources=tuple(rows),
    )


def collect(
    run: Runner,
    hosts: Sequence[str],
    runpod: Callable[[], Mapping[str, Any]] | None = None,
    *,
    now: datetime | None = None,
    backoff: HostBackoff | None = None,
    local_resources: Callable[[], ResourceRow] | None = None,
) -> GpuSnapshot:
    """Read every part once and return one snapshot. Never raises: a part that fails says so in its status line.

    ``hosts`` are saved SSH host names, read in parallel. ``runpod`` returns the local server's pod list, or raises
    when that server is not answering. Passing None means RunPod is not connected. ``backoff`` keeps the skips for
    hosts that failed to log in; the panel passes one that lives as long as the tab.
    """
    checked_at = now if now is not None else datetime.now(timezone.utc)
    names = [name for name in hosts[:MAX_HOSTS] if check_alias(name)]
    readings = [read_local(run, runpod, local_resources)]
    if names:
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(names))) as pool:
            readings.extend(pool.map(lambda alias: read_host(run, alias, backoff), names))
    return assemble(checked_at, readings)


class HostBackoff:
    """Skips a host after a failed login, for a growing interval: 1, 2, 4 ... minutes, capped at BACKOFF_CAP_MINUTES.

    A successful login clears the skip. The clock is injected, so tests move time without waiting. A lock keeps the
    counts right when several host reads report at once.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, int] = {}
        self._retry_at: dict[str, float] = {}

    def remaining(self, alias: str) -> float:
        """Seconds until the host may be tried again. Zero when it is not skipped."""
        with self._lock:
            retry_at = self._retry_at.get(alias)
        if retry_at is None:
            return 0.0
        return max(0.0, retry_at - self._clock())

    def failed(self, alias: str) -> None:
        with self._lock:
            count = self._failures.get(alias, 0) + 1
            self._failures[alias] = count
            minutes = min(2 ** (count - 1), BACKOFF_CAP_MINUTES)
            self._retry_at[alias] = self._clock() + minutes * 60

    def succeeded(self, alias: str) -> None:
        with self._lock:
            self._failures.pop(alias, None)
            self._retry_at.pop(alias, None)

    def forget(self, alias: str) -> None:
        """Drop a host's skip and its failure count, as a Refresh or a removal from Settings does."""
        self.succeeded(alias)


def _guarded_remote(run: Runner, alias: str, backoff: HostBackoff | None) -> Reading:
    try:
        return _read_remote(run, alias, backoff)
    except Exception:  # noqa: BLE001 - one bad host never stops the others
        return _failed_reading(alias)


def failed_local() -> Reading:
    """The local part when its whole read failed before any part could answer."""
    return Reading(statuses=(SourceStatus(LOCAL_HOST, PART_GPUS, STATE_FAILED, FAILED_READ_TEXT),))


def failed_host(alias: str) -> Reading:
    """The part for one saved host when its read failed. The text names no class and no value."""
    return _failed_reading(alias)


def failed_snapshot(now: datetime | None = None) -> GpuSnapshot:
    """What the tab shows when a whole read failed before any part could answer."""
    checked_at = now if now is not None else datetime.now(timezone.utc)
    return GpuSnapshot(
        checked_at=checked_at,
        statuses=(SourceStatus(LOCAL_HOST, PART_GPUS, STATE_FAILED, FAILED_READ_TEXT),),
    )


# ----------------------------------------------------------------- display text (pure)


def memory_text(used: int | None, total: int | None) -> str:
    if used is None or total is None:
        return "-"
    return f"{used} / {total} MiB"


def utilization_text(value: int | None) -> str:
    return "-" if value is None else f"{value}%"


def temperature_text(value: int | None) -> str:
    return "-" if value is None else f"{value} °C"


def power_text(draw: float | None, limit: float | None) -> str:
    if draw is None:
        return "-"
    if limit is None:
        return f"{draw:.0f} W"
    return f"{draw:.0f} / {limit:.0f} W"


def summary_text(snapshot: GpuSnapshot) -> str:
    """One line for the top of the tab: how many GPUs, how many are busy, and how much GPU memory is in use."""
    gpus = snapshot.gpus
    if not gpus:
        return "No GPUs were read."
    hosts = len({row.host for row in gpus})
    busy = sum(1 for row in gpus if row.utilization is not None and row.utilization > 0)
    used = sum(row.memory_used_mib or 0 for row in gpus)
    total = sum(row.memory_total_mib or 0 for row in gpus)
    gpu_noun = "GPU" if len(gpus) == 1 else "GPUs"
    host_noun = "host" if hosts == 1 else "hosts"
    return (
        f"{len(gpus)} {gpu_noun} on {hosts} {host_noun}. {busy} busy. "
        f"{used / 1024:.1f} of {total / 1024:.1f} GB GPU memory in use."
    )


def gpu_table_rows(snapshot: GpuSnapshot) -> list[tuple[str, ...]]:
    return [
        (
            row.host,
            row.index,
            row.name,
            utilization_text(row.utilization),
            memory_text(row.memory_used_mib, row.memory_total_mib),
            temperature_text(row.temperature_c),
            power_text(row.power_w, row.power_limit_w),
        )
        for row in snapshot.gpus
    ]


def job_table_rows(snapshot: GpuSnapshot) -> list[tuple[str, ...]]:
    return [
        (
            job.source,
            job.host,
            job.job_id,
            job.user or "-",
            job.name or "-",
            job.state or "-",
            job.gpu or "-",
            "-" if job.gpu_memory_mib is None else f"{job.gpu_memory_mib} MiB",
        )
        for job in snapshot.jobs
    ]


def status_table_rows(snapshot: GpuSnapshot) -> list[tuple[str, ...]]:
    return [(status.where, status.part, status.text) for status in snapshot.statuses]


def resource_table_rows(snapshot: GpuSnapshot) -> list[tuple[str, ...]]:
    return resources.rows_for_table(snapshot.resources)


__all__ = [
    "ALIAS_PATTERN",
    "APP_FIELDS",
    "BJOBS_ARGV",
    "GPU_FIELDS",
    "LOCAL_HOST",
    "MAX_HOSTS",
    "NVIDIA_APPS_ARGV",
    "NVIDIA_GPU_ARGV",
    "PART_GPUS",
    "PART_LSF",
    "PART_PROCESSES",
    "PART_RUNPOD",
    "PART_SLURM",
    "REMOTE_COMMAND",
    "SQUEUE_ARGV",
    "SOURCE_GPU_PROCESS",
    "SOURCE_LSF",
    "SOURCE_RUNPOD",
    "SOURCE_SLURM",
    "STATE_ABSENT",
    "STATE_EMPTY",
    "STATE_FAILED",
    "STATE_OK",
    "CommandResult",
    "GpuJob",
    "GpuRow",
    "GpuSnapshot",
    "Runner",
    "SourceStatus",
    "check_alias",
    "collect",
    "failed_snapshot",
    "gpu_table_rows",
    "job_table_rows",
    "parse_apps",
    "parse_bjobs",
    "parse_gpus",
    "parse_host_list",
    "parse_runpod_pods",
    "parse_squeue",
    "run_command",
    "ssh_argv",
    "resource_table_rows",
    "status_table_rows",
    "summary_text",
]
