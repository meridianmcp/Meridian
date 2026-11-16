"""Tests for the GPU jobs module (meridian_desktop/gpu_jobs.py). Pure Python, no Qt.

The parsers run on text recorded from this computer's own nvidia-smi (the RTX 3080 line and the process line that shows
"[Insufficient Permissions]"), and on squeue and bjobs text written in their documented formats. Every outside call is a
fake runner. Two guards turn any real process start or network connection into a test failure.
"""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
import threading
from datetime import datetime, timezone

import pytest

from meridian_desktop import gpu_jobs

# Taken at import, before the conftest guard replaces gpu_jobs.run_command, so the direct tests below run the real one.
REAL_RUN_COMMAND = gpu_jobs.run_command
from meridian_desktop.local_client import LocalClientError
from meridian_desktop.gpu_jobs import (
    BJOBS_ARGV,
    LOCAL_HOST,
    NVIDIA_APPS_ARGV,
    NVIDIA_GPU_ARGV,
    PART_GPUS,
    PART_LSF,
    PART_PROCESSES,
    PART_RUNPOD,
    PART_SLURM,
    SOURCE_GPU_PROCESS,
    SOURCE_LSF,
    SOURCE_RUNPOD,
    SOURCE_SLURM,
    STATE_ABSENT,
    STATE_EMPTY,
    STATE_FAILED,
    STATE_OK,
    SQUEUE_ARGV,
    CommandResult,
    GpuRow,
    collect,
)

NOW = datetime(2026, 11, 2, 16, 0, tzinfo=timezone.utc)
GPU_UUID = "GPU-85b6c4db-6eba-ae6e-4a6f-b1c6ebff448d"

# Recorded from this computer: nvidia-smi --query-gpu=... and --query-compute-apps=... with the csv, noheader and nounits format.
REAL_GPU_LINE = "0, NVIDIA GeForce RTX 3080, GPU-85b6c4db-6eba-ae6e-4a6f-b1c6ebff448d, 79, 1734, 20480, 59, 272.21, 320.00\n"
REAL_APPS_LINE = "4, [Insufficient Permissions], [N/A], GPU-85b6c4db-6eba-ae6e-4a6f-b1c6ebff448d\n"

TWO_GPU_TEXT = (
    "0, NVIDIA A100, GPU-aaaa, 0, 0, 40960, 31, 52.10, 400.00\n"
    "1, NVIDIA A100, GPU-bbbb, 98, 30000, 40960, 74, 380.55, 400.00\n"
)

SQUEUE_TEXT = (
    "123456|alice|train|RUNNING|01:02:03|1|gpu:2\n"
    "123457|bob|cpu-only|PENDING|0:00|1|N/A\n"
    "124000|carol|pipe|in|name|RUNNING|0:10|2|gpu:a100:4\n"
)

BJOBS_TEXT = (
    "4242 RUN carol 00:05 2 train_model\n"
    "4243 PEND dave 0 - cpu_job\n"
    "4244 RUN erin 01:00 1 llm finetune run\n"
)

RUNPOD_PAYLOAD = {
    "ok": True,
    "pods": [
        {"id": "pod-1", "name": "trainer", "status": "RUNNING", "gpu": {"id": "NVIDIA A100", "count": 2}},
        {"id": "pod-2", "status": "EXITED", "gpu": {"id": "RTX 4090"}},
        {"name": "no id"},
        "not a pod",
    ],
}


class FakeRunner:
    """Answers each argument list from a table. Records every call. Safe to call from several threads."""

    def __init__(self, answers: dict[tuple[str, ...], CommandResult | Exception]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, ...]] = []
        self._lock = threading.Lock()
        self.threads: set[int] = set()

    def __call__(self, argv, timeout):
        key = tuple(argv)
        with self._lock:
            self.calls.append(key)
            self.threads.add(threading.get_ident())
        answer = self.answers.get(key, CommandResult(None, missing=True))
        if isinstance(answer, Exception):
            raise answer
        return answer


def ok(stdout: str = "") -> CommandResult:
    return CommandResult(returncode=0, stdout=stdout)


def ssh_key(alias: str) -> tuple[str, ...]:
    return tuple(gpu_jobs.ssh_argv(alias))


def remote_stdout(
    gpu: str = REAL_GPU_LINE,
    apps: str = "",
    slurm: str | None = None,
    lsf: str | None = None,
    *,
    gpu_rc: int = 0,
    apps_rc: int = 0,
    slurm_rc: int = 0,
    lsf_rc: int = 0,
    finished: bool = True,
) -> str:
    """What the remote script prints: each section starts with its marker and ends with its own exit status. The end
    marker comes last, from the script's EXIT trap, so it appears only when the script finished."""
    parts = [gpu_jobs.GPU_MARKER, gpu, f"---GPU-RC={gpu_rc}---"]
    parts += [gpu_jobs.APPS_MARKER, apps, f"---APPS-RC={apps_rc}---"]
    if slurm is not None:
        parts += [gpu_jobs.SLURM_MARKER, slurm, f"---SLURM-RC={slurm_rc}---"]
    if lsf is not None:
        parts += [gpu_jobs.LSF_MARKER, lsf, f"---LSF-RC={lsf_rc}---"]
    if finished:
        parts.append(gpu_jobs.END_MARKER)
    return "\n".join(parts)


@pytest.fixture(autouse=True)
def no_process_and_no_socket(monkeypatch):
    """No test in this file may start a process or open a connection. A fake runner is the only way out."""

    def refuse(*args, **kwargs):
        raise AssertionError("this test must not start a process")

    def refuse_socket(*args, **kwargs):
        raise AssertionError("this test must not open a socket")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(socket, "socket", refuse_socket)
    monkeypatch.setattr(socket, "create_connection", refuse_socket)


# ----------------------------------------------------------------- GPU rows


def test_the_recorded_gpu_line_parses_to_every_field():
    rows, unmatched = gpu_jobs.parse_gpus(REAL_GPU_LINE, LOCAL_HOST)
    assert unmatched == 0
    assert rows == (
        GpuRow(
            host=LOCAL_HOST,
            index="0",
            name="NVIDIA GeForce RTX 3080",
            uuid=GPU_UUID,
            utilization=79,
            memory_used_mib=1734,
            memory_total_mib=20480,
            temperature_c=59,
            power_w=272.21,
            power_limit_w=320.0,
        ),
    )


def test_unreported_values_become_none_and_keep_the_row():
    text = "0, NVIDIA A100, GPU-x, [N/A], [N/A], 40960, [Not Supported], [N/A], 400.00\n"
    (row,), unmatched = gpu_jobs.parse_gpus(text, "box")
    assert unmatched == 0
    assert row.utilization is None
    assert row.memory_used_mib is None
    assert row.memory_total_mib == 40960
    assert row.temperature_c is None
    assert row.power_w is None
    assert row.power_limit_w == 400.0


def test_a_value_that_is_not_a_number_becomes_none_not_an_error():
    text = "0, A100, GPU-x, busy, 1, 2, 3, soon, 400\n"
    (row,), unmatched = gpu_jobs.parse_gpus(text, "box")
    assert unmatched == 0
    assert row.utilization is None
    assert row.power_w is None
    assert row.power_limit_w == 400.0


