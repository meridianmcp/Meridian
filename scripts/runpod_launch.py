"""Local RunPod launcher for the owner's own RunPod account.

Read-only by default. ``python scripts/runpod_launch.py`` (or ``... list``) sends GET
requests only: it lists the account's pods and the GPU types in the catalog.

``create`` needs ``--confirm-cost MAX_USD_PER_HOUR``. The launcher prices the chosen
GPU type from the catalog (hourly USD per GPU times ``--gpu-count``) and refuses to
POST when that estimate is above the cap, when the GPU type is not in the catalog, or
when no price can be read for it. ``terminate`` needs ``--confirm-terminate POD_ID``;
nothing is sent without it.

The API key comes from the same sources, in the same order, with the same shape rule
as scripts/runpod_key_check.py: the RUNPOD_API_KEY environment variable, then the
keychain entry meridian-desktop / runpod-api-key, resolved with
meridian.credentials.resolve_credential(hosted=False). The key is never printed,
written to a file or logged. Every request goes through
meridian.research.providers.runpod_client.RunPodClient, which refuses malformed keys
and scrubs the key from error text.

Wire details this file does not confirm against the live API:

* GPU_CATALOG_PATH (GET catalog/gpus, relative to /v2). The repo only refers to
  GET /v2/catalog/cpus. A wrong path shows up as an HTTP error on the first list run.
* Price fields in each GPU catalog entry. The parser takes the largest numeric value
  under any key whose name contains "price" or "cost". A higher-than-real estimate can
  only refuse a create. It cannot let one through.

Exit status: 0 on success, 1 when the key or the API fails, 2 when a command is refused
or the arguments are wrong.

Usage, from the repository root:

    pixi run python scripts/runpod_launch.py
    pixi run python scripts/runpod_launch.py create --name run1 --image IMAGE \\
        --gpu-type-id "NVIDIA A100 80GB" --gpu-count 1 --confirm-cost 2.50
    pixi run python scripts/runpod_launch.py terminate --confirm-terminate POD_ID
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
for _path in (REPO_ROOT, SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import runpod_key_check as _key_check  # noqa: E402 - the key sources and shape rule live there

from meridian.credentials import CredentialError, ResolvedCredential, resolve_credential  # noqa: E402
from meridian.research.providers.runpod_client import RunPodAPIError, RunPodClient  # noqa: E402

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2

#: GET path of the GPU catalog, relative to DEFAULT_BASE_URL. Not confirmed (see docstring).
GPU_CATALOG_PATH = "catalog/gpus"
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 1000
MAX_GPU_COUNT = 8
_PRICE_KEY_HINTS = ("price", "cost")
_GPU_LIST_KEYS = ("gpus", "data", "items", "gpuTypes")
_POD_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


def parse_usd_cap(text: str) -> float:
    """A USD-per-hour cap: a finite number greater than zero."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number of US dollars per hour") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than 0")
    return value


def _page_limit(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number") from None
    if not 1 <= value <= MAX_LIST_LIMIT:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_LIST_LIMIT}")
    return value


def _gpu_count(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number") from None
    if not 1 <= value <= MAX_GPU_COUNT:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_GPU_COUNT}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="runpod_launch.py",
        description="Read-only RunPod listing by default. create and terminate need explicit confirmation flags.",
    )
    sub = parser.add_subparsers(dest="command")

    list_parser = sub.add_parser("list", help="read-only: list pods and GPU types (the default)")
    list_parser.add_argument("--limit", type=_page_limit, default=DEFAULT_LIST_LIMIT, help="pods per page (1-1000)")

    create = sub.add_parser("create", help="create a GPU pod; needs --confirm-cost")
    create.add_argument("--name", required=True, help="pod name")
    create.add_argument("--image", required=True, help="container image")
    create.add_argument("--gpu-type-id", required=True, help="GPU type id from the list command")
    create.add_argument("--gpu-count", type=_gpu_count, default=1, help="GPUs on the pod (1-8)")
    create.add_argument(
        "--confirm-cost",
        type=parse_usd_cap,
        default=None,
        metavar="MAX_USD_PER_HOUR",
        help="the most this pod may cost per hour in total; required",
    )

    terminate = sub.add_parser("terminate", help="terminate a pod; needs --confirm-terminate POD_ID")
    terminate.add_argument(
        "--confirm-terminate",
        default=None,
        metavar="POD_ID",
        help="the id of the pod to terminate; required",
    )
    return parser


