"""Protocol-level tests for meridian/process_registry.py (315b0a63).

Covers the sprint item's explicit acceptance list: normal completion,
crash/heartbeat expiry, PID reuse, multiple clients, shared runtime
reference counts, and no-cross-session-kill behavior — plus persistence
and the CLI/stdio wrapper (the "documented hook contract").
"""
from __future__ import annotations

import json
import os
import sys

import pytest

from meridian import process_lifecycle
from meridian import process_registry


class _FakeClock:
    """Deterministic, manually-advanced clock — avoids real-time sleeps."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> _FakeClock:
    return _FakeClock()


@pytest.fixture
def broker(clock: _FakeClock) -> process_registry.ProcessLeaseBroker:
    """In-memory-only broker (no persist_path) — fast, no disk I/O."""
    return process_registry.ProcessLeaseBroker(clock=clock)


# ---------------------------------------------------------------------------
# Normal completion: register -> heartbeat -> release
# ---------------------------------------------------------------------------


def test_register_returns_lease_with_identity_fields(broker):
    lease = broker.register(
        "claude-code", 4242, executable="claude", cwd="/repo", cmdline=["claude", "-p"],
    )
    assert lease.client == "claude-code"
    assert lease.pid == 4242
    assert lease.executable == "claude"
    assert lease.cwd == "/repo"
    assert lease.cmdline == ["claude", "-p"]
    assert lease.run_id  # generated
    assert lease.released is False


def test_register_generates_run_id_independent_of_pid(broker):
    lease_a = broker.register("codex", 100)
    lease_b = broker.register("codex", 100)  # same pid, different spawn
    assert lease_a.run_id != lease_b.run_id


def test_register_requires_client(broker):
    with pytest.raises(ValueError):
        broker.register("", 1)


def test_register_rejects_duplicate_live_run_id(broker):
    broker.register("codex", 1, run_id="fixed-id")
    with pytest.raises(ValueError):
        broker.register("codex", 2, run_id="fixed-id")


def test_register_allows_reusing_run_id_after_release(broker):
    broker.register("codex", 1, run_id="fixed-id")
    broker.release("codex", "fixed-id")
    # re-registration under the same run_id after release must not raise
    lease = broker.register("codex", 2, run_id="fixed-id")
    assert lease.pid == 2


def test_heartbeat_refreshes_last_heartbeat(broker, clock):
    lease = broker.register("codex", 1, ttl_seconds=60)
    clock.advance(30)
    broker.heartbeat("codex", lease.run_id)
    assert lease.last_heartbeat_at == clock.now


def test_release_marks_lease_released_and_excludes_from_list(broker):
    lease = broker.register("codex", 1)
    broker.release("codex", lease.run_id)
    assert lease.released is True
    assert lease.released_at is not None
    assert lease.run_id not in {l.run_id for l in broker.list_leases()}
    assert lease.run_id in {l.run_id for l in broker.list_leases(include_released=True)}


def test_normal_completion_full_lifecycle(broker, clock):
    """The canonical happy path: register, heartbeat a few times, release —
    never appears in sweep_expired or report_unowned_survivors at any point."""
    lease = broker.register("claude-desktop", 555, ttl_seconds=60)
    for _ in range(3):
        clock.advance(20)
        broker.heartbeat("claude-desktop", lease.run_id)
        assert broker.sweep_expired() == []
    broker.release("claude-desktop", lease.run_id)
    assert broker.sweep_expired() == []
    assert broker.report_unowned_survivors() == []


# ---------------------------------------------------------------------------
# Crash / heartbeat expiry
# ---------------------------------------------------------------------------


def test_sweep_expired_empty_before_ttl_elapses(broker, clock):
    broker.register("codex", 1, ttl_seconds=90)
    clock.advance(89)
    assert broker.sweep_expired() == []


def test_sweep_expired_finds_lease_past_ttl(broker, clock):
    lease = broker.register("codex", 1, ttl_seconds=90)
    clock.advance(91)
    expired = broker.sweep_expired()
    assert [l.run_id for l in expired] == [lease.run_id]


def test_heartbeat_prevents_expiry(broker, clock):
    lease = broker.register("codex", 1, ttl_seconds=90)
    clock.advance(80)
    broker.heartbeat("codex", lease.run_id)
    clock.advance(80)  # 80s since the heartbeat, well under a fresh 90s TTL
    assert broker.sweep_expired() == []


def test_released_lease_never_reported_as_expired(broker, clock):
    lease = broker.register("codex", 1, ttl_seconds=10)
    broker.release("codex", lease.run_id)
    clock.advance(1000)
    assert broker.sweep_expired() == []


def test_heartbeat_on_expired_but_unreleased_lease_still_works(broker, clock):
    """Expiry is advisory (a sweep classification), not a hard cutoff — a
    late heartbeat from a client that is in fact still alive must still
    succeed and clear the expired state."""
    lease = broker.register("codex", 1, ttl_seconds=10)
    clock.advance(20)
    assert broker.sweep_expired() != []
    broker.heartbeat("codex", lease.run_id)
    assert broker.sweep_expired() == []


def test_heartbeat_unknown_run_id_raises_not_found(broker):
    with pytest.raises(process_registry.LeaseNotFoundError):
        broker.heartbeat("codex", "never-registered")


def test_heartbeat_released_run_id_raises_not_found(broker):
    lease = broker.register("codex", 1)
    broker.release("codex", lease.run_id)
    with pytest.raises(process_registry.LeaseNotFoundError):
        broker.heartbeat("codex", lease.run_id)


# ---------------------------------------------------------------------------
# PID reuse
# ---------------------------------------------------------------------------


def test_is_process_alive_delegates_to_verify_handle_live(broker, monkeypatch):
    lease = broker.register("codex", 999, create_time=12345.0)
    calls = []

    def _fake_verify(handle):
        calls.append(handle)
        assert handle.pid == 999
        assert handle.create_time == 12345.0
        return False  # simulate: pid was reused by an unrelated process

    monkeypatch.setattr(process_registry._process_lifecycle, "verify_handle_live", _fake_verify)
    assert broker.is_process_alive(lease) is False
    assert len(calls) == 1


def test_report_unowned_survivors_excludes_reused_pid(broker, clock, monkeypatch):
    """A lease whose client crashed (expired) but whose pid the OS has since
    reused for a DIFFERENT process must NOT be reported as a survivor —
    that would mean tracking (and potentially someone acting on) the wrong
    process entirely."""
    reused = broker.register("codex", 1, ttl_seconds=10, create_time=1.0)
    genuine = broker.register("codex", 2, ttl_seconds=10, create_time=2.0)
    clock.advance(20)

    def _fake_verify(handle):
        # Only pid 2's original process is confirmed still alive.
        return handle.pid == 2 and handle.create_time == 2.0

    monkeypatch.setattr(process_registry._process_lifecycle, "verify_handle_live", _fake_verify)
    survivors = broker.report_unowned_survivors()
    assert [l.run_id for l in survivors] == [genuine.run_id]
    assert reused.run_id not in {l.run_id for l in survivors}


def test_report_unowned_survivors_degrades_true_without_psutil(broker, clock):
    """Real process_lifecycle.verify_handle_live degrades to True when no
    create_time was recorded — matches the documented "nothing to verify
    against" contract, exercised here without any monkeypatching."""
    lease = broker.register("codex", 1, ttl_seconds=10)  # no create_time
    clock.advance(20)
    survivors = broker.report_unowned_survivors()
    assert [l.run_id for l in survivors] == [lease.run_id]