def test_process_output_this_desktop_does_not_read_is_a_failure_not_empty():
    run = FakeRunner({NVIDIA_GPU_ARGV: ok(REAL_GPU_LINE), NVIDIA_APPS_ARGV: ok("not a process line\n")})
    snapshot = collect(run, (), None, now=NOW)
    (status,) = [s for s in snapshot.statuses if s.part == PART_PROCESSES]
    assert status.state == STATE_FAILED
    assert status.text == "GPU processes printed output this desktop does not read."


def test_lines_with_the_wrong_field_count_are_counted_not_shown():
    text = "0, A100, GPU-x\n" + REAL_GPU_LINE + "\n\n"
    rows, unmatched = gpu_jobs.parse_gpus(text, "box")
    assert len(rows) == 1
    assert unmatched == 1


def test_two_gpus_parse_in_order_on_one_host():
    rows, unmatched = gpu_jobs.parse_gpus(TWO_GPU_TEXT, "box")
    assert unmatched == 0
    assert [row.index for row in rows] == ["0", "1"]
    assert rows[1].utilization == 98


# ----------------------------------------------------------------- GPU processes


def test_the_recorded_process_line_maps_its_gpu_and_flags_the_hidden_name():
    gpus, _ = gpu_jobs.parse_gpus(REAL_GPU_LINE, LOCAL_HOST)
    jobs, unmatched, hidden = gpu_jobs.parse_apps(REAL_APPS_LINE, LOCAL_HOST, gpus)
    assert unmatched == 0
    assert hidden is True
    (job,) = jobs
    assert job.source == SOURCE_GPU_PROCESS
    assert job.job_id == "4"
    assert job.name == "(not readable: Insufficient Permissions)"
    assert job.gpu == "0"
    assert job.gpu_memory_mib is None
    assert job.state == "running"


def test_a_process_name_is_shortened_to_its_program_file_name():
    apps = "10, C:\\Python312\\python.exe, 512, GPU-zzz\n11, /usr/bin/python3, 256, GPU-zzz\n"
    jobs, _, hidden = gpu_jobs.parse_apps(apps, "box", ())
    assert [job.name for job in jobs] == ["python.exe", "python3"]
    assert hidden is False
    # A GPU whose uuid is unknown shows the uuid itself, so nothing is dropped.
    assert jobs[0].gpu == "GPU-zzz"
    assert jobs[0].gpu_memory_mib == 512


def test_a_blank_process_name_says_no_name():
    jobs, _, _ = gpu_jobs.parse_apps("12, , 64, GPU-q\n", "box", ())
    assert jobs[0].name == "(no name)"


def test_a_process_line_with_the_wrong_field_count_is_counted():
    jobs, unmatched, hidden = gpu_jobs.parse_apps("oops\n", "box", ())
    assert jobs == ()
    assert unmatched == 1
    assert hidden is False


# ----------------------------------------------------------------- Slurm and LSF


def test_squeue_keeps_only_gpu_jobs_and_survives_a_pipe_in_the_name():
    jobs, unmatched = gpu_jobs.parse_squeue(SQUEUE_TEXT, "login")
    assert unmatched == 0
    assert [job.job_id for job in jobs] == ["123456", "124000"]
    assert jobs[0].user == "alice"
    assert jobs[0].name == "train"
    assert jobs[0].state == "RUNNING"
    assert jobs[0].gpu == "gpu:2"
    assert jobs[1].name == "pipe|in|name"
    assert jobs[1].gpu == "gpu:a100:4"
    assert all(job.source == SOURCE_SLURM for job in jobs)


def test_squeue_lines_that_are_too_short_are_counted():
    jobs, unmatched = gpu_jobs.parse_squeue("short|line\n", "login")
    assert jobs == ()
    assert unmatched == 1


def test_bjobs_keeps_only_jobs_with_a_gpu_count_and_keeps_the_name_whole():
    jobs, unmatched = gpu_jobs.parse_bjobs(BJOBS_TEXT, "lsf-host")
    assert unmatched == 0
    assert [job.job_id for job in jobs] == ["4242", "4244"]
    assert jobs[0].gpu == "2"
    assert jobs[0].user == "carol"
    assert jobs[1].name == "llm finetune run"
    assert all(job.source == SOURCE_LSF for job in jobs)


def test_bjobs_short_lines_are_counted():
    jobs, unmatched = gpu_jobs.parse_bjobs("4242 RUN carol\n", "lsf-host")
    assert jobs == ()
    assert unmatched == 1


# ----------------------------------------------------------------- RunPod


def test_runpod_pods_parse_and_bad_entries_are_skipped():
    jobs = gpu_jobs.parse_runpod_pods(RUNPOD_PAYLOAD)
    assert [job.job_id for job in jobs] == ["pod-1", "pod-2"]
    assert jobs[0].name == "trainer"
    assert jobs[0].gpu == "NVIDIA A100 x2"
    assert jobs[0].state == "RUNNING"
    assert jobs[1].name == "pod-2"
    assert jobs[1].gpu == "RTX 4090"
    assert all(job.source == SOURCE_RUNPOD for job in jobs)


def test_runpod_with_no_pod_list_has_no_jobs():
    assert gpu_jobs.parse_runpod_pods({"ok": True}) == ()
    assert gpu_jobs.parse_runpod_pods({"pods": "nope"}) == ()


# ----------------------------------------------------------------- host names


@pytest.mark.parametrize("name", ["gpu-box", "lab_a100", "a", "user@lab.example", "node.7"])
def test_safe_host_names_are_accepted(name):
    assert gpu_jobs.check_alias(name)


@pytest.mark.parametrize(
    "name",
    ["", "-oProxyCommand=x", "a b", "a;rm", "a$(x)", "x" * 64, "tab\there", None, "gpu-box\n"],
)
def test_unsafe_host_names_are_refused(name):
    assert not gpu_jobs.check_alias(name)


def test_the_host_list_splits_trims_and_drops_repeats():
    hosts, problems = gpu_jobs.parse_host_list(" gpu-box, lab_a100 ,gpu-box,, ")
    assert hosts == ("gpu-box", "lab_a100")
    assert problems == []


def test_an_empty_host_list_is_no_hosts_and_no_problem():
    assert gpu_jobs.parse_host_list("   ") == ((), [])


def test_a_bad_name_is_a_problem_and_the_good_ones_are_kept():
    hosts, problems = gpu_jobs.parse_host_list("gpu-box, -oX=y, lab")
    assert hosts == ("gpu-box", "lab")
    assert len(problems) == 1
    assert "-oX=y" in problems[0]


def test_more_than_the_limit_is_a_refusal_that_names_how_many_to_remove():
    names = ",".join(f"host{index}" for index in range(gpu_jobs.MAX_HOSTS + 2))
    hosts, problems = gpu_jobs.parse_host_list(names)
    # Nothing is cut silently: every valid name is returned, and the problem asks for the extra ones to go.
    assert len(hosts) == gpu_jobs.MAX_HOSTS + 2
    assert problems == [f"Save at most {gpu_jobs.MAX_HOSTS} SSH hosts. Remove 2 to save."]


def test_the_ssh_call_is_fixed_and_the_alias_comes_after_double_dash():
    argv = gpu_jobs.ssh_argv("gpu-box")
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert argv[argv.index("--") + 1] == "gpu-box"
    assert argv[-1] == gpu_jobs.REMOTE_COMMAND
    assert "-c" not in argv


def test_the_remote_command_asks_only_for_read_only_queries():
    command = gpu_jobs.REMOTE_SCRIPT
    for word in ("kill", "scancel", "bkill", "rm ", "sbatch", "bsub", "> ", "| sh"):
        assert word not in command
    assert "nvidia-smi --query-gpu=" in command
    assert "command -v squeue" in command
    assert "command -v bjobs" in command