def _refusal_before_network(args: argparse.Namespace) -> str | None:
    """Return a refusal message when the arguments are not enough to act. No request is made for these."""
    if args.command == "create":
        if args.confirm_cost is None:
            return "Refused: create needs --confirm-cost MAX_USD_PER_HOUR. Nothing was called."
        if not args.name.strip() or not args.image.strip() or not args.gpu_type_id.strip():
            return "Refused: --name, --image and --gpu-type-id must not be blank. Nothing was called."
    if args.command == "terminate":
        if args.confirm_terminate is None:
            return "Refused: terminate needs --confirm-terminate POD_ID. Nothing was called."
        if not _POD_ID_RE.fullmatch(args.confirm_terminate):
            return "Refused: POD_ID must be letters, digits, '-' or '_'. Nothing was called."
    return None


# ---------------------------------------------------------------------------
# Key
# ---------------------------------------------------------------------------


def load_api_key(
    resolve: Callable[..., ResolvedCredential] = resolve_credential,
) -> tuple[str | None, str]:
    """Return ``(secret, source label)``, or ``(None, fixed message)`` when no usable key exists.

    Same order and shape rule as scripts/runpod_key_check.py. The first source that
    resolves is the one used. The secret is never part of the message.
    """
    for label, ref in _key_check.SOURCES:
        try:
            credential = resolve(ref, hosted=False)
        except CredentialError:
            continue
        if not _key_check.key_shape_ok(credential.secret):
            return None, (
                f"Key used: {label}. The stored value is not a usable API key (it contains whitespace, "
                "control characters or non-ASCII characters). The value was not shown. Nothing was called."
            )
        return credential.secret, label
    tried = "; ".join(label for label, _ref in _key_check.SOURCES)
    return None, f"No RunPod key found (tried: {tried}). Nothing was called."


# ---------------------------------------------------------------------------
# Catalog and pricing
# ---------------------------------------------------------------------------


