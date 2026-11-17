"""Per-tool handlers for the RunPod key check and pod listing: runpod_check_key
and runpod_list_pods. Local only: hosted Meridian refuses both.

Both tools resolve a RunPod API key through :mod:`meridian.credentials` and make
read-only calls through :class:`meridian.research.providers.runpod_client.RunPodClient`.
Neither creates, stops nor terminates a pod.

OUTPUT CONTRACT. The key is never returned, logged, or echoed in any result, error
or log record, and it is sent only to RunPod. No result, error message or log record
carries the key, its length or any prefix of it.

* The credential source is reported as its KIND only (``env``, ``keyring`` or
  ``runpodctl``), from :func:`meridian.credentials.describe_source`. A reference
  string, variable name, keyring service or account, and config path are never
  returned.
* Errors use fixed messages. The client's own error text is not echoed, so an
  error that quotes the key cannot leak it through this layer.
* A pod is returned as a summary with the keys id, name, status, gpu, costPerHour
  and uptimeSeconds. Each one is read from the v2 spec's Pod object (see
  :func:`_pod_summary`). The raw pod payload is never returned, so pod environment
  variables are not exposed.

HOSTED REFUSAL. On a hosted deployment both tools refuse before any reference is
read. A hosted server's environment, keyring and config files belong to the
operator, so resolving a caller-supplied reference there would spend operator
credentials on a caller's request.

CREDENTIAL CHAIN. When ``credential_ref`` is omitted, the references in
:data:`DEFAULT_CREDENTIAL_REFS` are tried in order and the first that resolves is
used. A key that resolves but is not a usable API key is reported as
``invalid_credential``; the chain does not continue past it. A supplied
``credential_ref`` is used alone.
"""
from __future__ import annotations

import asyncio
import math
import sys
from collections.abc import Callable
from typing import Any

from meridian._deps import _hosted_mode as _deps_hosted_mode
from meridian.credentials import (
    CredentialError,
    ResolvedCredential,
    describe_source,
    resolve_credential,
)
from meridian.research.providers.runpod_client import RunPodAPIError, RunPodClient

#: Credential references tried, in order, when ``credential_ref`` is omitted.
DEFAULT_CREDENTIAL_REFS: tuple[str, ...] = (
    "env:RUNPOD_API_KEY",
    "keyring:meridian-desktop/runpod-api-key",
    "runpodctl:default",
)
#: Largest page RunPod is asked for. Past this, ``has_more_pods`` is true.
_POD_PAGE_LIMIT = 1000
#: Longest string copied from a pod field into a summary.
_TEXT_CAP = 200

_HOSTED_MESSAGE = (
    "RunPod key checks are not available on hosted Meridian: a hosted server's "
    "credentials belong to the operator, not to the caller. Run Meridian self-hosted."
)
_BAD_REF_MESSAGE = (
    "credential_ref must look like env:NAME, keyring:SERVICE/ACCOUNT or runpodctl:default. "
    "The value supplied was not shown."
)
_NO_CREDENTIAL_MESSAGE = (
    "No RunPod API key was found in the sources tried. Set RUNPOD_API_KEY, store the key "
    "in the keyring entry, or set up the runpodctl config."
)
_INVALID_CREDENTIAL_MESSAGE = (
    "The credential resolved, but it is not a usable API key (it contains whitespace, "
    "control characters or non-ASCII characters). The value was not shown."
)


def _hosted_mode() -> bool:
    """Same answer as handler.py's ``_hosted_mode``, copied from remote_task_tools.py:
    the handler's binding when that module is loaded (so a test patch on the handler
    name still applies), else the ``_deps`` one. Both read MERIDIAN_HOSTED."""
    handler_mod = sys.modules.get("meridian.mcp.handler")
    patched = getattr(handler_mod, "_hosted_mode", None)
    return bool((patched or _deps_hosted_mode)())


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **extra}


