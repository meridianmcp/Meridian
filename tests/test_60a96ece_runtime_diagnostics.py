"""60a96ece -- read-only runtime-pressure diagnostics for Windows
kernel-pool-pressure / duplicate Serena-MCP-runtime investigation.

Covers the NEW, purely-additive diagnostic surface added to
``meridian/orphan_reaper.py`` (and its dashboard route in
``meridian/server.py``):

1. ``_runtime_candidate_kind`` / ``_extract_repo_fingerprint`` -- pure
   string matching, no OS calls.
2. ``list_live_runtime_processes`` / ``find_duplicate_runtime_fingerprints``
   -- mocked process enumeration only, never touches real OS processes,
   never kills/signals anything (no kill_fn parameter exists on either).
3. ``process_pool_evidence`` -- Windows psutil-preferred / ctypes
   psutil-free fallback / unavailable; POSIX process-group evidence
   explicitly marked NOT kernel-pool-attributable.
4. ``_win32_pool_usage`` / ``Win32MemoryInfoAPI`` / ``_load_win32_memory_api``
   -- fake kernel32 double (never touches real ``ctypes.WinDLL``, mirroring
   ``tests/test_process_lifecycle.py``'s ``_FakeKernel32`` pattern).
5. ``poolmon_available`` -- PATH lookup, mocked ``shutil.which``.
6. ``diagnose_runtime_pressure`` -- full orchestration with injected
   process_iter/pool_evidence_fn; never kills anything.
7. ``GET /projects/{id}/runtime_diagnostics`` -- the dashboard route.
"""
from __future__ import annotations

import sys
import types

import pytest

from meridian import orphan_reaper


# ---------------------------------------------------------------------------
# 1. _runtime_candidate_kind / _extract_repo_fingerprint -- pure matching
# ---------------------------------------------------------------------------


def test_runtime_candidate_kind_serena_cmdline_matches_regardless_of_name():
    kind = orphan_reaper._runtime_candidate_kind(
        "uvx.exe",
        "uvx --from serena-agent serena start-mcp-server --project C:\\repo",
    )
    assert kind == "serena"


def test_runtime_candidate_kind_target_name_with_hint_matches():
    kind = orphan_reaper._runtime_candidate_kind("python.exe", "python -m meridian --mcp")
    assert kind == "python"


def test_runtime_candidate_kind_target_name_without_hint_is_none():
    """A plain python.exe with no mcp/meridian/serena hint in its cmdline is
    NOT flagged -- avoids treating every unrelated dev-box python process as
    a runtime-diagnostic candidate."""
    assert orphan_reaper._runtime_candidate_kind("python.exe", "python some_unrelated_script.py") is None


def test_runtime_candidate_kind_non_target_name_is_none():
    assert orphan_reaper._runtime_candidate_kind("explorer.exe", "explorer.exe mcp") is None


def test_runtime_candidate_kind_empty_cmdline_is_none():
    assert orphan_reaper._runtime_candidate_kind("python.exe", "") is None


def test_extract_repo_fingerprint_project_flag_unquoted():
    fp = orphan_reaper._extract_repo_fingerprint(None, "serena start-mcp-server --project C:\\repo\\meridian")
    assert fp == "c:/repo/meridian"


def test_extract_repo_fingerprint_project_flag_quoted():
    fp = orphan_reaper._extract_repo_fingerprint(None, 'serena --project "C:\\repo\\my project"')
    assert fp == "c:/repo/my project"


def test_extract_repo_fingerprint_project_flag_equals_form():
    fp = orphan_reaper._extract_repo_fingerprint(None, "serena --project=C:\\repo\\meridian")
    assert fp == "c:/repo/meridian"


def test_extract_repo_fingerprint_falls_back_to_cwd():
    fp = orphan_reaper._extract_repo_fingerprint("C:\\repo\\meridian", "python -m meridian --mcp")
    assert fp == "c:/repo/meridian"


def test_extract_repo_fingerprint_none_when_neither_available():
    assert orphan_reaper._extract_repo_fingerprint(None, None) is None
    assert orphan_reaper._extract_repo_fingerprint("", "") is None


