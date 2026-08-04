"""6f465466 -- Read-only host-memory diagnostic: kernel pool vs Meridian-owned.

Live evidence on 2026-08-04 (see the sprint item this module ships) showed a
Windows host reporting ~80-91% idle-memory pressure that was NOT explained by
visible Python/Node/uvx process working sets: the paged pool alone (~18 GB)
and nonpaged pool (~3.7 GB) dwarfed the ~13.5 GB total process working-set
sum. A pool-tag snapshot pointed at the ``Toke`` (security-token) tag, which
public reports correlate with a Windows 11 25H2 kernel paged-pool leak
triggered by console-process churn -- a real OS-level defect, not something
Meridian caused or can fix by killing processes.

This module exists to make that distinction (kernel-pool pressure vs
Meridian-owned process pressure) mechanically, every time, instead of by ad
hoc manual investigation:

* ``collect_host_memory_diagnostic`` gathers process totals (via psutil,
  degrading to an empty scan when psutil is unavailable -- same optional
  pattern as ``meridian/orphan_reaper.py`` and ``meridian/tunnel_client.py``),
  system memory totals, Windows kernel paged/nonpaged pool totals
  (``GetPerformanceInfo``, ``psapi.dll``), and best-effort per-tag pool
  attribution (``NtQuerySystemInformation(SystemPoolTagInformation)``,
  ``ntdll.dll``) when the host exposes it.
* It NEVER kills, terminates, or modifies anything. Every data source is
  independently best-effort: a failure in any one of them degrades that
  section to ``None``/``[]`` rather than raising, and the overall function
  itself never raises. The output's ``remediation.auto_action_taken`` is
  always ``False`` -- this is a report, not an actuator.
* Every data source is dependency-injectable (``*_fn`` keyword arguments)
  so the composition, aggregation, and assessment/remediation logic below
  are fully unit-testable without touching real OS state or a real ctypes
  call -- see ``tests/test_host_memory_diagnostics.py``. The two low-level
  Windows-only struct parsers (``_kernel_pool_bytes_from_raw`` and
  ``_parse_pool_tag_buffer``) are further split out as pure functions so
  the tricky pointer-width-dependent struct-layout arithmetic is testable
  with synthetic bytes on any OS, independent of the real syscalls.
* Process cmdlines are redacted for secret-shaped substrings
  (``_redact_secrets``) before being included in the diagnostic -- this is
  project-visible, potentially persisted output, not a local debug dump.

Windows-only sections (kernel pool totals, pool-tag attribution) degrade to
``None`` on any other platform or on any failure (including running without
sufficient privilege) -- see ``confidence`` in the output, which reports
"low" when kernel-pool data was unavailable at all, "medium" when totals were
available but tag-level attribution was not, "high" when both were
available.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import platform
import re
import struct
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# How many top-RSS processes / top pool tags to include in the report.
_TOP_PROCESS_LIMIT = 25
_TOP_POOL_TAG_LIMIT = 15

# Name/cmdline substrings (case-insensitive) that mark a process as
# Meridian-owned for the purposes of this diagnostic's pressure split. Kept
# intentionally narrow (specific to this codebase's own process family) so
# it does not accidentally sweep in an unrelated user "python"/"node"
# process -- unlike orphan_reaper's broader pixi/python/node matching, which
# is scoped by dead-worktree-path instead and doesn't need this precision.
_MERIDIAN_OWNED_SUBSTRINGS: tuple[str, ...] = (
    "meridian",
    "codebase-memory",
    "codebase_memory",
)

# Secret-shaped substrings redacted out of any process cmdline before it is
# included in this (project-visible, potentially persisted) diagnostic.
# Defense in depth, not a guarantee -- mirrors the heuristics already used
# by ``capability_manifest._SECRET_LIKE_RE`` for the same underlying reason.
_SECRET_LIKE_RE = re.compile(
    r"(?i)(sk-[a-z0-9]{10,}|sk_[a-z0-9]{10,}|api[_-]?key\s*[:=]\s*\S+|"
    r"bearer\s+[a-z0-9._-]{10,}|://[^/\s:]+:[^/\s@]+@|password\s*[:=]\s*\S+|"
    r"token\s*[:=]\s*\S+)"
)

# Pressure-classification thresholds. Deliberately conservative constants
# (not derived from a single incident's exact numbers) so the assessment
# generalizes rather than overfitting to the 2026-08-04 snapshot.
_KERNEL_POOL_PRESSURE_MIN_BYTES = 2 * 1024**3  # 2 GiB combined paged+nonpaged
_KERNEL_POOL_UNEXPLAINED_RATIO = 0.5  # must cover >=50% of the unaccounted-for gap
_MERIDIAN_PRESSURE_MIN_BYTES = 1 * 1024**3  # 1 GiB owned by Meridian processes


def _redact_secrets(text: str | None) -> str:
    """Best-effort redaction of secret-shaped substrings from *text*. Never
    raises; returns "" for falsy input."""
    if not text:
        return ""
    try:
        return _SECRET_LIKE_RE.sub("[REDACTED]", text)
    except Exception:  # noqa: BLE001 -- redaction must never crash the scan
        return "[REDACTION_FAILED]"


def _is_meridian_owned(name: str | None, cmdline: str | None) -> bool:
    haystack = f"{name or ''} {cmdline or ''}".lower()
    return any(sub in haystack for sub in _MERIDIAN_OWNED_SUBSTRINGS)


# ---------------------------------------------------------------------------
# Process + system memory sampling (psutil-backed, optional dependency)
# ---------------------------------------------------------------------------


def _psutil_process_snapshot() -> list[dict[str, Any]]:
    """Real process enumeration via ``psutil`` (optional dependency). Returns
    ``[]`` when psutil is unavailable or enumeration itself fails --
    best-effort, same degrade pattern as ``orphan_reaper._psutil_process_iter``."""
    try:
        import psutil  # type: ignore
    except Exception:  # noqa: BLE001 -- psutil not installed
        return []
    out: list[dict[str, Any]] = []
    try:
        proc_iter = psutil.process_iter(["pid", "name", "cmdline"])
    except Exception:  # noqa: BLE001 -- enumeration itself failed
        logger.warning("host_memory_diagnostics: process_iter failed", exc_info=True)
        return []
    for p in proc_iter:
        try:
            info = p.info
            cmdline = " ".join(info.get("cmdline") or [])
            rss = 0
            private = 0
            try:
                mem = p.memory_info()
                rss = int(getattr(mem, "rss", 0) or 0)
                private = int(getattr(mem, "private", None) if getattr(mem, "private", None) is not None else rss)
            except Exception:  # noqa: BLE001 -- process vanished/denied between iter and memory_info
                pass
            out.append(
                {
                    "pid": info.get("pid"),
                    "name": info.get("name") or "",
                    "cmdline": _redact_secrets(cmdline),
                    "rss_bytes": rss,
                    "private_bytes": private,
                }
            )
        except Exception:  # noqa: BLE001 -- a single vanished/permission-denied process must not sink the scan
            continue
    return out


def _real_system_memory() -> dict[str, Any] | None:
    """Total/available physical memory via ``psutil.virtual_memory()``.
    Returns ``None`` when psutil is unavailable or the call fails."""
    try:
        import psutil  # type: ignore
    except Exception:  # noqa: BLE001 -- psutil not installed
        return None
    try:
        vm = psutil.virtual_memory()
        return {
            "total_bytes": int(vm.total),
            "available_bytes": int(vm.available),
            "percent_used": float(vm.percent),
        }
    except Exception:  # noqa: BLE001 -- best-effort, never raises
        logger.warning("host_memory_diagnostics: virtual_memory() failed", exc_info=True)
        return None


def _os_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "platform": sys.platform,
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
    }
    if sys.platform == "win32":
        try:
            edition, win32_version, csd, _ptype = platform.win32_ver()
            info["win32_edition"] = edition
            info["win32_version"] = win32_version
            info["win32_csd"] = csd
        except Exception:  # noqa: BLE001 -- best-effort, never raises
            pass
    return info


# ---------------------------------------------------------------------------
# Kernel paged/nonpaged pool totals (Windows-only, GetPerformanceInfo)
# ---------------------------------------------------------------------------


class _PerformanceInformation(ctypes.Structure):
    """Mirrors WinAPI ``PERFORMANCE_INFORMATION`` (psapi.h). Defining this
    ctypes.Structure is safe on any platform (pure layout description, no OS
    call) -- only ``_real_kernel_pool_totals`` actually touches ``psapi.dll``,
    guarded to run on win32 only."""

    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("CommitTotal", ctypes.c_size_t),
        ("CommitLimit", ctypes.c_size_t),
        ("CommitPeak", ctypes.c_size_t),
        ("PhysicalTotal", ctypes.c_size_t),
        ("PhysicalAvailable", ctypes.c_size_t),
        ("SystemCache", ctypes.c_size_t),
        ("KernelTotal", ctypes.c_size_t),
        ("KernelPaged", ctypes.c_size_t),
        ("KernelNonpaged", ctypes.c_size_t),
        ("PageSize", ctypes.c_size_t),
        ("HandleCount", ctypes.c_ulong),
        ("ProcessCount", ctypes.c_ulong),
        ("ThreadCount", ctypes.c_ulong),
    ]


def _kernel_pool_bytes_from_raw(
    kernel_paged_pages: int,
    kernel_nonpaged_pages: int,
    kernel_total_pages: int,
    page_size_bytes: int,
) -> dict[str, int]:
    """Pure page-count -> byte-count conversion (``GetPerformanceInfo``
    reports pool sizes in pages, not bytes). Split out from
    ``_real_kernel_pool_totals`` so this arithmetic is unit-testable without
    any ctypes/WinAPI call."""
    page_size = int(page_size_bytes) or 4096
    return {
        "paged_bytes": int(kernel_paged_pages) * page_size,
        "nonpaged_bytes": int(kernel_nonpaged_pages) * page_size,
        "kernel_total_bytes": int(kernel_total_pages) * page_size,
        "page_size_bytes": page_size,
    }


def _real_kernel_pool_totals() -> dict[str, Any] | None:
    """Windows kernel paged/nonpaged pool totals via ``psapi.dll``'s
    ``GetPerformanceInfo``. Returns ``None`` on any non-Windows platform (the
    platform check runs BEFORE any ``ctypes.WinDLL`` reference, so this is
    safe to call unconditionally on any OS) or on any failure -- never raises."""
    if sys.platform != "win32":
        return None
    try:
        psapi = ctypes.WinDLL("psapi")  # type: ignore[attr-defined]
        info = _PerformanceInformation()
        info.cb = ctypes.sizeof(_PerformanceInformation)
        psapi.GetPerformanceInfo.argtypes = [ctypes.POINTER(_PerformanceInformation), ctypes.c_ulong]
        psapi.GetPerformanceInfo.restype = ctypes.c_int
        ok = psapi.GetPerformanceInfo(ctypes.byref(info), info.cb)
        if not ok:
            return None
        result = _kernel_pool_bytes_from_raw(info.KernelPaged, info.KernelNonpaged, info.KernelTotal, info.PageSize)
        result["source"] = "GetPerformanceInfo"
        return result
    except Exception:  # noqa: BLE001 -- best-effort diagnostic, never raises
        logger.warning("host_memory_diagnostics: GetPerformanceInfo failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Pool-tag attribution (Windows-only, best-effort, NtQuerySystemInformation)
# ---------------------------------------------------------------------------


def _align(offset: int, alignment: int) -> int:
    return (offset + alignment - 1) // alignment * alignment


def _pool_tag_struct_info(pointer_size: int) -> dict[str, int]:
    """Field offsets + sizes for the undocumented but stable (poolmon-
    verified) ``SYSTEM_POOLTAG`` / ``SYSTEM_POOLTAG_INFORMATION`` structures
    returned by ``NtQuerySystemInformation(SystemPoolTagInformation)``, for a
    host with the given pointer width (4 or 8 bytes). Pure arithmetic -- no
    ctypes/ffi involved -- so it is directly unit-testable for both pointer
    widths regardless of which OS runs the test.

    Layout (``SYSTEM_POOLTAG``): Tag[4] + PagedAllocs(ULONG) +
    PagedFrees(ULONG) + PagedUsed(SIZE_T, pointer-aligned) +
    NonPagedAllocs(ULONG) + NonPagedFrees(ULONG) + NonPagedUsed(SIZE_T,
    pointer-aligned). ``SYSTEM_POOLTAG_INFORMATION`` prefixes the array with
    a single ULONG ``Count``, padded to the entry's own alignment.
    """
    tag_off = 0
    paged_allocs_off = tag_off + 4
    paged_frees_off = paged_allocs_off + 4
    paged_used_off = _align(paged_frees_off + 4, pointer_size)
    nonpaged_allocs_off = paged_used_off + pointer_size
    nonpaged_frees_off = nonpaged_allocs_off + 4
    nonpaged_used_off = _align(nonpaged_frees_off + 4, pointer_size)
    entry_size = _align(nonpaged_used_off + pointer_size, pointer_size)
    header_size = _align(4, pointer_size)
    return {
        "header_size": header_size,
        "entry_size": entry_size,
        "tag_off": tag_off,
        "paged_used_off": paged_used_off,
        "nonpaged_used_off": nonpaged_used_off,
    }


def _parse_pool_tag_buffer(raw: bytes, *, pointer_size: int = 8, top_n: int = _TOP_POOL_TAG_LIMIT) -> list[dict[str, Any]]:
    """Pure parser for the raw ``NtQuerySystemInformation`` buffer:
    ``[Count: ULONG][padding][SYSTEM_POOLTAG entries...]``. Returns the top
    *top_n* tags by combined paged+nonpaged bytes used, descending, skipping
    zero-usage tags. Never raises -- returns ``[]`` on any
    malformed/truncated input so a corrupted or unexpected buffer shape
    degrades the diagnostic instead of crashing it."""
    try:
        if not raw or len(raw) < 4:
            return []
        count = struct.unpack_from("<L", raw, 0)[0]
        info = _pool_tag_struct_info(pointer_size)
        size_fmt = "<Q" if pointer_size == 8 else "<L"
        tags: list[dict[str, Any]] = []
        for i in range(count):
            base = info["header_size"] + i * info["entry_size"]
            end = base + info["entry_size"]
            if end > len(raw):
                break
            tag_bytes = raw[base + info["tag_off"] : base + info["tag_off"] + 4]
            paged_used = struct.unpack_from(size_fmt, raw, base + info["paged_used_off"])[0]
            nonpaged_used = struct.unpack_from(size_fmt, raw, base + info["nonpaged_used_off"])[0]
            if paged_used <= 0 and nonpaged_used <= 0:
                continue
            tag_str = tag_bytes.decode("ascii", errors="replace").rstrip("\x00").strip()
            tags.append(
                {
                    "tag": tag_str or tag_bytes.hex(),
                    "paged_bytes": int(paged_used),
                    "nonpaged_bytes": int(nonpaged_used),
                }
            )
        tags.sort(key=lambda t: t["paged_bytes"] + t["nonpaged_bytes"], reverse=True)
        return tags[:top_n]
    except Exception:  # noqa: BLE001 -- malformed buffer must degrade, not crash
        logger.warning("host_memory_diagnostics: pool tag buffer parse failed", exc_info=True)
        return []


def _real_pool_tag_attribution(top_n: int = _TOP_POOL_TAG_LIMIT) -> dict[str, Any] | None:
    """Best-effort per-tag paged/nonpaged pool attribution via
    ``ntdll.dll``'s ``NtQuerySystemInformation(SystemPoolTagInformation)``.
    Returns ``None`` on any non-Windows platform (checked before any
    ``ctypes.WinDLL`` reference), on a non-success NTSTATUS (e.g. access
    denied on a locked-down host), or on any other failure -- this is
    explicitly "when available" per the diagnostic's acceptance criteria,
    never a hard requirement."""
    if sys.platform != "win32":
        return None
    try:
        ntdll = ctypes.WinDLL("ntdll")  # type: ignore[attr-defined]
        system_pool_tag_information = 22
        pointer_size = ctypes.sizeof(ctypes.c_void_p)
        buf_len = 1 << 20  # 1 MiB -- comfortably covers the full system pool-tag table
        buf = ctypes.create_string_buffer(buf_len)
        return_length = ctypes.c_ulong(0)
        ntdll.NtQuerySystemInformation.argtypes = [
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        ntdll.NtQuerySystemInformation.restype = ctypes.c_long
        status = ntdll.NtQuerySystemInformation(
            system_pool_tag_information, buf, buf_len, ctypes.byref(return_length)
        )
        if status != 0:
            return None
        raw = buf.raw[: max(return_length.value, 4)]
        tags = _parse_pool_tag_buffer(raw, pointer_size=pointer_size, top_n=top_n)
        if not tags:
            return None
        return {"source": "NtQuerySystemInformation", "tags": tags}
    except Exception:  # noqa: BLE001 -- best-effort diagnostic, never raises
        logger.warning("host_memory_diagnostics: pool tag query failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Assessment / remediation (pure, given already-collected data)
# ---------------------------------------------------------------------------


def _assess(
    system_memory: dict[str, Any] | None,
    kernel_pool: dict[str, Any] | None,
    process_totals: dict[str, Any],
    pool_tags: dict[str, Any] | None,
) -> dict[str, Any]:
    """Classify kernel-pool pressure vs Meridian-owned process pressure from
    already-collected data. Pure (no OS calls) -- fully unit-testable."""
    kernel_pool_bytes = None
    if kernel_pool:
        kernel_pool_bytes = int(kernel_pool.get("paged_bytes") or 0) + int(kernel_pool.get("nonpaged_bytes") or 0)

    unexplained_bytes = None
    if system_memory:
        used_bytes = int(system_memory.get("total_bytes") or 0) - int(system_memory.get("available_bytes") or 0)
        unexplained_bytes = max(used_bytes - int(process_totals.get("rss_bytes") or 0), 0)

    kernel_pool_pressure = False
    if kernel_pool_bytes is not None and kernel_pool_bytes >= _KERNEL_POOL_PRESSURE_MIN_BYTES:
        if not unexplained_bytes:
            kernel_pool_pressure = True
        else:
            kernel_pool_pressure = (kernel_pool_bytes / unexplained_bytes) >= _KERNEL_POOL_UNEXPLAINED_RATIO

    meridian_process_pressure = int(process_totals.get("meridian_owned_rss_bytes") or 0) >= _MERIDIAN_PRESSURE_MIN_BYTES

    if kernel_pool is None:
        confidence = "low"
    elif not pool_tags or not pool_tags.get("tags"):
        confidence = "medium"
    else:
        confidence = "high"

    if kernel_pool_pressure and meridian_process_pressure:
        recommendation = (
            "Both a kernel pool pressure signal and elevated Meridian-owned process "
            "memory were observed. Address the Meridian-owned processes first (safe, "
            "within Meridian's control -- see orphan_reaper / owned-process budgets) "
            "and re-run this diagnostic before pursuing OS-level kernel remediation."
        )
    elif kernel_pool_pressure:
        recommendation = (
            "Kernel/driver pool growth is the dominant idle-memory signal and is not "
            "explained by Meridian-owned processes. This requires OS-level "
            "remediation (Windows Update / reboot / driver investigation) -- do not "
            "kill arbitrary processes. See pool_tag_attribution for the leaking tag "
            "when available."
        )
    elif meridian_process_pressure:
        recommendation = (
            "Meridian-owned processes account for a significant share of used "
            "memory. Review/quarantine runaway Meridian-owned indexers before "
            "assuming a kernel issue."
        )
    else:
        recommendation = (
            "No strong pressure signal from either kernel pool or Meridian-owned "
            "processes at this sample point. Insufficient evidence for a specific "
            "remediation -- re-run during/after the reported symptom window."
        )

    return {
        "assessment": {
            "kernel_pool_pressure": kernel_pool_pressure,
            "meridian_process_pressure": meridian_process_pressure,
            "kernel_pool_bytes": kernel_pool_bytes,
            "unexplained_bytes": unexplained_bytes,
            "confidence": confidence,
        },
        "remediation": {
            "recommendation": recommendation,
            "auto_action_taken": False,
        },
    }


# ---------------------------------------------------------------------------
# Composition -- the one function callers use
# ---------------------------------------------------------------------------


def collect_host_memory_diagnostic(
    *,
    process_samples_fn: Callable[[], list[dict[str, Any]]] | None = None,
    system_memory_fn: Callable[[], dict[str, Any] | None] | None = None,
    kernel_pool_fn: Callable[[], dict[str, Any] | None] | None = None,
    pool_tag_fn: Callable[[], dict[str, Any] | None] | None = None,
    os_info_fn: Callable[[], dict[str, Any]] | None = None,
    now_fn: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Read-only, machine-readable host-memory diagnostic (6f465466).

    Reports process totals (overall and Meridian-owned split), system memory
    totals, Windows kernel paged/nonpaged pool totals, best-effort pool-tag
    attribution, a timestamp, OS/build info, a confidence level, and an
    assessment that explicitly distinguishes kernel-pool pressure from
    Meridian-owned process pressure with a remediation recommendation.
    NEVER kills, terminates, or modifies anything -- ``remediation.
    auto_action_taken`` is always ``False``. Never raises: every data source
    is independently best-effort and degrades to ``None``/``[]`` on failure.

    All data sources are dependency-injectable so this function (and
    everything it composes) is unit-testable without touching real OS state.
    """
    process_samples_fn = process_samples_fn or _psutil_process_snapshot
    system_memory_fn = system_memory_fn or _real_system_memory
    kernel_pool_fn = kernel_pool_fn or _real_kernel_pool_totals
    pool_tag_fn = pool_tag_fn or _real_pool_tag_attribution
    os_info_fn = os_info_fn or _os_info
    now_fn = now_fn or time.time

    try:
        samples = list(process_samples_fn())
    except Exception:  # noqa: BLE001 -- one bad data source must not sink the whole report
        logger.warning("host_memory_diagnostics: process sampling failed", exc_info=True)
        samples = []

    try:
        system_memory = system_memory_fn()
    except Exception:  # noqa: BLE001
        logger.warning("host_memory_diagnostics: system memory sampling failed", exc_info=True)
        system_memory = None

    try:
        kernel_pool = kernel_pool_fn()
    except Exception:  # noqa: BLE001
        logger.warning("host_memory_diagnostics: kernel pool sampling failed", exc_info=True)
        kernel_pool = None

    try:
        pool_tags = pool_tag_fn()
    except Exception:  # noqa: BLE001
        logger.warning("host_memory_diagnostics: pool tag sampling failed", exc_info=True)
        pool_tags = None

    try:
        os_info = os_info_fn()
    except Exception:  # noqa: BLE001
        logger.warning("host_memory_diagnostics: os info collection failed", exc_info=True)
        os_info = {"platform": sys.platform}

    rss_total = 0
    private_total = 0
    meridian_rss_total = 0
    meridian_private_total = 0
    meridian_count = 0
    top_processes: list[dict[str, Any]] = []
    for s in samples:
        try:
            rss = int(s.get("rss_bytes") or 0)
            private = int(s.get("private_bytes") or 0)
            name = s.get("name") or ""
            cmdline = s.get("cmdline") or ""
        except Exception:  # noqa: BLE001 -- one malformed sample must not sink aggregation
            continue
        rss_total += rss
        private_total += private
        owned = _is_meridian_owned(name, cmdline)
        if owned:
            meridian_count += 1
            meridian_rss_total += rss
            meridian_private_total += private
        top_processes.append(
            {
                "pid": s.get("pid"),
                "name": name,
                "cmdline": _redact_secrets(cmdline),
                "rss_bytes": rss,
                "private_bytes": private,
                "meridian_owned": owned,
            }
        )
    top_processes.sort(key=lambda p: p["rss_bytes"], reverse=True)

    process_totals: dict[str, Any] = {
        "count": len(samples),
        "rss_bytes": rss_total,
        "private_bytes": private_total,
        "meridian_owned_count": meridian_count,
        "meridian_owned_rss_bytes": meridian_rss_total,
        "meridian_owned_private_bytes": meridian_private_total,
        "top_processes": top_processes[:_TOP_PROCESS_LIMIT],
    }

    verdict = _assess(system_memory, kernel_pool, process_totals, pool_tags)

    return {
        "schema_version": SCHEMA_VERSION,
        "timestamp": datetime.fromtimestamp(now_fn(), tz=timezone.utc).isoformat(),
        "os": os_info,
        "process_totals": process_totals,
        "system_memory": system_memory,
        "kernel_pool": kernel_pool,
        "pool_tag_attribution": pool_tags,
        "assessment": verdict["assessment"],
        "remediation": verdict["remediation"],
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for ``python -m meridian.host_memory_diagnostics``.
    Prints the diagnostic as JSON to stdout. Read-only -- takes no action
    beyond reporting; always exits 0, even if collection itself fails
    unexpectedly (the diagnostic degrades field-by-field internally, but this
    is one more outer safety net for a CLI meant to be safe to run anytime)."""
    parser = argparse.ArgumentParser(
        description=(
            "Read-only host-memory diagnostic: process totals (overall and "
            "Meridian-owned), system memory, Windows kernel paged/nonpaged "
            "pool totals, and best-effort pool-tag attribution. Never kills "
            "or modifies anything."
        )
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print the JSON output.")
    args = parser.parse_args(argv)

    try:
        diagnostic = collect_host_memory_diagnostic()
    except Exception:  # noqa: BLE001 -- CLI must never crash
        logger.warning("host_memory_diagnostics: collection failed", exc_info=True)
        diagnostic = {"schema_version": SCHEMA_VERSION, "error": "collection_failed"}

    print(json.dumps(diagnostic, indent=2 if args.pretty else None, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