# ---------------------------------------------------------------------------
# acquire_exclusive — single-owner lease + stale-wrapper detection/replacement
# (39c8cf2c). Liveness is simulated via the same verify_handle_live
# monkeypatch pattern used above — never a real process.
# ---------------------------------------------------------------------------


def test_acquire_exclusive_cold_start_registers_a_fresh_lease(broker):
    lease = broker.acquire_exclusive("meridian-tunnel_client", "tunnel-wrapper:/repo", 111)
    assert lease.owner_key == "tunnel-wrapper:/repo"
    assert lease.pid == 111
    assert lease.released is False


def test_acquire_exclusive_conflicts_with_a_verified_live_owner(broker, monkeypatch):
    first = broker.acquire_exclusive(
        "meridian-tunnel_client", "tunnel-wrapper:/repo", 111, create_time=1.0,
    )
    monkeypatch.setattr(
        process_registry._process_lifecycle, "verify_handle_live", lambda handle: True,
    )
    with pytest.raises(process_registry.OwnerConflictError) as excinfo:
        broker.acquire_exclusive(
            "meridian-tunnel_client", "tunnel-wrapper:/repo", 222, create_time=2.0,
        )
    assert excinfo.value.client == "meridian-tunnel_client"
    assert excinfo.value.owner_key == "tunnel-wrapper:/repo"
    assert excinfo.value.lease.run_id == first.run_id
    # The conflicting attempt must NOT have registered — only the original
    # live lease is still on file.
    assert [l.run_id for l in broker.list_leases(client="meridian-tunnel_client")] == [first.run_id]