def _api_error(exc: RunPodAPIError) -> dict[str, Any]:
    """Map a client error to a short code and a fixed message. The client's message
    is not used: fixed text cannot echo a key, whatever the server returned."""
    if exc.code == "INVALID_RESPONSE":
        return _error("invalid_response", "RunPod returned a response this check could not read.")
    if exc.status_code is None:
        return _error(
            "transport_error",
            "Could not reach the RunPod API. Check this machine's network connection and try again.",
        )
    if exc.status_code == 401:
        return _error("unauthorized", "RunPod rejected the API key (HTTP 401). Check that the key is current.")
    if exc.status_code == 403:
        return _error("forbidden", "RunPod refused this request for the API key (HTTP 403).")
    if exc.status_code == 429:
        return _error("rate_limited", "RunPod rate-limited this request (HTTP 429). Wait before trying again.")
    return _error("runpod_error", f"RunPod returned HTTP {exc.status_code}.")


def _resolve(credential_ref: Any) -> "tuple[ResolvedCredential | None, str | None, dict[str, Any] | None]":
    """Return ``(credential, kind, None)`` on success or ``(None, kind or None, error)``.

    The secret lives only in the returned credential. It is never copied into an
    error or a result. On ``no_credential`` the error lists the kinds tried, never
    the references.
    """
    if credential_ref is None:
        refs: tuple[str, ...] = DEFAULT_CREDENTIAL_REFS
    elif isinstance(credential_ref, str) and credential_ref:
        refs = (credential_ref,)
    else:
        return None, None, _error("invalid_credential_ref", _BAD_REF_MESSAGE)
    source_kinds_tried: list[str] = []
    for ref in refs:
        try:
            kind = describe_source(ref)
        except CredentialError:
            # The unsupported string is not echoed: it may be a pasted key.
            return None, None, _error("invalid_credential_ref", _BAD_REF_MESSAGE)
        source_kinds_tried.append(kind)
        try:
            return resolve_credential(ref, hosted=_hosted_mode()), kind, None
        except CredentialError:
            # Resolver messages can name variables, services and paths; none are copied out.
            continue
    return None, None, _error(
        "no_credential", _NO_CREDENTIAL_MESSAGE, source_kinds_tried=source_kinds_tried,
    )


def _run(
    credential_ref: Any,
    action: "Callable[[RunPodClient], dict[str, Any]]",
) -> "tuple[str | None, dict[str, Any] | None, dict[str, Any] | None]":
    """Resolve the credential, open one client, run ``action`` and close the client.

    Returns ``(source kind, body, error)``; exactly one of body and error is set.
    Blocking, so it runs on a worker thread, never on the event loop. Every failure
    becomes a fixed-message error here; nothing is raised to the caller.
    """
    kind: str | None = None
    client: RunPodClient | None = None
    try:
        credential, kind, error = _resolve(credential_ref)
        if error is not None or credential is None:
            return kind, None, error
        try:
            client = RunPodClient(credential.secret)
        except ValueError:
            # The client's message names the rule but not the value; fixed text is used anyway.
            return kind, None, _error("invalid_credential", _INVALID_CREDENTIAL_MESSAGE)
        return kind, action(client), None
    except RunPodAPIError as exc:
        return kind, None, _api_error(exc)
    except Exception as exc:  # noqa: BLE001 -- last resort; only the exception type is reported
        return kind, None, _error(
            "unexpected_error",
            f"Unexpected {type(exc).__name__} while calling RunPod. Details were withheld.",
        )
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 -- a failed close must not replace the result
                pass