# ----------------------------------------------------------------- collect: this computer


def test_a_clean_local_read_reports_each_part_and_no_remote_work():
    run = FakeRunner(
        {
            NVIDIA_GPU_ARGV: ok(REAL_GPU_LINE),
            NVIDIA_APPS_ARGV: ok(REAL_APPS_LINE),
            SQUEUE_ARGV: ok(SQUEUE_TEXT),
            BJOBS_ARGV: ok(BJOBS_TEXT),
        }
    )
    snapshot = collect(run, (), None, now=NOW)
    assert snapshot.checked_at == NOW
    assert len(snapshot.gpus) == 1
    sources = [job.source for job in snapshot.jobs]
    assert sources.count(SOURCE_GPU_PROCESS) == 1
    assert sources.count(SOURCE_SLURM) == 2
    assert sources.count(SOURCE_LSF) == 2
    states = {(status.part, status.state) for status in snapshot.statuses}
    assert (PART_GPUS, STATE_OK) in states
    assert (PART_PROCESSES, STATE_OK) in states
    assert (PART_SLURM, STATE_OK) in states
    assert (PART_LSF, STATE_OK) in states
    assert (PART_RUNPOD, STATE_ABSENT) in states
    assert not any(call[0] == "ssh" for call in run.calls)


def test_missing_nvidia_smi_says_so_plainly_and_the_other_parts_still_read():
    run = FakeRunner({SQUEUE_ARGV: ok(""), BJOBS_ARGV: ok("")})
    snapshot = collect(run, (), None, now=NOW)
    assert snapshot.gpus == ()
    (gpu_status,) = [s for s in snapshot.statuses if s.part == PART_GPUS]
    assert gpu_status.state == STATE_ABSENT
    assert gpu_status.text == "nvidia-smi is not on this computer, so its GPUs cannot be read."
    assert gpu_jobs.summary_text(snapshot) == "No GPUs were read."


def test_slurm_and_lsf_missing_here_say_not_on_this_machine():
    run = FakeRunner({NVIDIA_GPU_ARGV: ok(REAL_GPU_LINE)})
    snapshot = collect(run, (), None, now=NOW)
    texts = {s.part: s.text for s in snapshot.statuses if s.part in (PART_SLURM, PART_LSF)}
    assert texts == {PART_SLURM: "Slurm: not on this machine.", PART_LSF: "LSF: not on this machine."}


def test_an_nvidia_smi_failure_names_its_exit_code():
    run = FakeRunner({NVIDIA_GPU_ARGV: CommandResult(returncode=9, stderr="driver not loaded")})
    snapshot = collect(run, (), None, now=NOW)
    (status,) = [s for s in snapshot.statuses if s.part == PART_GPUS]
    assert status.state == STATE_FAILED
    assert status.text == "nvidia-smi could not read the GPUs (exit code 9)."
    assert "driver" not in status.text


def test_a_timed_out_nvidia_smi_says_so():
    run = FakeRunner({NVIDIA_GPU_ARGV: CommandResult(returncode=None, timed_out=True)})
    snapshot = collect(run, (), None, now=NOW)
    (status,) = [s for s in snapshot.statuses if s.part == PART_GPUS]
    assert status.text == "nvidia-smi timed out."


def test_gpu_output_this_desktop_does_not_read_is_a_failure_not_empty():
    run = FakeRunner({NVIDIA_GPU_ARGV: ok("this is not csv at all\n")})
    snapshot = collect(run, (), None, now=NOW)
    (status,) = [s for s in snapshot.statuses if s.part == PART_GPUS]
    assert status.state == STATE_FAILED
    assert "does not read" in status.text


def test_an_empty_gpu_answer_is_reported_as_no_gpus():
    run = FakeRunner({NVIDIA_GPU_ARGV: ok("")})
    snapshot = collect(run, (), None, now=NOW)
    (status,) = [s for s in snapshot.statuses if s.part == PART_GPUS]
    assert status.state == STATE_EMPTY
    assert status.text == "No GPUs reported."


def test_a_process_query_failure_does_not_hide_the_gpus():
    run = FakeRunner({NVIDIA_GPU_ARGV: ok(REAL_GPU_LINE), NVIDIA_APPS_ARGV: CommandResult(returncode=3)})
    snapshot = collect(run, (), None, now=NOW)
    assert len(snapshot.gpus) == 1
    (status,) = [s for s in snapshot.statuses if s.part == PART_PROCESSES]
    assert status.text == "GPU processes could not be read."


def test_a_timed_out_process_query_says_timed_out():
    run = FakeRunner({NVIDIA_GPU_ARGV: ok(REAL_GPU_LINE), NVIDIA_APPS_ARGV: CommandResult(None, timed_out=True)})
    snapshot = collect(run, (), None, now=NOW)
    (status,) = [s for s in snapshot.statuses if s.part == PART_PROCESSES]
    assert status.text == "GPU processes timed out."


def test_no_processes_is_an_empty_state_and_hidden_names_are_explained():
    run = FakeRunner({NVIDIA_GPU_ARGV: ok(REAL_GPU_LINE), NVIDIA_APPS_ARGV: ok("")})
    snapshot = collect(run, (), None, now=NOW)
    (status,) = [s for s in snapshot.statuses if s.part == PART_PROCESSES]
    assert status.state == STATE_EMPTY
    assert status.text == "No GPU processes are running."

    run = FakeRunner({NVIDIA_GPU_ARGV: ok(REAL_GPU_LINE), NVIDIA_APPS_ARGV: ok(REAL_APPS_LINE)})
    snapshot = collect(run, (), None, now=NOW)
    (status,) = [s for s in snapshot.statuses if s.part == PART_PROCESSES]
    assert status.text == "1 GPU process listed. Some names need admin rights to read."


def test_a_scheduler_that_fails_says_it_could_not_be_read():
    run = FakeRunner({NVIDIA_GPU_ARGV: ok(REAL_GPU_LINE), SQUEUE_ARGV: CommandResult(returncode=1)})
    snapshot = collect(run, (), None, now=NOW)
    (status,) = [s for s in snapshot.statuses if s.part == PART_SLURM]
    assert status.state == STATE_FAILED
    assert status.text == "Slurm could not be read."


def test_scheduler_output_this_desktop_does_not_read_is_a_failure():
    run = FakeRunner({NVIDIA_GPU_ARGV: ok(REAL_GPU_LINE), SQUEUE_ARGV: ok("bad\n")})
    snapshot = collect(run, (), None, now=NOW)
    (status,) = [s for s in snapshot.statuses if s.part == PART_SLURM]
    assert status.state == STATE_FAILED
    assert status.text == "Slurm printed output this desktop does not read."


def test_a_scheduler_with_no_gpu_jobs_is_an_empty_state():
    run = FakeRunner({NVIDIA_GPU_ARGV: ok(REAL_GPU_LINE), SQUEUE_ARGV: ok("123|a|b|RUNNING|0:01|1|N/A\n")})
    snapshot = collect(run, (), None, now=NOW)
    (status,) = [s for s in snapshot.statuses if s.part == PART_SLURM]
    assert status.state == STATE_EMPTY
    assert status.text == "No Slurm GPU jobs."


# ----------------------------------------------------------------- collect: saved SSH hosts