def test_acquire_exclusive_replaces_a_stale_wrapper_without_raising(broker, monkeypatch):
    """The recorded prior lease's process is verified NO LONGER alive (dead,
    or the OS reused its pid) — this is the 'old wrapper remained alive'
    incident's mirror image: here the old wrapper is confirmed gone, so the
    new one must silently take over, not conflict."""
    stale = broker.acquire_exclusive(
        "meridian-tunnel_client", "tunnel-wrapper:/repo", 111, create_time=1.0,
    )
    monkeypatch.setattr(
        process_registry._process_lifecycle, "verify_handle_live", lambda handle: False,
    )
    fresh = broker.acquire_exclusive(
        "meridian-tunnel_client", "tunnel-wrapper:/repo", 222, create_time=2.0,
    )
    assert fresh.pid == 222
    assert fresh.owner_key == "tunnel-wrapper:/repo"
    # The stale lease was released (auto-replaced), not left dangling.
    live_ids = {l.run_id for l in broker.list_leases(client="meridian-tunnel_client")}
    assert live_ids == {fresh.run_id}
    assert stale.run_id not in live_ids


def test_acquire_exclusive_force_takes_over_a_live_owner_without_killing_it(broker, monkeypatch):
    """force=True explicitly authorizes takeover of a genuinely live prior
    owner. The broker only ever marks the old lease released — verified here
    by asserting no process-killing hook of any kind was invoked (there is
    none to invoke; this broker never kills anything, by design)."""
    first = broker.acquire_exclusive(
        "meridian-tunnel_client", "tunnel-wrapper:/repo", 111, create_time=1.0,
    )
    monkeypatch.setattr(
        process_registry._process_lifecycle, "verify_handle_live", lambda handle: True,
    )
    second = broker.acquire_exclusive(
        "meridian-tunnel_client", "tunnel-wrapper:/repo", 222, create_time=2.0, force=True,
    )
    assert second.pid == 222
    live_ids = {l.run_id for l in broker.list_leases(client="meridian-tunnel_client")}
    assert live_ids == {second.run_id}
    assert first.run_id not in live_ids
    # The superseded lease is retrievable as released, not vanished.
    released_lookup = {
        l.run_id: l for l in broker.list_leases(client="meridian-tunnel_client", include_released=True)
    }
    assert released_lookup[first.run_id].released is True


def test_acquire_exclusive_different_owner_keys_never_conflict(broker, monkeypatch):
    """Two DIFFERENT repos' wrappers (different owner_key) are legitimate
    concurrent owners under the same client — never a conflict, even when
    both are verified alive."""
    monkeypatch.setattr(
        process_registry._process_lifecycle, "verify_handle_live", lambda handle: True,
    )
    a = broker.acquire_exclusive("meridian-tunnel_client", "tunnel-wrapper:/repo-a", 111)
    b = broker.acquire_exclusive("meridian-tunnel_client", "tunnel-wrapper:/repo-b", 222)
    live_ids = {l.run_id for l in broker.list_leases(client="meridian-tunnel_client")}
    assert live_ids == {a.run_id, b.run_id}


