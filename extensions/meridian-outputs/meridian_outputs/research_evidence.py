"""Typed research evidence and a lossless XML/JSON provenance envelope.

Sprint item 0ea8fd3c.

Problem
-------
Research artifacts a session touches -- claims, sources, citations, datasets,
code, runs, outputs, figures, tables, documents, reviews -- are today only
ever narrated in free-text Markdown (notes, handoff bodies, DEVLOG entries).
Markdown has no schema: two renderings of "this figure cites that dataset,
but we're not sure the dataset version still matches" are indistinguishable
from prose alone, and nothing stops a partial/unresolved record from reading
exactly like a fully-verified one.

What this module adds
----------------------
A canonical, typed data model -- :class:`ProvenanceEnvelope`, made of
:class:`EvidenceRecord` nodes (one per research-artifact kind) linked by
:class:`EvidenceLink` edges -- that is:

  * **Lossless**: :func:`serialize_provenance_envelope` /
    :func:`parse_provenance_envelope` round-trip an envelope through JSON
    *and* through XML with no data loss (``parse(serialize(env)) == env``
    for both formats). Both formats are two projections of the exact same
    canonical dict (see :func:`envelope_to_dict`) -- they can never drift
    apart in what they're able to express.
  * **Explicit about confidence and resolution state**: every record and
    every link carries a :class:`ResolverState` with one of eight explicit
    statuses (:data:`ResolverStatus.VERIFIED` / :data:`STALE` / :data:`HELD`
    / :data:`AMBIGUOUS` / :data:`UNAVAILABLE` / :data:`DEGRADED` /
    :data:`PENDING_RETRY` / :data:`FAILED`) plus a ``confidence`` float in
    ``[0.0, 1.0]`` -- never a bare boolean "is this trustworthy."
    ``PENDING_RETRY``/``FAILED`` were added by PROV-CANONICAL (7d9b8251) to
    close a gap the original six-state set left open: a genuinely still-
    executing/in-flight operation (``PENDING_RETRY``) was previously
    indistinguishable from a permanently, terminally failed one (``FAILED``)
    -- both collapsed onto ``UNAVAILABLE``/``DEGRADED`` respectively by
    callers with no better option (see
    ``run_manifest.run_manifest_to_evidence_record``, fixed in the same
    item to use these two new states instead of overloading the old ones).
  * **Honest about partial records**: ``partial``/``partial_reason`` fields
    exist at the record, link, and envelope level. A partial record is
    *never* rendered by :meth:`ProvenanceEnvelope.to_markdown` without an
    explicit "PARTIAL -- not authoritative" marker -- Markdown is only ever
    a rendered PROJECTION of the typed envelope, never the source of truth,
    and it is not allowed to silently upgrade a partial/unresolved record to
    look complete.
  * **Identity- and hash-aware**: :class:`EvidenceIdentity` carries a stable
    ``id``, a ``kind``, a ``locator`` (URI/path/DOI/etc.), and optional
    ``external_ids``; :class:`EvidenceHash` carries an algorithm + digest +
    optional soft fingerprint, reusing the SAME sha256-of-content idiom
    :mod:`fingerprint` already uses elsewhere in this package (see
    :func:`compute_content_hash`) -- no new hashing scheme invented here.
  * **Timestamp- and revision-aware**: :class:`EvidenceTimestamps` tracks
    ``observed_at``/``updated_at`` plus an ordered list of
    :class:`EvidenceRevision` entries, so a record's history is itself part
    of the lossless envelope, not lost on the next overwrite.

Non-goals (explicitly out of scope for this item)
---------------------------------------------------
This module does not implement an artifact *registry* (persistence/lookup
across many envelopes) or output-semantic validation. The registry gap was
closed by a later item (e1c979e3, ``meridian_outputs.artifact_registry`` --
a durable, relocation-safe artifact identity store bound to source/content-
hash/generator identity, independent of this envelope model); nothing here
changed to accommodate it, and this module has no dependency on it either
direction. ``validate_output_semantics`` remains a different, not-yet-built
capability with its own sprint item. This module owns exactly what its
title says: the typed evidence model and its JSON/XML envelope.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

__all__ = [
    "EvidenceKind",
    "ResolverStatus",
    "EnvelopeValidationError",
    "EvidenceHash",
    "EvidenceRevision",
    "EvidenceTimestamps",
    "ResolverState",
    "EvidenceIdentity",
    "EvidenceScope",
    "EvidenceRecord",
    "EvidenceLink",
    "ProvenanceEnvelope",
    "compute_content_hash",
    "make_hash",
    "build_envelope",
    "envelope_to_dict",
    "envelope_from_dict",
    "serialize_provenance_envelope",
    "parse_provenance_envelope",
    "canonical_envelope_hash",
    "merge_envelopes",
    "evidence_status_summary",
    "trusted_pointers",
]


class EnvelopeValidationError(ValueError):
    """Raised for any structurally-invalid evidence/envelope data.

    Every construction- and parse-time failure in this module raises THIS
    type (never a bare ``KeyError``/``ValueError``/``xml.etree.ElementTree
    .ParseError`` escaping to the caller) so callers can catch one thing.
    The original exception, when there is one, is chained via ``from``.
    """


class EvidenceKind(str, Enum):
    """The eleven research-artifact kinds this envelope covers (acceptance
    criteria: "claim/source/citation/dataset/code/run/output/figure/table/
    document/review edges")."""

    CLAIM = "claim"
    SOURCE = "source"
    CITATION = "citation"
    DATASET = "dataset"
    CODE = "code"
    RUN = "run"
    OUTPUT = "output"
    FIGURE = "figure"
    TABLE = "table"
    DOCUMENT = "document"
    REVIEW = "review"


class ResolverStatus(str, Enum):
    """The eight explicit resolver states this envelope model recognizes.

    ``VERIFIED`` is the only status :meth:`ResolverState.is_authoritative`
    treats as safe to present without a caveat -- every other status is a
    real, named degree of NOT being confirmed-good, distinct from a bare
    "unknown"/absent status field.

    PROV-CANONICAL (7d9b8251) added ``PENDING_RETRY`` and ``FAILED`` to the
    original six-state set (VERIFIED/STALE/HELD/AMBIGUOUS/UNAVAILABLE/
    DEGRADED). Before this, a caller bridging some OTHER system's status
    into this envelope (e.g. ``run_manifest``'s own ``phase``) had no way to
    say "this is still genuinely in flight, safe to poll/retry" without
    reusing ``UNAVAILABLE`` -- which also had to mean "this is a dead,
    orphaned, never-finalized receipt" -- and no way to say "this
    terminally failed" without reusing ``DEGRADED``, which also covers much
    milder "still usable but imperfect" cases. Adding these as new members
    of a plain ``str`` ``Enum`` is purely additive: every existing
    comparison/serialization of the prior six values is unaffected, and
    ``evidence_status_summary``'s ``status_counts`` dict (built via
    ``{s.value: 0 for s in ResolverStatus}``) picks the new members up
    automatically.
    """

    VERIFIED = "verified"
    STALE = "stale"
    HELD = "held"
    AMBIGUOUS = "ambiguous"
    UNAVAILABLE = "unavailable"
    DEGRADED = "degraded"
    #: Genuinely still in-flight / safe to poll or retry -- distinct from a
    #: stale/dead/orphaned operation, which stays UNAVAILABLE.
    PENDING_RETRY = "pending_retry"
    #: Terminally, confirmedly failed -- distinct from DEGRADED (still
    #: usable, just imperfect).
    FAILED = "failed"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_unit_interval(value: float, *, field_name: str) -> float:
    try:
        as_float = float(value)
    except (TypeError, ValueError) as exc:
        raise EnvelopeValidationError(
            f"{field_name} must be a number in [0.0, 1.0], got {value!r}"
        ) from exc
    if not 0.0 <= as_float <= 1.0:
        raise EnvelopeValidationError(
            f"{field_name} must be within [0.0, 1.0], got {as_float!r}"
        )
    return as_float


def _require_nonempty_str(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EnvelopeValidationError(
            f"{field_name} must be a non-empty string, got {value!r}"
        )
    return value


# ---------------------------------------------------------------------------
# Hashing / fingerprinting
# ---------------------------------------------------------------------------

def compute_content_hash(content: "str | bytes", algorithm: str = "sha256") -> str:
    """Hex digest of ``content`` under ``algorithm`` (default sha256 -- the
    same algorithm :mod:`fingerprint`'s ``script_content_hash`` already uses
    elsewhere in this package; no new hash scheme introduced here).

    ``content`` may be ``str`` (encoded UTF-8) or ``bytes``. Never raises for
    a bad algorithm name from :mod:`hashlib`'s own supported set -- an
    unsupported algorithm raises :class:`EnvelopeValidationError` instead of
    a raw ``ValueError``.
    """
    data = content.encode("utf-8") if isinstance(content, str) else content
    try:
        hasher = hashlib.new(algorithm)
    except (ValueError, TypeError) as exc:
        raise EnvelopeValidationError(f"unsupported hash algorithm {algorithm!r}") from exc
    hasher.update(data)
    return hasher.hexdigest()


def make_hash(
    content: "str | bytes", *, algorithm: str = "sha256", fingerprint: "str | None" = None,
) -> "EvidenceHash":
    """Convenience constructor: hash ``content`` and wrap it as an
    :class:`EvidenceHash`, optionally carrying a secondary soft
    ``fingerprint`` (e.g. a shape/structural signature that is stable across
    byte-level changes that don't matter semantically)."""
    return EvidenceHash(
        algorithm=algorithm,
        value=compute_content_hash(content, algorithm=algorithm),
        fingerprint=fingerprint,
    )


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EvidenceHash:
    """One hash/fingerprint claim about an evidence record's content.

    ``algorithm`` + ``value`` are the primary, verifiable digest (e.g.
    ``sha256``/hex digest). ``fingerprint`` is an OPTIONAL secondary, softer
    signature (e.g. "same CSV columns" rather than "byte-identical") -- never
    treated as a substitute for ``value`` by any code in this module.
    """

    algorithm: str
    value: str
    fingerprint: "str | None" = None

    def __post_init__(self) -> None:
        _require_nonempty_str(self.algorithm, field_name="EvidenceHash.algorithm")
        _require_nonempty_str(self.value, field_name="EvidenceHash.value")


@dataclass(frozen=True)
class EvidenceRevision:
    """One historical revision of an evidence record, kept so the envelope
    never loses history to the next overwrite (lossless requirement)."""

    revision_id: str
    created_at: str
    note: "str | None" = None

    def __post_init__(self) -> None:
        _require_nonempty_str(self.revision_id, field_name="EvidenceRevision.revision_id")
        _require_nonempty_str(self.created_at, field_name="EvidenceRevision.created_at")


@dataclass
class EvidenceTimestamps:
    """When this record/link was first observed and last updated, plus its
    full revision history (never truncated by this module)."""

    observed_at: str
    updated_at: str
    revisions: "list[EvidenceRevision]" = field(default_factory=list)

    def __post_init__(self) -> None:
        _require_nonempty_str(self.observed_at, field_name="EvidenceTimestamps.observed_at")
        _require_nonempty_str(self.updated_at, field_name="EvidenceTimestamps.updated_at")


@dataclass
class ResolverState:
    """The explicit resolution state + confidence for one record or link.

    ``status`` is always one of :class:`ResolverStatus`'s six explicit
    values -- there is no "unset"/``None`` option, so a caller can never
    accidentally treat "we never checked" as "verified."
    """

    status: ResolverStatus
    confidence: float
    resolved_at: "str | None" = None
    resolver: "str | None" = None
    reason: "str | None" = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ResolverStatus):
            try:
                object.__setattr__(self, "status", ResolverStatus(self.status))
            except ValueError as exc:
                raise EnvelopeValidationError(
                    f"invalid ResolverState.status {self.status!r}; must be one of "
                    f"{[s.value for s in ResolverStatus]}"
                ) from exc
        self.confidence = _require_unit_interval(self.confidence, field_name="ResolverState.confidence")

    @property
    def is_authoritative(self) -> bool:
        """``True`` only for a fully :data:`ResolverStatus.VERIFIED` state.

        Every other status (stale/held/ambiguous/unavailable/degraded) is,
        by construction, NOT safe to present as authoritative on its own --
        callers needing "can I trust this" should check this property rather
        than re-deriving the same five-way exclusion themselves.
        """
        return self.status is ResolverStatus.VERIFIED


@dataclass
class EvidenceIdentity:
    """Stable identity for one evidence record: a ``kind``-tagged ``id`` plus
    a ``locator`` (path/URI/DOI/etc.) pointing at the underlying thing, and
    any number of ``external_ids`` (e.g. ``{"doi": "...", "arxiv": "..."}``).
    """

    id: str
    kind: EvidenceKind
    locator: str
    label: "str | None" = None
    external_ids: "dict[str, str]" = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_nonempty_str(self.id, field_name="EvidenceIdentity.id")
        _require_nonempty_str(self.locator, field_name="EvidenceIdentity.locator")
        if not isinstance(self.kind, EvidenceKind):
            try:
                self.kind = EvidenceKind(self.kind)
            except ValueError as exc:
                raise EnvelopeValidationError(
                    f"invalid EvidenceIdentity.kind {self.kind!r}; must be one of "
                    f"{[k.value for k in EvidenceKind]}"
                ) from exc


@dataclass(frozen=True)
class EvidenceScope:
    """PROV-CANONICAL (7d9b8251) -- stable project/tenant/subproject scope
    for one :class:`EvidenceRecord`, closing the gap where this module had
    NO scope fields at all while sibling systems disagreed on which ones
    they carried (``action_audit_log``: tenant_id+project_id;
    ``research_graph``: project_id only; ``run_manifest``'s own ``scope``
    dict: project_id+version+sprint_item_id, no tenant_id). This is a
    superset of all three so a record can carry whichever subset its
    producer actually knows, rather than smuggling scope into the untyped
    ``attributes`` catch-all.

    Every field is optional -- a record legitimately may not know its
    tenant (self-hosted, no multi-tenancy) or its subproject (most
    projects have none). ``None`` means "not applicable/not known", never
    "explicitly empty"."""

    project_id: "str | None" = None
    tenant_id: "str | None" = None
    subproject_id: "str | None" = None
    version: "str | None" = None
    sprint_item_id: "str | None" = None


@dataclass
class EvidenceRecord:
    """One node in the provenance graph: a single claim/source/citation/
    dataset/code/run/output/figure/table/document/review, fully typed.

    ``partial``/``partial_reason`` mark a record as KNOWN-incomplete (e.g.
    "citation text captured, DOI resolution still pending") -- distinct from
    ``resolver.status``, which describes confidence in what IS recorded, not
    whether the record itself is complete. A record can be both fully
    ``VERIFIED`` (what's there is correct) and ``partial`` (there's more to
    capture) at the same time.
    """

    identity: EvidenceIdentity
    timestamps: EvidenceTimestamps
    resolver: ResolverState
    hashes: "list[EvidenceHash]" = field(default_factory=list)
    partial: bool = False
    partial_reason: "str | None" = None
    attributes: "dict[str, Any]" = field(default_factory=dict)
    # MDE-5 -- explicit redaction state, same shape/validation as partial/
    # partial_reason: a redacted record must say WHY (never silently dropped
    # -- the whole point of an explicit envelope over free-text Markdown is
    # that "this exists but was redacted" is itself a first-class, visible
    # fact, not an absence a reader can't distinguish from "never collected").
    redacted: bool = False
    redaction_reason: "str | None" = None
    # PROV-CANONICAL (7d9b8251) -- stable scope, schema version, idempotency
    # key, and first-class derivation links. See EvidenceScope's own
    # docstring for why scope is a typed field rather than smuggled into
    # ``attributes``. ``schema_version`` is THE one canonical name/meaning
    # for "payload schema version" this item settles on -- distinct from
    # ``ProvenanceEnvelope.version`` (a string, envelope-FORMAT version, not
    # payload schema version) and matching the int convention
    # ``docx_integrity_gate.GATE_SCHEMA_VERSION`` and
    # ``run_manifest``'s own (module-private) ``_SCHEMA_VERSION`` already
    # use. ``operation_key`` is an optional caller-supplied idempotency key
    # (mirrors ``run_manifest``'s ``run_id``+``manifest_hash`` identity and
    # ``batch_management``'s deterministic ``action_audit_log.id`` --
    # neither of which exposed a SHARED, named concept for this before).
    # ``parent_ids`` is a first-class "this record was derived from these
    # other record ids" list -- distinct from (and independent of)
    # EvidenceLink's free-text ``relation`` edges and research_graph's typed
    # ``edge_kind`` closed vocabulary; this is the lightweight, always-
    # available derivation pointer every record can carry without needing a
    # separate link object.
    scope: "EvidenceScope | None" = None
    schema_version: int = 1
    operation_key: "str | None" = None
    parent_ids: "list[str]" = field(default_factory=list)
    # MDE-5 -- unknown top-level keys from a newer/different producer's
    # schema, preserved verbatim across a JSON/XML round trip instead of
    # being silently dropped by envelope_from_dict. Never populated by code
    # in THIS module for a record it built itself; only ever set by
    # _record_from_dict when parsing foreign data. See _encode()'s special
    # casing: these are merged back into the record's own top-level dict on
    # re-serialization, not nested under an "extra_fields" key.
    extra_fields: "dict[str, Any]" = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.partial and not self.partial_reason:
            raise EnvelopeValidationError(
                f"EvidenceRecord {self.identity.id!r} is marked partial=True but has "
                "no partial_reason -- a partial record must say what's missing"
            )
        if self.redacted and not self.redaction_reason:
            raise EnvelopeValidationError(
                f"EvidenceRecord {self.identity.id!r} is marked redacted=True but has "
                "no redaction_reason -- a redacted record must say why"
            )
        if not isinstance(self.schema_version, int) or isinstance(self.schema_version, bool) or self.schema_version < 1:
            raise EnvelopeValidationError(
                f"EvidenceRecord {self.identity.id!r}: schema_version must be a "
                f"positive int, got {self.schema_version!r}"
            )
        if self.scope is not None and not isinstance(self.scope, EvidenceScope):
            try:
                self.scope = EvidenceScope(**self.scope)  # type: ignore[arg-type]
            except TypeError as exc:
                raise EnvelopeValidationError(
                    f"EvidenceRecord {self.identity.id!r}: invalid scope {self.scope!r}"
                ) from exc

    @property
    def is_authoritative(self) -> bool:
        """Safe to present as complete + trustworthy without a caveat:
        fully resolved, not flagged partial, and not redacted."""
        return self.resolver.is_authoritative and not self.partial and not self.redacted


@dataclass
class EvidenceLink:
    """One typed edge between two :class:`EvidenceRecord` identities (e.g.
    a claim record ``cites`` a source record, a run record ``produced`` an
    output record). ``relation`` is a free-text but required verb.

    Endpoints are NOT required to resolve to a record present in the same
    envelope -- an :data:`ResolverStatus.UNAVAILABLE`/``partial`` link is the
    correct way to represent "we know this edge should exist but couldn't
    resolve/fetch the far end yet," which is exactly the "partial records ...
    not ... authoritative" requirement applied to edges, not just nodes.
    """

    id: str
    relation: str
    source_id: str
    target_id: str
    resolver: ResolverState
    partial: bool = False
    partial_reason: "str | None" = None
    note: "str | None" = None
    redacted: bool = False
    redaction_reason: "str | None" = None
    extra_fields: "dict[str, Any]" = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_nonempty_str(self.id, field_name="EvidenceLink.id")
        _require_nonempty_str(self.relation, field_name="EvidenceLink.relation")
        _require_nonempty_str(self.source_id, field_name="EvidenceLink.source_id")
        _require_nonempty_str(self.target_id, field_name="EvidenceLink.target_id")
        if self.partial and not self.partial_reason:
            raise EnvelopeValidationError(
                f"EvidenceLink {self.id!r} is marked partial=True but has no "
                "partial_reason -- a partial link must say what's missing"
            )
        if self.redacted and not self.redaction_reason:
            raise EnvelopeValidationError(
                f"EvidenceLink {self.id!r} is marked redacted=True but has no "
                "redaction_reason -- a redacted link must say why"
            )

    @property
    def is_authoritative(self) -> bool:
        return self.resolver.is_authoritative and not self.partial and not self.redacted


@dataclass
class ProvenanceEnvelope:
    """The canonical, lossless container: a set of :class:`EvidenceRecord`
    nodes plus :class:`EvidenceLink` edges between them, with envelope-level
    identity/timestamp/partial metadata of its own.

    Markdown is produced ONLY via :meth:`to_markdown` -- a read-only
    PROJECTION for human display. It is never parsed back; JSON/XML via
    :func:`serialize_provenance_envelope`/:func:`parse_provenance_envelope`
    are the only round-trippable, authoritative representations.
    """

    envelope_id: str
    generated_at: str
    records: "list[EvidenceRecord]" = field(default_factory=list)
    links: "list[EvidenceLink]" = field(default_factory=list)
    version: str = "1.0"
    partial: bool = False
    partial_reason: "str | None" = None
    extra_fields: "dict[str, Any]" = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_nonempty_str(self.envelope_id, field_name="ProvenanceEnvelope.envelope_id")
        _require_nonempty_str(self.generated_at, field_name="ProvenanceEnvelope.generated_at")
        seen_ids: set[str] = set()
        for rec in self.records:
            if rec.identity.id in seen_ids:
                raise EnvelopeValidationError(
                    f"duplicate EvidenceRecord id {rec.identity.id!r} in envelope "
                    f"{self.envelope_id!r} -- record ids must be unique"
                )
            seen_ids.add(rec.identity.id)
        if self.partial and not self.partial_reason:
            raise EnvelopeValidationError(
                f"ProvenanceEnvelope {self.envelope_id!r} is marked partial=True but "
                "has no partial_reason"
            )

    def dangling_link_endpoints(self) -> "list[str]":
        """Non-raising diagnostic: link ids whose source_id/target_id does
        not match any record in THIS envelope.

        This is informational, not a validation failure -- a dangling
        endpoint is expected and legitimate for a link whose far end is
        genuinely :data:`ResolverStatus.UNAVAILABLE`/``partial`` (the entire
        point of allowing partial edges). Callers that want strictness can
        assert this list is empty themselves.
        """
        known = {rec.identity.id for rec in self.records}
        dangling = []
        for link in self.links:
            if link.source_id not in known or link.target_id not in known:
                dangling.append(link.id)
        return dangling

    def to_markdown(self) -> str:
        """Render a human-readable Markdown PROJECTION of this envelope.

        Explicitly a projection, never the source of truth: the header says
        so, and every non-:attr:`EvidenceRecord.is_authoritative` record or
        link is prefixed with a visible ``PARTIAL``/status caveat so a
        partial or unresolved record can never read the same as a fully
        verified one.
        """
        lines = [
            f"# Provenance Envelope `{self.envelope_id}`",
            "",
            "_Generated projection of a typed `ProvenanceEnvelope` -- JSON/XML via "
            "`serialize_provenance_envelope` is the authoritative, round-trippable "
            "source of truth; this Markdown rendering is read-only and is never "
            "parsed back._",
            "",
            f"- version: `{self.version}`",
            f"- generated_at: `{self.generated_at}`",
        ]
        if self.partial:
            lines.append(f"- **PARTIAL ENVELOPE** -- {self.partial_reason}")
        lines.append("")
        lines.append("## Records")
        lines.append("")
        if not self.records:
            lines.append("_none_")
        for rec in self.records:
            caveat = "" if rec.is_authoritative else (
                f" -- **{rec.resolver.status.value.upper()}**"
                + (", **PARTIAL**" if rec.partial else "")
                + (f" ({rec.partial_reason})" if rec.partial else "")
                + (", **REDACTED**" if rec.redacted else "")
                + (f" ({rec.redaction_reason})" if rec.redacted else "")
            )
            lines.append(
                f"- `{rec.identity.kind.value}` **{rec.identity.id}** "
                f"({rec.identity.locator}){caveat}"
            )
        lines.append("")
        lines.append("## Links")
        lines.append("")
        if not self.links:
            lines.append("_none_")
        for link in self.links:
            caveat = "" if link.is_authoritative else (
                f" -- **{link.resolver.status.value.upper()}**"
                + (", **PARTIAL**" if link.partial else "")
                + (f" ({link.partial_reason})" if link.partial else "")
                + (", **REDACTED**" if link.redacted else "")
                + (f" ({link.redaction_reason})" if link.redacted else "")
            )
            lines.append(
                f"- `{link.source_id}` --[{link.relation}]--> `{link.target_id}`{caveat}"
            )
        return "\n".join(lines) + "\n"


def build_envelope(
    records: "list[EvidenceRecord] | None" = None,
    links: "list[EvidenceLink] | None" = None,
    *,
    envelope_id: "str | None" = None,
    generated_at: "str | None" = None,
    version: str = "1.0",
    partial: bool = False,
    partial_reason: "str | None" = None,
) -> ProvenanceEnvelope:
    """Ergonomic constructor: auto-generates ``envelope_id`` (uuid4) and
    ``generated_at`` (current UTC ISO-8601) when not supplied explicitly.

    Pass explicit ``envelope_id``/``generated_at`` for deterministic,
    reproducible construction (e.g. in tests).
    """
    return ProvenanceEnvelope(
        envelope_id=envelope_id or str(uuid.uuid4()),
        generated_at=generated_at or _utcnow_iso(),
        records=list(records or []),
        links=list(links or []),
        version=version,
        partial=partial,
        partial_reason=partial_reason,
    )


# ---------------------------------------------------------------------------
# Canonical dict encoding (shared core for BOTH JSON and XML projections)
# ---------------------------------------------------------------------------

def _encode(obj: Any) -> Any:
    """Recursively reduce dataclasses/enums to plain JSON-able values
    (dict/list/str/int/float/bool/None). Shared by both serialization
    formats so JSON and XML can never drift in what they can express.

    MDE-5 -- a dataclass field literally named ``extra_fields`` (the unknown-
    field-preservation escape hatch on :class:`EvidenceRecord`/
    :class:`EvidenceLink`/:class:`ProvenanceEnvelope`) is special-cased: its
    contents are merged directly into the RESULT dict rather than nested
    under an ``"extra_fields"`` key, so a foreign producer's unrecognized
    top-level keys round-trip back out at the same level they came in at
    (see ``_record_from_dict``/``_link_from_dict``/``envelope_from_dict``,
    which populate ``extra_fields`` from exactly the keys NOT already known
    to this schema). Known keys always win: ``extra_fields`` is constructed
    to exclude every known key in the first place, so this merge can never
    silently overwrite a real field.
    """
    if isinstance(obj, Enum):
        return obj.value
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        result: "dict[str, Any]" = {}
        extra: "dict[str, Any]" = {}
        for f in dataclasses.fields(obj):
            if f.name == "extra_fields":
                extra = _encode(getattr(obj, f.name)) or {}
                continue
            result[f.name] = _encode(getattr(obj, f.name))
        if extra:
            result.update(extra)
        return result
    if isinstance(obj, dict):
        return {str(k): _encode(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_encode(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    raise EnvelopeValidationError(f"cannot encode value of type {type(obj)!r}")


def envelope_to_dict(envelope: ProvenanceEnvelope) -> "dict[str, Any]":
    """The single canonical dict both JSON and XML serialize from."""
    if not isinstance(envelope, ProvenanceEnvelope):
        raise EnvelopeValidationError(
            f"envelope_to_dict expects a ProvenanceEnvelope, got {type(envelope)!r}"
        )
    return _encode(envelope)


def _get(d: "dict[str, Any]", key: str, *, ctx: str) -> Any:
    if key not in d:
        raise EnvelopeValidationError(f"malformed {ctx}: missing required key {key!r}")
    return d[key]


def _hash_from_dict(d: "dict[str, Any]") -> EvidenceHash:
    return EvidenceHash(
        algorithm=_get(d, "algorithm", ctx="EvidenceHash"),
        value=_get(d, "value", ctx="EvidenceHash"),
        fingerprint=d.get("fingerprint"),
    )


def _revision_from_dict(d: "dict[str, Any]") -> EvidenceRevision:
    return EvidenceRevision(
        revision_id=_get(d, "revision_id", ctx="EvidenceRevision"),
        created_at=_get(d, "created_at", ctx="EvidenceRevision"),
        note=d.get("note"),
    )


def _timestamps_from_dict(d: "dict[str, Any]") -> EvidenceTimestamps:
    return EvidenceTimestamps(
        observed_at=_get(d, "observed_at", ctx="EvidenceTimestamps"),
        updated_at=_get(d, "updated_at", ctx="EvidenceTimestamps"),
        revisions=[_revision_from_dict(r) for r in d.get("revisions", [])],
    )


def _resolver_state_from_dict(d: "dict[str, Any]") -> ResolverState:
    return ResolverState(
        status=_get(d, "status", ctx="ResolverState"),
        confidence=_get(d, "confidence", ctx="ResolverState"),
        resolved_at=d.get("resolved_at"),
        resolver=d.get("resolver"),
        reason=d.get("reason"),
    )


def _identity_from_dict(d: "dict[str, Any]") -> EvidenceIdentity:
    return EvidenceIdentity(
        id=_get(d, "id", ctx="EvidenceIdentity"),
        kind=_get(d, "kind", ctx="EvidenceIdentity"),
        locator=_get(d, "locator", ctx="EvidenceIdentity"),
        label=d.get("label"),
        external_ids=dict(d.get("external_ids") or {}),
    )


#: Keys _record_from_dict/_link_from_dict/envelope_from_dict already know how
#: to interpret. Anything else on the input dict is preserved verbatim via
#: extra_fields (MDE-5 "preserve unknown fields") rather than dropped.
_RECORD_KNOWN_KEYS = frozenset({
    "identity", "timestamps", "resolver", "hashes", "partial", "partial_reason",
    "attributes", "redacted", "redaction_reason",
    # PROV-CANONICAL (7d9b8251)
    "scope", "schema_version", "operation_key", "parent_ids",
})
_LINK_KNOWN_KEYS = frozenset({
    "id", "relation", "source_id", "target_id", "resolver", "partial",
    "partial_reason", "note", "redacted", "redaction_reason",
})
_ENVELOPE_KNOWN_KEYS = frozenset({
    "envelope_id", "generated_at", "records", "links", "version",
    "partial", "partial_reason",
})


def _scope_from_dict(d: "dict[str, Any] | None") -> "EvidenceScope | None":
    if not d:
        return None
    return EvidenceScope(
        project_id=d.get("project_id"),
        tenant_id=d.get("tenant_id"),
        subproject_id=d.get("subproject_id"),
        version=d.get("version"),
        sprint_item_id=d.get("sprint_item_id"),
    )


def _record_from_dict(d: "dict[str, Any]") -> EvidenceRecord:
    return EvidenceRecord(
        identity=_identity_from_dict(_get(d, "identity", ctx="EvidenceRecord")),
        timestamps=_timestamps_from_dict(_get(d, "timestamps", ctx="EvidenceRecord")),
        resolver=_resolver_state_from_dict(_get(d, "resolver", ctx="EvidenceRecord")),
        hashes=[_hash_from_dict(h) for h in d.get("hashes", [])],
        partial=bool(d.get("partial", False)),
        partial_reason=d.get("partial_reason"),
        attributes=dict(d.get("attributes") or {}),
        redacted=bool(d.get("redacted", False)),
        redaction_reason=d.get("redaction_reason"),
        scope=_scope_from_dict(d.get("scope")),
        schema_version=int(d.get("schema_version", 1) or 1),
        operation_key=d.get("operation_key"),
        parent_ids=list(d.get("parent_ids") or []),
        extra_fields={k: v for k, v in d.items() if k not in _RECORD_KNOWN_KEYS},
    )


def _link_from_dict(d: "dict[str, Any]") -> EvidenceLink:
    return EvidenceLink(
        id=_get(d, "id", ctx="EvidenceLink"),
        relation=_get(d, "relation", ctx="EvidenceLink"),
        source_id=_get(d, "source_id", ctx="EvidenceLink"),
        target_id=_get(d, "target_id", ctx="EvidenceLink"),
        resolver=_resolver_state_from_dict(_get(d, "resolver", ctx="EvidenceLink")),
        partial=bool(d.get("partial", False)),
        partial_reason=d.get("partial_reason"),
        note=d.get("note"),
        redacted=bool(d.get("redacted", False)),
        redaction_reason=d.get("redaction_reason"),
        extra_fields={k: v for k, v in d.items() if k not in _LINK_KNOWN_KEYS},
    )


def envelope_from_dict(d: "dict[str, Any]") -> ProvenanceEnvelope:
    """Inverse of :func:`envelope_to_dict`. Raises
    :class:`EnvelopeValidationError` (never a raw ``KeyError``/``TypeError``)
    for any malformed input."""
    if not isinstance(d, dict):
        raise EnvelopeValidationError(
            f"envelope_from_dict expects a dict, got {type(d)!r}"
        )
    try:
        return ProvenanceEnvelope(
            envelope_id=_get(d, "envelope_id", ctx="ProvenanceEnvelope"),
            generated_at=_get(d, "generated_at", ctx="ProvenanceEnvelope"),
            records=[_record_from_dict(r) for r in d.get("records", [])],
            links=[_link_from_dict(link) for link in d.get("links", [])],
            version=d.get("version", "1.0"),
            partial=bool(d.get("partial", False)),
            partial_reason=d.get("partial_reason"),
            extra_fields={k: v for k, v in d.items() if k not in _ENVELOPE_KNOWN_KEYS},
        )
    except EnvelopeValidationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise EnvelopeValidationError(f"malformed provenance envelope: {exc}") from exc


# ---------------------------------------------------------------------------
# Generic, lossless dict <-> XML codec (used only by the XML projection)
# ---------------------------------------------------------------------------
#
# Dict keys become an "entry"/"item" child's ``key`` ATTRIBUTE rather than
# the element's tag name, so arbitrary keys (including ones that aren't
# valid XML names) are always safe to round-trip. A ``type`` attribute on
# every element records the Python type so numbers/bools/None survive the
# round trip exactly (XML text alone can't distinguish "0" the int from "0"
# the string).

def _value_to_xml(tag: str, value: Any) -> ET.Element:
    if value is None:
        return ET.Element(tag, {"type": "null"})
    if isinstance(value, bool):
        el = ET.Element(tag, {"type": "bool"})
        el.text = "true" if value else "false"
        return el
    if isinstance(value, int):
        el = ET.Element(tag, {"type": "int"})
        el.text = str(value)
        return el
    if isinstance(value, float):
        el = ET.Element(tag, {"type": "float"})
        el.text = repr(value)
        return el
    if isinstance(value, str):
        el = ET.Element(tag, {"type": "str"})
        el.text = value
        return el
    if isinstance(value, list):
        el = ET.Element(tag, {"type": "list"})
        for item in value:
            el.append(_value_to_xml("item", item))
        return el
    if isinstance(value, dict):
        el = ET.Element(tag, {"type": "dict"})
        for k, v in value.items():
            child = _value_to_xml("entry", v)
            child.set("key", str(k))
            el.append(child)
        return el
    raise EnvelopeValidationError(
        f"cannot XML-encode value of type {type(value)!r} for tag {tag!r}"
    )


def _xml_to_value(el: ET.Element) -> Any:
    kind = el.get("type")
    if kind == "null":
        return None
    if kind == "bool":
        return el.text == "true"
    if kind == "int":
        try:
            return int(el.text)
        except (TypeError, ValueError) as exc:
            raise EnvelopeValidationError(
                f"malformed int content {el.text!r} in element <{el.tag}>"
            ) from exc
    if kind == "float":
        try:
            return float(el.text)
        except (TypeError, ValueError) as exc:
            raise EnvelopeValidationError(
                f"malformed float content {el.text!r} in element <{el.tag}>"
            ) from exc
    if kind == "str":
        return el.text or ""
    if kind == "list":
        return [_xml_to_value(child) for child in el]
    if kind == "dict":
        result: "dict[str, Any]" = {}
        for child in el:
            key = child.get("key")
            if key is None:
                raise EnvelopeValidationError(
                    f"dict entry element <{child.tag}> is missing its 'key' attribute"
                )
            result[key] = _xml_to_value(child)
        return result
    raise EnvelopeValidationError(
        f"element <{el.tag}> has missing/unknown type attribute {kind!r}"
    )


_XML_ROOT_TAG = "provenance_envelope"

# MDE-5 -- XML namespace for the provenance envelope root element. The
# generic dict<->XML codec above is entirely STRUCTURE/ATTRIBUTE-driven (see
# _value_to_xml: a dict entry's real key lives in the ``key`` attribute, not
# the element's tag name), so applying a namespace to the root is safe: it
# never affects how any child element is matched during parsing. Registered
# as the DEFAULT namespace (empty prefix) so ET.tostring emits a plain
# ``xmlns="..."`` on the root rather than a ``ns0:`` prefix on every element.
_XML_NAMESPACE = "https://schemas.usemeridian.us/provenance-envelope/v1"
ET.register_namespace("", _XML_NAMESPACE)


# ---------------------------------------------------------------------------
# Public serialize / parse
# ---------------------------------------------------------------------------

_SUPPORTED_FORMATS = ("json", "xml")


def serialize_provenance_envelope(envelope: ProvenanceEnvelope, format: str = "json") -> str:
    """Serialize ``envelope`` to a canonical string in ``format``
    (``"json"`` or ``"xml"``, default ``"json"``).

    Deterministic: JSON output is key-sorted with stable indentation, and
    dict-typed field encoding preserves insertion order for XML -- the same
    envelope always serializes to byte-identical output (matching this
    package's existing "same inputs always produce the same results"
    convention).

    Round-trips losslessly through :func:`parse_provenance_envelope` for
    both formats: ``parse_provenance_envelope(serialize_provenance_envelope(
    env, fmt), fmt) == env``.
    """
    fmt = (format or "").strip().lower()
    data = envelope_to_dict(envelope)
    if fmt == "json":
        return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
    if fmt == "xml":
        # MDE-5 -- the root element carries the provenance-envelope XML
        # namespace (Clark notation -> a plain xmlns="..." on the root once
        # tostring renders it, since the namespace is registered as the
        # default/empty prefix above). Every child element's tag stays a
        # bare "item"/"entry" (see _value_to_xml) and is never itself
        # namespaced -- safe, because this codec never matches on tag names
        # during parsing (see _xml_to_value), only on the "type"/"key"
        # attributes, which XML namespaces never qualify.
        root = _value_to_xml(f"{{{_XML_NAMESPACE}}}{_XML_ROOT_TAG}", data)
        return ET.tostring(root, encoding="unicode")
    raise EnvelopeValidationError(
        f"unsupported format {format!r}; expected one of {_SUPPORTED_FORMATS}"
    )


def parse_provenance_envelope(payload: str, format: str = "json") -> ProvenanceEnvelope:
    """Inverse of :func:`serialize_provenance_envelope`. Raises
    :class:`EnvelopeValidationError` (never a raw ``json.JSONDecodeError``/
    ``xml.etree.ElementTree.ParseError``) for malformed input."""
    fmt = (format or "").strip().lower()
    if fmt == "json":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise EnvelopeValidationError(f"invalid JSON provenance envelope: {exc}") from exc
    elif fmt == "xml":
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise EnvelopeValidationError(f"invalid XML provenance envelope: {exc}") from exc
        data = _xml_to_value(root)
    else:
        raise EnvelopeValidationError(
            f"unsupported format {format!r}; expected one of {_SUPPORTED_FORMATS}"
        )
    return envelope_from_dict(data)


# ---------------------------------------------------------------------------
# Canonical ordering / content-addressing (MDE-5)
# ---------------------------------------------------------------------------

def canonical_envelope_hash(envelope: ProvenanceEnvelope, *, algorithm: str = "sha256") -> str:
    """A stable, content-addressed digest of *envelope*, independent of the
    order records/links happen to have been APPENDED in.

    :func:`envelope_to_dict`/:func:`serialize_provenance_envelope`
    deliberately preserve the caller's own list order (an exact,
    order-faithful round trip -- see their own docstrings); this is the
    separate "canonical ordering" primitive for when two envelopes built
    from the SAME evidence in a different construction order need to
    compare/dedup/hash as equal. Records are sorted by ``identity.id``,
    links by ``id``, before hashing a compact canonical JSON encoding
    (sorted keys, no whitespace) via :func:`compute_content_hash` -- the same
    sha256-of-content idiom this module already uses, never Python's
    randomized ``hash()`` builtin. This is the only place in the module that
    reorders anything; every other codepath is order-preserving.
    """
    data = envelope_to_dict(envelope)
    canonical = dict(data)
    canonical["records"] = sorted(
        canonical.get("records") or [],
        key=lambda r: (r.get("identity") or {}).get("id") or "",
    )
    canonical["links"] = sorted(
        canonical.get("links") or [], key=lambda link: link.get("id") or "",
    )
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return compute_content_hash(payload, algorithm=algorithm)


# ---------------------------------------------------------------------------
# Merge -- "one partial resolver cannot erase other evidence" (MDE-5)
# ---------------------------------------------------------------------------

def _record_beats(current: EvidenceRecord, candidate: EvidenceRecord) -> bool:
    """True if *candidate* should REPLACE *current* for the same record id."""
    cur_auth, cand_auth = current.is_authoritative, candidate.is_authoritative
    if cand_auth and not cur_auth:
        return True
    if cur_auth and not cand_auth:
        return False
    # Both (non-)authoritative alike -- most-recently-updated wins; ties
    # (equal/unparseable timestamps) prefer the incoming candidate.
    return (candidate.timestamps.updated_at or "") >= (current.timestamps.updated_at or "")


def _link_beats(current: EvidenceLink, candidate: EvidenceLink) -> bool:
    cur_auth, cand_auth = current.is_authoritative, candidate.is_authoritative
    if cand_auth and not cur_auth:
        return True
    if cur_auth and not cand_auth:
        return False
    # EvidenceLink carries no timestamp of its own -- incoming wins ties,
    # matching EvidenceRecord's tie-break direction.
    return True


def merge_envelopes(
    base: ProvenanceEnvelope,
    incoming: ProvenanceEnvelope,
    *,
    envelope_id: "str | None" = None,
    generated_at: "str | None" = None,
) -> ProvenanceEnvelope:
    """Combine two envelopes' records/links by id WITHOUT letting a partial/
    non-authoritative contribution silently erase already-good evidence.

    MDE-5 acceptance: "one partial resolver cannot erase other evidence."
    For each record/link id present in either side:

    - An AUTHORITATIVE side (``is_authoritative`` -- resolver VERIFIED, not
      partial, not redacted) always wins over a non-authoritative one for
      the same id, regardless of which envelope it came from or which was
      merged "later". A resolver that only produced a partial/degraded
      result for an id another resolver already fully verified can never
      downgrade that record.
    - If both sides are (non-)authoritative alike, the more RECENTLY updated
      one wins (``timestamps.updated_at``, string-comparable ISO-8601);
      links (which carry no timestamp) prefer the incoming side on a tie.

    An id present on only one side is carried through unchanged. Never
    mutates ``base``/``incoming`` -- always returns a NEW
    :class:`ProvenanceEnvelope`. The merged envelope's own ``partial`` flag
    is the OR of both inputs' (an envelope built from one partial source is
    itself partial), with a combined, non-empty ``partial_reason``.
    """
    merged_records: "dict[str, EvidenceRecord]" = {}
    for rec in base.records:
        merged_records[rec.identity.id] = rec
    for rec in incoming.records:
        existing = merged_records.get(rec.identity.id)
        if existing is None or _record_beats(existing, rec):
            merged_records[rec.identity.id] = rec

    merged_links: "dict[str, EvidenceLink]" = {}
    for link in base.links:
        merged_links[link.id] = link
    for link in incoming.links:
        existing = merged_links.get(link.id)
        if existing is None or _link_beats(existing, link):
            merged_links[link.id] = link

    merged_partial = bool(base.partial or incoming.partial)
    merged_partial_reason = None
    if merged_partial:
        reasons = [r for r in (base.partial_reason, incoming.partial_reason) if r]
        merged_partial_reason = (
            "; ".join(dict.fromkeys(reasons)) if reasons
            else "merged envelope includes at least one partial source envelope"
        )

    return ProvenanceEnvelope(
        envelope_id=envelope_id or str(uuid.uuid4()),
        generated_at=generated_at or _utcnow_iso(),
        records=list(merged_records.values()),
        links=list(merged_links.values()),
        version=incoming.version or base.version,
        partial=merged_partial,
        partial_reason=merged_partial_reason,
    )


# ---------------------------------------------------------------------------
# Handoff integration surface (MDE-5) -- small, boundable projections a
# handoff manifest can embed without carrying the full envelope.
# ---------------------------------------------------------------------------

def evidence_status_summary(envelope: ProvenanceEnvelope) -> "dict[str, Any]":
    """Compact, machine-readable status summary of *envelope*: counts by
    resolver status, partial/redacted counts, and the envelope's own partial
    flag. This is the small, BOUNDED projection a handoff manifest embeds
    (MDE-5: "handoff includes machine-readable evidence status") -- never the
    full envelope, which can be arbitrarily large.
    """
    status_counts: "dict[str, int]" = {s.value: 0 for s in ResolverStatus}
    partial_records = 0
    redacted_records = 0
    authoritative_records = 0
    for rec in envelope.records:
        key = rec.resolver.status.value
        status_counts[key] = status_counts.get(key, 0) + 1
        if rec.partial:
            partial_records += 1
        if rec.redacted:
            redacted_records += 1
        if rec.is_authoritative:
            authoritative_records += 1
    partial_links = sum(1 for link in envelope.links if link.partial)
    return {
        "envelope_id": envelope.envelope_id,
        "generated_at": envelope.generated_at,
        "version": envelope.version,
        "partial": envelope.partial,
        "partial_reason": envelope.partial_reason,
        "record_count": len(envelope.records),
        "link_count": len(envelope.links),
        "authoritative_record_count": authoritative_records,
        "partial_record_count": partial_records,
        "redacted_record_count": redacted_records,
        "partial_link_count": partial_links,
        "status_counts": status_counts,
        "dangling_link_count": len(envelope.dangling_link_endpoints()),
    }


def trusted_pointers(
    envelope: ProvenanceEnvelope, *, limit: "int | None" = None,
) -> "list[dict[str, Any]]":
    """The subset of *envelope*'s records safe to hand a receiver as
    already-verified pointers, without re-resolving anything: exactly the
    records where :attr:`EvidenceRecord.is_authoritative` is True (VERIFIED
    resolver state, not partial, not redacted). Sorted by id for
    determinism. ``limit`` caps the count -- NEVER silently: a caller that
    needs to know whether more exist should compare
    ``len(trusted_pointers(env))`` against
    ``evidence_status_summary(env)["authoritative_record_count"]`` itself.
    """
    pointers = [
        {
            "id": rec.identity.id,
            "kind": rec.identity.kind.value,
            "locator": rec.identity.locator,
            "label": rec.identity.label,
        }
        for rec in envelope.records
        if rec.is_authoritative
    ]
    pointers.sort(key=lambda p: p["id"])
    if limit is not None:
        pointers = pointers[:limit]
    return pointers
