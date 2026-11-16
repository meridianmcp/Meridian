"""Shared test setup. Qt runs on the offscreen platform, so no display is needed.

For the whole session the keyring module uses an in-memory backend, and HOME,
USERPROFILE, LOCALAPPDATA, APPDATA and MERIDIAN_LOCAL_RUNNER_STATE_DIR point at a temp directory. CODEX_HOME is unset,
so Codex's config falls under that temp home too. So no test can reach the real OS
credential store, the real user home directory, or the real Claude Desktop and Codex
config files, even by accident.
"""

import gc
import os

import keyring
import pytest

from _fakes import MemoryKeyring

os.environ["QT_QPA_PLATFORM"] = "offscreen"


@pytest.fixture(scope="session", autouse=True)
def isolated_keyring_and_home(tmp_path_factory):
    """Install the in-memory keyring backend and a temp home for the whole session."""
    home = tmp_path_factory.mktemp("home")
    backend = MemoryKeyring()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(home))
        mp.setenv("USERPROFILE", str(home))
        mp.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
        mp.setenv("APPDATA", str(home / "AppData" / "Roaming"))
        # The Meridian Zotero bridge reads this variable before the home folder, so it is redirected too.
        mp.setenv("MERIDIAN_LOCAL_RUNNER_STATE_DIR", str(home / ".meridian" / "local_runner"))
        mp.delenv("CODEX_HOME", raising=False)
        keyring.set_keyring(backend)
        yield backend


@pytest.fixture(autouse=True)
def no_loopback_probe_waits(monkeypatch):
    """Home's services card probes the LaTeX engine and the slot ports on 127.0.0.1. On a loopback address those
    probes wait out their timeouts, which takes seconds, so every test gets fast stand-ins. A test that wants the
    real probe replaces these again. No test opens a socket for these checks."""
    from meridian_desktop.latex_client import ENGINE_NOT_RUNNING, EngineStatus
    from meridian_desktop.slot_ports import SlotPortResult
    from meridian_desktop.ui import home_tab

    monkeypatch.setattr(
        home_tab,
        "probe_engine_status",
        lambda url, timeout=None: EngineStatus(state=ENGINE_NOT_RUNNING, message="Not running (test stand-in)."),
    )
    monkeypatch.setattr(
        home_tab,
        "probe_slot_ports",
        lambda: [SlotPortResult("test slot", 1, False)],
    )
    # The Tunnel panel's slot check is the same connect-only probe, so it gets the same stand-in.
    # This computer's CPU, cores and memory are read from the system, and the read sleeps a sample interval. A stand-in
    # keeps every test fast and off the machine's real counters. A test that wants the real read replaces it again.
    from meridian_desktop import resources

    monkeypatch.setattr(
        resources,
        "local_row",
        lambda host: resources.ResourceRow(host, resources.STATE_FAILED, note="Stand-in for this computer's counters."),
    )
    from meridian_desktop.ui import tunnel_panel

    monkeypatch.setattr(
        tunnel_panel,
        "probe_slot_ports",
        lambda: [SlotPortResult("test slot", 1, False)],
    )


@pytest.fixture(autouse=True)
def no_real_zotero_reads(monkeypatch):
    """Zotero's process list and its local API are the machine's own state, and a window reads them by default.

    A window or a runner built without a Zotero fake reaches zotero_client's defaults. On Windows the process list is a
    real tasklist.exe launch, and the API probe is a real GET to 127.0.0.1:23119, which answers whenever the owner's
    Zotero is open. So those results depended on the machine, and each read ran as a real process or socket on a pool
    thread. Both are replaced here: no process is launched, no socket opens, and the API reads as not running. Tests that
    call zotero_client directly pass their own reader, and test_zotero_client imports http_get by name, so it keeps the
    real function."""
    from meridian_desktop import zotero_client

    def no_process_listed() -> str:
        return ""

    def api_not_running(base_url, path, headers, timeout):
        raise zotero_client.ZoteroError("not_running", zotero_client.NOT_RUNNING_MESSAGE)

    monkeypatch.setattr(zotero_client, "_tasklist_output", no_process_listed)
    monkeypatch.setattr(zotero_client, "http_get", api_not_running)


@pytest.fixture(autouse=True)
def isolated_zotero_journal(tmp_path, monkeypatch):
    """The Zotero save journal lives in the desktop state folder. Each test gets its own, so no held line leaks between tests.

    A test that relaunches a panel passes its own journal path, so the relaunch reads the same file the first window wrote.
    """
    from meridian_desktop import zotero_journal

    path = tmp_path / "zotero_saves" / zotero_journal.JOURNAL_NAME
    monkeypatch.setattr(zotero_journal, "default_path", lambda: path)


@pytest.fixture(autouse=True)
def no_real_zotero_bridge_writes(monkeypatch):
    """The Meridian Zotero bridge on 127.0.0.1:23125 writes into the owner's own Zotero library, so no test may reach it.

    Every request through the bridge's default transport is refused here, without a socket. A write (POST or DELETE)
    that a test attempts is recorded, and the test fails at teardown, even when the code went on to swallow the
    refusal. Reads are refused the same way. The Zotero API on 127.0.0.1:23119 is covered by no_real_zotero_reads.
    """
    from meridian_desktop import zotero_bridge, zotero_client

    writes: list[str] = []

    def refused(method, path, headers, body, timeout):
        if method != "GET":
            writes.append(f"{method} {path}")
        raise zotero_client.ZoteroError("not_running", zotero_bridge.NOT_ANSWERING_TEXT)

    monkeypatch.setattr(zotero_bridge, "http_request", refused)
    yield writes
    assert not writes, f"a test tried to write to the Zotero bridge: {writes}"