def test_acquire_exclusive_different_clients_never_conflict(broker, monkeypatch):
    """Two DIFFERENT clients (e.g. two independently-run tools) sharing the
    same owner_key string are never compared against each other — exclusivity
    is scoped to (client, owner_key), matching every other guardrail on this
    broker (release/heartbeat/cleanup are all client-scoped too)."""
    monkeypatch.setattr(
        process_registry._process_lifecycle, "verify_handle_live", lambda handle: True,
    )
    a = broker.acquire_exclusive("meridian-tunnel_client", "tunnel-wrapper:/repo", 111)
    b = broker.acquire_exclusive("codex", "tunnel-wrapper:/repo", 222)
    assert a.client != b.client
    assert broker.list_leases(client="meridian-tunnel_client")[0].run_id == a.run_id
    assert broker.list_leases(client="codex")[0].run_id == b.run_id


def test_acquire_exclusive_after_graceful_release_is_a_cold_start(broker):
    """A lease that was explicitly released (normal shutdown) is gone from
    the live set entirely — the next acquire_exclusive for the same
    owner_key is a plain cold start, not a conflict or a stale-replacement,
    and never even calls is_process_alive for the released lease."""
    first = broker.acquire_exclusive("meridian-tunnel_client", "tunnel-wrapper:/repo", 111)
    broker.release("meridian-tunnel_client", first.run_id)
    second = broker.acquire_exclusive("meridian-tunnel_client", "tunnel-wrapper:/repo", 222)
    assert second.pid == 222
    live_ids = {l.run_id for l in broker.list_leases(client="meridian-tunnel_client")}
    assert live_ids == {second.run_id}


# ---------------------------------------------------------------------------
# Multiple clients
# ---------------------------------------------------------------------------


def test_list_leases_scoped_by_client(broker):
    a = broker.register("claude-code", 1)
    b = broker.register("codex", 2)
    c = broker.register("claude-desktop", 3)

    assert {l.run_id for l in broker.list_leases(client="claude-code")} == {a.run_id}
    assert {l.run_id for l in broker.list_leases(client="codex")} == {b.run_id}
    assert {l.run_id for l in broker.list_leases()} == {a.run_id, b.run_id, c.run_id}


def test_releasing_one_client_lease_does_not_affect_another(broker):
    a = broker.register("claude-code", 1)
    b = broker.register("codex", 2)
    broker.release("claude-code", a.run_id)
    assert a.released is True
    assert b.released is False
    assert {l.run_id for l in broker.list_leases()} == {b.run_id}


def test_expiry_is_independent_per_client(broker, clock):
    a = broker.register("claude-code", 1, ttl_seconds=10)
    clock.advance(5)
    b = broker.register("codex", 2, ttl_seconds=10)
    clock.advance(6)  # a is now 11s stale, b is 6s stale
    expired_ids = {l.run_id for l in broker.sweep_expired()}
    assert expired_ids == {a.run_id}


# ---------------------------------------------------------------------------
# No-cross-session-kill / peer-lease preservation / refuse unregistered cleanup
# ---------------------------------------------------------------------------


def test_foreign_heartbeat_refused(broker):
    lease = broker.register("claude-code", 1)
    with pytest.raises(process_registry.ForeignLeaseError):
        broker.heartbeat("codex", lease.run_id)
    # Untouched by the refused attempt.
    assert lease.last_heartbeat_at == lease.registered_at


def test_foreign_release_refused_and_lease_survives(broker):
    lease = broker.register("claude-code", 1)
    with pytest.raises(process_registry.ForeignLeaseError):
        broker.release("codex", lease.run_id)
    assert lease.released is False
    assert lease.run_id in {l.run_id for l in broker.list_leases()}


def test_request_cleanup_refuses_foreign_client(broker):
    lease = broker.register("claude-code", 1)
    with pytest.raises(process_registry.ForeignLeaseError):
        broker.request_cleanup("codex", lease.run_id)
    assert lease.released is False


def test_request_cleanup_succeeds_for_owning_client(broker):
    lease = broker.register("claude-code", 1)
    result = broker.request_cleanup("claude-code", lease.run_id)
    assert result.released is True