def test_a_saved_host_reports_its_gpus_processes_and_schedulers():
    answer = ok(remote_stdout(gpu=TWO_GPU_TEXT, apps=REAL_APPS_LINE, slurm=SQUEUE_TEXT, lsf=BJOBS_TEXT))
    run = FakeRunner({ssh_key("gpu-box"): answer, NVIDIA_GPU_ARGV: ok(""), NVIDIA_APPS_ARGV: ok("")})
    snapshot = collect(run, ("gpu-box",), None, now=NOW)
    remote_gpus = [row for row in snapshot.gpus if row.host == "gpu-box"]
    assert [row.index for row in remote_gpus] == ["0", "1"]
    remote_jobs = [job for job in snapshot.jobs if job.host == "gpu-box"]
    assert {job.source for job in remote_jobs} == {SOURCE_GPU_PROCESS, SOURCE_SLURM, SOURCE_LSF}
    gpu_status = [s for s in snapshot.statuses if s.where == "gpu-box" and s.part == PART_GPUS]
    assert gpu_status[0].text == "2 GPUs read."


def test_a_host_without_squeue_or_bjobs_says_not_on_this_host():
    run = FakeRunner({ssh_key("plain"): ok(remote_stdout())})
    snapshot = collect(run, ("plain",), None, now=NOW)
    texts = {s.part: s.text for s in snapshot.statuses if s.where == "plain" and s.part in (PART_SLURM, PART_LSF)}
    assert texts == {PART_SLURM: "Slurm: not on this host.", PART_LSF: "LSF: not on this host."}


def test_a_started_gpu_section_with_status_127_is_failed_not_absent():
    # The script started nvidia-smi and it exited 127. That is a failure, not a host that has no nvidia-smi.
    answer = CommandResult(returncode=0, stdout=remote_stdout(gpu="", gpu_rc=127))
    run = FakeRunner({ssh_key("cpu-only"): answer})
    snapshot = collect(run, ("cpu-only",), None, now=NOW)
    (status,) = [s for s in snapshot.statuses if s.where == "cpu-only" and s.part == PART_GPUS]
    assert status.state == STATE_FAILED
    assert status.text == "nvidia-smi could not read the GPUs (exit code 127)."


def test_a_squeue_that_exits_127_after_it_started_is_failed_not_absent():
    answer = CommandResult(returncode=0, stdout=remote_stdout(slurm="", slurm_rc=127))
    run = FakeRunner({ssh_key("login"): answer})
    snapshot = collect(run, ("login",), None, now=NOW)
    (status,) = [s for s in snapshot.statuses if s.where == "login" and s.part == PART_SLURM]
    assert status.state == STATE_FAILED
    assert status.text == "Slurm could not be read."


@pytest.mark.parametrize("status", [127, 1])
def test_a_shell_that_cannot_run_the_script_fails_every_section(status):
    # No end marker and no output: the script never ran. Every part is failed, and none is called absent.
    answer = CommandResult(returncode=status, stdout="", stderr="sh: not found")
    run = FakeRunner({ssh_key("no-sh"): answer})
    snapshot = collect(run, ("no-sh",), None, now=NOW)
    host = {s.part: s for s in snapshot.statuses if s.where == "no-sh"}
    assert {part: host[part].state for part in host} == {
        PART_GPUS: STATE_FAILED,
        PART_PROCESSES: STATE_FAILED,
        PART_SLURM: STATE_FAILED,
        PART_LSF: STATE_FAILED,
    }
    assert host[PART_GPUS].text == "nvidia-smi could not be read on this host."
    assert host[PART_SLURM].text == "Slurm could not be read."
    assert host[PART_LSF].text == "LSF could not be read."
    assert [row for row in snapshot.gpus if row.host == "no-sh"] == []


def test_a_host_with_the_end_marker_reads_normally():
    answer = CommandResult(returncode=0, stdout=remote_stdout(gpu=REAL_GPU_LINE, finished=True))
    run = FakeRunner({ssh_key("done"): answer})
    snapshot = collect(run, ("done",), None, now=NOW)
    assert [row.host for row in snapshot.gpus] == ["done"]
    assert gpu_jobs.END_MARKER in answer.stdout


def test_the_remote_script_prints_the_end_marker_from_an_exit_trap_before_its_final_exit():
    assert gpu_jobs.REMOTE_SCRIPT.startswith("trap 'echo " + gpu_jobs.END_MARKER + "' EXIT")
    assert gpu_jobs.REMOTE_SCRIPT.endswith("exit 0")



def test_an_nvml_error_without_the_word_nvidia_smi_is_a_failure_with_its_status():
    # The driver can fail with a message that never names nvidia-smi. The exit status still decides.
    answer = CommandResult(
        returncode=0,
        stdout=remote_stdout(gpu="", gpu_rc=9),
        stderr="Failed to initialize NVML: Driver/library version mismatch",
    )
    run = FakeRunner({ssh_key("nvml"): answer})
    snapshot = collect(run, ("nvml",), None, now=NOW)
    (status,) = [s for s in snapshot.statuses if s.where == "nvml" and s.part == PART_GPUS]
    assert status.state == STATE_FAILED
    assert status.text == "nvidia-smi could not read the GPUs (exit code 9)."


def test_a_failed_remote_gpu_status_shows_no_rows_even_if_some_were_printed():
    answer = ok(remote_stdout(gpu=REAL_GPU_LINE, gpu_rc=9))
    run = FakeRunner({ssh_key("partial"): answer})
    snapshot = collect(run, ("partial",), None, now=NOW)
    assert [row for row in snapshot.gpus if row.host == "partial"] == []
    (status,) = [s for s in snapshot.statuses if s.where == "partial" and s.part == PART_GPUS]
    assert status.text == "nvidia-smi could not read the GPUs (exit code 9)."


def test_a_controller_that_is_down_is_reported_as_slurm_could_not_be_read():
    answer = ok(remote_stdout(slurm="", slurm_rc=1))
    run = FakeRunner({ssh_key("login"): answer})
    snapshot = collect(run, ("login",), None, now=NOW)
    (status,) = [s for s in snapshot.statuses if s.where == "login" and s.part == PART_SLURM]
    assert status.state == STATE_FAILED
    assert status.text == "Slurm could not be read."


def test_a_failed_lsf_and_a_failed_process_query_say_could_not_be_read_on_the_host():
    answer = ok(remote_stdout(apps="", apps_rc=3, lsf="", lsf_rc=2))
    run = FakeRunner({ssh_key("hpc"): answer})
    snapshot = collect(run, ("hpc",), None, now=NOW)
    texts = {s.part: s.text for s in snapshot.statuses if s.where == "hpc"}
    assert texts[PART_PROCESSES] == "GPU processes could not be read."
    assert texts[PART_LSF] == "LSF could not be read."


def test_a_remote_section_that_never_reports_its_status_is_a_failure():
    # The script was cut off after the GPU section started, before its status line was printed.
    cut_off = f"{gpu_jobs.GPU_MARKER}\n{REAL_GPU_LINE}"
    run = FakeRunner({ssh_key("cut"): ok(cut_off)})
    snapshot = collect(run, ("cut",), None, now=NOW)
    (status,) = [s for s in snapshot.statuses if s.where == "cut" and s.part == PART_GPUS]
    assert status.state == STATE_FAILED
    assert status.text == "nvidia-smi could not be read on this host."
    assert [row for row in snapshot.gpus if row.host == "cut"] == []