# ---------------------------------------------------------------------------
# 2. list_live_runtime_processes / find_duplicate_runtime_fingerprints
# ---------------------------------------------------------------------------


def _fake_live_processes():
    return [
        # Two Serena daemons pointed at the SAME repo -- the exact scenario
        # from the investigation's fresh evidence -- must be flagged.
        {"pid": 10, "name": "uvx.exe", "cwd": None,
         "cmdline": "uvx --from serena-agent serena start-mcp-server --project C:\\repo\\meridian --port 8700"},
        {"pid": 11, "name": "uvx.exe", "cwd": None,
         "cmdline": "uvx --from serena-agent serena start-mcp-server --project C:\\repo\\meridian --port 8701"},
        # A Serena daemon for a DIFFERENT repo (a legitimate distinct
        # worktree, or an unrelated thesis repo) -- must NOT be grouped with
        # the two above.
        {"pid": 12, "name": "uvx.exe", "cwd": None,
         "cmdline": "uvx --from serena-agent serena start-mcp-server --project C:\\repo\\thesis --port 8702"},
        # A single, non-duplicated meridian MCP process.
        {"pid": 13, "name": "python.exe", "cwd": "C:\\repo\\meridian", "cmdline": "python -m meridian --mcp"},
        # An unrelated python process sharing NO mcp/meridian/serena hint --
        # must be excluded entirely, not merely un-grouped.
        {"pid": 14, "name": "python.exe", "cwd": "C:\\repo\\meridian", "cmdline": "python unrelated_script.py"},
        # Wrong process family entirely -- must be excluded.
        {"pid": 15, "name": "explorer.exe", "cwd": "C:\\repo\\meridian", "cmdline": "explorer.exe"},
    ]


def test_list_live_runtime_processes_filters_and_fingerprints():
    procs = orphan_reaper.list_live_runtime_processes(process_iter=_fake_live_processes)
    pids = {p["pid"] for p in procs}
    assert pids == {10, 11, 12, 13}
    by_pid = {p["pid"]: p for p in procs}
    assert by_pid[10]["runtime_kind"] == "serena"
    assert by_pid[10]["repo_key"] == "c:/repo/meridian"
    assert by_pid[10]["fingerprint"] == by_pid[11]["fingerprint"]
    assert by_pid[10]["fingerprint"] != by_pid[12]["fingerprint"]
    assert by_pid[13]["runtime_kind"] == "python"


def test_list_live_runtime_processes_enumeration_failure_returns_empty():
    def _boom():
        raise RuntimeError("psutil exploded")

    assert orphan_reaper.list_live_runtime_processes(process_iter=_boom) == []


def test_list_live_runtime_processes_never_exposes_a_kill_capability():
    """This is a pure enumeration function -- it must not accept or expose
    any way to signal/kill a process (unlike reap_orphans, which does, via
    kill_fn)."""
    import inspect

    sig = inspect.signature(orphan_reaper.list_live_runtime_processes)
    assert "kill_fn" not in sig.parameters


def test_find_duplicate_runtime_fingerprints_groups_same_repo_and_kind():
    groups = orphan_reaper.find_duplicate_runtime_fingerprints(process_iter=_fake_live_processes)
    assert len(groups) == 1
    group = groups[0]
    assert group["runtime_kind"] == "serena"
    assert group["repo_key"] == "c:/repo/meridian"
    assert set(group["pids"]) == {10, 11}


def test_find_duplicate_runtime_fingerprints_empty_when_no_repeats():
    def _procs():
        return [
            {"pid": 1, "name": "python.exe", "cwd": "C:\\repo\\a", "cmdline": "python -m meridian --mcp"},
            {"pid": 2, "name": "python.exe", "cwd": "C:\\repo\\b", "cmdline": "python -m meridian --mcp"},
        ]

    assert orphan_reaper.find_duplicate_runtime_fingerprints(process_iter=_procs) == []