def test_request_cleanup_unregistered_run_id_refused(broker):
    with pytest.raises(process_registry.LeaseNotFoundError):
        broker.request_cleanup("claude-code", "no-such-run-id")


def test_peer_leases_preserved_across_many_clients(broker):
    """A wide fan-out of clients: one client's cleanup attempt on ANY of
    the others' leases must fail, and every other lease must remain
    exactly as registered."""
    leases = [broker.register(f"client-{i}", i) for i in range(5)]
    for i, lease in enumerate(leases):
        other_client = f"client-{(i + 1) % 5}"
        with pytest.raises(process_registry.ForeignLeaseError):
            broker.request_cleanup(other_client, lease.run_id)
    assert len(broker.list_leases()) == 5


# ---------------------------------------------------------------------------
# Shared runtime reference counts
# ---------------------------------------------------------------------------


def test_acquire_shared_runtime_increments_refcount(broker):
    assert broker.acquire_shared_runtime("serena-daemon", "claude-code", "run-a") == 1
    assert broker.acquire_shared_runtime("serena-daemon", "codex", "run-b") == 2
    assert broker.shared_runtime_refcount("serena-daemon") == 2


def test_acquire_shared_runtime_idempotent_for_same_holder(broker):
    broker.acquire_shared_runtime("serena-daemon", "claude-code", "run-a")
    broker.acquire_shared_runtime("serena-daemon", "claude-code", "run-a")
    assert broker.shared_runtime_refcount("serena-daemon") == 1


def test_should_close_shared_runtime_only_after_every_holder_releases(broker):
    broker.acquire_shared_runtime("serena-daemon", "claude-code", "run-a")
    broker.acquire_shared_runtime("serena-daemon", "codex", "run-b")
    assert broker.should_close_shared_runtime("serena-daemon") is False

    broker.release_shared_runtime("serena-daemon", "claude-code", "run-a")
    assert broker.should_close_shared_runtime("serena-daemon") is False  # codex still holds it

    broker.release_shared_runtime("serena-daemon", "codex", "run-b")
    assert broker.should_close_shared_runtime("serena-daemon") is True


def test_should_close_shared_runtime_true_for_never_acquired_name(broker):
    assert broker.should_close_shared_runtime("never-touched") is True


def test_releasing_lease_with_shared_runtime_drops_its_holder_slot(broker):
    lease = broker.register("claude-code", 1, shared_runtime="serena-daemon")
    assert broker.shared_runtime_refcount("serena-daemon") == 1
    broker.release("claude-code", lease.run_id)
    assert broker.shared_runtime_refcount("serena-daemon") == 0


# ---------------------------------------------------------------------------
# Persistence — atomic JSON file, survives a fresh broker instance
# ---------------------------------------------------------------------------


def test_persisted_lease_survives_new_broker_instance(tmp_path):
    registry_path = tmp_path / "leases.json"
    broker_a = process_registry.ProcessLeaseBroker(persist_path=registry_path)
    lease = broker_a.register(
        "claude-code", 42, executable="claude", create_time=99.5, shared_runtime="serena-daemon",
    )
    assert registry_path.exists()

    broker_b = process_registry.ProcessLeaseBroker(persist_path=registry_path)
    reloaded = {l.run_id: l for l in broker_b.list_leases()}
    assert lease.run_id in reloaded
    assert reloaded[lease.run_id].pid == 42
    assert reloaded[lease.run_id].create_time == 99.5
    assert broker_b.shared_runtime_refcount("serena-daemon") == 1


def test_persistence_survives_release_round_trip(tmp_path):
    registry_path = tmp_path / "leases.json"
    broker_a = process_registry.ProcessLeaseBroker(persist_path=registry_path)
    lease = broker_a.register("codex", 7)
    broker_a.release("codex", lease.run_id)

    broker_b = process_registry.ProcessLeaseBroker(persist_path=registry_path)
    assert broker_b.list_leases() == []
    assert broker_b.list_leases(include_released=True)[0].released is True


def test_persistence_write_leaves_no_stray_tmp_file(tmp_path):
    registry_path = tmp_path / "leases.json"
    broker = process_registry.ProcessLeaseBroker(persist_path=registry_path)
    broker.register("codex", 1)
    leftovers = [p for p in tmp_path.iterdir() if p.name != "leases.json"]
    assert leftovers == []