def test_the_remote_command_starts_with_sh_c_and_the_script_is_one_quoted_argument():
    # The remote login shell is not assumed to be POSIX, so the script is handed to sh -c as one quoted argument.
    words = shlex.split(gpu_jobs.REMOTE_COMMAND)
    assert gpu_jobs.REMOTE_COMMAND.startswith("sh -c ")
    assert words[:2] == ["sh", "-c"]
    assert len(words) == 3
    assert words[2].startswith(f"trap 'echo {gpu_jobs.END_MARKER}' EXIT; echo {gpu_jobs.GPU_MARKER}")
    assert words[2].endswith("exit 0")


def test_the_remote_command_ends_with_an_explicit_exit_zero():
    # ssh's own failures are 255. The remote status comes from each section's marker line, so the script itself must
    # end with exit 0, and no later command's status can stand in for an earlier failure.
    assert gpu_jobs.REMOTE_SCRIPT.endswith("exit 0")


def test_a_host_that_prints_nothing_for_gpus_is_an_empty_state():
    run = FakeRunner({ssh_key("quiet"): ok(remote_stdout(gpu=""))})
    snapshot = collect(run, ("quiet",), None, now=NOW)
    (status,) = [s for s in snapshot.statuses if s.where == "quiet" and s.part == PART_GPUS]
    assert status.state == STATE_EMPTY
    assert status.text == "No GPUs reported."


@pytest.mark.parametrize(
    ("answer", "state", "expected"),
    [
        (
            CommandResult(
                returncode=255,
                stderr="No ED25519 host key is known for gpu-box and you have requested strict checking.\r\n"
                "Host key verification failed.",
            ),
            STATE_FAILED,
            "This host has no key of this type in known_hosts. If you have connected to it before, its key may have changed. "
            "Check its fingerprint with the host's admin before you add any key. Do not add a fingerprint you have not verified.",
        ),
        (
            CommandResult(
                returncode=255,
                stderr="@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\r\n"
                "@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\r\n"
                "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\r\n"
                "Host key verification failed.",
            ),
            STATE_FAILED,
            "Host key changed. This may be a reinstall or an attack. "
            "Do not accept the new key until the host's admin confirms it.",
        ),
        (
            CommandResult(returncode=255, stderr="Host key verification failed."),
            STATE_FAILED,
            "Host key not accepted. Check the host's key in a terminal before trying again.",
        ),
        (
            CommandResult(returncode=255, stderr="user@x: Permission denied (publickey)."),
            STATE_FAILED,
            "Auth failed. The key for this host is not accepted without a password.",
        ),
        (
            CommandResult(returncode=255, stderr="ssh: connect to host x port 22: Connection timed out"),
            STATE_FAILED,
            "Timed out. The host did not answer in time.",
        ),
        (
            CommandResult(returncode=255, stderr="ssh: Could not resolve hostname x"),
            STATE_FAILED,
            "Unreachable. The host name or the network did not answer.",
        ),
        (CommandResult(returncode=None, timed_out=True), STATE_FAILED, "Timed out. The host did not answer in time."),
        (
            CommandResult(returncode=None, missing=True),
            STATE_ABSENT,
            "ssh is not on this computer, so this host cannot be read.",
        ),
        (CommandResult(returncode=None, failed_to_start=True), STATE_FAILED, "ssh could not be started."),
    ],
)
def test_each_host_failure_names_its_reason(answer, state, expected):
    run = FakeRunner({ssh_key("bad"): answer})
    snapshot = collect(run, ("bad",), None, now=NOW)
    (status,) = [s for s in snapshot.statuses if s.where == "bad"]
    assert status.state == state
    assert status.text == expected
    assert snapshot.gpus == ()


def test_one_failing_host_never_blocks_the_others():
    good = ok(remote_stdout(gpu=REAL_GPU_LINE))
    run = FakeRunner({ssh_key("good"): good, ssh_key("boom"): RuntimeError("secret detail")})
    snapshot = collect(run, ("boom", "good"), None, now=NOW)
    assert [row.host for row in snapshot.gpus] == ["good"]
    (boom,) = [s for s in snapshot.statuses if s.where == "boom"]
    assert boom.state == STATE_FAILED
    assert boom.text == "This could not be read."
    assert "secret" not in boom.text


def test_hosts_are_read_in_parallel_and_keep_their_order_in_the_snapshot():
    answers = {ssh_key(f"h{index}"): ok(remote_stdout(gpu=REAL_GPU_LINE)) for index in range(3)}
    run = FakeRunner(answers)
    snapshot = collect(run, ("h0", "h1", "h2"), None, now=NOW)
    assert [row.host for row in snapshot.gpus] == ["h0", "h1", "h2"]
    assert sum(1 for call in run.calls if call[0] == "ssh") == 3


def test_three_hosts_are_in_flight_at_the_same_time():
    """Covers collect(): the three hosts are read in parallel, not one after another."""
    # The barrier releases only when all three ssh calls are waiting together. Read one after another, the first call
    # would wait until the barrier times out, and that host would then be missing from the snapshot.
    barrier = threading.Barrier(3, timeout=5)

    class BarrierRunner(FakeRunner):
        def __call__(self, argv, timeout):
            if argv[0] == "ssh":
                barrier.wait()
            return super().__call__(argv, timeout)

    answers = {ssh_key(f"h{index}"): ok(remote_stdout(gpu=REAL_GPU_LINE)) for index in range(3)}
    snapshot = collect(BarrierRunner(answers), ("h0", "h1", "h2"), None, now=NOW)
    assert sorted(row.host for row in snapshot.gpus) == ["h0", "h1", "h2"]


def test_a_bad_saved_name_is_skipped_and_never_run():
    run = FakeRunner({ssh_key("good"): ok(remote_stdout(gpu=REAL_GPU_LINE))})
    snapshot = collect(run, ("-oProxyCommand=x", "good"), None, now=NOW)
    assert [row.host for row in snapshot.gpus] == ["good"]
    assert not any("-oProxyCommand=x" in call for call in run.calls)