def test_find_duplicate_runtime_fingerprints_excludes_entries_without_repo_key():
    """Two Serena-shaped processes with NO --project flag and NO cwd have no
    repo_key at all -- ambiguous evidence must never be silently grouped as
    a duplicate."""
    def _procs():
        return [
            {"pid": 1, "name": "uvx.exe", "cwd": None, "cmdline": "uvx serena-agent serena start-mcp-server"},
            {"pid": 2, "name": "uvx.exe", "cwd": None, "cmdline": "uvx serena-agent serena start-mcp-server"},
        ]

    assert orphan_reaper.find_duplicate_runtime_fingerprints(process_iter=_procs) == []


# ---------------------------------------------------------------------------
# 3 & 4. process_pool_evidence / _win32_pool_usage / Win32MemoryInfoAPI
# ---------------------------------------------------------------------------


class _FakeMemKernel32:
    """Records every call; OpenProcess hands back an incrementing fake
    handle, K32GetProcessMemoryInfo fills the caller's struct -- mirrors
    tests/test_process_lifecycle.py's _FakeKernel32 pattern exactly."""

    def __init__(self, *, open_fails=False, get_info_fails=False, paged=176_128, nonpaged=14_000):
        self.calls = []
        self._next_handle = 5000
        self.open_fails = open_fails
        self.get_info_fails = get_info_fails
        self.paged = paged
        self.nonpaged = nonpaged

    def OpenProcess(self, access, inherit, pid):
        self.calls.append(("OpenProcess", access, pid))
        if self.open_fails:
            return 0
        self._next_handle += 1
        return self._next_handle

    def K32GetProcessMemoryInfo(self, handle, info_ptr, cb):
        self.calls.append(("K32GetProcessMemoryInfo", handle, cb))
        if self.get_info_fails:
            return 0
        counters = orphan_reaper.ctypes.cast(
            info_ptr, orphan_reaper.ctypes.POINTER(orphan_reaper._PROCESS_MEMORY_COUNTERS_EX)
        ).contents
        counters.QuotaPagedPoolUsage = self.paged
        counters.QuotaNonPagedPoolUsage = self.nonpaged
        return 1

    def CloseHandle(self, handle):
        self.calls.append(("CloseHandle", handle))
        return 1


def test_win32_memory_info_api_reads_pool_counters():
    kernel32 = _FakeMemKernel32(paged=123456, nonpaged=7890)
    api = orphan_reaper.Win32MemoryInfoAPI(kernel32)
    handle = api.open_process(4242)
    assert handle is not None
    counters = api.get_memory_info(handle)
    assert counters.QuotaPagedPoolUsage == 123456
    assert counters.QuotaNonPagedPoolUsage == 7890
    assert api.close_handle(handle) is True


def test_win32_pool_usage_success_via_injected_loader():
    kernel32 = _FakeMemKernel32(paged=176_128, nonpaged=14_000)
    result = orphan_reaper._win32_pool_usage(999, api_loader=lambda: orphan_reaper.Win32MemoryInfoAPI(kernel32))
    assert result == {
        "paged_pool_bytes": 176_128,
        "nonpaged_pool_bytes": 14_000,
        "source": "ctypes",
    }
    kinds = [c[0] for c in kernel32.calls]
    assert kinds == ["OpenProcess", "K32GetProcessMemoryInfo", "CloseHandle"]


def test_win32_pool_usage_none_when_open_process_fails():
    kernel32 = _FakeMemKernel32(open_fails=True)
    result = orphan_reaper._win32_pool_usage(999, api_loader=lambda: orphan_reaper.Win32MemoryInfoAPI(kernel32))
    assert result is None


def test_win32_pool_usage_none_when_get_info_fails():
    kernel32 = _FakeMemKernel32(get_info_fails=True)
    result = orphan_reaper._win32_pool_usage(999, api_loader=lambda: orphan_reaper.Win32MemoryInfoAPI(kernel32))
    assert result is None


def test_win32_pool_usage_none_when_loader_returns_none():
    assert orphan_reaper._win32_pool_usage(999, api_loader=lambda: None) is None


def test_win32_pool_usage_none_when_loader_raises():
    def _boom():
        raise RuntimeError("no ctypes on this box")

    assert orphan_reaper._win32_pool_usage(999, api_loader=_boom) is None


def test_load_win32_memory_api_returns_none_off_windows():
    if sys.platform != "win32":
        assert orphan_reaper._load_win32_memory_api() is None