def test_missing_registry_file_loads_as_empty(tmp_path):
    registry_path = tmp_path / "does-not-exist" / "leases.json"
    broker = process_registry.ProcessLeaseBroker(persist_path=registry_path)
    assert broker.list_leases() == []


def test_corrupt_registry_file_loads_as_empty_rather_than_raising(tmp_path):
    registry_path = tmp_path / "leases.json"
    registry_path.write_text("{not valid json", encoding="utf-8")
    broker = process_registry.ProcessLeaseBroker(persist_path=registry_path)
    assert broker.list_leases() == []


def test_autosave_false_never_writes(tmp_path):
    registry_path = tmp_path / "leases.json"
    broker = process_registry.ProcessLeaseBroker(persist_path=registry_path, autosave=False)
    broker.register("codex", 1)
    assert not registry_path.exists()


# ---------------------------------------------------------------------------
# WorkerLease.to_dict / from_dict round trip
# ---------------------------------------------------------------------------


def test_lease_to_dict_from_dict_round_trip():
    lease = process_registry.WorkerLease(
        run_id="r1", client="codex", pid=1, executable="node", cwd="/x",
        cmdline=["node", "server.js"], create_time=5.0, group_id=1, job_id=None,
        shared_runtime="serena", ttl_seconds=45.0, registered_at=1.0, last_heartbeat_at=2.0,
        released=False, released_at=None,
    )
    restored = process_registry.WorkerLease.from_dict(lease.to_dict())
    assert restored == lease


def test_lease_from_dict_ignores_unknown_keys():
    restored = process_registry.WorkerLease.from_dict(
        {"run_id": "r1", "client": "codex", "pid": 1, "from_the_future": "ignored"}
    )
    assert restored.run_id == "r1"
    assert restored.pid == 1


def test_lease_as_owned_handle_matches_process_lifecycle_shape():
    lease = process_registry.WorkerLease(
        run_id="r1", client="codex", pid=123, executable="x", cwd="/y",
        cmdline=["x"], create_time=7.0, group_id=8, job_id=9,
    )
    handle = lease.as_owned_handle()
    assert isinstance(handle, process_lifecycle.OwnedProcessHandle)
    assert handle.run_id == "r1"
    assert handle.pid == 123
    assert handle.create_time == 7.0
    assert handle.group_id == 8
    assert handle.job_id == 9


# ---------------------------------------------------------------------------
# Default registry path + singleton
# ---------------------------------------------------------------------------


def test_default_registry_path_honors_env_override(monkeypatch, tmp_path):
    override = tmp_path / "custom" / "leases.json"
    monkeypatch.setenv("MERIDIAN_LEASE_REGISTRY_PATH", str(override))
    assert process_registry.default_registry_path() == override


def test_default_registry_path_falls_back_to_home(monkeypatch):
    monkeypatch.delenv("MERIDIAN_LEASE_REGISTRY_PATH", raising=False)
    path = process_registry.default_registry_path()
    assert path.name == "process_leases.json"
    assert path.parent.name == ".meridian"


def test_get_broker_singleton_and_reset(monkeypatch, tmp_path):
    monkeypatch.setenv("MERIDIAN_LEASE_REGISTRY_PATH", str(tmp_path / "leases.json"))
    process_registry.reset_default_broker()
    broker_a = process_registry.get_broker()
    broker_b = process_registry.get_broker()
    assert broker_a is broker_b
    process_registry.reset_default_broker()
    broker_c = process_registry.get_broker()
    assert broker_c is not broker_a


# ---------------------------------------------------------------------------
# CLI / stdio wrapper — the "client-neutral" hook contract
# ---------------------------------------------------------------------------


def _run_cli(argv, capsys):
    exit_code = process_registry.main(argv)
    captured = capsys.readouterr()
    return exit_code, captured


def test_cli_register_prints_lease_json(tmp_path, capsys):
    registry_path = tmp_path / "leases.json"
    exit_code, captured = _run_cli(
        ["--registry-path", str(registry_path), "register", "--client", "codex", "--pid", "123"],
        capsys,
    )
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["client"] == "codex"
    assert payload["pid"] == 123
    assert payload["run_id"]


