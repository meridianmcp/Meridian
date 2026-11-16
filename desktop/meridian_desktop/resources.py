"""CPU, cores and memory for each target: this computer, each saved SSH host, and each RunPod pod. Pure Python, no Qt.

Every number is read from the system, never estimated:
- This computer: the logical core count from os.cpu_count. Memory and CPU time come from the Windows API on Windows, and from
  /proc on Linux. CPU use is the busy share of CPU time across one short sample (CPU_SAMPLE_SECONDS).
- A saved host: the same numbers from one POSIX section of the existing ssh call (gpu_jobs.REMOTE_SCRIPT). Nothing extra
  is run on the host, and its two /proc/stat samples are one second apart.
- A RunPod pod: the local server's pod list. That list does not report CPU or memory, so each pod row says so. Filling
  those in needs the local server to pass them on.

A number that cannot be read is None and shows as a dash, and the note says what is missing. Nothing here starts, stops or
changes anything. The reads are local counters and the section text the caller already has.
"""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from dataclasses import dataclass

CPU_SAMPLE_SECONDS = 0.5
MIB = 1024 * 1024

# The state words. They match gpu_jobs' state words by value, and this module never imports gpu_jobs.
STATE_OK = "ok"
STATE_FAILED = "failed"
STATE_ABSENT = "absent"  # the target does not report these numbers

NOT_REPORTED_NOTE = "Not reported by the RunPod list, so its CPU and memory are not read."
SECTION_FAILED_NOTE = "Resources could not be read on this host."
SECTION_ABSENT_NOTE = "Resources were not read on this host."
LOCAL_FAILED_NOTE = "This computer's resources could not be read."
MISSING_PREFIX = "Could not read: "


@dataclass(frozen=True)
class ResourceRow:
    """One target's cores, CPU use and memory. A None number is shown as a dash. ``note`` is one fixed sentence, or empty."""

    host: str
    state: str
    cores: int | None = None
    cpu_percent: float | None = None
    memory_used_mib: float | None = None
    memory_total_mib: float | None = None
    note: str = ""


# ----------------------------------------------------------------- parsing (pure)


def parse_cores(text: str) -> int | None:
    """The first line that is a positive whole number: the output of nproc or getconf. None when there is none."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.isdigit() and int(stripped) > 0:
            return int(stripped)
    return None


def parse_meminfo(text: str) -> tuple[float, float] | None:
    """(total MiB, used MiB) from /proc/meminfo's MemTotal and MemAvailable, which are in kB. None when either is missing."""
    values: dict[str, float] = {}
    for line in text.splitlines():
        parts = line.replace(":", " ").split()
        if len(parts) >= 2 and parts[0] in ("MemTotal", "MemAvailable"):
            try:
                values[parts[0]] = float(parts[1])
            except ValueError:
                continue
    if "MemTotal" not in values or "MemAvailable" not in values or values["MemTotal"] <= 0:
        return None
    total = values["MemTotal"] / 1024
    used = max((values["MemTotal"] - values["MemAvailable"]) / 1024, 0.0)
    return total, used


def parse_cpu_samples(text: str) -> list[tuple[float, float]]:
    """Every aggregate 'cpu' line of /proc/stat as (busy, total) jiffies, in order. Busy excludes idle and iowait."""
    samples: list[tuple[float, float]] = []
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] != "cpu" or len(parts) < 5:
            continue
        try:
            fields = [float(value) for value in parts[1:9]]
        except ValueError:
            continue
        total = sum(fields)
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0.0)
        samples.append((total - idle, total))
    return samples


def cpu_percent(first: tuple[float, float] | None, second: tuple[float, float] | None) -> float | None:
    """The busy share between two (busy, total) samples, as 0 to 100. None when a sample is missing or no time passed."""
    if first is None or second is None:
        return None
    elapsed = second[1] - first[1]
    if elapsed <= 0:
        return None
    share = 100.0 * (second[0] - first[0]) / elapsed
    return max(0.0, min(100.0, share))


def _missing_note(cores: int | None, cpu: float | None, memory: tuple[float, float] | None) -> str:
    missing = [name for name, value in (("cores", cores), ("CPU use", cpu), ("memory", memory)) if value is None]
    return "" if not missing else MISSING_PREFIX + ", ".join(missing) + "."


def _row(
    host: str,
    cores: int | None,
    cpu: float | None,
    memory: tuple[float, float] | None,
    failed_note: str = LOCAL_FAILED_NOTE,
) -> ResourceRow:
    """One row from the three numbers. It is ok when any number was read, and failed with ``failed_note`` when none was."""
    found = any(value is not None for value in (cores, cpu, memory))
    total, used = memory if memory is not None else (None, None)
    return ResourceRow(
        host=host,
        state=STATE_OK if found else STATE_FAILED,
        cores=cores,
        cpu_percent=cpu,
        memory_used_mib=used,
        memory_total_mib=total,
        note=_missing_note(cores, cpu, memory) if found else failed_note,
    )