def test_process_pool_evidence_windows_prefers_psutil(monkeypatch):
    monkeypatch.setattr(orphan_reaper.sys, "platform", "win32")

    fake_psutil = types.ModuleType("psutil")

    class _Mem:
        paged_pool = 200_000
        nonpaged_pool = 30_000

    class _P:
        def __init__(self, pid):
            self.pid = pid

        def memory_info(self):
            return _Mem()

    fake_psutil.Process = _P
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    evidence = orphan_reaper.process_pool_evidence(4242)
    assert evidence == {
        "paged_pool_bytes": 200_000,
        "nonpaged_pool_bytes": 30_000,
        "source": "psutil",
        "kernel_pool_attributable": True,
    }


def test_process_pool_evidence_windows_falls_back_to_ctypes_when_psutil_missing(monkeypatch):
    monkeypatch.setattr(orphan_reaper.sys, "platform", "win32")
    import builtins

    real_import = builtins.__import__

    def _fail_psutil_import(name, *a, **kw):
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fail_psutil_import)

    kernel32 = _FakeMemKernel32(paged=1000, nonpaged=2000)
    evidence = orphan_reaper.process_pool_evidence(
        4242, api_loader=lambda: orphan_reaper.Win32MemoryInfoAPI(kernel32)
    )
    assert evidence == {
        "paged_pool_bytes": 1000,
        "nonpaged_pool_bytes": 2000,
        "source": "ctypes",
        "kernel_pool_attributable": True,
    }


def test_process_pool_evidence_windows_unavailable_when_both_fail(monkeypatch):
    monkeypatch.setattr(orphan_reaper.sys, "platform", "win32")
    import builtins

    real_import = builtins.__import__

    def _fail_psutil_import(name, *a, **kw):
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fail_psutil_import)

    evidence = orphan_reaper.process_pool_evidence(4242, api_loader=lambda: None)
    assert evidence == {"source": "unavailable", "kernel_pool_attributable": False}


def test_process_pool_evidence_posix_reports_process_group_not_kernel_pool(monkeypatch):
    monkeypatch.setattr(orphan_reaper.sys, "platform", "linux")
    monkeypatch.setattr(orphan_reaper, "_getpgid", lambda pid: 777)

    evidence = orphan_reaper.process_pool_evidence(4242)
    assert evidence["process_group_id"] == 777
    assert evidence["source"] == "process_group"
    # Explicit contract from the sprint scope: never claimed as kernel-pool
    # evidence on a non-Windows platform.
    assert evidence["kernel_pool_attributable"] is False


def test_process_pool_evidence_posix_unavailable_when_getpgid_missing(monkeypatch):
    """Simulates running the POSIX branch on a real Windows box where
    os.getpgid does not exist at all -- _getpgid resolves to None at import
    time via getattr(os, "getpgid", None), and the function must degrade
    gracefully rather than raise AttributeError."""
    monkeypatch.setattr(orphan_reaper.sys, "platform", "linux")
    monkeypatch.setattr(orphan_reaper, "_getpgid", None)

    evidence = orphan_reaper.process_pool_evidence(4242)
    assert evidence == {"source": "unavailable", "kernel_pool_attributable": False}


def test_process_pool_evidence_posix_unavailable_when_getpgid_raises(monkeypatch):
    monkeypatch.setattr(orphan_reaper.sys, "platform", "linux")

    def _boom(pid):
        raise ProcessLookupError("gone")

    monkeypatch.setattr(orphan_reaper, "_getpgid", _boom)
    evidence = orphan_reaper.process_pool_evidence(4242)
    assert evidence == {"source": "unavailable", "kernel_pool_attributable": False}


# ---------------------------------------------------------------------------
# 5. poolmon_available -- PATH lookup, mocked shutil.which
# ---------------------------------------------------------------------------


def test_poolmon_available_true_when_on_path(monkeypatch):
    monkeypatch.setattr(orphan_reaper.shutil, "which", lambda name: r"C:\wdk\poolmon.exe" if name == "poolmon.exe" else None)
    assert orphan_reaper.poolmon_available() is True


