"""CPU, cores and memory per target (meridian_desktop/resources.py), and how they join the GPU read (gpu_jobs).

The parsers are pure. The Windows and POSIX readers are tested with their system reads replaced by fixed values, so no
test reads the real counters, starts a process or opens a socket. The remote part runs through a fake runner."""

from datetime import datetime, timezone

import pytest

from meridian_desktop import gpu_jobs, resources
from meridian_desktop.gpu_jobs import CommandResult
from meridian_desktop.resources import ResourceRow

# Captured at import, before the conftest stand-in replaces resources.local_row, so this test reads the real function.
REAL_LOCAL_ROW = resources.local_row

NOW = datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc)
MEMINFO = "MemTotal:       33554432 kB\nMemFree:          100 kB\nMemAvailable:   25165824 kB\n"
# Two aggregate samples: busy 150 of 1000 jiffies, then busy 300 of 1400. That is 150 of 400 busy: 37.5 percent.
FIRST_STAT = "cpu  100 0 50 800 50 0 0 0"
SECOND_STAT = "cpu  200 0 100 1000 100 0 0 0"
RES_TEXT = "8\n" + MEMINFO + FIRST_STAT + "\n" + SECOND_STAT + "\n"
PAYLOAD_POD = {"ok": True, "pods": [{"id": "pod-1", "name": "trainer", "status": "RUNNING", "gpu": {"id": "A100", "count": 1}}]}


class FakeRunner:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, argv, timeout):
        key = tuple(argv)
        self.calls.append(key)
        return self.answers.get(key, CommandResult(returncode=None, missing=True))


def _ssh(alias):
    return tuple(gpu_jobs.ssh_argv(alias))


def _remote(res_lines=None, res_rc=0, finished=True):
    """What the remote script prints for a host whose GPU and process queries are empty, with an optional RES section."""
    parts = [gpu_jobs.GPU_MARKER, "---GPU-RC=0---", gpu_jobs.APPS_MARKER, "---APPS-RC=0---"]
    if res_lines is not None:
        parts += [gpu_jobs.RES_MARKER, res_lines.rstrip("\n"), f"---RES-RC={res_rc}---"]
    if finished:
        parts.append(gpu_jobs.END_MARKER)
    return "\n".join(parts)


def _host_row(snapshot, host):
    matches = [row for row in snapshot.resources if row.host == host]
    assert len(matches) == 1, f"expected one resource row for {host}"
    return matches[0]


# ----------------------------------------------------------------- the pure parsers


def test_cores_is_the_first_positive_whole_number():
    assert resources.parse_cores("8\n") == 8
    assert resources.parse_cores("garbage\n16\n") == 16
    assert resources.parse_cores("") is None
    assert resources.parse_cores("0\n") is None


def test_memory_is_total_and_used_in_mib_from_kb():
    assert resources.parse_meminfo(MEMINFO) == (32768.0, 8192.0)


def test_memory_without_available_is_none():
    assert resources.parse_meminfo("MemTotal:       1024 kB\n") is None


def test_cpu_share_is_busy_over_total_between_two_samples():
    first, second = resources.parse_cpu_samples(FIRST_STAT), resources.parse_cpu_samples(SECOND_STAT)
    assert first == [(150.0, 1000.0)] and second == [(300.0, 1400.0)]
    assert resources.cpu_percent(first[0], second[0]) == pytest.approx(37.5)


def test_cpu_share_is_none_without_two_samples_or_elapsed_time_and_is_clamped():
    assert resources.cpu_percent(None, (1.0, 2.0)) is None
    assert resources.cpu_percent((5.0, 100.0), (5.0, 100.0)) is None
    assert resources.cpu_percent((0.0, 100.0), (150.0, 200.0)) == 100.0


def test_a_full_section_gives_every_number_and_no_note():
    row = resources.parse_section("gpu-box", RES_TEXT)
    assert row.state == resources.STATE_OK
    assert (row.cores, row.cpu_percent) == (8, pytest.approx(37.5))
    assert (row.memory_used_mib, row.memory_total_mib) == (8192.0, 32768.0)
    assert row.note == ""


def test_a_partial_section_names_what_could_not_be_read():
    row = resources.parse_section("gpu-box", "4\n")
    assert row.state == resources.STATE_OK and row.cores == 4
    assert row.cpu_percent is None and row.memory_total_mib is None
    assert row.note == "Could not read: CPU use, memory."


def test_an_empty_section_is_a_failed_row_with_one_fixed_note():
    row = resources.parse_section("gpu-box", "")
    assert row.state == resources.STATE_FAILED
    assert row.note == resources.SECTION_FAILED_NOTE


def test_the_table_shows_dashes_for_missing_numbers_and_rounds_the_share():
    rows = resources.rows_for_table(
        [
            ResourceRow("gpu-box", resources.STATE_OK, cores=8, cpu_percent=40.0, memory_used_mib=8192.0, memory_total_mib=32768.0),
            ResourceRow("RunPod: trainer", resources.STATE_ABSENT, note=resources.NOT_REPORTED_NOTE),
        ]
    )
    assert rows[0] == ("gpu-box", "8", "40%", "8.0 / 32.0 GB", "-")
    assert rows[1] == ("RunPod: trainer", "-", "-", "-", resources.NOT_REPORTED_NOTE)


# ----------------------------------------------------------------- this computer


