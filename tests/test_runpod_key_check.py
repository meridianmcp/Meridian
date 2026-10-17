"""Tests for the RunPod key check and pod listing: the runpod_check_key and
runpod_list_pods MCP tools, their handler module, their dispatch registration and
the GET /compute/runpod/check and GET /compute/runpod/pods routes.

Nothing here reaches the network. RunPodClient is replaced by a fake that records
calls and returns canned payloads or raises canned errors. SENTINEL is placed in the
environment as the RunPod key, and no output or log record may contain it.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from meridian.credentials import CredentialError, ResolvedCredential
from meridian.mcp.handlers import runpod_tools
from meridian.research.providers.runpod_client import RunPodAPIError

SENTINEL = "SENTINEL-RUNPOD-KEY-DO-NOT-LEAK-7f3a9c"
POD_SECRET = "POD-ENV-SECRET-41d2"
RESOLVER_MARKER = "RESOLVER-MESSAGE-MARKER"
DEFAULT_KINDS = ["env", "keyring", "runpodctl"]
CHECK_KEYS = {"ok", "credential_source", "pod_count", "billing_available", "error"}
LIST_KEYS = {"ok", "credential_source", "pods", "pod_count", "has_more_pods", "error"}

SAMPLE_PODS = {
    "pods": [
        {
            "id": "pod-a",
            "name": "train-run-1",
            "status": "RUNNING",
            "gpu": {"id": "NVIDIA A100 80GB", "count": 1, "displayName": "A100"},
            "cost": 1.89,
            "runtime": {"uptime": 3600},
            "env": {"TOKEN": POD_SECRET},
        },
        {
            # A CPU pod (the spec omits gpu) that has exited (the spec nulls runtime when not RUNNING).
            "id": "pod-b",
            "name": "eval",
            "status": "EXITED",
            "cost": 0.0,
            "runtime": None,
            "env": {"TOKEN": POD_SECRET},
        },
    ],
    "pagination": {"hasNextPage": False, "nextCursor": None},
}


def _fake_client(*, billing=None, pods=None, billing_error=None, pods_error=None):
    """A RunPodClient stand-in. Returns the class and the list of instances it built."""
    created: list[Any] = []

    class FakeRunPodClient:
        def __init__(self, api_key, **_kwargs):
            self.api_key = api_key
            self.calls: list[Any] = []
            self.closed = False
            created.append(self)

        def account_billing(self):
            self.calls.append("account_billing")
            if billing_error is not None:
                raise billing_error
            return billing if billing is not None else {"records": [], "metadata": {}}

        def list_pods(self, *, cursor=None, limit=None, include_cluster_pods=None):
            self.calls.append(("list_pods", limit))
            if pods_error is not None:
                raise pods_error
            return pods if pods is not None else {"pods": [], "pagination": {"hasNextPage": False}}

        def close(self):
            self.closed = True

    return FakeRunPodClient, created


def _install_client(monkeypatch, **script):
    fake_cls, created = _fake_client(**script)
    monkeypatch.setattr(runpod_tools, "RunPodClient", fake_cls)
    return created


def _recording_resolver(monkeypatch, *, succeed_on: "str | None" = None, secret: str = SENTINEL):
    """Replace resolve_credential. Records each reference tried. Only ``succeed_on``
    resolves; every other reference raises a message carrying RESOLVER_MARKER."""
    calls: list[str] = []

    def fake_resolve(ref, *, hosted):
        calls.append(ref)
        assert hosted is False, "local mode: runpod_tools passes its live _hosted_mode(), which is False here"
        if ref == succeed_on:
            return ResolvedCredential(kind=ref.split(":", 1)[0], secret=secret)
        raise CredentialError(f"{RESOLVER_MARKER} {ref}")

    monkeypatch.setattr(runpod_tools, "resolve_credential", fake_resolve)
    return calls


@pytest.fixture(autouse=True)
def _local_mode(monkeypatch):
    """Every test starts self-hosted with no RunPod key in the environment."""
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# Credential chain
# ---------------------------------------------------------------------------


def test_default_chain_is_the_documented_order():
    assert runpod_tools.DEFAULT_CREDENTIAL_REFS == (
        "env:RUNPOD_API_KEY",
        "keyring:meridian-desktop/runpod-api-key",
        "runpodctl:default",
    )


@pytest.mark.asyncio
async def test_no_credential_tries_the_default_chain_and_reports_kinds_only(monkeypatch):
    calls = _recording_resolver(monkeypatch)  # nothing resolves
    result = await runpod_tools.handle_runpod_check_key({})

    assert calls == list(runpod_tools.DEFAULT_CREDENTIAL_REFS)
    assert result["ok"] is False
    assert result["credential_source"] is None
    assert result["error"]["code"] == "no_credential"
    assert result["error"]["source_kinds_tried"] == DEFAULT_KINDS
    assert "refs_tried" not in result["error"]
    blob = json.dumps(result)
    assert "env:RUNPOD_API_KEY" not in blob
    assert "meridian-desktop/runpod-api-key" not in blob
    assert RESOLVER_MARKER not in blob


@pytest.mark.asyncio
async def test_first_resolving_source_wins_and_the_chain_stops(monkeypatch):
    calls = _recording_resolver(monkeypatch, succeed_on="keyring:meridian-desktop/runpod-api-key")
    created = _install_client(monkeypatch, pods=SAMPLE_PODS)
    result = await runpod_tools.handle_runpod_check_key({})

    assert calls == ["env:RUNPOD_API_KEY", "keyring:meridian-desktop/runpod-api-key"]
    assert result["ok"] is True
    assert result["credential_source"] == "keyring"
    assert created[0].api_key == SENTINEL


@pytest.mark.asyncio
async def test_explicit_credential_ref_is_used_alone(monkeypatch):
    calls = _recording_resolver(monkeypatch, succeed_on="runpodctl:default")
    _install_client(monkeypatch, pods=SAMPLE_PODS)
    result = await runpod_tools.handle_runpod_list_pods({"credential_ref": "runpodctl:default"})

    assert calls == ["runpodctl:default"]
    assert result["ok"] is True
    assert result["credential_source"] == "runpodctl"


@pytest.mark.asyncio
async def test_explicit_credential_ref_that_fails_is_not_retried_against_the_chain(monkeypatch):
    calls = _recording_resolver(monkeypatch)
    result = await runpod_tools.handle_runpod_check_key({"credential_ref": "keyring:svc/acct"})

    assert calls == ["keyring:svc/acct"]
    assert result["error"]["code"] == "no_credential"
    assert result["error"]["source_kinds_tried"] == ["keyring"]
    assert "svc/acct" not in json.dumps(result)


@pytest.mark.asyncio
async def test_a_raw_key_pasted_as_credential_ref_is_rejected_without_being_echoed(monkeypatch):
    calls = _recording_resolver(monkeypatch)
    pasted = "rpa_PASTED-RAW-KEY-0042"
    result = await runpod_tools.handle_runpod_check_key({"credential_ref": pasted})

    assert calls == []
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_credential_ref"
    assert "PASTED-RAW-KEY" not in json.dumps(result)


@pytest.mark.asyncio
async def test_a_non_string_credential_ref_is_rejected(monkeypatch):
    calls = _recording_resolver(monkeypatch)
    result = await runpod_tools.handle_runpod_list_pods({"credential_ref": 12345})

    assert calls == []
    assert result["error"]["code"] == "invalid_credential_ref"


@pytest.mark.asyncio
async def test_a_key_that_is_not_a_usable_api_key_is_reported_without_its_value(monkeypatch):
    # Real RunPodClient here: its constructor rejects whitespace before any network call.
    monkeypatch.setenv("RUNPOD_API_KEY", f"value with spaces {SENTINEL}")
    result = await runpod_tools.handle_runpod_check_key({})

    assert result["ok"] is False
    assert result["credential_source"] == "env"
    assert result["error"]["code"] == "invalid_credential"
    blob = json.dumps(result)
    assert SENTINEL not in blob
    assert "value with spaces" not in blob


@pytest.mark.parametrize(
    ("unusable_key", "marker"),
    [
        ("KEYTAB\tSEP-1", "KEYTAB"),
        ("KEYBEL\x07-2", "KEYBEL"),
        ("KEYDEL\x7f-3", "KEYDEL"),
        ("KEYNONé-4", "KEYNON"),
    ],
)
@pytest.mark.asyncio
async def test_whitespace_control_and_non_ascii_keys_map_to_invalid_credential(
    monkeypatch, unusable_key, marker,
):
    # RunPodClient rejects whitespace, non-printable ASCII and non-ASCII keys. The message
    # must name all three, and the value (here a marker inside it) must not be echoed.
    monkeypatch.setenv("RUNPOD_API_KEY", unusable_key)
    result = await runpod_tools.handle_runpod_check_key({})

    assert result["ok"] is False
    assert result["credential_source"] == "env"
    assert result["error"]["code"] == "invalid_credential"
    assert "whitespace" in result["error"]["message"]
    assert "control characters" in result["error"]["message"]
    assert "non-ASCII characters" in result["error"]["message"]
    assert marker not in json.dumps(result)


def test_resolver_is_told_the_live_hosted_mode(monkeypatch):
    # Handlers refuse hosted before resolving, so this is defense in depth: whatever the
    # live mode is, the resolver receives it rather than a hard-coded False.
    seen: list[bool] = []

    def recording_resolve(ref, *, hosted):
        seen.append(hosted)
        raise CredentialError("not available")

    monkeypatch.setattr(runpod_tools, "resolve_credential", recording_resolve)
    monkeypatch.setattr(runpod_tools, "_hosted_mode", lambda: True)
    runpod_tools._resolve(None)

    assert seen == [True, True, True]


# ---------------------------------------------------------------------------
# Hosted refusal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hosted_refuses_both_tools_before_any_credential_is_read(monkeypatch):
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")
    monkeypatch.setenv("RUNPOD_API_KEY", SENTINEL)
    calls = _recording_resolver(monkeypatch, succeed_on="env:RUNPOD_API_KEY")
    created = _install_client(monkeypatch, pods=SAMPLE_PODS)

    for handler in (runpod_tools.handle_runpod_check_key, runpod_tools.handle_runpod_list_pods):
        for args in ({}, {"credential_ref": "env:RUNPOD_API_KEY"}):
            result = await handler(args)
            assert result["ok"] is False
            assert result["credential_source"] is None
            assert result["error"]["code"] == "hosted_refused"

    assert calls == []
    assert created == []
    assert SENTINEL not in json.dumps(
        [await runpod_tools.handle_runpod_check_key({}), await runpod_tools.handle_runpod_list_pods({})]
    )


# ---------------------------------------------------------------------------
# Check key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_success_reports_ok_count_source_kind_and_billing(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", SENTINEL)
    created = _install_client(monkeypatch, pods=SAMPLE_PODS)
    result = await runpod_tools.handle_runpod_check_key({})

    assert set(result) == CHECK_KEYS
    assert result == {
        "ok": True,
        "credential_source": "env",
        "pod_count": 2,
        "billing_available": True,
        "error": None,
    }
    assert created[0].calls == ["account_billing", ("list_pods", 1000)]
    assert created[0].closed is True


@pytest.mark.asyncio
async def test_billing_failure_keeps_ok_when_the_pod_listing_succeeds(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", SENTINEL)
    _install_client(
        monkeypatch,
        pods=SAMPLE_PODS,
        billing_error=RunPodAPIError(403, "FORBIDDEN", "no billing for this key"),
    )
    result = await runpod_tools.handle_runpod_check_key({})

    assert result["ok"] is True
    assert result["billing_available"] is False
    assert result["pod_count"] == 2
    assert result["error"] is None


@pytest.mark.asyncio
async def test_a_401_on_the_pod_listing_returns_a_code_and_no_key(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", SENTINEL)
    _install_client(
        monkeypatch,
        pods_error=RunPodAPIError(401, "UNAUTHORIZED", f"invalid key {SENTINEL}"),
    )
    result = await runpod_tools.handle_runpod_check_key({})

    assert result["ok"] is False
    assert result["credential_source"] == "env"
    assert result["error"]["code"] == "unauthorized"
    assert SENTINEL not in json.dumps(result)


@pytest.mark.asyncio
async def test_a_non_object_pod_page_is_an_invalid_response(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", SENTINEL)
    _install_client(monkeypatch, pods={"unexpected": True})
    result = await runpod_tools.handle_runpod_check_key({})

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_response"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "unauthorized"),
        (403, "forbidden"),
        (429, "rate_limited"),
        (500, "runpod_error"),
        (None, "transport_error"),
    ],
)
@pytest.mark.asyncio
async def test_client_errors_map_to_short_codes_without_key_material(monkeypatch, status, code):
    monkeypatch.setenv("RUNPOD_API_KEY", SENTINEL)
    _install_client(
        monkeypatch,
        pods_error=RunPodAPIError(status, "SERVER_TITLE", f"server echoed {SENTINEL}"),
    )
    for handler in (runpod_tools.handle_runpod_check_key, runpod_tools.handle_runpod_list_pods):
        result = await handler({})
        assert result["ok"] is False
        assert result["error"]["code"] == code
        assert SENTINEL not in json.dumps(result)


# ---------------------------------------------------------------------------
# List pods
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_pods_returns_summaries_never_raw_payloads(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", SENTINEL)
    _install_client(monkeypatch, pods=SAMPLE_PODS)
    result = await runpod_tools.handle_runpod_list_pods({})

    assert set(result) == LIST_KEYS
    assert result["ok"] is True
    assert result["credential_source"] == "env"
    assert result["pod_count"] == 2
    assert result["has_more_pods"] is False
    assert result["error"] is None
    assert result["pods"][0] == {
        "id": "pod-a",
        "name": "train-run-1",
        "status": "RUNNING",
        "gpu": {"id": "NVIDIA A100 80GB", "count": 1},
        "costPerHour": 1.89,
        "uptimeSeconds": 3600,
    }
    assert result["pods"][1] == {
        "id": "pod-b",
        "name": "eval",
        "status": "EXITED",
        "gpu": None,
        "costPerHour": 0.0,
        "uptimeSeconds": None,
    }
    assert POD_SECRET not in json.dumps(result)
    # Pod summaries carry none of a pod's raw fields (its env block in particular).
    for pod in result["pods"]:
        assert "env" not in pod


@pytest.mark.asyncio
async def test_list_pods_reports_when_more_pods_exist(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", SENTINEL)
    _install_client(
        monkeypatch,
        pods={"pods": SAMPLE_PODS["pods"], "pagination": {"hasNextPage": True, "nextCursor": "c2"}},
    )
    result = await runpod_tools.handle_runpod_list_pods({})

    assert result["ok"] is True
    assert result["has_more_pods"] is True


# ---------------------------------------------------------------------------
# Summary fields come from the v2 spec's Pod object
# ---------------------------------------------------------------------------


def test_pod_summary_reads_status_cost_gpu_and_runtime_uptime_by_spec_name():
    pod = {
        "id": "pod-s",
        "name": "s",
        "status": "RUNNING",
        "gpu": {"id": "NVIDIA RTX 4090", "count": 2},
        "cost": 0.69,
        "runtime": {"uptime": 120},
    }

    assert runpod_tools._pod_summary(pod) == {
        "id": "pod-s",
        "name": "s",
        "status": "RUNNING",
        "gpu": {"id": "NVIDIA RTX 4090", "count": 2},
        "costPerHour": 0.69,
        "uptimeSeconds": 120,
    }


def test_pod_summary_does_not_read_names_the_spec_does_not_declare():
    pod = {"id": "pod-x", "name": "x", "desiredStatus": "RUNNING", "costPerHr": 9.9, "uptimeSeconds": 5}

    summary = runpod_tools._pod_summary(pod)

    assert summary["status"] is None
    assert summary["costPerHour"] is None
    assert summary["uptimeSeconds"] is None


def test_cpu_pod_summary_has_a_null_gpu_and_every_key():
    # The spec omits Pod.gpu for CPU pods, and the summary still carries every key.
    pod = {
        "id": "cpu-1",
        "name": "c",
        "status": "RUNNING",
        "cpu": {"id": "cpu3c", "vcpuCount": 4},
        "cost": 0.12,
        "runtime": {"uptime": 9},
    }

    summary = runpod_tools._pod_summary(pod)

    assert summary["gpu"] is None
    assert set(summary) == {"id", "name", "status", "gpu", "costPerHour", "uptimeSeconds"}
    assert summary["costPerHour"] == 0.12
    assert summary["uptimeSeconds"] == 9


@pytest.mark.parametrize(
    "pod",
    [
        {"status": "RUNNING", "runtime": None},
        {"status": "RUNNING"},
        {"status": "EXITED", "runtime": {"uptime": 50}},
        {"status": "PROVISIONING", "runtime": {"uptime": 50}},
        {"status": "RUNNING", "runtime": {"uptime": float("nan")}},
        {"status": "RUNNING", "runtime": {"uptime": True}},
        {"status": "RUNNING", "runtime": "not-an-object"},
    ],
)
def test_uptime_is_null_unless_the_pod_is_running_with_a_numeric_runtime_uptime(pod):
    assert runpod_tools._pod_summary(pod)["uptimeSeconds"] is None


def test_cost_of_zero_passes_through_for_an_exited_pod():
    pod = {"id": "pod-z", "name": "z", "status": "EXITED", "cost": 0.0, "runtime": None}

    assert runpod_tools._pod_summary(pod)["costPerHour"] == 0.0


def test_list_pods_description_names_each_output_key_and_its_spec_field():
    from meridian.mcp_tools import _MCP_TOOLS_LIST

    description = next(tool for tool in _MCP_TOOLS_LIST if tool["name"] == "runpod_list_pods")["description"]

    assert "status (Pod.status" in description
    assert "gpu (Pod.gpu" in description
    assert "costPerHour (Pod.cost" in description
    assert "uptimeSeconds (Pod.runtime.uptime" in description
    assert "includeClusterPods" in description
    assert "defaults to false" in description
    assert "desiredStatus" not in description
    assert "costPerHr" not in description


# ---------------------------------------------------------------------------
# Non-finite numbers from a pod never reach a response
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (float("nan"), None),
        (float("inf"), None),
        (float("-inf"), None),
        (1.89, 1.89),
        (0.0, 0.0),
        (7, 7),
        (True, True),
        (None, None),
    ],
)
def test_scalar_keeps_finite_values_and_nulls_non_finite_floats(raw, expected):
    result = runpod_tools._scalar(raw)

    assert result == expected
    assert type(result) is type(expected)
    json.dumps(result, allow_nan=False)


NON_FINITE_POD_PAGE = {
    "pods": [
        {
            "id": "pod-n",
            "name": "nan-pod",
            "status": "RUNNING",
            "gpu": {"id": "NVIDIA A100 80GB", "count": float("inf")},
            "cost": float("nan"),
            "runtime": {"uptime": float("-inf")},
        },
    ],
    "pagination": {"hasNextPage": False},
}


@pytest.mark.asyncio
async def test_non_finite_pod_numbers_become_null_and_the_result_serializes(monkeypatch):
    # A JSON body can carry NaN and Infinity. Starlette's JSONResponse refuses them
    # (allow_nan=False), so a summary that kept them would fail as a server error.
    monkeypatch.setenv("RUNPOD_API_KEY", SENTINEL)
    _install_client(monkeypatch, pods=NON_FINITE_POD_PAGE)
    result = await runpod_tools.handle_runpod_list_pods({})

    assert result["ok"] is True
    assert result["pods"] == [
        {
            "id": "pod-n",
            "name": "nan-pod",
            "status": "RUNNING",
            "gpu": {"id": "NVIDIA A100 80GB", "count": None},
            "costPerHour": None,
            "uptimeSeconds": None,
        },
    ]
    json.dumps(result, allow_nan=False)


def test_pods_route_answers_200_for_a_pod_with_non_finite_numbers(loopback, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", SENTINEL)
    _install_client(monkeypatch, pods=NON_FINITE_POD_PAGE)
    resp = loopback.get("/compute/runpod/pods")

    assert resp.status_code == 200
    assert resp.json()["pods"][0]["costPerHour"] is None


# ---------------------------------------------------------------------------
# Sentinel never reaches output or logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sentinel_never_appears_in_any_output_or_log_record(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("RUNPOD_API_KEY", SENTINEL)

    _install_client(
        monkeypatch,
        pods=SAMPLE_PODS,
        billing_error=RunPodAPIError(401, "T", f"echo {SENTINEL}"),
    )
    ok_result = await runpod_tools.handle_runpod_check_key({})

    _install_client(monkeypatch, pods_error=RunPodAPIError(401, "T", f"echo {SENTINEL}"))
    err_result = await runpod_tools.handle_runpod_list_pods({})

    _install_client(monkeypatch, pods_error=RuntimeError(f"boom {SENTINEL}"))
    crash_result = await runpod_tools.handle_runpod_list_pods({})

    outputs = json.dumps([ok_result, err_result, crash_result])
    assert SENTINEL not in outputs
    assert SENTINEL not in caplog.text
    assert all(SENTINEL not in record.getMessage() for record in caplog.records)
    assert crash_result["error"]["code"] == "unexpected_error"
    assert "RuntimeError" in crash_result["error"]["message"]


# ---------------------------------------------------------------------------
# MCP registration
# ---------------------------------------------------------------------------


def test_both_tools_are_listed_with_only_an_optional_credential_ref():
    from meridian.mcp_tools import _MCP_TOOLS_LIST

    by_name = {tool["name"]: tool for tool in _MCP_TOOLS_LIST}
    for name in ("runpod_check_key", "runpod_list_pods"):
        tool = by_name[name]
        assert tool["description"]
        assert "refused on hosted Meridian" in tool["description"]
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert schema["required"] == []
        assert set(schema["properties"]) == {"credential_ref"}


@pytest.mark.asyncio
async def test_both_tools_dispatch_through_the_mcp_handler(db, tmp_path, monkeypatch):
    from meridian import server as srv

    monkeypatch.setenv("RUNPOD_API_KEY", SENTINEL)
    _install_client(monkeypatch, pods=SAMPLE_PODS)

    check = await srv._dispatch_mcp_tool("runpod_check_key", {}, db, str(tmp_path))
    listed = await srv._dispatch_mcp_tool("runpod_list_pods", {}, db, str(tmp_path))

    assert check["ok"] is True
    assert check["pod_count"] == 2
    assert listed["ok"] is True
    assert [pod["id"] for pod in listed["pods"]] == ["pod-a", "pod-b"]


@pytest.mark.asyncio
async def test_a_raw_key_as_credential_ref_through_mcp_dispatch_is_never_echoed(
    db, tmp_path, monkeypatch, caplog,
):
    from meridian import server as srv

    caplog.set_level(logging.DEBUG)
    pasted = "rpa_DISPATCH-PASTED-KEY-0077"
    monkeypatch.setenv("RUNPOD_API_KEY", SENTINEL)
    created = _install_client(monkeypatch, pods=SAMPLE_PODS)

    check = await srv._dispatch_mcp_tool("runpod_check_key", {"credential_ref": pasted}, db, str(tmp_path))
    listed = await srv._dispatch_mcp_tool("runpod_list_pods", {"credential_ref": pasted}, db, str(tmp_path))

    for result in (check, listed):
        assert result["ok"] is False
        assert result["error"]["code"] == "invalid_credential_ref"
    # Neither the pasted value nor the environment key may build a client.
    assert created == []
    outputs = json.dumps([check, listed])
    assert "DISPATCH-PASTED-KEY" not in outputs
    assert SENTINEL not in outputs
    assert "DISPATCH-PASTED-KEY" not in caplog.text


@pytest.mark.asyncio
async def test_tools_list_advertises_both_tools_as_read_only_and_open_world(tmp_path):
    import meridian.server  # noqa: F401
    from meridian.mcp import handler as mcp_handler

    resp = await mcp_handler._handle_mcp_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        db=None, data_dir=str(tmp_path), tenant=None,
    )
    tools_by_name = {tool["name"]: tool for tool in resp["result"]["tools"]}
    for name in ("runpod_check_key", "runpod_list_pods"):
        annotations = tools_by_name[name]["annotations"]
        assert annotations["readOnlyHint"] is True, name
        assert annotations["openWorldHint"] is True, name
        assert annotations["destructiveHint"] is False, name


def test_runpod_tools_carry_no_member_write_operation():
    from meridian.mcp_tools import _READ_ONLY_TOOLS, member_policy_operation

    for name in ("runpod_check_key", "runpod_list_pods"):
        assert name in _READ_ONLY_TOOLS
        # The member policy defines no read level. A tool absent from the operation map
        # carries no project_write requirement, which is the read level.
        assert member_policy_operation(name) is None


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------


@pytest.fixture
def loopback(client):
    """The full app, driven from a loopback peer. Starlette's TestClient reports
    ("testclient", 50000) by default, which the self-hosted loopback gate refuses."""
    from fastapi.testclient import TestClient

    from meridian import server as srv

    return TestClient(srv.app, client=("127.0.0.1", 50000))


def test_compute_routes_are_mounted_on_the_app():
    from meridian import server as srv

    paths = {getattr(route, "path", None) for route in srv.app.routes}
    assert "/compute/runpod/check" in paths
    assert "/compute/runpod/pods" in paths


def test_check_route_returns_the_same_shape_as_the_tool(loopback, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", SENTINEL)
    _install_client(monkeypatch, pods=SAMPLE_PODS)
    resp = loopback.get("/compute/runpod/check")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == CHECK_KEYS
    assert body == {
        "ok": True,
        "credential_source": "env",
        "pod_count": 2,
        "billing_available": True,
        "error": None,
    }
    assert SENTINEL not in resp.text


def test_pods_route_returns_the_same_shape_as_the_tool(loopback, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", SENTINEL)
    _install_client(monkeypatch, pods=SAMPLE_PODS)
    resp = loopback.get("/compute/runpod/pods")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == LIST_KEYS
    assert body["ok"] is True
    assert [pod["id"] for pod in body["pods"]] == ["pod-a", "pod-b"]
    assert POD_SECRET not in resp.text
    assert SENTINEL not in resp.text


def test_routes_take_no_credential_ref_from_the_url(loopback, monkeypatch):
    calls = _recording_resolver(monkeypatch)
    resp = loopback.get("/compute/runpod/check", params={"credential_ref": "PASTED-RAW-KEY-0042"})

    assert resp.status_code == 200
    assert calls == list(runpod_tools.DEFAULT_CREDENTIAL_REFS)
    assert "PASTED-RAW-KEY" not in resp.text


def test_route_maps_a_hosted_refusal_to_403(loopback, monkeypatch):
    from meridian.routes import compute as compute_routes

    async def refused(args):
        return {
            "ok": False,
            "credential_source": None,
            "pod_count": 0,
            "billing_available": False,
            "error": {"code": "hosted_refused", "message": "refused"},
        }

    monkeypatch.setattr(compute_routes, "handle_runpod_check_key", refused)
    resp = loopback.get("/compute/runpod/check")

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "hosted_refused"


# ---------------------------------------------------------------------------
# Self-hosted loopback gate. A bare router app is used so the hosted route gate of
# the full app does not answer first. The peer address is set per client.
# ---------------------------------------------------------------------------

ROUTES = (
    ("/compute/runpod/check", "handle_runpod_check_key", CHECK_KEYS),
    ("/compute/runpod/pods", "handle_runpod_list_pods", LIST_KEYS),
)


def _bare_route_client(peer_host):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from meridian.routes import compute as compute_routes

    app = FastAPI()
    app.include_router(compute_routes.router)
    return TestClient(app, client=(peer_host, 50000))


def _spy_handler(monkeypatch, handler_name):
    from meridian.routes import compute as compute_routes

    calls: list[Any] = []

    async def spy(args):
        calls.append(args)
        raise AssertionError("the handler must not run for this caller")

    monkeypatch.setattr(compute_routes, handler_name, spy)
    return calls


@pytest.mark.parametrize(("path", "handler_name", "keys"), ROUTES)
@pytest.mark.parametrize("peer", ["203.0.113.9", "10.0.0.4", "testclient"])
def test_self_hosted_non_loopback_caller_gets_403_and_the_handler_is_not_called(
    monkeypatch, path, handler_name, keys, peer,
):
    monkeypatch.setenv("RUNPOD_API_KEY", SENTINEL)
    calls = _spy_handler(monkeypatch, handler_name)
    created = _install_client(monkeypatch, pods=SAMPLE_PODS)

    resp = _bare_route_client(peer).get(path)

    assert resp.status_code == 403
    body = resp.json()
    assert set(body) == keys
    assert body["ok"] is False
    assert body["error"]["code"] == "local_only"
    assert calls == []
    assert created == []
    assert SENTINEL not in resp.text


@pytest.mark.parametrize(("path", "handler_name", "keys"), ROUTES)
@pytest.mark.parametrize(
    "header",
    ["X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded", "X-Real-IP"],
)
def test_a_loopback_request_relayed_by_a_proxy_gets_403_and_the_handler_is_not_called(
    monkeypatch, path, handler_name, keys, header,
):
    calls = _spy_handler(monkeypatch, handler_name)

    resp = _bare_route_client("127.0.0.1").get(path, headers={header: "198.51.100.23"})

    assert resp.status_code == 403
    assert set(resp.json()) == keys
    assert resp.json()["error"]["code"] == "local_only"
    assert calls == []


@pytest.mark.parametrize(("path", "handler_name", "keys"), ROUTES)
@pytest.mark.parametrize("loopback_host", ["127.0.0.1", "::1"])
def test_a_self_hosted_loopback_caller_gets_the_normal_response(
    monkeypatch, path, handler_name, keys, loopback_host,
):
    monkeypatch.setenv("RUNPOD_API_KEY", SENTINEL)
    created = _install_client(monkeypatch, pods=SAMPLE_PODS)

    resp = _bare_route_client(loopback_host).get(path)

    assert resp.status_code == 200
    assert set(resp.json()) == keys
    assert resp.json()["ok"] is True
    assert resp.json()["error"] is None
    assert len(created) == 1
    assert SENTINEL not in resp.text


@pytest.mark.parametrize(("path", "handler_name", "keys"), ROUTES)
def test_hosted_non_loopback_caller_still_gets_the_handler_hosted_refusal(
    monkeypatch, path, handler_name, keys,
):
    # On hosted the loopback rule does not apply: the handler answers, as it did before the gate.
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")
    monkeypatch.setenv("RUNPOD_API_KEY", SENTINEL)
    calls = _recording_resolver(monkeypatch, succeed_on="env:RUNPOD_API_KEY")
    created = _install_client(monkeypatch, pods=SAMPLE_PODS)

    resp = _bare_route_client("203.0.113.9").get(path)

    assert resp.status_code == 403
    assert set(resp.json()) == keys
    assert resp.json()["error"]["code"] == "hosted_refused"
    assert calls == []
    assert created == []