def test_poolmon_available_false_when_not_on_path(monkeypatch):
    monkeypatch.setattr(orphan_reaper.shutil, "which", lambda name: None)
    assert orphan_reaper.poolmon_available() is False


def test_poolmon_available_false_on_exception(monkeypatch):
    def _boom(name):
        raise OSError("PATH lookup failed")

    monkeypatch.setattr(orphan_reaper.shutil, "which", _boom)
    assert orphan_reaper.poolmon_available() is False


# ---------------------------------------------------------------------------
# 6. diagnose_runtime_pressure -- full orchestration, never kills anything
# ---------------------------------------------------------------------------


def test_diagnose_runtime_pressure_attaches_pool_evidence_and_duplicate_groups(monkeypatch):
    monkeypatch.setattr(orphan_reaper, "poolmon_available", lambda: False)
    evidence_calls = []

    def _fake_evidence(pid):
        evidence_calls.append(pid)
        return {"source": "fake", "kernel_pool_attributable": True}

    result = orphan_reaper.diagnose_runtime_pressure(
        process_iter=_fake_live_processes, pool_evidence_fn=_fake_evidence
    )
    assert result["process_count"] == 4  # 10, 11, 12, 13 -- see _fake_live_processes
    assert set(evidence_calls) == {10, 11, 12, 13}
    assert all(p["pool_evidence"]["source"] == "fake" for p in result["processes"])
    assert result["duplicate_group_count"] == 1
    assert result["duplicate_groups"][0]["runtime_kind"] == "serena"
    assert set(result["duplicate_groups"][0]["pids"]) == {10, 11}
    assert result["poolmon_available"] is False
    assert "kernel_pool_attribution_note" in result
    assert "platform" in result


def test_diagnose_runtime_pressure_pool_evidence_failure_degrades_to_none():
    def _boom(pid):
        raise RuntimeError("evidence lookup exploded")

    result = orphan_reaper.diagnose_runtime_pressure(
        process_iter=_fake_live_processes, pool_evidence_fn=_boom
    )
    assert all(p["pool_evidence"] is None for p in result["processes"])


def test_diagnose_runtime_pressure_no_duplicates_when_none_present(monkeypatch):
    def _procs():
        return [
            {"pid": 1, "name": "python.exe", "cwd": "C:\\repo\\a", "cmdline": "python -m meridian --mcp"},
        ]

    result = orphan_reaper.diagnose_runtime_pressure(
        process_iter=_procs, pool_evidence_fn=lambda pid: {"source": "fake"}
    )
    assert result["duplicate_groups"] == []
    assert result["duplicate_group_count"] == 0


def test_diagnose_runtime_pressure_never_exposes_a_kill_capability():
    """This whole diagnostic surface is read-only by contract -- no kill_fn
    parameter anywhere in its signature."""
    import inspect

    sig = inspect.signature(orphan_reaper.diagnose_runtime_pressure)
    assert "kill_fn" not in sig.parameters
    assert "dry_run" not in sig.parameters  # there is nothing to dry-run -- it never acts


# ---------------------------------------------------------------------------
# 7. GET /projects/{id}/runtime_diagnostics -- dashboard route
# ---------------------------------------------------------------------------


def test_runtime_diagnostics_route_returns_diagnostic_snapshot(client, monkeypatch):
    fake_result = {
        "platform": "win32",
        "process_count": 2,
        "processes": [],
        "duplicate_groups": [{"fingerprint": "serena:c:/repo", "runtime_kind": "serena", "repo_key": "c:/repo", "pids": [10, 11]}],
        "duplicate_group_count": 1,
        "poolmon_available": False,
        "kernel_pool_attribution_note": "note",
    }
    monkeypatch.setattr(orphan_reaper, "diagnose_runtime_pressure", lambda: fake_result)

    project = client.post("/projects", json={"name": "runtime-diag-route"}).json()
    r = client.get(f"/projects/{project['id']}/runtime_diagnostics")
    assert r.status_code == 200
    assert r.json() == fake_result


def test_runtime_diagnostics_route_404_for_unknown_project(client):
    r = client.get("/projects/does-not-exist/runtime_diagnostics")
    assert r.status_code == 404
