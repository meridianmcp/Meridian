"""Default image-storage rights policy -- a pure leaf module.

Question answered here: may Meridian store the image BYTES of a figure, table
or source, given its license? Links and metadata (paths, captions, source ids)
are always kept. Only image bytes are gated.

Tiers
-----
STORE_IMAGE       cc0, public-domain, cc-by, cc-by-sa: bytes may be stored. A
                  derived figure keeps the license, and a cc-by-sa derivative
                  must carry the share-alike flag.
STORE_UNMODIFIED  cc-by-nd: an unmodified copy only. Redrawn or cropped
                  derivatives are refused.
LINK_ONLY         cc-by-nc, cc-by-nc-sa, cc-by-nc-nd, arxiv-nonexclusive,
                  all-rights-reserved and unknown: metadata and links only.
                  Unknown counts as link-only, never as storable.

Override
--------
The only way past a refusal is a human approval, passed in as a
:class:`RightsOverride`. This module cannot verify that a human approved
anything: it validates the shape of the override and refuses agent approvers.
The approval itself is verified by
``meridian.rights_override.authorize_image_storage`` through the existing
gate-override mechanism (``meridian.gate_override``). Never build a
``RightsOverride`` from agent-supplied input.

This module imports nothing from the database layer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

STORE_IMAGE = "STORE_IMAGE"
STORE_UNMODIFIED = "STORE_UNMODIFIED"
LINK_ONLY = "LINK_ONLY"

#: Gate name bound into the override HITL and its approval record.
RIGHTS_OVERRIDE_GATE = "rights_storage_override"
#: ``action_audit_log.event_type`` written for every override that is used.
RIGHTS_OVERRIDE_EVENT_TYPE = "rights_storage_override"

UNKNOWN = "unknown"

_TIER_BY_LICENSE: dict[str, str] = {
    "cc0": STORE_IMAGE,
    "public-domain": STORE_IMAGE,
    "cc-by": STORE_IMAGE,
    "cc-by-sa": STORE_IMAGE,
    "cc-by-nd": STORE_UNMODIFIED,
    "cc-by-nc": LINK_ONLY,
    "cc-by-nc-sa": LINK_ONLY,
    "cc-by-nc-nd": LINK_ONLY,
    "arxiv-nonexclusive": LINK_ONLY,
    "all-rights-reserved": LINK_ONLY,
    UNKNOWN: LINK_ONLY,
}

# The non-BY clauses of a CC license, mapped to the canonical id.
_CC_FAMILY: dict[frozenset[str], str] = {
    frozenset(): "cc-by",
    frozenset({"sa"}): "cc-by-sa",
    frozenset({"nd"}): "cc-by-nd",
    frozenset({"nc"}): "cc-by-nc",
    frozenset({"nc", "sa"}): "cc-by-nc-sa",
    frozenset({"nc", "nd"}): "cc-by-nc-nd",
}

_FULLNAME_SUBS = (
    (re.compile(r"non[\s\-_]?commercial"), " nc "),
    (re.compile(r"no[\s\-_]?deriv(?:ative)?s?"), " nd "),
    (re.compile(r"share[\s\-_]?alike"), " sa "),
)
_CC_MARKER = re.compile(r"(?<![a-z0-9])cc(?![a-z0-9])|attribution")
_VERSION = re.compile(r"v?\d+(?:\.\d+)*")
_TOKEN_SPLIT = re.compile(r"[^a-z0-9.]+")
_IGNORED_TOKENS = frozenset({
    "license", "licence", "creative", "commons", "international", "unported", "legalcode",
})
_CC0 = re.compile(r"^cc[\s\-_]?0(?![0-9])")
_PUBLIC_DOMAIN = re.compile(r"public[\s\-_]?domain")
_ARXIV = re.compile(r"arxiv")
_NON_EXCLUSIVE = re.compile(r"non[\s\-_]?exclusive")
_ALL_RIGHTS = re.compile(r"all[\s\-_]?rights[\s\-_]?reserved")
_CC_LICENSE_PATH = re.compile(r"creativecommons\.org/licenses/([a-z\-]+)")
_CC0_PATH = re.compile(
    r"creativecommons\.org/(?:publicdomain/zero|share-your-work/public-domain/cc0)(?=[/?#\s]|$)"
)
_PDM_PATH = re.compile(
    r"creativecommons\.org/(?:publicdomain/mark|share-your-work/public-domain/pdm)(?=[/?#\s]|$)"
)
_AGENT_PREFIXES = ("agent", "session", "claude")
_AGENT_EXACT = frozenset({"auto"})


class RightsPolicyError(ValueError):
    """A rights decision could not be made safely.

    ``code`` is a stable machine-readable discriminator so a caller can react
    without parsing prose.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RightsOverride:
    """A human approval for one tier refusal.

    Build one only from a verified gate-override approval (see
    ``meridian.rights_override.authorize_image_storage``). This class validates
    shape; it cannot prove who approved.
    """

    hitl_id: str
    subject_id: str
    approved_by: str
    reason: str
    gate: str = RIGHTS_OVERRIDE_GATE