def _collect_prices(node: Any, under_price: bool, found: list[float]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            hinted = under_price or any(hint in str(key).lower() for hint in _PRICE_KEY_HINTS)
            _collect_prices(value, hinted, found)
    elif isinstance(node, list):
        for value in node:
            _collect_prices(value, under_price, found)
    elif (
        under_price
        and isinstance(node, (int, float))
        and not isinstance(node, bool)
        and math.isfinite(node)
        and node >= 0
    ):
        found.append(float(node))


def max_price_usd(entry: dict[str, Any]) -> float | None:
    """The largest non-negative number under any price- or cost-named key of a catalog entry, or None."""
    found: list[float] = []
    _collect_prices(entry, False, found)
    return max(found) if found else None


def fetch_gpu_types(client: RunPodClient) -> list[dict[str, Any]]:
    """GET the GPU catalog. Returns ``{"id", "displayName", "hourly_usd"}`` per GPU type. Only GET is sent."""
    body = client._json(client._request("GET", GPU_CATALOG_PATH))  # noqa: SLF001 - GET only, same client and auth
    items: list[Any] | None = None
    if isinstance(body, dict):
        for key in _GPU_LIST_KEYS:
            if isinstance(body.get(key), list):
                items = body[key]
                break
    if items is None:
        raise ValueError("GPU catalog response has no recognised list of GPU types")
    gpus: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            continue
        gpus.append(
            {
                "id": item["id"],
                "displayName": item.get("displayName") or item.get("name") or item["id"],
                "hourly_usd": max_price_usd(item),
            }
        )
    return gpus


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _format_pod(pod: dict[str, Any]) -> str:
    # Only these fields are shown. The raw pod object can carry environment values, which are never printed.
    gpu = pod.get("gpu")
    gpu_text = "CPU pod" if not isinstance(gpu, dict) else f"{gpu.get('id')} x{gpu.get('count')}"
    cost = pod.get("costPerHour", pod.get("cost"))
    return f"{pod.get('id')}  name={pod.get('name')}  status={pod.get('status')}  gpu={gpu_text}  cost={cost}/hr"


def _format_gpu(gpu: dict[str, Any]) -> str:
    price = "no price in catalog" if gpu["hourly_usd"] is None else f"{gpu['hourly_usd']} USD/hr per GPU"
    return f"{gpu['id']}  ({gpu['displayName']})  {price}"


def _run_list(client: RunPodClient, limit: int) -> int:
    page = client.list_pods(limit=limit)
    pods = page.get("pods")
    if not isinstance(pods, list):
        print("RunPod API error: the pod listing has no pods array.")
        return EXIT_FAILED
    print(f"Pods ({len(pods)} on this page):")
    for pod in pods:
        if isinstance(pod, dict):
            print("  " + _format_pod(pod))
    pagination = page.get("pagination")
    if isinstance(pagination, dict) and pagination.get("hasNextPage"):
        print(f"  More pods exist. Re-run with --limit up to {MAX_LIST_LIMIT} to see them.")

    print("GPU types:")
    try:
        gpus = fetch_gpu_types(client)
    except (RunPodAPIError, ValueError) as exc:
        print(f"  unavailable: {exc}")
        return EXIT_FAILED
    for gpu in gpus:
        print("  " + _format_gpu(gpu))
    return EXIT_OK


def _run_create(client: RunPodClient, args: argparse.Namespace) -> int:
    gpus = fetch_gpu_types(client)
    gpu = next((entry for entry in gpus if entry["id"] == args.gpu_type_id), None)
    if gpu is None:
        print(f"Refused: GPU type {args.gpu_type_id!r} is not in the catalog. Nothing was created.")
        return EXIT_REFUSED
    if gpu["hourly_usd"] is None:
        print("Refused: no hourly price could be read for this GPU type, so the cost cannot be checked. "
              "Nothing was created.")
        return EXIT_REFUSED

    estimate = Decimal(str(gpu["hourly_usd"])) * args.gpu_count
    cap = Decimal(str(args.confirm_cost))
    if estimate > cap:
        print(
            f"Refused: the estimated cost is {estimate} USD/hr ({args.gpu_count} x {gpu['hourly_usd']}), "
            f"above the cap of {cap} USD/hr. Nothing was created."
        )
        return EXIT_REFUSED

    pod = client.create_pod(
        name=args.name,
        image_name=args.image,
        gpu_type_id=args.gpu_type_id,
        gpu_count=args.gpu_count,
    )
    print(
        f"Created pod {pod.get('id')} (status {pod.get('status')}). "
        f"Estimated cost {estimate} USD/hr, cap {cap} USD/hr."
    )
    return EXIT_OK


def _run_terminate(client: RunPodClient, pod_id: str) -> int:
    client.terminate_pod(pod_id)
    print(f"Terminate request sent for pod {pod_id}. Run the list command to confirm it is gone.")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(
    argv: list[str] | None = None,
    *,
    resolve: Callable[..., ResolvedCredential] | None = None,
    transport: Any = None,
) -> int:
    """Run one command. ``resolve`` and ``transport`` are for tests; left out, the real keychain and network are used."""
    args = build_parser().parse_args(argv)
    if args.command is None:
        args.command = "list"
        args.limit = DEFAULT_LIST_LIMIT

    refusal = _refusal_before_network(args)
    if refusal is not None:
        print(refusal)
        return EXIT_REFUSED

    secret, label_or_message = load_api_key(resolve or resolve_credential)
    if secret is None:
        print(label_or_message)
        return EXIT_FAILED
    print(f"Key used: {label_or_message}.")

    with RunPodClient(secret, transport=transport) as client:
        try:
            if args.command == "list":
                return _run_list(client, args.limit)
            if args.command == "create":
                return _run_create(client, args)
            return _run_terminate(client, args.confirm_terminate)
        except RunPodAPIError as exc:
            # The client scrubs the key from this text before it is built.
            print(f"RunPod API error: {exc}")
            return EXIT_FAILED
        except ValueError as exc:
            # Raised with fixed text by this file or by the client (catalog shape, pod id). Never holds the key.
            print(f"Error: {exc}")
            return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