@pytest.fixture(autouse=True)
def no_real_gpu_or_runpod_reads(monkeypatch):
    """The GPUs tab reads this computer's GPUs with nvidia-smi and ssh, and the RunPod pods from the local server. A
    window that shows the tab without a fake would run those reads for real. Here each one is replaced: no program is
    started, no socket opens, and the reads report as missing or not running. Every attempt is recorded, and the test
    fails at teardown when there was one, even one the code went on to swallow. Tests that call the real run_command
    keep a reference taken when test_gpu_jobs is imported, before this fixture runs."""
    from meridian_desktop import gpu_jobs, local_client

    hits: list[str] = []

    def guarded_run(argv, timeout):
        hits.append(f"run_command {argv[0] if argv else ''}".strip())
        return gpu_jobs.CommandResult(returncode=None, missing=True)

    def guarded_pods(base_url, timeout=local_client.DEFAULT_TIMEOUT):
        hits.append("runpod_list_pods")
        raise local_client.LocalClientError("not_running", local_client.NOT_RUNNING_MESSAGE)

    def guarded_check(base_url, timeout=local_client.DEFAULT_TIMEOUT):
        hits.append("runpod_check")
        raise local_client.LocalClientError("not_running", local_client.NOT_RUNNING_MESSAGE)

    def guarded_offers(base_url, query=None, timeout=local_client.DEFAULT_TIMEOUT):
        hits.append("runpod_offers")
        raise local_client.LocalClientError("not_running", local_client.NOT_RUNNING_MESSAGE)

    monkeypatch.setattr(gpu_jobs, "run_command", guarded_run)
    monkeypatch.setattr(local_client, "runpod_list_pods", guarded_pods)
    monkeypatch.setattr(local_client, "runpod_check", guarded_check)
    monkeypatch.setattr(local_client, "runpod_offers", guarded_offers)
    yield hits
    assert not hits, f"a test reached a real GPU or RunPod read: {hits}"


@pytest.fixture(autouse=True)
def no_modal_dialog(monkeypatch):
    """A modal dialog never returns offscreen, so a test that opens one by accident would hang the whole run.

    Any exec() of a message box or dialog fails that test at once. A test that means to answer a question injects the
    answer instead, as the confirmation fakes do.
    """
    from PySide6.QtWidgets import QDialog, QMessageBox

    def refuse(self, *args, **kwargs):
        raise AssertionError("a modal dialog opened in a test; inject the answer instead")

    for cls in (QDialog, QMessageBox):
        for name in ("exec", "exec_"):
            if hasattr(cls, name):
                monkeypatch.setattr(cls, name, refuse)


@pytest.fixture(autouse=True)
def drain_pool_jobs(request):
    """Let every thread-pool job a test started finish before the next test starts.

    A window that is shown starts its Home checks on the pool. A check still running when a test ends would emit its
    result during a later test, and that test's recording of the shared delivery object would count it. Waiting here
    keeps each test's deliveries to its own.

    A window test also leaves cyclic garbage, which holds Qt widgets. The collector runs on whichever thread allocates,
    so a pool worker could collect a widget left by an earlier test, and free it off the GUI thread. That is an access
    violation on Windows. So a qtbot test collects its garbage here, on the GUI thread, before the next test starts.
    """
    yield
    from PySide6.QtCore import QThreadPool

    QThreadPool.globalInstance().waitForDone(5000)
    if "qtbot" in request.fixturenames:
        gc.collect()


@pytest.fixture(autouse=True)
def fresh_shared_delivery(monkeypatch):
    """Give each test its own shared delivery object, with the recorder connected before any page of the test exists.

    A pool job takes the shared object when it starts, so a job from an earlier test emits to that test's object, and
    nothing of this test is connected to it. A page connects to the object that is current when the page is built, so
    the pages of this test connect to this test's object. The recorder is the object's first connection, so it records
    each delivery before a page's queued handler is posted. Once the pool has drained, every connection of the object is
    dropped.
    """
    from PySide6.QtCore import QThreadPool

    from _shared_delivery import start_recording
    from meridian_desktop.ui import delivery as delivery_module

    fresh = delivery_module.Delivery()
    monkeypatch.setattr(delivery_module, "_SHARED", fresh)
    start_recording(fresh)
    yield fresh
    QThreadPool.globalInstance().waitForDone(5000)
    fresh.delivered.disconnect()


@pytest.fixture(autouse=True)
def no_hosted_network(monkeypatch):
    """The connection bar's check and the Settings sign-in reach the hosted host through hosted_session.live_transport.
    No test may reach it, so that transport is replaced here by one that fails as an unreachable host would. A test
    that drives the hosted flow passes its own fake transport instead."""
    from meridian_desktop import hosted_session

    def offline(request, timeout):
        raise OSError("network is off in tests")

    monkeypatch.setattr(hosted_session, "live_transport", offline)


@pytest.fixture(autouse=True)
def no_real_user_environment(monkeypatch):
    """The Windows user environment is HKCU\\Environment on the machine running the tests. No test may write it.

    The real writer refuses, and pytest.fail is a BaseException, so the refusal cannot be swallowed by the code under test.
    A test that means to set a user variable injects a fake registry instead.
    """
    from meridian_desktop import user_environment

    def refuse(self, *args, **kwargs):
        pytest.fail("a test tried to write the real Windows user environment; inject a fake registry", pytrace=False)

    monkeypatch.setattr(user_environment.WindowsRegistry, "write_user_value", refuse)
    monkeypatch.setattr(user_environment.WindowsRegistry, "announce_change", refuse)


@pytest.fixture
def settings_path(tmp_path):
    """A settings file in pytest's temp dir. Tests never touch the real one."""
    return tmp_path / "settings" / "settings.json"