@dataclass(frozen=True)
class ImageStorageDecision:
    """The outcome of :func:`decide_image_storage`."""

    license: str
    tier: str
    allowed: bool
    reason: str
    override_used: bool = False
    derived: bool = False
    redrawn: bool = False
    share_alike_required: bool = False
    audit: dict[str, Any] | None = None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _parse_cc_family(text: str) -> str | None:
    """Canonical CC BY-family id for ``text``, or None if it is not one.

    Requires a CC marker (a ``cc`` token or the word "attribution") and a BY
    clause. Any token outside the known set makes the result None, so an
    unrecognised clause fails closed to unknown.
    """
    if not _CC_MARKER.search(text):
        return None
    for pattern, repl in _FULLNAME_SUBS:
        text = pattern.sub(repl, text)
    kept: set[str] = set()
    for token in _TOKEN_SPLIT.split(text):
        if not token or token == "cc" or _VERSION.fullmatch(token) or token in _IGNORED_TOKENS:
            continue
        if token == "attribution":
            kept.add("by")
            continue
        kept.add(token)
    if "by" not in kept:
        return None
    return _CC_FAMILY.get(frozenset(kept - {"by"}))


def _from_cc_url(text: str) -> str:
    match = _CC_LICENSE_PATH.search(text)
    if match:
        return _parse_cc_family("cc " + match.group(1)) or UNKNOWN
    if _CC0_PATH.search(text):
        return "cc0"
    if _PDM_PATH.search(text):
        return "public-domain"
    return UNKNOWN


def normalize_license(raw: Any) -> str:
    """Map a license string or URL to a canonical id, or ``"unknown"``.

    Accepts creativecommons.org license and public-domain URLs, strings such as
    ``"CC BY-NC-ND 4.0"``, ``"cc-by"``, ``"CC0"``, ``"public domain"``,
    ``"arXiv non-exclusive"`` and ``"All Rights Reserved"``, in any case and
    spacing. Canonical ids map to themselves. Non-strings, blanks and anything
    unrecognised map to ``"unknown"``.
    """
    if not isinstance(raw, str):
        return UNKNOWN
    text = " ".join(raw.split()).lower()
    if not text:
        return UNKNOWN
    if text in _TIER_BY_LICENSE:
        return text
    if "creativecommons.org" in text:
        return _from_cc_url(text)
    if _CC0.match(text):
        return "cc0"
    if _PUBLIC_DOMAIN.search(text) and not _CC_MARKER.search(text):
        return "public-domain"
    if _ARXIV.search(text) and _NON_EXCLUSIVE.search(text):
        return "arxiv-nonexclusive"
    if _ALL_RIGHTS.search(text):
        return "all-rights-reserved"
    return _parse_cc_family(text) or UNKNOWN


def tier_for(canonical: str) -> str:
    """Tier for a license. Accepts a canonical id or a raw string.

    Anything unrecognised is ``LINK_ONLY``, so the policy fails closed.
    """
    return _TIER_BY_LICENSE.get(normalize_license(canonical), LINK_ONLY)