def _scalar(value: Any) -> Any:
    """Keep a JSON scalar from a pod field, capped in length. Anything else is dropped.
    NaN and the infinities are dropped too: a JSON body can carry them, but they have no
    JSON form, so a response holding one could not be serialized."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:_TEXT_CAP]
    return None


def _pod_page(page: Any) -> "tuple[list[dict[str, Any]], bool]":
    """Return ``(pod objects on this page, has_more_pods)``. A page with no pods
    array is an invalid response, not an empty account."""
    pods = page.get("pods") if isinstance(page, dict) else None
    if not isinstance(pods, list):
        raise RunPodAPIError(None, "INVALID_RESPONSE", "list_pods response has no pods array")
    pagination = page.get("pagination")
    has_more = isinstance(pagination, dict) and pagination.get("hasNextPage") is True
    return [pod for pod in pods if isinstance(pod, dict)], has_more


def _pod_summary(pod: dict[str, Any]) -> dict[str, Any]:
    """A summary of one pod, read from the v2 spec's Pod object. Every key is always
    present, and a value the pod does not supply is null:

    * ``id``, ``name``: Pod.id and Pod.name.
    * ``status``: Pod.status, one of PROVISIONING, STARTING, RUNNING, EXITED, ERROR,
      TERMINATED.
    * ``gpu``: Pod.gpu as ``{"id", "count"}``. The spec omits Pod.gpu for CPU pods,
      so a CPU pod gets null.
    * ``costPerHour``: Pod.cost, the current cost in USD per hour (0.0 when EXITED
      or TERMINATED).
    * ``uptimeSeconds``: Pod.runtime.uptime, seconds since the container started.
      Pod.runtime is null unless the pod is RUNNING, so this is null otherwise too.

    env, ports and other raw fields are never copied."""
    gpu = pod.get("gpu")
    runtime = pod.get("runtime")
    uptime = runtime.get("uptime") if isinstance(runtime, dict) and pod.get("status") == "RUNNING" else None
    uptime_seconds = (
        _scalar(uptime) if isinstance(uptime, (int, float)) and not isinstance(uptime, bool) else None
    )
    return {
        "id": _scalar(pod.get("id")),
        "name": _scalar(pod.get("name")),
        "status": _scalar(pod.get("status")),
        "gpu": (
            {"id": _scalar(gpu.get("id")), "count": _scalar(gpu.get("count"))}
            if isinstance(gpu, dict)
            else None
        ),
        "costPerHour": _scalar(pod.get("cost")),
        "uptimeSeconds": uptime_seconds,
    }


def _check_action(client: RunPodClient) -> dict[str, Any]:
    """One billing call, then one pod-listing call. A billing failure only sets
    billing_available to false; the pod listing decides ok."""
    billing_available = True
    try:
        client.account_billing()
    except RunPodAPIError:
        billing_available = False
    pods, _has_more = _pod_page(client.list_pods(limit=_POD_PAGE_LIMIT))
    return {"pod_count": len(pods), "billing_available": billing_available}


def _list_action(client: RunPodClient) -> dict[str, Any]:
    pods, has_more = _pod_page(client.list_pods(limit=_POD_PAGE_LIMIT))
    return {"pods": [_pod_summary(pod) for pod in pods], "has_more_pods": has_more}


def _check_payload(kind: "str | None", error: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": False,
        "credential_source": kind,
        "pod_count": 0,
        "billing_available": False,
        "error": error,
    }


def _list_payload(kind: "str | None", error: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": False,
        "credential_source": kind,
        "pods": [],
        "pod_count": 0,
        "has_more_pods": False,
        "error": error,
    }


_HOSTED_ERROR_CODE = "hosted_refused"


async def handle_runpod_check_key(args: dict[str, Any]) -> dict[str, Any]:
    """MCP tool: runpod_check_key. Confirms a RunPod key works with one billing call
    and one pod-listing call.

    ``ok`` means the pod listing, which requires an authenticated call, succeeded.
    ``billing_available`` says whether the billing call also succeeded. ``pod_count``
    counts the first page of pods (up to 1000). Optional ``credential_ref`` selects
    one reference; omitted, :data:`DEFAULT_CREDENTIAL_REFS` is tried in order.
    """
    if _hosted_mode():
        return _check_payload(None, _error(_HOSTED_ERROR_CODE, _HOSTED_MESSAGE))
    kind, body, error = await asyncio.to_thread(_run, args.get("credential_ref"), _check_action)
    if error is not None or body is None:
        return _check_payload(kind, error or _error("unexpected_error", "No result was produced."))
    return {
        "ok": True,
        "credential_source": kind,
        "pod_count": body["pod_count"],
        "billing_available": body["billing_available"],
        "error": None,
    }


async def handle_runpod_list_pods(args: dict[str, Any]) -> dict[str, Any]:
    """MCP tool: runpod_list_pods. Lists one page (up to 1000) of the account's pods
    as summaries. ``has_more_pods`` is true when RunPod reports more. Reads only."""
    if _hosted_mode():
        return _list_payload(None, _error(_HOSTED_ERROR_CODE, _HOSTED_MESSAGE))
    kind, body, error = await asyncio.to_thread(_run, args.get("credential_ref"), _list_action)
    if error is not None or body is None:
        return _list_payload(kind, error or _error("unexpected_error", "No result was produced."))
    pods: list[dict[str, Any]] = body["pods"]
    return {
        "ok": True,
        "credential_source": kind,
        "pods": pods,
        "pod_count": len(pods),
        "has_more_pods": body["has_more_pods"],
        "error": None,
    }