class FakeClock:
    """A clock the test moves by hand. The backoff reads it, so no test waits for real time."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


DENIED = CommandResult(returncode=255, stderr="user@gpu-box: Permission denied (publickey).")


def _ssh_calls(run, alias):
    return sum(1 for call in run.calls if call == ssh_key(alias))


# The backoff tests below call read_host, the per-host read the panel makes. They do not go through collect().


def test_a_failed_login_skips_the_host_for_one_minute_then_tries_again():
    clock = FakeClock()
    backoff = gpu_jobs.HostBackoff(clock=clock)
    run = FakeRunner({ssh_key("gpu-box"): DENIED})
    gpu_jobs.read_host(run, "gpu-box", backoff)
    assert _ssh_calls(run, "gpu-box") == 1

    clock.advance(30)
    reading = gpu_jobs.read_host(run, "gpu-box", backoff)
    assert _ssh_calls(run, "gpu-box") == 1  # skipped: no second login attempt
    assert reading.statuses[0].text == "Skipped for 1 min after a failed login."

    clock.advance(31)  # now 61 seconds after the failure
    gpu_jobs.read_host(run, "gpu-box", backoff)
    assert _ssh_calls(run, "gpu-box") == 2


def test_the_skip_grows_one_two_four_minutes_and_is_capped_at_ten():
    clock = FakeClock()
    backoff = gpu_jobs.HostBackoff(clock=clock)
    run = FakeRunner({ssh_key("gpu-box"): DENIED})
    expected_minutes = [1, 2, 4, 8, 10, 10]
    for minutes in expected_minutes:
        gpu_jobs.read_host(run, "gpu-box", backoff)  # a login that fails
        clock.advance(1)
        skipped = gpu_jobs.read_host(run, "gpu-box", backoff)
        assert skipped.statuses[0].text == f"Skipped for {minutes} min after a failed login."
        clock.advance(minutes * 60 - 1)  # now exactly at the retry time
    assert _ssh_calls(run, "gpu-box") == len(expected_minutes)


def test_a_successful_login_clears_the_skip():
    clock = FakeClock()
    backoff = gpu_jobs.HostBackoff(clock=clock)
    run = FakeRunner({ssh_key("gpu-box"): DENIED})
    gpu_jobs.read_host(run, "gpu-box", backoff)
    clock.advance(61)
    run.answers = {ssh_key("gpu-box"): ok(remote_stdout())}
    gpu_jobs.read_host(run, "gpu-box", backoff)
    assert backoff.remaining("gpu-box") == 0
    # The next failure starts again at one minute, not where the last skip left off.
    run.answers = {ssh_key("gpu-box"): DENIED}
    gpu_jobs.read_host(run, "gpu-box", backoff)
    clock.advance(59)
    reading = gpu_jobs.read_host(run, "gpu-box", backoff)
    assert reading.statuses[0].text == "Skipped for 1 min after a failed login."


def test_a_host_key_failure_starts_the_same_skip():
    clock = FakeClock()
    backoff = gpu_jobs.HostBackoff(clock=clock)
    changed = CommandResult(
        returncode=255,
        stderr="WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!\r\nHost key verification failed.",
    )
    run = FakeRunner({ssh_key("gpu-box"): changed})
    gpu_jobs.read_host(run, "gpu-box", backoff)
    assert backoff.remaining("gpu-box") == 60


def test_a_timeout_or_unreachable_host_is_not_skipped():
    clock = FakeClock()
    backoff = gpu_jobs.HostBackoff(clock=clock)
    run = FakeRunner({ssh_key("gpu-box"): CommandResult(returncode=None, timed_out=True)})
    gpu_jobs.read_host(run, "gpu-box", backoff)
    assert backoff.remaining("gpu-box") == 0


def test_no_saved_hosts_means_no_ssh_call():
    run = FakeRunner({NVIDIA_GPU_ARGV: ok(REAL_GPU_LINE)})
    collect(run, (), None, now=NOW)
    assert not any(call[0] == "ssh" for call in run.calls)


# ----------------------------------------------------------------- collect: RunPod


def test_runpod_pods_appear_when_the_local_server_lists_them():
    run = FakeRunner({NVIDIA_GPU_ARGV: ok("")})
    snapshot = collect(run, (), lambda: RUNPOD_PAYLOAD, now=NOW)
    runpod_jobs = [job for job in snapshot.jobs if job.source == SOURCE_RUNPOD]
    assert len(runpod_jobs) == 2
    (status,) = [s for s in snapshot.statuses if s.part == PART_RUNPOD]
    assert status.state == STATE_OK
    assert status.text == "2 RunPod pods."


def test_runpod_with_no_pods_is_an_empty_state():
    run = FakeRunner({NVIDIA_GPU_ARGV: ok("")})
    snapshot = collect(run, (), lambda: {"ok": True, "pods": []}, now=NOW)
    (status,) = [s for s in snapshot.statuses if s.part == PART_RUNPOD]
    assert status.state == STATE_EMPTY
    assert status.text == "No RunPod pods."


def test_runpod_not_connected_when_the_server_is_not_answering():
    def unreachable():
        raise OSError("connection refused at 127.0.0.1")

    run = FakeRunner({NVIDIA_GPU_ARGV: ok("")})
    snapshot = collect(run, (), unreachable, now=NOW)
    (status,) = [s for s in snapshot.statuses if s.part == PART_RUNPOD]
    assert status.text == gpu_jobs.RUNPOD_NOT_CONNECTED_TEXT
    assert "refused" not in status.text


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("http_404", gpu_jobs.RUNPOD_NO_ROUTE_TEXT),
        ("http_403", gpu_jobs.RUNPOD_REFUSED_TEXT),
        ("http_500", gpu_jobs.RUNPOD_SERVER_ERROR_TEXT),
        ("not_running", gpu_jobs.RUNPOD_NOT_CONNECTED_TEXT),
        ("timeout", gpu_jobs.RUNPOD_NOT_LISTED_TEXT),
    ],
)
def test_each_runpod_failure_code_has_its_own_text(code, expected):
    def failing():
        raise LocalClientError(code, "the local message is not shown")

    run = FakeRunner({NVIDIA_GPU_ARGV: ok("")})
    snapshot = collect(run, (), failing, now=NOW)
    (status,) = [s for s in snapshot.statuses if s.part == PART_RUNPOD]
    assert status.text == expected
    assert "local message" not in status.text


def test_a_404_says_to_update_meridian_and_is_not_called_not_answering():
    def no_route():
        raise LocalClientError("http_404", "not found")

    run = FakeRunner({NVIDIA_GPU_ARGV: ok("")})
    snapshot = collect(run, (), no_route, now=NOW)
    (status,) = [s for s in snapshot.statuses if s.part == PART_RUNPOD]
    assert "Update Meridian" in status.text
    assert "not answering" not in status.text


def test_runpod_not_listed_when_the_server_says_not_ok():
    run = FakeRunner({NVIDIA_GPU_ARGV: ok("")})
    snapshot = collect(run, (), lambda: {"ok": False, "error": "x"}, now=NOW)
    (status,) = [s for s in snapshot.statuses if s.part == PART_RUNPOD]
    assert status.state == STATE_FAILED
    assert status.text == gpu_jobs.RUNPOD_NOT_LISTED_TEXT


def test_runpod_not_connected_when_there_is_no_provider_at_all():
    run = FakeRunner({NVIDIA_GPU_ARGV: ok("")})
    snapshot = collect(run, (), None, now=NOW)
    (status,) = [s for s in snapshot.statuses if s.part == PART_RUNPOD]
    assert status.state == STATE_ABSENT
    assert status.text == gpu_jobs.RUNPOD_NOT_CONNECTED_TEXT


# ----------------------------------------------------------------- the default runner


# An absolute path that need not exist. The default-runner tests patch resolve_program to return it, so they run the
# same on any machine, with or without nvidia-smi installed.
FAKE_PROGRAM = os.path.join(os.sep, "fake", "bin", "nvidia-smi.exe")


def test_a_missing_program_reaches_the_file_not_found_branch_and_is_reported_missing(monkeypatch):
    # The name resolves to an absolute path, then starting it fails: that is the FileNotFoundError branch.
    started = []

    def absent(argv, **kwargs):
        started.append(list(argv))
        raise FileNotFoundError("no such program")

    monkeypatch.setattr(gpu_jobs, "resolve_program", lambda name: FAKE_PROGRAM)
    monkeypatch.setattr(subprocess, "run", absent)
    result = REAL_RUN_COMMAND(("nvidia-smi", "--version"), 1.0)
    assert result == CommandResult(returncode=None, missing=True)
    assert started == [[FAKE_PROGRAM, "--version"]]


def test_a_program_that_cannot_be_resolved_is_missing_and_never_started(monkeypatch):
    started = []
    monkeypatch.setattr(gpu_jobs, "resolve_program", lambda name: None)
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: started.append(argv))
    assert REAL_RUN_COMMAND(("definitely-missing-program",), 1.0) == CommandResult(returncode=None, missing=True)
    assert started == []


def test_a_timeout_is_reported_as_timed_out(monkeypatch):
    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    monkeypatch.setattr(gpu_jobs, "resolve_program", lambda name: FAKE_PROGRAM)
    monkeypatch.setattr(subprocess, "run", slow)
    result = REAL_RUN_COMMAND(("nvidia-smi",), 1.0)
    assert result.timed_out is True
    assert result.returncode is None


def test_an_os_error_is_reported_as_failed_to_start(monkeypatch):
    def denied(argv, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(gpu_jobs, "resolve_program", lambda name: FAKE_PROGRAM)
    monkeypatch.setattr(subprocess, "run", denied)
    result = REAL_RUN_COMMAND(("nvidia-smi",), 1.0)
    assert result.failed_to_start is True


class _Done:
    returncode = 0
    stdout = ""
    stderr = ""


def test_every_program_is_started_by_an_absolute_path(monkeypatch):
    started = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: started.append(list(argv)) or _Done())
    for name in ("nvidia-smi", "ssh", "squeue", "bjobs"):
        started.clear()
        result = REAL_RUN_COMMAND([name, "--probe"], 1.0)
        if started:
            assert os.path.isabs(started[0][0]), name
        else:
            # A program that is not on this machine is reported as missing, and nothing is started.
            assert result.missing is True, name


def test_a_program_planted_in_the_current_directory_is_never_run(monkeypatch, tmp_path):
    (tmp_path / "squeue.exe").write_bytes(b"not a real program")
    (tmp_path / "squeue").write_bytes(b"not a real program")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "")
    gpu_jobs.clear_program_cache()
    assert gpu_jobs.resolve_program("squeue") is None
    started = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: started.append(argv) or _Done())
    assert REAL_RUN_COMMAND(["squeue", "--me"], 1.0).missing is True
    assert started == []


def test_a_relative_path_entry_is_not_searched(monkeypatch, tmp_path):
    (tmp_path / "bjobs.exe").write_bytes(b"not a real program")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "relative-bin"]))
    gpu_jobs.clear_program_cache()
    assert gpu_jobs.resolve_program("bjobs") is None


def test_a_program_on_an_absolute_path_entry_resolves_to_that_absolute_file(monkeypatch, tmp_path):
    program = tmp_path / ("squeue.exe" if os.name == "nt" else "squeue")
    program.write_bytes(b"not a real program")
    monkeypatch.setenv("PATH", str(tmp_path))
    gpu_jobs.clear_program_cache()
    resolved = gpu_jobs.resolve_program("squeue")
    assert resolved is not None
    assert os.path.isabs(resolved)
    assert os.path.normcase(resolved) == os.path.normcase(str(program))


def test_a_program_is_resolved_once_and_the_answer_is_kept(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    gpu_jobs.clear_program_cache()
    assert gpu_jobs.resolve_program("bjobs") is None
    # A file that appears later is not picked up: the first answer stands until the cache is cleared.
    (tmp_path / ("bjobs.exe" if os.name == "nt" else "bjobs")).write_bytes(b"late")
    assert gpu_jobs.resolve_program("bjobs") is None


def test_a_finished_program_returns_its_exit_code_and_text(monkeypatch):
    class Done:
        returncode = 0
        stdout = "out"
        stderr = "err"

    seen = {}

    def finished(argv, **kwargs):
        seen.update(kwargs)
        seen["argv"] = argv
        return Done()

    monkeypatch.setattr(subprocess, "run", finished)
    monkeypatch.setattr(gpu_jobs, "resolve_program", lambda name: os.path.join("C:\\", "Windows", name + ".exe"))
    result = REAL_RUN_COMMAND(("nvidia-smi", "--version"), 2.0)
    assert result == CommandResult(returncode=0, stdout="out", stderr="err")
    assert seen["argv"] == [os.path.join("C:\\", "Windows", "nvidia-smi.exe"), "--version"]
    assert seen["shell"] is False
    assert seen["timeout"] == 2.0
    assert seen["stdin"] == subprocess.DEVNULL


# ----------------------------------------------------------------- display text


def test_the_summary_counts_gpus_hosts_busy_and_memory():
    run = FakeRunner({NVIDIA_GPU_ARGV: ok(REAL_GPU_LINE)})
    snapshot = collect(run, (), None, now=NOW)
    assert gpu_jobs.summary_text(snapshot) == "1 GPU on 1 host. 1 busy. 1.7 of 20.0 GB GPU memory in use."


def test_the_summary_with_several_hosts_uses_plural_words():
    run = FakeRunner({NVIDIA_GPU_ARGV: ok(TWO_GPU_TEXT), ssh_key("box"): ok(remote_stdout(gpu=REAL_GPU_LINE))})
    snapshot = collect(run, ("box",), None, now=NOW)
    assert gpu_jobs.summary_text(snapshot) == "3 GPUs on 2 hosts. 2 busy. 31.0 of 100.0 GB GPU memory in use."


def test_the_table_rows_show_dashes_for_missing_values():
    text = "0, A100, GPU-x, [N/A], [N/A], [N/A], [N/A], [N/A], [N/A]\n"
    run = FakeRunner({NVIDIA_GPU_ARGV: ok(text)})
    snapshot = collect(run, (), None, now=NOW)
    (row,) = gpu_jobs.gpu_table_rows(snapshot)
    assert row == (LOCAL_HOST, "0", "A100", "-", "-", "-", "-")


def test_the_table_rows_format_values_and_jobs():
    run = FakeRunner({NVIDIA_GPU_ARGV: ok(REAL_GPU_LINE), NVIDIA_APPS_ARGV: ok(REAL_APPS_LINE)})
    snapshot = collect(run, (), lambda: RUNPOD_PAYLOAD, now=NOW)
    (gpu_row,) = gpu_jobs.gpu_table_rows(snapshot)
    assert gpu_row == (LOCAL_HOST, "0", "NVIDIA GeForce RTX 3080", "79%", "1734 / 20480 MiB", "59 °C", "272 / 320 W")
    job_rows = gpu_jobs.job_table_rows(snapshot)
    assert job_rows[0] == (
        SOURCE_GPU_PROCESS,
        LOCAL_HOST,
        "4",
        "-",
        "(not readable: Insufficient Permissions)",
        "running",
        "0",
        "-",
    )
    assert job_rows[1][0] == SOURCE_RUNPOD
    assert job_rows[1][6] == "NVIDIA A100 x2"
    status_rows = gpu_jobs.status_table_rows(snapshot)
    assert status_rows[0] == (LOCAL_HOST, PART_GPUS, "1 GPU read.")


def test_power_and_memory_text_handle_partial_values():
    assert gpu_jobs.power_text(None, None) == "-"
    assert gpu_jobs.power_text(100.4, None) == "100 W"
    assert gpu_jobs.memory_text(None, 100) == "-"
    assert gpu_jobs.temperature_text(None) == "-"


def test_a_failed_whole_read_is_a_plain_status():
    snapshot = gpu_jobs.failed_snapshot(now=NOW)
    assert snapshot.checked_at == NOW
    (status,) = snapshot.statuses
    assert status.state == STATE_FAILED
    assert status.text == "The GPU read failed. Press Refresh to try again."


# ----------------------------------------------------------------- read_host: key-type errors and auth backoff (round 3)

KEY_TYPE_STDERR = "Unable to negotiate with 10.0.0.5 port 22: no matching host key type found. Their offer: ssh-rsa"
KEY_TYPE_TEXT = "The host offers no SSH key type this computer accepts. This needs a change on the host, not here."
CHANGED_TEXT = (
    "Host key changed. This may be a reinstall or an attack. "
    "Do not accept the new key until the host's admin confirms it."
)
STRICT_UNKNOWN_STDERR = (
    "No ED25519 host key is known for gpu-box and you have requested strict checking.\r\nHost key verification failed."
)


def _host_status(run, alias, backoff):
    reading = gpu_jobs.read_host(run, alias, backoff)
    (status,) = [s for s in reading.statuses if s.part == PART_GPUS]
    return status


def _no_accept_instruction(text):
    lowered = text.lower()
    return "accept its key" not in lowered and "to accept" not in lowered and "accept the key" not in lowered


def test_a_key_type_mismatch_gets_its_own_message_and_no_accept_wording():
    run = FakeRunner({ssh_key("legacy"): CommandResult(returncode=255, stderr=KEY_TYPE_STDERR)})
    status = _host_status(run, "legacy", None)
    assert status.state == STATE_FAILED
    assert status.text == KEY_TYPE_TEXT
    assert _no_accept_instruction(status.text)


def test_a_key_type_mismatch_is_not_backed_off_as_a_login_failure():
    clock = FakeClock()
    backoff = gpu_jobs.HostBackoff(clock=clock)
    run = FakeRunner({ssh_key("legacy"): CommandResult(returncode=255, stderr=KEY_TYPE_STDERR)})
    gpu_jobs.read_host(run, "legacy", backoff)
    assert backoff.remaining("legacy") == 0


FIRST_CONTACT_TEXT = (
    "This host has no key of this type in known_hosts. If you have connected to it before, its key may have changed. "
    "Check its fingerprint with the host's admin before you add any key. Do not add a fingerprint you have not verified."
)


def test_a_first_contact_host_gets_the_not_in_known_hosts_text_that_never_says_changed():
    # OpenSSH prints the "No <type> host key is known" line for a host with no known key. That is first contact, not a
    # changed key, so the text must not say the key changed.
    run = FakeRunner({ssh_key("gpu-box"): CommandResult(returncode=255, stderr=STRICT_UNKNOWN_STDERR)})
    status = _host_status(run, "gpu-box", None)
    assert status.state == STATE_FAILED
    assert status.text == FIRST_CONTACT_TEXT
    assert "may have changed" in status.text
    assert "Host key changed" not in status.text
    assert _no_accept_instruction(status.text)


def test_every_sentence_about_adding_a_key_in_the_first_contact_path_carries_the_verify_clause():
    for sentence in FIRST_CONTACT_TEXT.split(". "):
        if "add" in sentence.lower():
            assert "verif" in sentence.lower() or "fingerprint" in sentence.lower(), sentence
    assert "accept" not in FIRST_CONTACT_TEXT.lower()
    assert "Do not add a fingerprint you have not verified." in FIRST_CONTACT_TEXT


def test_a_changed_banner_keeps_the_changed_key_text_with_no_accept_advice():
    changed = CommandResult(
        returncode=255,
        stderr="WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!\r\nHost key verification failed.",
    )
    run = FakeRunner({ssh_key("gpu-box"): changed})
    status = _host_status(run, "gpu-box", None)
    assert status.state == STATE_FAILED
    assert status.text == CHANGED_TEXT
    assert "accept its key" not in status.text.lower()
    assert "to accept" not in status.text.lower()


def test_too_many_authentication_failures_backs_the_host_off():
    clock = FakeClock()
    backoff = gpu_jobs.HostBackoff(clock=clock)
    refused = CommandResult(
        returncode=255,
        stderr="Received disconnect from 10.0.0.5 port 22:2: Too many authentication failures",
    )
    run = FakeRunner({ssh_key("busy"): refused})
    gpu_jobs.read_host(run, "busy", backoff)
    assert backoff.remaining("busy") == 60


# ----------------------------------------------------------------- resolve_program: .exe only (round 3)


@pytest.fixture(autouse=True)
def fresh_program_cache():
    """The resolver keeps its answers for the whole process, so each test starts and ends with an empty cache."""
    gpu_jobs.clear_program_cache()
    yield
    gpu_jobs.clear_program_cache()


@pytest.mark.skipif(os.name != "nt", reason="the .exe rule is the Windows program rule")
@pytest.mark.parametrize("name", ["nvidia-smi", "ssh", "squeue", "bjobs"])
def test_a_script_on_path_is_refused_with_a_clear_message(monkeypatch, tmp_path, name):
    (tmp_path / f"{name}.cmd").write_text("@echo off\r\n", encoding="utf-8")
    monkeypatch.setattr(gpu_jobs, "PROGRAM_CANDIDATES", {})
    monkeypatch.setenv("PATH", str(tmp_path))
    started = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: started.append(argv) or _Done())
    result = REAL_RUN_COMMAND((name, "--version"), 1.0)
    assert result.missing is True
    assert started == []
    assert f"{name}.cmd" in result.refusal
    assert ".exe" in result.refusal
    assert "only .exe programs are run" in result.refusal


@pytest.mark.skipif(os.name != "nt", reason="the .exe rule is the Windows program rule")
def test_a_bat_on_path_is_refused_too(monkeypatch, tmp_path):
    (tmp_path / "bjobs.bat").write_text("@echo off\r\n", encoding="utf-8")
    monkeypatch.setattr(gpu_jobs, "PROGRAM_CANDIDATES", {})
    monkeypatch.setenv("PATH", str(tmp_path))
    assert gpu_jobs.resolve_program("bjobs") is None
    assert "bjobs.bat" in gpu_jobs.program_refusal("bjobs")


@pytest.mark.skipif(os.name != "nt", reason="the .exe rule is the Windows program rule")
def test_an_exe_on_path_is_accepted_and_a_script_beside_it_is_ignored(monkeypatch, tmp_path):
    (tmp_path / "squeue.exe").write_bytes(b"not a real program")
    (tmp_path / "squeue.cmd").write_text("@echo off\r\n", encoding="utf-8")
    monkeypatch.setattr(gpu_jobs, "PROGRAM_CANDIDATES", {})
    monkeypatch.setenv("PATH", str(tmp_path))
    resolved = gpu_jobs.resolve_program("squeue")
    assert resolved is not None
    assert resolved.lower().endswith("squeue.exe")
    assert os.path.isabs(resolved)


@pytest.mark.skipif(os.name != "nt", reason="the .exe rule is the Windows program rule")
def test_a_refused_script_is_named_in_the_nvidia_gpu_status(monkeypatch, tmp_path):
    (tmp_path / "nvidia-smi.bat").write_text("@echo off\r\n", encoding="utf-8")
    monkeypatch.setattr(gpu_jobs, "PROGRAM_CANDIDATES", {})
    monkeypatch.setenv("PATH", str(tmp_path))
    run = FakeRunner({})
    real_run = REAL_RUN_COMMAND
    snapshot = collect(lambda argv, timeout: real_run(argv, timeout), (), None, now=NOW)
    (status,) = [s for s in snapshot.statuses if s.part == PART_GPUS]
    assert "nvidia-smi.bat" in status.text
    assert status.state == STATE_FAILED
