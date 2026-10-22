"""RunPod v2 REST client (synchronous, httpx) for the RunPod research adapter
(:mod:`meridian.research.providers.runpod`).

Wire surface, paths and field names follow the RunPod v2 API as documented
at docs.runpod.io/api-reference-v2 (overview and v1-to-v2 migration pages) and
the v2 OpenAPI document. Where that spec does not confirm a wire shape this
client does NOT guess one: the call raises ``NotImplementedError`` from a
clearly named helper (see :func:`_unconfirmed_query_param`), so the gap is
visible at the call site instead of producing a request RunPod might misread.

AUTH: ``Authorization: Bearer <api key>``. The key is held only as the httpx
client's default header and in a private attribute used to scrub error text.
It never appears in a URL, a query string, a request body, or in an exception
message or exception attribute.

ERRORS: every non-2xx response and every transport failure (any
``httpx.HTTPError``) is raised as :class:`RunPodAPIError`. Its message is built
from the RFC 9457 problem body's ``detail`` (or ``title``), passed through
:func:`meridian.secret_redaction.redact`, with the literal API key removed
first, so a server that echoes the key cannot leak it. Exceptions outside
``httpx.HTTPError`` are not wrapped and propagate unchanged: the client's own
``ValueError`` and ``NotImplementedError`` checks, which run before any request,
and httpx errors such as ``httpx.InvalidURL``, which is not an ``HTTPError``
subclass. None of them is built from request headers, so none carries the key.
This client never retries: retry and backoff belong to the caller
(:mod:`meridian.research.scheduler`).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, NoReturn
from urllib.parse import quote

import httpx

from meridian.exception_chain import raise_unchained
from meridian.secret_redaction import redact

#: RunPod v2 REST base. Full paths are e.g. https://api.runpod.io/v2/pods.
#: The serverless job-run API is a different host and is not covered here.
DEFAULT_BASE_URL = "https://api.runpod.io/v2"
DEFAULT_TIMEOUT_SECONDS = 30.0
#: Upper bound on bytes :meth:`RunPodClient.pod_logs` reads from the SSE stream.
DEFAULT_LOG_MAX_BYTES = 256 * 1024

_LOG_SOURCES = frozenset({"container", "system"})
_MAX_ERROR_CHARS = 500
_KEY_REDACTION_LABEL = "[REDACTED:runpod_api_key]"


class RunPodAPIError(Exception):
    """A failed RunPod API call.

    ``status_code`` is the HTTP status, or ``None`` for a transport failure
    (no response was received). ``code`` is the RFC 9457 problem ``title``
    when present, else ``HTTP_<status>``. ``message`` is the problem ``detail``
    (or a generic label), redacted; it never contains the API key or the
    ``Authorization`` header. ``retry_after`` is the ``Retry-After`` header in
    seconds when the server sent a numeric value, else ``None``.
    """

    def __init__(
        self,
        status_code: "int | None",
        code: str,
        message: str,
        *,
        retry_after: "float | None" = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retry_after = retry_after
        status_text = "transport" if status_code is None else f"HTTP {status_code}"
        super().__init__(f"runpod {status_text} ({code}): {message}")


def _unconfirmed_query_param(param: str) -> NoReturn:
    """Raised instead of sending a query parameter this client does not map yet.
    The v2 spec declares the parameter, but nothing is sent."""
    raise NotImplementedError(
        f"RunPod v2 query parameter {param!r} is declared by the v2 spec but is "
        "not yet mapped by this client; not sent"
    )


def _pod_path(pod_id: str) -> str:
    if not isinstance(pod_id, str) or not pod_id.strip():
        raise ValueError("pod_id must be a non-empty string")
    # "." and ".." survive percent-quoting and would resolve to /v2 or /v2/pods.
    # "/", "?" and "#" would start a new path segment, query or fragment.
    if pod_id in (".", "..") or any(ch in pod_id for ch in "/?#"):
        raise ValueError("pod_id must not be '.' or '..' or contain '/', '?' or '#'")
    # Quoted so every other character in a caller-supplied id stays in one segment.
    return f"pods/{quote(pod_id, safe='')}"


def _parse_retry_after(value: "str | None") -> "float | None":
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None  # HTTP-date form is not interpreted
    return seconds if seconds >= 0 else None


def build_create_pod_body(
    *,
    name: str,
    image_name: str,
    env: "Mapping[str, str] | None" = None,
    gpu_type_id: "str | None" = None,
    gpu_count: int = 0,
    command: "Sequence[str] | None" = None,
) -> "dict[str, Any]":
    """Build the POST /v2/pods (CreatePodRequest) body from the fields this
    client confirms: ``name``, ``image``, ``gpu`` (``id`` and ``count``),
    ``env`` and ``cmd``.

    ``cmd`` is sent as an array of strings, matching ContainerConfig's ``cmd``
    in the v2 spec (type array of string, "Container CMD in exec form").
    ``cpu`` (see :meth:`RunPodClient.create_pod`), ``cloud`` (server default
    SECURE), ``disk``, ``mounts``, ``ports`` and ``templateId`` are not sent.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("create_pod requires a non-empty name")
    if not image_name:
        raise ValueError(
            "create_pod requires image_name: the v2 spec requires image unless "
            "templateId is set, and templateId is not supported here"
        )
    if gpu_count < 0:
        raise ValueError("gpu_count must be >= 0")
    if gpu_count > 0 and not gpu_type_id:
        raise ValueError("gpu_count > 0 requires gpu_type_id (the v2 gpu object requires id)")
    if gpu_count == 0 and gpu_type_id:
        raise ValueError("gpu_type_id requires gpu_count >= 1")
    if gpu_count == 0 and not gpu_type_id:
        # The spec requires exactly one of gpu or cpu. CPU pods are not mapped
        # yet, so refuse here rather than send a body with neither.
        raise ValueError(
            "create requires a GPU (gpu_type_id and gpu_count >= 1); CPU pods are not mapped yet"
        )

    body: dict[str, Any] = {"name": name, "image": image_name}
    if gpu_type_id:
        body["gpu"] = {"id": gpu_type_id, "count": gpu_count}
    if env:
        body["env"] = {str(key): str(value) for key, value in env.items()}
    if command:
        body["cmd"] = [str(part) for part in command]
    return body


