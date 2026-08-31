"""Small, declarative render-policy contract for document workflows.

The policy is deliberately independent from :mod:`render_gate`: callers can
choose and persist the level of checking they want without changing the
existing render-capability API.  The default is the safe draft-iteration
level, which validates structure but does not require any visual work.

Policies are JSON-safe dictionaries with explicit flags.  A policy name is
not used to infer an omitted flag after normalization; every normalized
policy contains the complete flag set.  Structural validation is a hard
invariant for every policy, including policies loaded from JSON.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping


class RenderPolicyError(ValueError):
    """Raised when a render policy is unknown, incomplete, or inconsistent."""


STRUCTURAL = "structural"
TARGETED = "targeted"
PUBLICATION = "publication"
RELEASE = "release"
POLICY_NAMES = (STRUCTURAL, TARGETED, PUBLICATION, RELEASE)

_VERSION = "1"
_FLAG_NAMES = (
    "structural_validation",
    "render",
    "raster",
    "tms",
    "require_visual_render",
)
_SCHEMA_KEYS = ("version", "policy", *_FLAG_NAMES)


# Keep the presets as plain JSON-safe data.  The public normalizer returns a
# copy so callers cannot mutate the module's defaults for later requests.
RENDER_POLICIES: dict[str, dict[str, Any]] = {
    STRUCTURAL: {
        "version": _VERSION,
        "policy": STRUCTURAL,
        "structural_validation": True,
        "render": False,
        "raster": False,
        "tms": False,
        "require_visual_render": False,
    },
    TARGETED: {
        "version": _VERSION,
        "policy": TARGETED,
        "structural_validation": True,
        "render": True,
        "raster": False,
        "tms": False,
        "require_visual_render": False,
    },
    PUBLICATION: {
        "version": _VERSION,
        "policy": PUBLICATION,
        "structural_validation": True,
        "render": True,
        "raster": True,
        "tms": True,
        "require_visual_render": False,
    },
    RELEASE: {
        "version": _VERSION,
        "policy": RELEASE,
        "structural_validation": True,
        "render": True,
        "raster": True,
        "tms": True,
        "require_visual_render": True,
    },
}

DEFAULT_RENDER_POLICY: dict[str, Any] = deepcopy(RENDER_POLICIES[STRUCTURAL])
_ACTIVE_RENDER_POLICY: ContextVar["RenderPolicy | None"] = ContextVar(
    "meridian_docs_active_render_policy", default=None
)


def _invalid(message: str) -> RenderPolicyError:
    return RenderPolicyError(f"invalid render policy: {message}")


def _validate_mapping(policy: Mapping[str, Any]) -> dict[str, Any]:
    non_string_keys = [key for key in policy if not isinstance(key, str)]
    if non_string_keys:
        raise _invalid("field names must be strings")
    unknown = set(policy) - set(_SCHEMA_KEYS)
    if unknown:
        raise _invalid(f"unknown field(s): {', '.join(sorted(unknown))}")

    version = policy.get("version", _VERSION)
    if isinstance(version, bool) or not isinstance(version, (str, int)) or not str(version).strip():
        raise _invalid("version must be a non-empty string or integer")
    version = str(version).strip()
    if version != _VERSION:
        raise _invalid(f"unsupported version: {version!r}")

    name = policy.get("policy", STRUCTURAL)
    if not isinstance(name, str) or name not in POLICY_NAMES:
        raise _invalid(f"policy must be one of {', '.join(POLICY_NAMES)}")

    expected = RENDER_POLICIES[name]
    normalized = deepcopy(expected)
    normalized["version"] = version
    for key in _FLAG_NAMES:
        if key not in policy:
            continue
        value = policy[key]
        if not isinstance(value, bool):
            raise _invalid(f"{key} must be boolean")
        if value != expected[key]:
            raise _invalid(f"{name} policy requires {key}={expected[key]!r}")
        normalized[key] = value

    # This is intentionally a separate invariant rather than merely a
    # property of the presets: it must also hold for every deserialized input.
    if normalized["structural_validation"] is not True:
        raise _invalid("structural_validation must always be true")
    if name == RELEASE and normalized["require_visual_render"] is not True:
        raise _invalid("release policy requires visual render")
    return normalized


@dataclass(frozen=True, slots=True)
class RenderPolicy:
    """Validated, immutable representation of one render-policy preset."""

    policy: str = STRUCTURAL
    structural_validation: bool = True
    render: bool = False
    raster: bool = False
    tms: bool = False
    require_visual_render: bool = False
    version: str = _VERSION

    def __post_init__(self) -> None:
        _validate_mapping(
            {
                "version": self.version,
                "policy": self.policy,
                "structural_validation": self.structural_validation,
                "render": self.render,
                "raster": self.raster,
                "tms": self.tms,
                "require_visual_render": self.require_visual_render,
            }
        )

    @classmethod
    def from_value(cls, value: "RenderPolicy | str | Mapping[str, Any] | None" = None) -> "RenderPolicy":
        normalized = normalize_render_policy(value)
        return cls(**normalized)

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON-compatible representation."""

        return {
            "version": self.version,
            "policy": self.policy,
            "structural_validation": self.structural_validation,
            "render": self.render,
            "raster": self.raster,
            "tms": self.tms,
            "require_visual_render": self.require_visual_render,
        }

    def to_json(self) -> str:
        """Serialize this policy deterministically as compact JSON."""

        return serialize_render_policy(self)