def parse_section(host: str, text: str) -> ResourceRow:
    """One saved host's row from the text of its remote section: the core count, the meminfo lines, and two /proc/stat
    samples. Anything missing is None and named in the note."""
    samples = parse_cpu_samples(text)
    cpu = cpu_percent(samples[0], samples[1]) if len(samples) >= 2 else None
    return _row(host, parse_cores(text), cpu, parse_meminfo(text), SECTION_FAILED_NOTE)


# ----------------------------------------------------------------- this computer


def _posix_stat_sample() -> tuple[float, float] | None:
    with open("/proc/stat", encoding="utf-8") as handle:
        samples = parse_cpu_samples(handle.read())
    return samples[0] if samples else None


def _posix_row(host: str, cores: int | None) -> ResourceRow:
    with open("/proc/meminfo", encoding="utf-8") as handle:
        memory = parse_meminfo(handle.read())
    first = _posix_stat_sample()
    time.sleep(CPU_SAMPLE_SECONDS)
    second = _posix_stat_sample()
    return _row(host, cores, cpu_percent(first, second), memory)


def _windows_times() -> tuple[float, float] | None:
    """(busy, total) in 100 ns units from GetSystemTimes. Kernel time includes idle time, so busy is total minus idle."""
    import ctypes  # noqa: PLC0415 - Windows only, imported where it is used

    class FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    def value(ft: FileTime) -> int:
        return (ft.high << 32) | ft.low

    idle, kernel, user = FileTime(), FileTime(), FileTime()
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None
    total = float(value(kernel) + value(user))
    return total - float(value(idle)), total


def _windows_memory() -> tuple[float, float] | None:
    """(total MiB, used MiB) from GlobalMemoryStatusEx."""
    import ctypes  # noqa: PLC0415 - Windows only, imported where it is used

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_uint32),
            ("load", ctypes.c_uint32),
            ("total_phys", ctypes.c_uint64),
            ("avail_phys", ctypes.c_uint64),
            ("total_page", ctypes.c_uint64),
            ("avail_page", ctypes.c_uint64),
            ("total_virtual", ctypes.c_uint64),
            ("avail_virtual", ctypes.c_uint64),
            ("avail_extended", ctypes.c_uint64),
        ]

    status = MemoryStatus()
    status.length = ctypes.sizeof(MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return status.total_phys / MIB, (status.total_phys - status.avail_phys) / MIB


def _windows_row(host: str, cores: int | None) -> ResourceRow:
    first = _windows_times()
    time.sleep(CPU_SAMPLE_SECONDS)
    second = _windows_times()
    return _row(host, cores, cpu_percent(first, second), _windows_memory())


def local_row(host: str) -> ResourceRow:
    """This computer's row, read now. Never raises: a read that fails leaves its numbers empty and says so."""
    try:
        cores = os.cpu_count()
    except Exception:  # noqa: BLE001 - a missing count is shown as a dash
        cores = None
    try:
        if os.name == "nt":
            return _windows_row(host, cores)
        return _posix_row(host, cores)
    except Exception:  # noqa: BLE001 - a read that fails is shown as not read, never as a crash
        return ResourceRow(host=host, state=STATE_FAILED, cores=cores, note=LOCAL_FAILED_NOTE)


def not_reported_row(host: str) -> ResourceRow:
    """A RunPod pod's row. The pod list does not report CPU or memory, so none is shown."""
    return ResourceRow(host=host, state=STATE_ABSENT, note=NOT_REPORTED_NOTE)


def failed_row(host: str, note: str) -> ResourceRow:
    return ResourceRow(host=host, state=STATE_FAILED, note=note)


def absent_row(host: str, note: str) -> ResourceRow:
    return ResourceRow(host=host, state=STATE_ABSENT, note=note)


# ----------------------------------------------------------------- display (pure)


def cores_text(value: int | None) -> str:
    return "-" if value is None else str(value)


def cpu_text(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}%"


def memory_gb_text(used_mib: float | None, total_mib: float | None) -> str:
    """Memory in GB, used of total, such as 9.4 / 31.2 GB. A dash when either number is missing."""
    if used_mib is None or total_mib is None:
        return "-"
    return f"{used_mib / 1024:.1f} / {total_mib / 1024:.1f} GB"


def rows_for_table(rows: Sequence[ResourceRow]) -> list[tuple[str, ...]]:
    return [
        (
            row.host,
            cores_text(row.cores),
            cpu_text(row.cpu_percent),
            memory_gb_text(row.memory_used_mib, row.memory_total_mib),
            row.note or "-",
        )
        for row in rows
    ]


__all__ = [
    "CPU_SAMPLE_SECONDS",
    "LOCAL_FAILED_NOTE",
    "NOT_REPORTED_NOTE",
    "SECTION_ABSENT_NOTE",
    "SECTION_FAILED_NOTE",
    "STATE_ABSENT",
    "STATE_FAILED",
    "STATE_OK",
    "ResourceRow",
    "absent_row",
    "cpu_percent",
    "cpu_text",
    "cores_text",
    "failed_row",
    "local_row",
    "memory_gb_text",
    "not_reported_row",
    "parse_cores",
    "parse_cpu_samples",
    "parse_meminfo",
    "parse_section",
    "rows_for_table",
]
