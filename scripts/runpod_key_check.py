"""Read-only RunPod key check for an agent shell on this machine.

It reports whether a RunPod API key is present in each local source, then makes one
GET request to RunPod's pod listing (limit=1) and reports the HTTP status. The key is
never printed, logged or echoed: output holds only presence words, status codes and
fixed phrases.

Sources, in the order the core tries them (the first two entries of
meridian.mcp.handlers.runpod_tools.DEFAULT_CREDENTIAL_REFS):

* env:RUNPOD_API_KEY
* keyring:meridian-desktop/runpod-api-key, the keychain entry the desktop writes

The runpodctl config file is deliberately not read. No pod is created, stopped or
terminated. The only request is GET /v2/pods?limit=1.

Exit status: 0 when RunPod answers 2xx, 1 otherwise.

Usage, from the repository root:

    pixi run python scripts/runpod_key_check.py
"""
from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402

from meridian.credentials import CredentialError, ResolvedCredential, resolve_credential  # noqa: E402
from meridian.research.providers.runpod_client import DEFAULT_BASE_URL  # noqa: E402

#: (label shown in output, credential reference). Checked in this order; the first present key is used.
SOURCES: tuple[tuple[str, str], ...] = (
    ("environment variable RUNPOD_API_KEY", "env:RUNPOD_API_KEY"),
    ("keychain entry meridian-desktop / runpod-api-key", "keyring:meridian-desktop/runpod-api-key"),
)
PODS_URL = f"{DEFAULT_BASE_URL}/pods"
TIMEOUT_SECONDS = 15.0
HEADER = "RunPod key check (read-only: one GET to /v2/pods?limit=1; no pod is created, stopped or terminated)"


def key_shape_ok(secret: str) -> bool:
    """The rule RunPodClient applies: printable ASCII with no whitespace."""
    return secret.isascii() and secret.isprintable() and not any(ch.isspace() for ch in secret)


def http_get_status(secret: str) -> int:
    """One GET to the pod listing with limit=1. Returns the HTTP status code.

    Redirects are not followed, so the key is never sent to a second host. A transport
    failure raises httpx.HTTPError, which the caller reports by fixed text only.
    """
    headers = {"Authorization": f"Bearer {secret}", "Accept": "application/json"}
    with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
        response = client.get(PODS_URL, params={"limit": 1}, headers=headers)
    return response.status_code


def run_check(
    *,
    resolve: Callable[..., ResolvedCredential] | None = None,
    get_status: Callable[[str], int] | None = None,
) -> tuple[int, list[str]]:
    """Return ``(exit code, output lines)``. The key appears in no line and in no exception text."""
    resolve = resolve or resolve_credential
    get_status = get_status or http_get_status
    lines = [HEADER]
    secret: str | None = None
    used_label: str | None = None
    for label, ref in SOURCES:
        try:
            credential = resolve(ref, hosted=False)
        except CredentialError:
            # The resolver's message names the variable, service or path. None of it is printed.
            lines.append(f"  {label}: not found")
            continue
        lines.append(f"  {label}: present")
        if secret is None:
            secret, used_label = credential.secret, label

    if secret is None:
        lines.append("RunPod API: not called, because no key was found in the sources above.")
        return 1, lines
    lines.append(f"Key used: {used_label}.")
    if not key_shape_ok(secret):
        lines.append(
            "RunPod API: not called, because the stored value is not a usable API key "
            "(it contains whitespace, control characters or non-ASCII characters). The value was not shown."
        )
        return 1, lines

    try:
        status = get_status(secret)
    except httpx.HTTPError:
        lines.append("RunPod API: no HTTP response (network or transport error).")
        return 1, lines
    except Exception as exc:  # noqa: BLE001 - only the exception class is reported, never its text
        lines.append(f"RunPod API: request failed before a response ({type(exc).__name__}).")
        return 1, lines

    if 200 <= status < 300:
        lines.append(f"RunPod API: HTTP {status}, key accepted.")
        return 0, lines
    lines.append(f"RunPod API: HTTP {status}, key not accepted.")
    return 1, lines


def main() -> int:
    code, lines = run_check()
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
