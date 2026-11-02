"""Tests for scripts/runpod_launch.py, the local RunPod launcher.

Nothing here touches the real keychain or the network. The keyring is a fake module in
sys.modules, which the real resolve_credential imports, and every RunPod request goes
through httpx.MockTransport. The key sentinels must never appear in any printed line.
Each test checks what was sent, so a POST, PUT, PATCH or DELETE that should not happen
fails the test.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import types
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "runpod_launch.py"
SENTINEL = "SENTINEL-RUNPOD-KEY-DO-NOT-LEAK-3e8b"
ENV_SENTINEL = "SENTINEL-ENV-RUNPOD-KEY-DO-NOT-LEAK-91aa"
POD_SECRET = "POD-ENV-SECRET-7c4f"

POD_PAGE = {
    "pods": [
        {
            "id": "pod-a",
            "name": "train-run-1",
            "status": "RUNNING",
            "gpu": {"id": "NVIDIA A100 80GB", "count": 1},
            "cost": 1.89,
            "runtime": {"uptime": 3600},
            # Environment values are secrets in a real pod. They must never be printed.
            "env": {"TOKEN": POD_SECRET},
        },
        {
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

CATALOG = {
    "gpus": [
        {"id": "NVIDIA A100 80GB", "displayName": "A100 80GB", "securePrice": 1.89, "communityPrice": 1.19},
        {
            "id": "NVIDIA H100 80GB",
            "displayName": "H100",
            "lowestPrice": {"minimumBidPrice": 2.5, "uninterruptablePrice": 2.9},
        },
        {"id": "NVIDIA RTX 4090", "displayName": "RTX 4090"},
    ]
}

CREATED_POD = {"id": "pod-new", "name": "run1", "status": "CREATING"}


class FakeRunPod:
    """Answers the RunPod routes the launcher uses and records every request it receives."""

    def __init__(
        self,
        *,
        pods=None,
        catalog=None,
        catalog_status: int = 200,
        delete_status: int = 204,
        delete_detail: str | None = None,
    ) -> None:
        self.pods = POD_PAGE if pods is None else pods
        self.catalog = CATALOG if catalog is None else catalog
        self.catalog_status = catalog_status
        self.delete_status = delete_status
        self.delete_detail = delete_detail
        self.requests: list[dict] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        self.requests.append(
            {
                "method": request.method,
                "path": url.path,
                "query": url.query,
                "body": request.content,
                "auth": request.headers.get("authorization"),
            }
        )
        if request.method == "GET" and url.path == "/v2/pods":
            return httpx.Response(200, json=self.pods)
        if request.method == "GET" and url.path == "/v2/catalog/gpus":
            if self.catalog_status != 200:
                return httpx.Response(self.catalog_status, json={"title": "Upstream", "detail": "catalog down"})
            return httpx.Response(200, json=self.catalog)
        if request.method == "POST" and url.path == "/v2/pods":
            return httpx.Response(201, json=CREATED_POD)
        if request.method == "DELETE" and url.path.startswith("/v2/pods/"):
            if self.delete_status == 204:
                return httpx.Response(204)
            return httpx.Response(
                self.delete_status,
                json={"title": "Failed", "detail": self.delete_detail or "delete failed"},
            )
        return httpx.Response(404, json={"title": "Not found", "detail": "no such route"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def methods(self) -> set[str]:
        return {entry["method"] for entry in self.requests}

    def paths(self) -> list[str]:
        return [entry["path"] for entry in self.requests]

    def posted_bodies(self) -> list[dict]:
        return [json.loads(entry["body"]) for entry in self.requests if entry["method"] == "POST"]


@pytest.fixture(scope="module")
def launch():
    spec = importlib.util.spec_from_file_location("runpod_launch", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch):
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def keyring(monkeypatch):
    """A fake keyring module. The real resolve_credential imports it, so the keychain path is exercised
    without a real keychain. ``state['value']`` is what the desktop entry holds."""
    state = {"value": SENTINEL, "asked": [], "raise": None}

    def get_password(service, account):
        state["asked"].append((service, account))
        if state["raise"] is not None:
            raise state["raise"]
        return state["value"]

    monkeypatch.setitem(sys.modules, "keyring", types.SimpleNamespace(get_password=get_password))
    return state


def _run(launch, capsys, argv, fake: FakeRunPod | None = None):
    fake = fake if fake is not None else FakeRunPod()
    code = launch.main(argv, transport=fake.transport())
    captured = capsys.readouterr()
    return code, captured.out + captured.err, fake


# ---------------------------------------------------------------------------
# Read-only listing
# ---------------------------------------------------------------------------


def test_no_arguments_lists_pods_and_gpu_types_with_gets_only(launch, capsys):
    code, out, fake = _run(launch, capsys, [])

    assert code == 0
    assert fake.methods() == {"GET"}
    assert fake.paths() == ["/v2/pods", "/v2/catalog/gpus"]
    assert "pod-a  name=train-run-1  status=RUNNING  gpu=NVIDIA A100 80GB x1  cost=1.89/hr" in out
    assert "pod-b" in out
    assert "NVIDIA H100 80GB  (H100)  2.9 USD/hr per GPU" in out
    assert "NVIDIA RTX 4090  (RTX 4090)  no price in catalog" in out
    assert "Key used: keychain entry meridian-desktop / runpod-api-key." in out


def test_list_subcommand_passes_the_limit_as_a_query_parameter(launch, capsys):
    code, _out, fake = _run(launch, capsys, ["list", "--limit", "5"])

    assert code == 0
    assert fake.requests[0]["path"] == "/v2/pods"
    assert fake.requests[0]["query"] == "limit=5"
    assert fake.methods() == {"GET"}


@pytest.mark.parametrize("bad", ["0", "1001", "many"])
def test_list_rejects_a_bad_limit_before_any_request(launch, capsys, bad):
    fake = FakeRunPod()
    with pytest.raises(SystemExit) as excinfo:
        launch.main(["list", "--limit", bad], transport=fake.transport())

    assert excinfo.value.code == 2
    assert fake.requests == []


def test_list_notes_when_more_pods_exist(launch, capsys):
    page = {"pods": POD_PAGE["pods"], "pagination": {"hasNextPage": True, "nextCursor": "c2"}}
    code, out, _fake = _run(launch, capsys, [], FakeRunPod(pods=page))

    assert code == 0
    assert "More pods exist" in out


def test_catalog_failure_keeps_the_pod_listing_and_exits_1(launch, capsys):
    code, out, fake = _run(launch, capsys, [], FakeRunPod(catalog_status=500))

    assert code == 1
    assert "pod-a" in out
    assert "GPU types:" in out
    assert "unavailable" in out
    assert fake.methods() == {"GET"}


def test_unrecognised_catalog_shape_is_reported_and_exits_1(launch, capsys):
    code, out, fake = _run(launch, capsys, [], FakeRunPod(catalog={"unexpected": []}))

    assert code == 1
    assert "unavailable: GPU catalog response has no recognised list of GPU types" in out
    assert fake.methods() == {"GET"}


def test_pod_environment_values_are_never_printed(launch, capsys):
    code, out, _fake = _run(launch, capsys, [])

    assert code == 0
    assert POD_SECRET not in out
    assert "TOKEN" not in out


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_without_confirm_cost_is_refused_and_makes_no_request(launch, capsys):
    code, out, fake = _run(
        launch,
        capsys,
        ["create", "--name", "run1", "--image", "img:tag", "--gpu-type-id", "NVIDIA A100 80GB"],
    )

    assert code == 2
    assert "needs --confirm-cost" in out
    assert fake.requests == []


@pytest.mark.parametrize("cap", ["0", "-1", "nan", "inf", "abc"])
def test_create_rejects_a_bad_cap_before_any_request(launch, capsys, cap):
    fake = FakeRunPod()
    with pytest.raises(SystemExit) as excinfo:
        launch.main(
            ["create", "--name", "run1", "--image", "img:tag", "--gpu-type-id", "NVIDIA A100 80GB",
             "--confirm-cost", cap],
            transport=fake.transport(),
        )

    assert excinfo.value.code == 2
    assert fake.requests == []


def test_create_within_cap_posts_exactly_one_pod_without_env(launch, capsys):
    code, out, fake = _run(
        launch,
        capsys,
        ["create", "--name", "run1", "--image", "img:tag", "--gpu-type-id", "NVIDIA A100 80GB",
         "--gpu-count", "1", "--confirm-cost", "2.00"],
    )

    assert code == 0
    assert fake.methods() == {"GET", "POST"}
    assert fake.paths() == ["/v2/catalog/gpus", "/v2/pods"]
    assert fake.posted_bodies() == [{"name": "run1", "image": "img:tag", "gpu": {"id": "NVIDIA A100 80GB", "count": 1}}]
    assert "Created pod pod-new" in out
    assert "Estimated cost 1.89 USD/hr, cap 2.0 USD/hr." in out


def test_create_at_exactly_the_cap_is_allowed(launch, capsys):
    code, _out, fake = _run(
        launch,
        capsys,
        ["create", "--name", "run1", "--image", "img:tag", "--gpu-type-id", "NVIDIA H100 80GB",
         "--confirm-cost", "2.9"],
    )

    assert code == 0
    assert fake.methods() == {"GET", "POST"}
    assert fake.posted_bodies()[0]["gpu"] == {"id": "NVIDIA H100 80GB", "count": 1}


def test_create_above_cap_is_refused_before_the_post(launch, capsys):
    # 2 x 2.9 USD/hr = 5.8 USD/hr, above the 4.00 cap.
    code, out, fake = _run(
        launch,
        capsys,
        ["create", "--name", "run1", "--image", "img:tag", "--gpu-type-id", "NVIDIA H100 80GB",
         "--gpu-count", "2", "--confirm-cost", "4.00"],
    )

    assert code == 2
    assert "Refused" in out
    assert "5.8" in out
    assert "POST" not in fake.methods()
    assert fake.methods() == {"GET"}


def test_create_refused_when_the_gpu_has_no_catalog_price(launch, capsys):
    code, out, fake = _run(
        launch,
        capsys,
        ["create", "--name", "run1", "--image", "img:tag", "--gpu-type-id", "NVIDIA RTX 4090",
         "--confirm-cost", "10"],
    )

    assert code == 2
    assert "no hourly price could be read" in out
    assert fake.methods() == {"GET"}


def test_create_refused_for_a_gpu_type_not_in_the_catalog(launch, capsys):
    code, out, fake = _run(
        launch,
        capsys,
        ["create", "--name", "run1", "--image", "img:tag", "--gpu-type-id", "NVIDIA NOT-A-GPU",
         "--confirm-cost", "10"],
    )

    assert code == 2
    assert "is not in the catalog" in out
    assert fake.methods() == {"GET"}


def test_create_refused_when_the_catalog_cannot_be_read(launch, capsys):
    code, _out, fake = _run(
        launch,
        capsys,
        ["create", "--name", "run1", "--image", "img:tag", "--gpu-type-id", "NVIDIA A100 80GB",
         "--confirm-cost", "10"],
        FakeRunPod(catalog_status=500),
    )

    assert code == 1
    assert "POST" not in fake.methods()


def test_create_with_an_unrecognised_catalog_exits_1_and_posts_nothing(launch, capsys):
    code, out, fake = _run(
        launch,
        capsys,
        ["create", "--name", "run1", "--image", "img:tag", "--gpu-type-id", "NVIDIA A100 80GB",
         "--confirm-cost", "2"],
        FakeRunPod(catalog={"unexpected": []}),
    )

    assert code == 1
    assert "Error: GPU catalog response has no recognised list of GPU types" in out
    assert "POST" not in fake.methods()


def test_create_blank_name_is_refused_before_any_request(launch, capsys):
    code, out, fake = _run(
        launch,
        capsys,
        ["create", "--name", "   ", "--image", "img:tag", "--gpu-type-id", "NVIDIA A100 80GB",
         "--confirm-cost", "2"],
    )

    assert code == 2
    assert "must not be blank" in out
    assert fake.requests == []


def test_create_gpu_count_out_of_range_is_a_usage_error(launch):
    fake = FakeRunPod()
    with pytest.raises(SystemExit) as excinfo:
        launch.main(
            ["create", "--name", "run1", "--image", "img:tag", "--gpu-type-id", "NVIDIA A100 80GB",
             "--gpu-count", "0", "--confirm-cost", "2"],
            transport=fake.transport(),
        )

    assert excinfo.value.code == 2
    assert fake.requests == []


# ---------------------------------------------------------------------------
# terminate
# ---------------------------------------------------------------------------


def test_terminate_without_confirm_is_refused_and_makes_no_request(launch, capsys):
    code, out, fake = _run(launch, capsys, ["terminate"])

    assert code == 2
    assert "needs --confirm-terminate POD_ID" in out
    assert fake.requests == []


@pytest.mark.parametrize("pod_id", ["../pods", "pod/a", "pod a", "."])
def test_terminate_rejects_a_pod_id_that_is_not_a_plain_id(launch, capsys, pod_id):
    code, out, fake = _run(launch, capsys, ["terminate", "--confirm-terminate", pod_id])

    assert code == 2
    assert "POD_ID must be" in out
    assert fake.requests == []


def test_terminate_with_confirm_sends_one_delete(launch, capsys):
    code, out, fake = _run(launch, capsys, ["terminate", "--confirm-terminate", "pod-a"])

    assert code == 0
    assert fake.methods() == {"DELETE"}
    assert fake.requests == [
        {"method": "DELETE", "path": "/v2/pods/pod-a", "query": "", "body": b"", "auth": f"Bearer {SENTINEL}"}
    ]
    assert "Terminate request sent for pod pod-a" in out


def test_terminate_api_error_is_reported_with_the_key_scrubbed(launch, capsys):
    fake = FakeRunPod(delete_status=500, delete_detail=f"upstream echoed {SENTINEL}")
    code, out, _fake = _run(launch, capsys, ["terminate", "--confirm-terminate", "pod-a"], fake)

    assert code == 1
    assert "RunPod API error" in out
    assert SENTINEL not in out
    assert "[REDACTED:runpod_api_key]" in out


# ---------------------------------------------------------------------------
# Key handling
# ---------------------------------------------------------------------------


def test_key_is_read_from_the_desktop_keychain_entry_when_env_is_unset(launch, capsys, keyring):
    code, _out, fake = _run(launch, capsys, [])

    assert code == 0
    assert keyring["asked"] == [("meridian-desktop", "runpod-api-key")]
    assert fake.requests[0]["auth"] == f"Bearer {SENTINEL}"


def test_environment_variable_is_used_before_the_keychain(launch, capsys, keyring, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", ENV_SENTINEL)
    code, out, fake = _run(launch, capsys, [])

    assert code == 0
    assert "Key used: environment variable RUNPOD_API_KEY." in out
    assert keyring["asked"] == []
    assert fake.requests[0]["auth"] == f"Bearer {ENV_SENTINEL}"


def test_no_key_anywhere_exits_1_and_calls_nothing(launch, capsys, keyring):
    keyring["value"] = None
    code, out, fake = _run(launch, capsys, [])

    assert code == 1
    assert "No RunPod key found" in out
    assert "keychain entry meridian-desktop / runpod-api-key" in out
    assert fake.requests == []


def test_keyring_error_text_is_not_printed(launch, capsys, keyring):
    keyring["raise"] = RuntimeError(f"backend echoed {SENTINEL}")
    code, out, fake = _run(launch, capsys, [])

    assert code == 1
    assert SENTINEL not in out
    assert fake.requests == []


def test_malformed_stored_key_is_refused_without_echo(launch, capsys, keyring):
    keyring["value"] = f"{SENTINEL} trailing"
    code, out, fake = _run(launch, capsys, [])

    assert code == 1
    assert "not a usable API key" in out
    assert SENTINEL not in out
    assert fake.requests == []


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["list", "--limit", "3"],
        ["create", "--name", "run1", "--image", "img:tag", "--gpu-type-id", "NVIDIA A100 80GB",
         "--confirm-cost", "2"],
        ["terminate", "--confirm-terminate", "pod-a"],
    ],
)
def test_key_and_pod_secrets_are_never_printed(launch, capsys, argv):
    code, out, fake = _run(launch, capsys, argv)

    assert code in (0, 1, 2)
    assert SENTINEL not in out
    assert POD_SECRET not in out
    # The key is sent only as the Authorization header of each request.
    assert all(entry["auth"] == f"Bearer {SENTINEL}" for entry in fake.requests)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_parse_usd_cap_accepts_only_positive_finite_numbers(launch):
    assert launch.parse_usd_cap("2.5") == 2.5
    assert launch.parse_usd_cap(" 3 ") == 3.0
    for bad in ["0", "-0.5", "nan", "inf", "-inf", "free"]:
        with pytest.raises(argparse.ArgumentTypeError):
            launch.parse_usd_cap(bad)


def test_max_price_usd_takes_the_largest_price_like_number(launch):
    entry = {
        "id": "gpu-x",
        "displayName": "X",
        "securePrice": 1.5,
        "lowestPrice": {"minimumBidPrice": 0.4, "uninterruptablePrice": 2.25},
    }
    assert launch.max_price_usd(entry) == 2.25


@pytest.mark.parametrize(
    "entry",
    [
        {"id": "a"},
        {"id": "a", "price": True},
        {"id": "a", "price": "1.5"},
        {"id": "a", "price": -1},
        {"id": "a", "price": float("inf")},
        {"id": "a", "memory": 80},
    ],
)
def test_max_price_usd_returns_none_when_no_usable_price(launch, entry):
    assert launch.max_price_usd(entry) is None


def test_max_price_usd_reads_prices_inside_lists(launch):
    assert launch.max_price_usd({"id": "a", "tiers": [{"price": 0.7}, {"price": 0.9}]}) == 0.9


def test_parser_defaults_to_no_command(launch):
    args = launch.build_parser().parse_args([])
    assert args.command is None