def normalize_render_policy(
    policy: RenderPolicy | str | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a complete validated policy dictionary.

    ``None`` and an empty mapping intentionally resolve to ``structural`` so
    existing draft-oriented callers get structural validation without opting
    into rendering.  A string selects one of the named presets.  Mapping
    inputs may repeat preset fields, but explicit values must agree with the
    selected preset.
    """

    if policy is None:
        return deepcopy(DEFAULT_RENDER_POLICY)
    if isinstance(policy, RenderPolicy):
        return policy.to_dict()
    if isinstance(policy, str):
        return _validate_mapping({"policy": policy})
    if not isinstance(policy, Mapping):
        raise _invalid("must be a policy name or object")
    return _validate_mapping(policy)


def validate_render_policy(
    policy: RenderPolicy | str | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and return the normalized policy representation."""

    return normalize_render_policy(policy)


def serialize_render_policy(
    policy: RenderPolicy | str | Mapping[str, Any] | None = None,
) -> str:
    """Serialize a validated policy to deterministic compact JSON."""

    normalized = normalize_render_policy(policy)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def deserialize_render_policy(serialized: str | bytes | bytearray) -> dict[str, Any]:
    """Deserialize and validate a JSON render policy."""

    if not isinstance(serialized, (str, bytes, bytearray)):
        raise RenderPolicyError("serialized render policy must be JSON text")
    try:
        value = json.loads(serialized)
    except (TypeError, ValueError) as exc:
        raise RenderPolicyError(f"invalid render policy JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RenderPolicyError("serialized render policy must contain a JSON object")
    return normalize_render_policy(value)


def current_render_policy() -> "RenderPolicy | None":
    """Return the policy active for the current edit/batch context.

    ``None`` means legacy/direct-call behavior: callers that have not opted
    into a workflow policy continue to use the existing write-time render
    gate.  This preserves compatibility while allowing a batch wrapper to
    defer visual work explicitly.
    """

    return _ACTIVE_RENDER_POLICY.get()


@contextmanager
def render_policy_scope(
    policy: RenderPolicy | str | Mapping[str, Any],
):
    """Temporarily apply a validated render policy to document mutations.

    Structural validation remains the caller's responsibility and invariant;
    this scope only controls whether visual render work is deferred until a
    later finalize/promotion step. Context-local state prevents one concurrent
    document operation from changing another operation's policy.
    """

    active = RenderPolicy.from_value(policy)
    token = _ACTIVE_RENDER_POLICY.set(active)
    try:
        yield active
    finally:
        _ACTIVE_RENDER_POLICY.reset(token)


__all__ = [
    "DEFAULT_RENDER_POLICY",
    "POLICY_NAMES",
    "PUBLICATION",
    "RELEASE",
    "RENDER_POLICIES",
    "RenderPolicy",
    "RenderPolicyError",
    "STRUCTURAL",
    "TARGETED",
    "deserialize_render_policy",
    "current_render_policy",
    "normalize_render_policy",
    "render_policy_scope",
    "serialize_render_policy",
    "validate_render_policy",
]
