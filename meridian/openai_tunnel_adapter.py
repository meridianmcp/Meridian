"""Optional OpenAI Secure MCP Tunnel transport adapter (45049071).

Meridian's own permanent tunnel (``meridian --tunnel`` -> ``routes/tunnel.py``)
is the primary, hosted transport that lets Claude/Cursor/other local clients
reach a private local MCP server. OpenAI publishes a SEPARATE mechanism —
the "Secure MCP Tunnel" — that lets ChatGPT, Codex, and Responses API
workflows reach a private local/stdio MCP server through OpenAI's own
infrastructure instead of Meridian's relay.

This module is an OPTIONAL, ADDITIVE adapter: it lets a project describe and
diagnose an OpenAI Secure MCP Tunnel configuration using the exact same
shape Meridian already uses for its own tunnel diagnostics (see
``tunnel_client.SlotDiagnostics`` / ``routes/tunnel.py``'s ``tunnel_status``),
WITHOUT replacing or hard-depending on Meridian's existing transport and
WITHOUT ever holding a live connection or credential itself. Concretely, in
this module/item:

* No process is spawned, no socket is opened, no OpenAI API is called.
* Config is validated/normalized only (:func:`normalize_config`) — pure,
  no I/O — mirroring ``meridian.capability_manifest``'s own schema-first
  design (deterministic rejection, never partial normalization).
* State/health (:class:`OpenAITunnelState`, :func:`build_diagnostics`) is
  derived ONLY from the config plus an optional caller-supplied
  ``reported_status`` snapshot — never probed live by this module. A future
  item can wire live probing by passing ``reported_status`` from wherever
  that probe actually runs (mirrors ``capability_contract``'s injectable
  ``availability_checker`` seam).
* :data:`OPENAI_TUNNEL_CAPABILITY_ID` / :func:`default_capability_entry` let
  a project opt in through the EXISTING, generic
  ``meridian.capability_manifest`` / ``set_capability_manifest`` mechanism
  (required/optional/degraded_ok + fallback_chain) — no new opt-in
  mechanism, no special-casing added to ``capability_manifest.py`` or
  ``capability_contract.py`` themselves (both already handle any declared
  capability id generically; see ``meridian.code_intel_receipt`` for the
  established precedent of a feature module layering on top of the generic
  manifest schema without modifying it).

Secret handling: :func:`normalize_config` screens every string field for a
secret-shaped value (API key / bearer token / password — reusing
``capability_manifest``'s own regex, never a weaker duplicate) so a raw
``tunnel_id`` or credential can never be embedded directly in this config —
keep it external (an env var reference, a locally-resolved secret store
lookup) per the item's "keep tunnel_id and runtime credentials external and
secret-safe" requirement. Unlike ``capability_manifest`` (project-shared,
multi-machine DB state), this adapter's raw runtime config
(:func:`normalize_config`) is expected to be LOCAL-machine-only (e.g. read
from an env var by ``tunnel_client.py``, or supplied per-request to the
diagnostics route by a caller that already has it) — so, deliberately unlike
``capability_manifest``, an absolute local command path is NOT rejected here
(a local stdio launcher command is completely normal and machine-specific;
see :func:`_check_no_embedded_secrets`). Only :func:`default_capability_entry`
touches actual shared multi-machine state, and it does so by delegating to
``capability_manifest.normalize_capability`` unchanged, which keeps applying
its own full secret-AND-path screen.

See ``docs/secure-openai-mcp-tunnel-adapter.md`` for the end-to-end contract
this module implements, current scope boundaries, and the explicit follow-up
list (persistence, live health probing, real process spawn).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from . import capability_manifest as _cm
from .tunnel_plugins import _coerce_command

#: Well-known capability id a project's manifest opts in with (see
#: set_capability_manifest / meridian.capability_manifest). Absent from a
#: project's manifest -> this whole adapter is simply never surfaced for
#: that project; nothing about Meridian's own tunnel changes either way.
OPENAI_TUNNEL_CAPABILITY_ID = "openai_secure_mcp_tunnel"

VALID_TRANSPORTS = frozenset({"stdio", "http"})
VALID_APPROVAL_POLICIES = frozenset({"always_ask", "auto_approve_allowlisted", "never"})


class OpenAITunnelAdapterError(ValueError):
    """Raised when an OpenAI Secure MCP Tunnel adapter config fails validation."""


class OpenAITunnelState(str, Enum):
    """Lifecycle states for the OPTIONAL OpenAI Secure MCP Tunnel adapter.

    Deliberately a SEPARATE enum from ``meridian.tunnel_client.SlotState``:
    this adapter's lifecycle tracks OpenAI's own tunnel concept, not a
    Meridian connector slot, and the two must never be conflated by a
    diagnostics consumer — see :func:`combined_diagnostics`'s explicit
    ``meridian_tunnel`` / ``openai_tunnel`` split. String-valued for the same
    reason ``SlotState`` is: it serializes as-is into a diagnostics dict with
    no extra mapping step.
    """

    NOT_CONFIGURED = "not_configured"  # no config, or config present but disabled
    CONFIGURED = "configured"          # config present + valid; never (yet) health-checked
    CONNECTING = "connecting"          # only reachable via an injected reported_status
    CONNECTED = "connected"            # only reachable via an injected reported_status
    DEGRADED = "degraded"              # only reachable via an injected reported_status
    DISCONNECTED = "disconnected"      # only reachable via an injected reported_status
    ERROR = "error"                    # invalid config, or an unrecognized reported_status


_VALID_STATE_VALUES = frozenset(s.value for s in OpenAITunnelState)


def _check_no_embedded_secrets(value: Any, *, path: str) -> None:
    """Reject secret-shaped strings (API keys / bearer tokens / passwords)
    anywhere in *value*.

    Deliberately narrower than
    ``capability_manifest._check_no_secrets_or_local_paths``: this adapter's
    raw runtime config is local-machine config (a local stdio launcher
    command, a local/loopback HTTP URL), not ``capability_manifest``'s
    project-shared, multi-machine manifest state — an absolute local command
    path is completely normal here and must not be rejected the way a
    shared manifest field would be (see module docstring). Reuses
    ``capability_manifest``'s own secret-shaped regex directly (never
    duplicates or weakens it), the same "reach into a sibling module's
    private validation rather than re-implement it" pattern
    ``capability_contract.py`` already establishes for this exact regex.
    """
    if isinstance(value, str):
        if _cm._SECRET_LIKE_RE.search(value):
            raise OpenAITunnelAdapterError(
                f"{path}: secret-shaped value not allowed in adapter config -- "
                "keep tunnel_id/credentials external (e.g. an env var reference), "
                "never embedded directly"
            )
    elif isinstance(value, dict):
        for key, sub in value.items():
            _check_no_embedded_secrets(sub, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for idx, sub in enumerate(value):
            _check_no_embedded_secrets(sub, path=f"{path}[{idx}]")


def _normalize_optional_str(raw: Any, *, field_name: str) -> "str | None":
    """A ``str | None`` field: ``None``/absent stays ``None``; anything else
    must be a non-empty (post-strip) string."""
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise OpenAITunnelAdapterError(f"{field_name} must be a non-empty string or null")
    return raw.strip()


def normalize_config(raw: "dict[str, Any] | None") -> dict[str, Any]:
    """Validate and normalize a raw OpenAI Secure MCP Tunnel adapter config.

    Pure — no I/O, no network, no process. Raises
    :class:`OpenAITunnelAdapterError` on any schema/safety violation
    (deterministic rejection, never partial normalization — mirrors
    ``capability_manifest.normalize_capability``'s own contract). ``None`` or
    ``{}`` normalizes to the fully-disabled default rather than raising, so
    "adapter not configured" is always a valid, cheap input.

    Fields (all optional unless ``enabled`` is true):

    * ``enabled`` (bool, default False) — whether this adapter is active.
    * ``tunnel_id`` (str | None) — an OPAQUE external reference the human
      configured out-of-band with OpenAI; screened for secret-shaped values
      (see module docstring) but otherwise passed through unchanged.
    * ``transport`` ("stdio" | "http") — required when ``enabled``.
    * ``command`` (list[str] | str, stdio only) — coerced via
      ``tunnel_plugins._coerce_command``; required when ``enabled`` and
      ``transport == "stdio"``.
    * ``url`` (str, http only) — must be an ``http://``/``https://`` URL;
      required when ``enabled`` and ``transport == "http"``.
    * ``allowed_tools`` (list[str], default ``[]``) — explicit allowlist;
      secure-by-default (empty means NO tools are allowed, never "all").
      Deduplicated, order preserved.
    * ``approval_policy`` (one of :data:`VALID_APPROVAL_POLICIES`, default
      ``"always_ask"``).
    * ``tenant_id`` / ``project_id`` (str | None) — Meridian-side scope this
      adapter instance is associated with, for diagnostics only.
    * ``env`` (dict[str, str] | None, stdio only) — spawn environment
      overrides; screened the same as every other field.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise OpenAITunnelAdapterError(
            "openai tunnel adapter config must be an object or null"
        )

    enabled = bool(raw.get("enabled", False))

    transport = raw.get("transport")
    if transport is not None:
        if not isinstance(transport, str) or transport.strip().lower() not in VALID_TRANSPORTS:
            raise OpenAITunnelAdapterError(
                f"transport must be one of {sorted(VALID_TRANSPORTS)} or null"
            )
        transport = transport.strip().lower()

    command = _coerce_command(raw.get("command"))

    url = raw.get("url")
    if url is not None:
        if not isinstance(url, str) or not url.strip():
            raise OpenAITunnelAdapterError("url must be a non-empty string or null")
        url = url.strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            raise OpenAITunnelAdapterError("url must be an http:// or https:// URL")

    if enabled:
        if transport is None:
            raise OpenAITunnelAdapterError("transport is required when enabled=true")
        if transport == "stdio" and not command:
            raise OpenAITunnelAdapterError(
                "command is required for stdio transport when enabled=true"
            )
        if transport == "http" and not url:
            raise OpenAITunnelAdapterError(
                "url is required for http transport when enabled=true"
            )

    allowed_tools_raw = raw.get("allowed_tools")
    if allowed_tools_raw is None:
        allowed_tools: list[str] = []
    elif isinstance(allowed_tools_raw, list) and all(
        isinstance(t, str) and t.strip() for t in allowed_tools_raw
    ):
        seen: set[str] = set()
        allowed_tools = []
        for t in allowed_tools_raw:
            tt = t.strip()
            if tt not in seen:
                seen.add(tt)
                allowed_tools.append(tt)
    else:
        raise OpenAITunnelAdapterError(
            "allowed_tools must be a list of non-empty strings"
        )

    approval_policy = raw.get("approval_policy") or "always_ask"
    if (
        not isinstance(approval_policy, str)
        or approval_policy.strip().lower() not in VALID_APPROVAL_POLICIES
    ):
        raise OpenAITunnelAdapterError(
            f"approval_policy must be one of {sorted(VALID_APPROVAL_POLICIES)}"
        )
    approval_policy = approval_policy.strip().lower()

    tenant_id = _normalize_optional_str(raw.get("tenant_id"), field_name="tenant_id")
    project_id = _normalize_optional_str(raw.get("project_id"), field_name="project_id")
    tunnel_id = _normalize_optional_str(raw.get("tunnel_id"), field_name="tunnel_id")

    env_raw = raw.get("env")
    env: "dict[str, str] | None" = None
    if isinstance(env_raw, dict):
        coerced = {str(k): str(v) for k, v in env_raw.items() if str(k).strip()}
        env = coerced or None
    elif env_raw is not None:
        raise OpenAITunnelAdapterError("env must be an object of string->string or null")

    normalized: dict[str, Any] = {
        "enabled": enabled,
        "tunnel_id": tunnel_id,
        "transport": transport,
        "command": command,
        "url": url,
        "allowed_tools": allowed_tools,
        "approval_policy": approval_policy,
        "tenant_id": tenant_id,
        "project_id": project_id,
        "env": env,
    }
    _check_no_embedded_secrets(normalized, path="openai_tunnel_adapter.config")
    return normalized


def default_capability_entry(**overrides: Any) -> dict[str, Any]:
    """A ready-to-use ``capability_manifest`` entry template for opting a
    project into the OpenAI Secure MCP Tunnel adapter via the EXISTING,
    generic ``set_capability_manifest`` mechanism.

    Deliberately ``availability_policy="optional"`` and a ``fallback_chain``
    naming Meridian's own tunnel: this adapter must never make a project
    non-executable when unavailable — Meridian's own transport is always the
    working fallback (the item's "do not replace or hard-depend on
    Meridian's existing transport" requirement). Passes straight through
    ``capability_manifest.normalize_capability`` (never re-implements its
    validation), so the result carries that function's FULL guarantees
    (schema-valid, no secrets, no machine-local absolute paths — the shared,
    multi-machine-safe check; contrast with :func:`normalize_config`'s
    narrower local-only screen, see module docstring).

    ``overrides`` lets a caller adjust any field (e.g. a stricter
    ``availability_policy``) before normalization; unknown keys still raise
    via the same schema check.
    """
    entry: dict[str, Any] = {
        "id": OPENAI_TUNNEL_CAPABILITY_ID,
        "purpose": (
            "Optional OpenAI Secure MCP Tunnel transport for ChatGPT/Codex/"
            "Responses API clients, alongside Meridian's own tunnel"
        ),
        "required_tools": ["openai_secure_mcp_tunnel"],
        "fallback_chain": ["meridian_tunnel"],
        "availability_policy": "optional",
    }
    entry.update(overrides)
    return _cm.normalize_capability(entry)


def resolve_state(
    config: dict[str, Any], *, reported_status: "dict[str, Any] | None" = None,
) -> "tuple[OpenAITunnelState, str | None]":
    """Derive ``(state, detail)`` for an already-normalized *config*.

    Never probes anything live. When *reported_status* is omitted, the
    result can only ever be ``NOT_CONFIGURED`` (disabled/absent config) or
    ``CONFIGURED`` (valid config, no live signal wired) — by construction,
    since nothing else has told this function otherwise. Passing
    *reported_status* (``{"state": ..., "detail": ...}``) is the injectable
    seam a future live-health integration uses, mirroring
    ``capability_contract``'s own ``availability_checker`` pattern: an
    unrecognized ``state`` value degrades to ``ERROR`` rather than silently
    passing through, so a malformed/foreign status payload can never spoof a
    state this module doesn't know about.
    """
    if not config or not config.get("enabled"):
        return OpenAITunnelState.NOT_CONFIGURED, "adapter is not configured or is disabled"
    if reported_status is not None:
        raw_state = reported_status.get("state") if isinstance(reported_status, dict) else None
        if isinstance(raw_state, str) and raw_state in _VALID_STATE_VALUES:
            return OpenAITunnelState(raw_state), reported_status.get("detail")
        return (
            OpenAITunnelState.ERROR,
            "reported_status carried an unrecognized or missing state",
        )
    return (
        OpenAITunnelState.CONFIGURED,
        "config present and valid; no live health probe wired for this adapter yet",
    )


@dataclass
class OpenAITunnelDiagnostics:
    """Machine-readable diagnostics for the OpenAI Secure MCP Tunnel adapter.

    Mirrors the SHAPE of ``tunnel_client.SlotDiagnostics.to_dict()`` (state +
    detail + a handful of scope fields) so a consumer already familiar with
    Meridian's own connector diagnostics recognizes this immediately as the
    same kind of object, even though the two are intentionally separate
    types (see :class:`OpenAITunnelState`'s docstring).
    """

    state: OpenAITunnelState = OpenAITunnelState.NOT_CONFIGURED
    detail: "str | None" = None
    transport: "str | None" = None
    tenant_id: "str | None" = None
    project_id: "str | None" = None
    allowed_tool_count: int = 0
    approval_policy: "str | None" = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe snapshot for a diagnostics route / dashboard card."""
        return {
            "state": self.state.value,
            "detail": self.detail,
            "transport": self.transport,
            "tenant_id": self.tenant_id,
            "project_id": self.project_id,
            "allowed_tool_count": self.allowed_tool_count,
            "approval_policy": self.approval_policy,
        }


def build_diagnostics(
    config: "dict[str, Any] | None",
    *,
    reported_status: "dict[str, Any] | None" = None,
) -> OpenAITunnelDiagnostics:
    """Normalize *config* and build its :class:`OpenAITunnelDiagnostics`.

    Raises :class:`OpenAITunnelAdapterError` iff *config* itself is invalid
    (never for an unrecognized *reported_status*, which degrades to an
    ``ERROR`` state entry instead — see :func:`resolve_state`) so a caller
    can distinguish "the config I was given is broken" (worth a 400) from
    "the adapter's live status looks bad" (a normal diagnostics response).
    """
    normalized = normalize_config(config)
    state, detail = resolve_state(normalized, reported_status=reported_status)
    return OpenAITunnelDiagnostics(
        state=state,
        detail=detail,
        transport=normalized.get("transport"),
        tenant_id=normalized.get("tenant_id"),
        project_id=normalized.get("project_id"),
        allowed_tool_count=len(normalized.get("allowed_tools") or []),
        approval_policy=normalized.get("approval_policy"),
    )


def combined_diagnostics(
    tenant_id: str,
    *,
    openai_config: "dict[str, Any] | None" = None,
    reported_status: "dict[str, Any] | None" = None,
    meridian_tunnel_active: "bool | None" = None,
) -> dict[str, Any]:
    """Compose ONE diagnostics payload covering BOTH transports for
    *tenant_id*, explicitly namespaced (``meridian_tunnel`` /
    ``openai_tunnel``) so a caller can never conflate the two — the item's
    "distinguish OpenAI tunnel state from Meridian tunnel state"
    requirement. ``meridian_tunnel_active`` is the caller's own
    already-known Meridian tunnel signal (e.g. ``tenant_id in
    _tunnel_sockets`` in ``routes/tunnel.py``) — this module has no way to
    know it itself and never guesses.
    """
    openai_diag = build_diagnostics(openai_config, reported_status=reported_status)
    return {
        "tenant_id": tenant_id,
        "meridian_tunnel": {"active": meridian_tunnel_active},
        "openai_tunnel": openai_diag.to_dict(),
    }