def test_this_computers_windows_row_uses_both_samples_and_the_memory_read(monkeypatch):
    samples = iter([(100.0, 1000.0), (150.0, 1400.0)])
    monkeypatch.setattr(resources, "_windows_times", lambda: next(samples))
    # _windows_memory gives (total MiB, used MiB).
    monkeypatch.setattr(resources, "_windows_memory", lambda: (16384.0, 4096.0))
    monkeypatch.setattr(resources.time, "sleep", lambda seconds: None)

    row = resources._windows_row("This computer", 8)

    assert row.state == resources.STATE_OK and row.cores == 8
    assert row.cpu_percent == pytest.approx(12.5)
    assert (row.memory_used_mib, row.memory_total_mib) == (4096.0, 16384.0)


def test_a_failed_local_read_is_a_failed_row_and_never_raises(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("the counters went away")

    monkeypatch.setattr(resources, "_windows_row", boom)
    monkeypatch.setattr(resources, "_posix_row", boom)

    row = REAL_LOCAL_ROW("This computer")

    assert row.state == resources.STATE_FAILED
    assert row.note == resources.LOCAL_FAILED_NOTE
    assert "counters went away" not in row.note


# ----------------------------------------------------------------- joined into the GPU read


def test_a_saved_host_reports_its_cores_cpu_and_memory_from_one_section():
    run = FakeRunner({_ssh("gpu-box"): CommandResult(returncode=0, stdout=_remote(RES_TEXT))})
    snapshot = gpu_jobs.collect(run, ("gpu-box",), None, now=NOW)

    row = _host_row(snapshot, "gpu-box")
    assert (row.cores, row.cpu_percent) == (8, pytest.approx(37.5))
    assert (row.memory_used_mib, row.memory_total_mib) == (8192.0, 32768.0)


def test_a_host_whose_resource_section_failed_says_so():
    run = FakeRunner({_ssh("gpu-box"): CommandResult(returncode=0, stdout=_remote(RES_TEXT, res_rc=1))})
    row = _host_row(gpu_jobs.collect(run, ("gpu-box",), None, now=NOW), "gpu-box")
    assert row.state == resources.STATE_FAILED and row.note == resources.SECTION_FAILED_NOTE


def test_a_host_that_printed_no_resource_section_says_not_read():
    run = FakeRunner({_ssh("gpu-box"): CommandResult(returncode=0, stdout=_remote(None))})
    row = _host_row(gpu_jobs.collect(run, ("gpu-box",), None, now=NOW), "gpu-box")
    assert row.state == resources.STATE_ABSENT and row.note == resources.SECTION_ABSENT_NOTE


def test_an_unfinished_host_script_gives_a_failed_resource_row():
    run = FakeRunner({_ssh("gpu-box"): CommandResult(returncode=0, stdout=_remote(RES_TEXT, finished=False))})
    row = _host_row(gpu_jobs.collect(run, ("gpu-box",), None, now=NOW), "gpu-box")
    assert row.state == resources.STATE_FAILED and row.note == resources.SECTION_FAILED_NOTE


def test_this_computer_has_a_row_only_when_the_caller_reads_it():
    run = FakeRunner({})
    without = gpu_jobs.collect(run, (), None, now=NOW)
    assert not [row for row in without.resources if row.host == gpu_jobs.LOCAL_HOST]

    local = ResourceRow(gpu_jobs.LOCAL_HOST, resources.STATE_OK, cores=4, cpu_percent=12.0, memory_used_mib=2048.0, memory_total_mib=8192.0)
    with_local = gpu_jobs.collect(run, (), None, now=NOW, local_resources=lambda: local)
    assert _host_row(with_local, gpu_jobs.LOCAL_HOST) == local


def test_runpod_pods_get_a_row_that_says_the_pod_list_reports_no_cpu_or_memory():
    run = FakeRunner({})
    snapshot = gpu_jobs.collect(run, (), lambda: PAYLOAD_POD, now=NOW)

    row = _host_row(snapshot, "RunPod: trainer")
    assert row.state == resources.STATE_ABSENT and row.note == resources.NOT_REPORTED_NOTE
    assert row.cores is None and row.cpu_percent is None and row.memory_total_mib is None


def test_the_remote_script_only_reads_the_counters_it_names():
    script = gpu_jobs.REMOTE_SCRIPT
    assert gpu_jobs.RES_MARKER in script
    assert "/proc/meminfo" in script and "/proc/stat" in script and "sleep 1" in script
    for word in ("kill ", "rm -", "reboot", "shutdown", "systemctl", "> /"):
        assert word not in script


def test_the_panel_resources_table_shows_each_target_in_its_own_cells(qtbot):
    from PySide6.QtWidgets import QTableWidget

    from meridian_desktop.ui.gpu_jobs_panel import GpuJobsPanel

    panel = GpuJobsPanel(lambda: (), None, run=FakeRunner({}))
    qtbot.addWidget(panel)
    local = ResourceRow(gpu_jobs.LOCAL_HOST, resources.STATE_OK, cores=4, cpu_percent=12.0, memory_used_mib=2048.0, memory_total_mib=8192.0)
    panel._show(gpu_jobs.assemble(NOW, [gpu_jobs.Reading(resources=(local,))]))

    table = panel.findChild(QTableWidget, "gpu-resources")
    assert table.rowCount() == 1
    assert [table.item(0, column).text() for column in range(table.columnCount())] == [
        gpu_jobs.LOCAL_HOST, "4", "12%", "2.0 / 8.0 GB", "-",
    ]