def validate_override(override: Any) -> RightsOverride:
    """Return ``override`` if it is a well-formed human approval, else raise.

    Refuses any plain value (a dict or a string) and any approver whose name
    starts with ``agent``, ``session`` or ``claude``, or equals ``auto``.
    """
    if not isinstance(override, RightsOverride):
        raise RightsPolicyError(
            "OVERRIDE_INVALID",
            "an override must be a RightsOverride built from a verified human "
            "approval; a plain value is refused",
        )
    if override.gate != RIGHTS_OVERRIDE_GATE:
        raise RightsPolicyError(
            "OVERRIDE_INVALID",
            f"override gate must be {RIGHTS_OVERRIDE_GATE!r}, not {override.gate!r}",
        )
    if not _text(override.hitl_id):
        raise RightsPolicyError(
            "HUMAN_APPROVAL_REQUIRED", "an override needs the answered approval id (hitl_id)",
        )
    if not _text(override.subject_id):
        raise RightsPolicyError(
            "OVERRIDE_INVALID", "an override must name its subject (figure, table or source id)",
        )
    if not _text(override.reason):
        raise RightsPolicyError("OVERRIDE_REASON_REQUIRED", "an override requires a non-empty reason")
    approver = _text(override.approved_by)
    if not approver:
        raise RightsPolicyError("APPROVER_REQUIRED", "an override must name the human who approved it")
    low = approver.lower()
    if low in _AGENT_EXACT or low.startswith(_AGENT_PREFIXES):
        raise RightsPolicyError(
            "AGENT_APPROVAL_REFUSED",
            f"approved_by {approver!r} names an agent or session; only a human can "
            "approve a rights override",
        )
    return override


def _base_rule(canonical: str, tier: str, *, is_derivative: bool) -> tuple[bool, str]:
    if tier == STORE_IMAGE:
        return True, f"{canonical} permits storing image bytes"
    if tier == STORE_UNMODIFIED:
        if is_derivative:
            return False, (
                f"{canonical} allows only an unmodified copy; a redrawn or cropped "
                "derivative is refused"
            )
        return True, f"{canonical} allows storing an unmodified copy only"
    if canonical == UNKNOWN:
        return False, (
            "unknown license is treated as link-only: metadata and links are kept, "
            "image bytes are refused"
        )
    return False, f"{canonical} is link-only: metadata and links are kept, image bytes are refused"


def decide_image_storage(
    raw_license: Any,
    *,
    derived: bool = False,
    redrawn: bool = False,
    override: RightsOverride | None = None,
) -> ImageStorageDecision:
    """Decide whether image BYTES may be stored for a source with this license.

    ``derived`` and ``redrawn`` mark a derivative of the source image. A
    derivative of a cc-by-nd image is refused. ``override`` is a verified human
    approval (see :class:`RightsOverride`). It is only consulted when the tier
    refuses the bytes. A malformed override raises :class:`RightsPolicyError`
    even when it is not needed.

    This function returns the decision. The caller must persist
    ``decision.audit`` when ``override_used`` is true.
    """
    canonical = normalize_license(raw_license)
    tier = tier_for(canonical)
    is_derivative = bool(derived) or bool(redrawn)
    share_alike = is_derivative and canonical == "cc-by-sa"
    if override is not None:
        override = validate_override(override)
    allowed, reason = _base_rule(canonical, tier, is_derivative=is_derivative)
    if share_alike and allowed:
        reason += "; the derivative must carry the share-alike flag"
    common = {
        "license": canonical,
        "tier": tier,
        "derived": bool(derived),
        "redrawn": bool(redrawn),
        "share_alike_required": share_alike,
    }
    if allowed:
        return ImageStorageDecision(allowed=True, reason=reason, **common)
    if override is None:
        return ImageStorageDecision(
            allowed=False,
            reason=f"{reason}; a human-approved override is required",
            **common,
        )
    audit = {
        "event_type": RIGHTS_OVERRIDE_EVENT_TYPE,
        "gate": override.gate,
        "subject_id": override.subject_id.strip(),
        "hitl_id": override.hitl_id.strip(),
        "approved_by": override.approved_by.strip(),
        "reason": override.reason.strip(),
        "tier": tier,
        "license": canonical,
        "derived": bool(derived),
        "redrawn": bool(redrawn),
    }
    return ImageStorageDecision(
        allowed=True,
        reason=f"{reason}; allowed by human-approved override {override.hitl_id.strip()}",
        override_used=True,
        audit=audit,
        **common,
    )