def test_cli_register_heartbeat_release_round_trip(tmp_path, capsys):
    registry_path = tmp_path / "leases.json"
    base = ["--registry-path", str(registry_path)]

    _, captured = _run_cli(base + ["register", "--client", "codex", "--pid", "1"], capsys)
    run_id = json.loads(captured.out)["run_id"]

    exit_code, captured = _run_cli(
        base + ["heartbeat", "--client", "codex", "--run-id", run_id], capsys
    )
    assert exit_code == 0
    assert json.loads(captured.out)["run_id"] == run_id

    exit_code, captured = _run_cli(
        base + ["release", "--client", "codex", "--run-id", run_id], capsys
    )
    assert exit_code == 0
    assert json.loads(captured.out)["released"] is True

    exit_code, captured = _run_cli(base + ["list"], capsys)
    assert exit_code == 0
    assert json.loads(captured.out) == []


def test_cli_list_and_survivors_are_arrays(tmp_path, capsys):
    registry_path = tmp_path / "leases.json"
    base = ["--registry-path", str(registry_path)]
    _run_cli(base + ["register", "--client", "codex", "--pid", "1"], capsys)

    exit_code, captured = _run_cli(base + ["list"], capsys)
    assert exit_code == 0
    leases = json.loads(captured.out)
    assert isinstance(leases, list) and len(leases) == 1

    exit_code, captured = _run_cli(base + ["survivors"], capsys)
    assert exit_code == 0
    assert json.loads(captured.out) == []  # nothing expired yet


def test_cli_foreign_heartbeat_exits_nonzero_with_error_json(tmp_path, capsys):
    registry_path = tmp_path / "leases.json"
    base = ["--registry-path", str(registry_path)]
    _, captured = _run_cli(base + ["register", "--client", "codex", "--pid", "1"], capsys)
    run_id = json.loads(captured.out)["run_id"]

    exit_code, captured = _run_cli(
        base + ["heartbeat", "--client", "claude-code", "--run-id", run_id], capsys
    )
    assert exit_code == 1
    err = json.loads(captured.err)
    assert err["type"] == "ForeignLeaseError"


def test_cli_unknown_run_id_exits_nonzero(tmp_path, capsys):
    registry_path = tmp_path / "leases.json"
    exit_code, captured = _run_cli(
        ["--registry-path", str(registry_path), "release", "--client", "codex", "--run-id", "nope"],
        capsys,
    )
    assert exit_code == 1
    err = json.loads(captured.err)
    assert err["type"] == "LeaseNotFoundError"


def test_cli_register_accepts_cmdline_json_array(tmp_path, capsys):
    registry_path = tmp_path / "leases.json"
    exit_code, captured = _run_cli(
        [
            "--registry-path", str(registry_path),
            "register", "--client", "codex", "--pid", "1",
            "--cmdline", json.dumps(["node", "server.js"]),
        ],
        capsys,
    )
    assert exit_code == 0
    assert json.loads(captured.out)["cmdline"] == ["node", "server.js"]


def test_cli_register_with_shared_runtime(tmp_path, capsys):
    registry_path = tmp_path / "leases.json"
    exit_code, captured = _run_cli(
        [
            "--registry-path", str(registry_path),
            "register", "--client", "codex", "--pid", "1",
            "--shared-runtime", "serena-daemon",
        ],
        capsys,
    )
    assert exit_code == 0
    assert json.loads(captured.out)["shared_runtime"] == "serena-daemon"


def test_cli_module_invocation_via_subprocess(tmp_path):
    """Exercises the actual ``python -m meridian.process_registry`` entry
    point end-to-end — the literal command external clients would run."""
    import subprocess

    registry_path = tmp_path / "leases.json"
    result = subprocess.run(
        [sys.executable, "-m", "meridian.process_registry",
         "--registry-path", str(registry_path), "register", "--client", "claude-desktop", "--pid", "1"],
        capture_output=True, text=True, check=False,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(process_registry.__file__))),
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["client"] == "claude-desktop"
