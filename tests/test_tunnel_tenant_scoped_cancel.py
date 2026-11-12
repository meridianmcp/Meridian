"""A tunnel socket drop cancels only the in-flight requests of the tenant whose socket dropped.

The pending-request dicts in meridian/routes/tunnel.py are process-wide and keyed by request id, so the
disconnect cleanup must consult the owner map rather than cancel every entry.
"""

import asyncio

import pytest

from meridian.routes import tunnel as tunnel_routes


@pytest.fixture
def owners(monkeypatch):
    table: dict[str, str] = {}
    monkeypatch.setattr(tunnel_routes, "_pending_owner", table)
    return table


@pytest.fixture
def loop():
    event_loop = asyncio.new_event_loop()
    yield event_loop
    event_loop.close()


def test_a_drop_cancels_only_the_dropping_tenants_requests(owners, loop):
    pending = {}
    for req_id, tenant in [("a-1", "tenant-a"), ("b-1", "tenant-b"), ("a-2", "tenant-a")]:
        pending[req_id] = loop.create_future()
        owners[req_id] = tenant

    tunnel_routes._cancel_pending_for_tenant(pending, "tenant-a")

    assert pending["a-1"].cancelled()
    assert pending["a-2"].cancelled()
    assert not pending["b-1"].done()


def test_finished_requests_are_left_alone(owners, loop):
    pending = {"a-1": loop.create_future()}
    owners["a-1"] = "tenant-a"
    pending["a-1"].set_result({"status": 200})

    tunnel_routes._cancel_pending_for_tenant(pending, "tenant-a")

    assert pending["a-1"].result() == {"status": 200}


def test_a_request_with_no_recorded_owner_is_not_cancelled(owners, loop):
    pending = {"orphan": loop.create_future()}

    tunnel_routes._cancel_pending_for_tenant(pending, "tenant-a")

    assert not pending["orphan"].done()


def test_the_other_tenants_request_survives_a_drop_on_every_pending_dict(owners, loop):
    # The fs disconnect runs the helper over both the proxy and run_cmd dicts; neither may touch tenant B.
    proxy = {"b-proxy": loop.create_future()}
    run_cmd = {"b-run": loop.create_future()}
    owners.update({"b-proxy": "tenant-b", "b-run": "tenant-b"})

    tunnel_routes._cancel_pending_for_tenant(proxy, "tenant-a")
    tunnel_routes._cancel_pending_for_tenant(run_cmd, "tenant-a")

    assert not proxy["b-proxy"].done()
    assert not run_cmd["b-run"].done()
