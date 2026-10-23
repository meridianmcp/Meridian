"""Default image-storage rights policy -- a pure leaf module.

Question answered here: may Meridian store the image BYTES of a figure, table
or source, given its license and the use mode of the project? Links and
metadata (paths, captions, source ids) are always kept. Only image bytes are
gated, and only in commercialized use.

Use modes
---------
STANDARD_PUBLICATION (default)  academic or standard publication. Nothing is
                                gated. Restricted items are stored with credit
                                and a review flag.
COMMERCIALIZED                  opt-in per project. Restricted items are refused
                                unless a human approves an override.

Tiers
-----
STORE_IMAGE       cc0, public-domain, cc-by, cc-by-sa: bytes may be stored. A
                  derived figure keeps the license, and a cc-by-sa derivative
                  must carry the share-alike flag.
STORE_UNMODIFIED  cc-by-nd: an unmodified copy is stored. A redrawn or cropped
                  derivative is flagged in standard publication and gated in
                  commercialized use.
LINK_ONLY         cc-by-nc, cc-by-nc-sa, cc-by-nc-nd, arxiv-nonexclusive,
                  all-rights-reserved and unknown. Standard publication stores
                  the bytes with credit and a review flag. Commercialized use
                  refuses them unless a human approves an override.

Jurisdiction and IGO ports of a CC license (``CC BY 3.0 IGO``, ``CC BY 4.0 US``)
map to their base license. The port is recorded as a note on the decision and
as a flag. It never changes the tier.

Override
--------
In commercialized use, the only way past a refusal is a human approval, passed
in as a :class:`RightsOverride`. This module cannot verify that a human
approved anything: it validates the shape of the override and refuses agent
approvers. The approval itself is verified by
``meridian.rights_override.authorize_image_storage`` through the existing
gate-override mechanism (``meridian.gate_override``). Never build a
``RightsOverride`` from agent-supplied input.

This module imports nothing from the database layer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

STANDARD_PUBLICATION = "standard_publication"
COMMERCIALIZED = "commercialized"
_USE_MODES = frozenset({STANDARD_PUBLICATION, COMMERCIALIZED})

STORE_IMAGE = "STORE_IMAGE"
STORE_UNMODIFIED = "STORE_UNMODIFIED"
LINK_ONLY = "LINK_ONLY"

#: Gate name bound into the override HITL and its approval record.
RIGHTS_OVERRIDE_GATE = "rights_storage_override"
#: ``action_audit_log.event_type`` written for every override that is used.
RIGHTS_OVERRIDE_EVENT_TYPE = "rights_storage_override"

FLAG_REQUIRES_CREDIT = "requires_credit"
FLAG_NEEDS_REVIEW = "needs_review"
FLAG_SHARE_ALIKE = "share_alike"
FLAG_NO_DERIVATIVES_REVIEW = "no_derivatives_review"
FLAG_JURISDICTION = "jurisdiction"

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

# Licenses that carry no attribution requirement.
_NO_CREDIT = frozenset({"cc0", "public-domain"})

# Port codes that may follow a CC version number: the IGO port and the country
# and region ports used by creativecommons.org. A port that is not listed stays
# an unrecognised token, so the license is unknown (link-only) until it is added.
_JURISDICTION_CODES = frozenset({
    "igo", "england", "scotland", "wales",
    "ar", "at", "au", "be", "bg", "br", "ca", "ch", "cl", "cn", "co", "cr", "cy",
    "cz", "de", "dk", "ec", "ee", "eg", "es", "fi", "fr", "gr", "gt", "hk", "hr",
    "hu", "id", "ie", "il", "in", "is", "it", "jp", "kr", "lt", "lu", "lv", "mk",
    "mt", "mx", "my", "nl", "no", "nz", "pe", "ph", "pl", "pr", "pt", "ro", "rs",
    "ru", "se", "sg", "si", "sk", "th", "tr", "tw", "ua", "ug", "uk", "us", "za",
})

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
_CC_PORT_PATH = re.compile(
    r"creativecommons\.org/licenses/[a-z\-]+/\d+(?:\.\d+)*/([a-z]{2,3})(?=[/?#\s]|$)"
)
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
    """The outcome of :func:`decide_image_storage`.

    ``flags`` lists obligations and review hints for the item, such as
    ``requires_credit`` or ``needs_review``. ``jurisdiction`` is the CC port
    recorded as a note (``None`` when there is no port).
    """

    license: str
    tier: str
    allowed: bool
    reason: str
    use_mode: str = STANDARD_PUBLICATION
    override_used: bool = False
    derived: bool = False
    redrawn: bool = False
    jurisdiction: str | None = None
    flags: list[str] = field(default_factory=list)
    audit: dict[str, Any] | None = None

    @property
    def share_alike_required(self) -> bool:
        return FLAG_SHARE_ALIKE in self.flags


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def normalize_use_mode(raw: Any) -> str:
    """Return the canonical use mode.

    ``None``, or an empty or blank string, means :data:`STANDARD_PUBLICATION`.
    Any other value that is not one of the two known modes raises
    :class:`RightsPolicyError`, so a bad value fails closed.
    """
    if raw is None:
        return STANDARD_PUBLICATION
    if not isinstance(raw, str):
        raise RightsPolicyError(
            "USE_MODE_INVALID",
            f"use_mode must be {STANDARD_PUBLICATION!r} or {COMMERCIALIZED!r}, not {raw!r}",
        )
    text = raw.strip()
    if not text:
        return STANDARD_PUBLICATION
    if text in _USE_MODES:
        return text
    raise RightsPolicyError(
        "USE_MODE_INVALID",
        f"use_mode must be {STANDARD_PUBLICATION!r} or {COMMERCIALIZED!r}, not {raw!r}",
    )


def _strip_jurisdiction(tokens: list[str]) -> tuple[list[str], str | None]:
    """Remove a jurisdiction or IGO port that directly follows the version.

    Only the run of known port codes right after the first version token is
    removed. Any other token is left in place, so it still fails closed.
    """
    for index, token in enumerate(tokens):
        if not _VERSION.fullmatch(token):
            continue
        end = index + 1
        while end < len(tokens) and tokens[end] in _JURISDICTION_CODES:
            end += 1
        if end == index + 1:
            return tokens, None
        port = " ".join(tokens[index + 1:end])
        return tokens[:index + 1] + tokens[end:], port
    return tokens, None


def _parse_cc(text: str) -> tuple[str | None, str | None]:
    """Canonical CC BY-family id and jurisdiction port for ``text``.

    Either part is None when it is not recognised. Requires a CC marker (a
    ``cc`` token or the word "attribution") and a BY clause. Any token outside
    the known set makes the canonical id None, so an unrecognised clause fails
    closed to unknown.
    """
    if not _CC_MARKER.search(text):
        return None, None
    for pattern, repl in _FULLNAME_SUBS:
        text = pattern.sub(repl, text)
    tokens = [token for token in _TOKEN_SPLIT.split(text) if token]
    tokens, jurisdiction = _strip_jurisdiction(tokens)
    kept: set[str] = set()
    for token in tokens:
        if token == "cc" or _VERSION.fullmatch(token) or token in _IGNORED_TOKENS:
            continue
        if token == "attribution":
            kept.add("by")
            continue
        kept.add(token)
    if "by" not in kept:
        return None, None
    return _CC_FAMILY.get(frozenset(kept - {"by"})), jurisdiction


def _from_cc_url(text: str) -> tuple[str, str | None]:
    match = _CC_LICENSE_PATH.search(text)
    if match:
        canonical, _ = _parse_cc("cc " + match.group(1))
        port_match = _CC_PORT_PATH.search(text)
        port = port_match.group(1) if port_match else None
        jurisdiction = port if port in _JURISDICTION_CODES else None
        return canonical or UNKNOWN, jurisdiction
    if _CC0_PATH.search(text):
        return "cc0", None
    if _PDM_PATH.search(text):
        return "public-domain", None
    return UNKNOWN, None


def _classify(raw: Any) -> tuple[str, str | None]:
    """Canonical license id and jurisdiction port (or None) for a raw value."""
    if not isinstance(raw, str):
        return UNKNOWN, None
    text = " ".join(raw.split()).lower()
    if not text:
        return UNKNOWN, None
    if text in _TIER_BY_LICENSE:
        return text, None
    if "creativecommons.org" in text:
        return _from_cc_url(text)
    if _CC0.match(text):
        return "cc0", None
    if _PUBLIC_DOMAIN.search(text) and not _CC_MARKER.search(text):
        return "public-domain", None
    if _ARXIV.search(text) and _NON_EXCLUSIVE.search(text):
        return "arxiv-nonexclusive", None
    if _ALL_RIGHTS.search(text):
        return "all-rights-reserved", None
    canonical, jurisdiction = _parse_cc(text)
    return canonical or UNKNOWN, jurisdiction


def normalize_license(raw: Any) -> str:
    """Map a license string or URL to a canonical id, or ``"unknown"``.

    Accepts creativecommons.org license and public-domain URLs, strings such as
    ``"CC BY-NC-ND 4.0"``, ``"cc-by"``, ``"CC0"``, ``"public domain"``,
    ``"arXiv non-exclusive"`` and ``"All Rights Reserved"``, in any case and
    spacing. A jurisdiction or IGO port of a CC license maps to its base
    license (``"CC BY 3.0 IGO"`` is ``"cc-by"``). Canonical ids map to
    themselves. Non-strings, blanks and anything unrecognised map to
    ``"unknown"``.
    """
    return _classify(raw)[0]


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


def _rule(canonical: str, tier: str, *, is_derivative: bool, mode: str) -> tuple[bool, str]:
    """Return ``(refused, reason)`` for the bytes under this tier and use mode.

    ``refused`` is True only in commercialized use, where it means a human
    override is needed. Standard publication never refuses.
    """
    commercial = mode == COMMERCIALIZED
    if tier == STORE_IMAGE:
        return False, f"{canonical} permits storing image bytes"
    if tier == STORE_UNMODIFIED:
        if not is_derivative:
            return False, f"{canonical} permits storing an unmodified copy"
        if not commercial:
            return False, (
                f"{canonical} permits an unmodified copy; this redrawn or cropped "
                f"derivative is allowed in {STANDARD_PUBLICATION} and flagged for "
                "no-derivatives review"
            )
        return True, (
            f"tier {STORE_UNMODIFIED}: {canonical} permits only an unmodified copy; "
            f"a redrawn or cropped derivative is refused in {COMMERCIALIZED} use"
        )
    label = "unknown license" if canonical == UNKNOWN else canonical
    if not commercial:
        return False, (
            f"tier {LINK_ONLY}: {label} is allowed in {STANDARD_PUBLICATION} with "
            "credit and a review flag; commercialized use would refuse the bytes"
        )
    return True, f"tier {LINK_ONLY}: {label} image bytes are refused in {COMMERCIALIZED} use"


def _flags(canonical: str, tier: str, *, is_derivative: bool, jurisdiction: str | None) -> list[str]:
    flags: list[str] = []
    if canonical not in _NO_CREDIT:
        flags.append(FLAG_REQUIRES_CREDIT)
    if tier == LINK_ONLY:
        flags.append(FLAG_NEEDS_REVIEW)
    if canonical == "cc-by-sa" and is_derivative:
        flags.append(FLAG_SHARE_ALIKE)
    if canonical == "cc-by-nd" and is_derivative:
        flags.append(FLAG_NO_DERIVATIVES_REVIEW)
    if jurisdiction:
        flags.append(FLAG_JURISDICTION)
    return flags


def decide_image_storage(
    raw_license: Any,
    *,
    derived: bool = False,
    redrawn: bool = False,
    use_mode: Any = STANDARD_PUBLICATION,
    override: RightsOverride | None = None,
) -> ImageStorageDecision:
    """Decide whether image BYTES may be stored for a source with this license.

    ``use_mode`` is :data:`STANDARD_PUBLICATION` by default. Standard
    publication never refuses. Commercialized use refuses LINK_ONLY items, and
    redrawn or cropped derivatives of a cc-by-nd image, unless ``override`` is a
    verified human approval (see :class:`RightsOverride`). ``override`` is only
    consulted when the bytes would be refused. A malformed override or an
    unknown use mode raises :class:`RightsPolicyError` even when it is not
    needed.

    ``derived`` and ``redrawn`` both mark a derivative of the source image.
    This function returns the decision. The caller must persist
    ``decision.audit`` when ``override_used`` is true.
    """
    mode = normalize_use_mode(use_mode)
    canonical, jurisdiction = _classify(raw_license)
    tier = _TIER_BY_LICENSE.get(canonical, LINK_ONLY)
    is_derivative = bool(derived) or bool(redrawn)
    if override is not None:
        override = validate_override(override)
    refused, reason = _rule(canonical, tier, is_derivative=is_derivative, mode=mode)
    share_alike = canonical == "cc-by-sa" and is_derivative
    if share_alike and not refused:
        reason += "; the derivative must carry the share-alike flag"
    common: dict[str, Any] = {
        "license": canonical,
        "tier": tier,
        "use_mode": mode,
        "derived": bool(derived),
        "redrawn": bool(redrawn),
        "jurisdiction": jurisdiction,
        "flags": _flags(
            canonical, tier, is_derivative=is_derivative, jurisdiction=jurisdiction,
        ),
    }
    if not refused:
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
        "use_mode": mode,
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