class RunPodClient:
    """Synchronous RunPod v2 client. Satisfies
    :class:`meridian.research.providers.runpod.RunPodClientProtocol`, so it can
    be injected into :class:`RunPodProvider` directly.

    ``transport`` is an optional httpx transport (tests pass
    ``httpx.MockTransport``); when omitted, httpx's default network transport
    is used.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: "httpx.BaseTransport | None" = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        # Whitespace or control characters would make httpx reject the header,
        # and httpx's own error text would then echo the key. Non-ASCII characters
        # make httpx raise UnicodeEncodeError, which names the character and its
        # index. Refuse all of these up front, without echoing the key.
        if not isinstance(api_key, str) or not api_key or not api_key.isascii() or any(
            ch.isspace() or not ch.isprintable() for ch in api_key
        ):
            raise ValueError("RunPodClient requires a non-empty api_key of printable ASCII with no whitespace")
        self._api_key = api_key
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            transport=transport,
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "RunPodClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Pods
    # ------------------------------------------------------------------

    def list_pods(
        self,
        *,
        cursor: "str | None" = None,
        limit: "int | None" = None,
        include_cluster_pods: "bool | None" = None,
    ) -> "dict[str, Any]":
        """GET /v2/pods (operationId listPods). Returns the raw page:
        ``pods`` plus ``pagination`` (``nextCursor``, ``hasNextPage``).

        The spec declares ``includeClusterPods`` on listPods. This client does
        not map it yet, so passing ``include_cluster_pods`` raises
        NotImplementedError and nothing is sent.
        """
        if include_cluster_pods is not None:
            _unconfirmed_query_param("includeClusterPods")
        params: dict[str, Any] = {}
        if cursor is not None:
            params["cursor"] = cursor
        if limit is not None:
            if not 1 <= limit <= 1000:
                raise ValueError("limit must be between 1 and 1000")
            params["limit"] = limit
        return self._json(self._request("GET", "pods", params=params or None))

    def find_pod_by_name(self, name: str) -> "dict[str, Any] | None":
        """Page through :meth:`list_pods` and return the pod whose name equals
        ``name`` exactly, or ``None`` once the listing is exhausted.

        Exact equality only: a substring or prefix match could merge two
        distinct runs. If pagination is inconsistent (``hasNextPage`` is true
        but no new cursor is given, or a cursor repeats), this raises
        :class:`RunPodAPIError` instead of returning ``None``. ``None`` would
        mean "no such pod", which cannot be proven from an incomplete listing.
        """
        cursor: "str | None" = None
        seen_cursors: set[str] = set()
        while True:
            page = self.list_pods(cursor=cursor, limit=1000)
            pods = page.get("pods")
            if not isinstance(pods, list):
                raise RunPodAPIError(None, "INVALID_RESPONSE", "list_pods response has no pods array")
            for pod in pods:
                if isinstance(pod, dict) and pod.get("name") == name:
                    return pod
            pagination = page.get("pagination")
            pagination = pagination if isinstance(pagination, dict) else {}
            if not pagination.get("hasNextPage"):
                return None
            next_cursor = pagination.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                raise RunPodAPIError(
                    None,
                    "PAGINATION_INCONSISTENT",
                    "list_pods pagination is inconsistent; cannot prove no matching pod exists",
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def get_pod(self, pod_id: str) -> "dict[str, Any]":
        """GET /v2/pods/{id} (operationId getPod). Returns the Pod object."""
        return self._json(self._request("GET", _pod_path(pod_id)))

    def create_pod(
        self,
        *,
        name: str,
        image_name: str,
        env: "Mapping[str, str] | None" = None,
        gpu_type_id: "str | None" = None,
        gpu_count: int = 0,
        command: "Sequence[str] | None" = None,
    ) -> "dict[str, Any]":
        """POST /v2/pods (operationId createPod). Returns the created Pod
        (201). Body fields are described in :func:`build_create_pod_body`.

        CPU sizing is not yet mapped. The v2 spec defines a CPU create object
        (``CreateCpuConfig``: ``id``, a flavor from GET /v2/catalog/cpus, and
        ``vcpuCount``), but this client does not resolve the flavor id, so it
        sends no ``cpu``. Memory sizing and templates are not mapped either.
        """
        body = build_create_pod_body(
            name=name,
            image_name=image_name,
            env=env,
            gpu_type_id=gpu_type_id,
            gpu_count=gpu_count,
            command=command,
        )
        return self._json(self._request("POST", "pods", json_body=body))

    def stop_pod(self, pod_id: str) -> "dict[str, Any]":
        """POST /v2/pods/{id}/action with ``{"action": "stop"}`` (operationId
        podAction). Returns the Pod (200). Poll :meth:`get_pod` for the
        settled status."""
        return self._json(
            self._request("POST", f"{_pod_path(pod_id)}/action", json_body={"action": "stop"})
        )

    def terminate_pod(self, pod_id: str) -> "dict[str, Any]":
        """DELETE /v2/pods/{id} (operationId deletePod). The spec describes a
        204 with no body; a single read of the spec supports that, so any 2xx
        is accepted and an empty body returns ``{}``."""
        return self._json(self._request("DELETE", _pod_path(pod_id)))

    def pod_logs(
        self,
        pod_id: str,
        *,
        source: str = "container",
        last_event_id: "str | None" = None,
        tail: "int | None" = None,
        since: "str | None" = None,
        max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    ) -> str:
        """GET /v2/pods/{id}/logs (operationId getPodLogs), a
        ``text/event-stream`` (SSE) response.

        Returns the raw SSE text, read up to ``max_bytes``. Per-event payload
        fields (``source``, ``line``, ``ts``) are not parsed here: their names
        come from a single unverified read of the spec. ``last_event_id`` is
        sent as the ``Last-Event-ID`` header to resume a stream.

        The spec declares ``tail`` (LogTail) and ``since`` (LogSince) on this
        operation. This client does not map them yet, so passing either raises
        NotImplementedError and nothing is sent.
        """
        if source not in _LOG_SOURCES:
            raise ValueError("source must be 'container' or 'system'")
        if tail is not None:
            _unconfirmed_query_param("tail")
        if since is not None:
            _unconfirmed_query_param("since")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        headers = {"Accept": "text/event-stream"}
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        buffer = bytearray()
        try:
            with self._http.stream(
                "GET", f"{_pod_path(pod_id)}/logs", params={"source": source}, headers=headers
            ) as response:
                if not response.is_success:
                    response.read()
                    raise self._error_from_response(response)
                for chunk in response.iter_bytes():
                    buffer.extend(chunk[: max_bytes - len(buffer)])
                    if len(buffer) >= max_bytes:
                        break
        except httpx.HTTPError as exc:
            transport_error = self._transport_error(exc)
        else:
            return buffer.decode("utf-8", errors="replace")
        raise_unchained(transport_error)  # outside the except block, so exc is not chained

    # ------------------------------------------------------------------
    # Billing
    # ------------------------------------------------------------------

    def account_billing(self) -> "dict[str, Any]":
        """GET /v2/billing (operationId listBilling): time-bucketed account
        billing, returned raw (``records`` and ``metadata``).

        Amounts are in USD, as the v2 spec states. BillingAmounts.totalAmount is
        "Total cost in USD for the bucket across all resources", and
        podGpuAmount is "GPU pod compute cost in USD for the bucket". The spec
        also says serverless amounts are inclusive of platform charges. The
        BillingAmounts and BillingRecord descriptions do not say whether amounts
        are net of account credits. No totals are computed here.

        The spec declares ``startTime``, ``endTime``, ``bucketSize`` and ``lastN``
        on listBilling. Per the spec, ``startTime`` defaults to 30 days ago,
        ``endTime`` to now and ``bucketSize`` to day. This client does not map
        them yet, so no window is sent and the server's defaults apply.
        """
        return self._json(self._request("GET", "billing"))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _scrub(self, text: str) -> str:
        cleaned = text.replace(self._api_key, _KEY_REDACTION_LABEL)
        return redact(cleaned)[:_MAX_ERROR_CHARS]

    def _transport_error(self, exc: httpx.HTTPError) -> RunPodAPIError:
        return RunPodAPIError(None, "TRANSPORT_ERROR", self._scrub(f"{type(exc).__name__}: {exc}"))

    def _error_from_response(self, response: httpx.Response) -> RunPodAPIError:
        status = response.status_code
        title: "str | None" = None
        detail: "str | None" = None
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            if isinstance(body.get("title"), str):
                title = body["title"]
            if isinstance(body.get("detail"), str):
                detail = body["detail"]
        code = self._scrub(title) if title else f"HTTP_{status}"
        message = self._scrub(detail or title or f"HTTP {status} with no problem detail")
        return RunPodAPIError(
            status,
            code,
            message,
            retry_after=_parse_retry_after(response.headers.get("Retry-After")),
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: "dict[str, Any] | None" = None,
        json_body: "dict[str, Any] | None" = None,
    ) -> httpx.Response:
        try:
            response = self._http.request(method, path, params=params, json=json_body)
        except httpx.HTTPError as exc:
            transport_error = self._transport_error(exc)
        else:
            if not response.is_success:
                raise self._error_from_response(response)
            return response
        raise_unchained(transport_error)  # outside the except block, so exc is not chained

    @staticmethod
    def _json(response: httpx.Response) -> "dict[str, Any]":
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError:
            # Raised outside the except block: JSONDecodeError.doc holds the whole body.
            not_json = RunPodAPIError(response.status_code, "INVALID_RESPONSE", "response body is not JSON")
        else:
            if not isinstance(data, dict):
                raise RunPodAPIError(response.status_code, "INVALID_RESPONSE", "response JSON is not an object")
            return data
        raise_unchained(not_json)
