"""Prose-check finding schema and the check runner (draft-prose-check-runner).

A prose check reads one drafted block and emits zero or more findings. This
module defines the finding shape, the severity vocabulary (the four levels of
the review ledger item model, so findings ingest without mapping) and a runner
that enforces these rules:

* a block with ``skip`` set to true is not checked;
* each registered check runs once per checked block;
* a ``model_judgement`` check may only emit candidates: severity ``info`` and a
  ``question`` for the author to answer. A judgement check that emits a higher
  severity, or a candidate without a question, rejects the whole run;
* output is ordered by block position, then ``check_id``, so two runs over the
  same blocks and checks return identical output.

The runner does not depend on the text-model block type. A block is anything
that exposes ``block_id`` and an optional boolean ``skip``, either as an
attribute or as a mapping key.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

EVIDENCE_MAX_CHARS = 200


class CheckMode(str, Enum):
    """How a check reaches its verdict."""

    DETERMINISTIC = "deterministic"
    MODEL_JUDGEMENT = "model_judgement"
    HYBRID = "hybrid"


class Severity(str, Enum):
    """Ledger severity vocabulary, most serious first, with ``info`` last."""

    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"


class ProseCheckError(ValueError):
    """An invalid finding, a malformed check, or a check that broke its contract."""


def _coerce_enum(enum_cls: type[Enum], value: Any, name: str) -> Any:
    try:
        return enum_cls(value)
    except ValueError:
        allowed = ", ".join(member.value for member in enum_cls)
        raise ProseCheckError(
            f"{name} must be one of {allowed}; got {value!r}"
        ) from None


def _require_text(name: str, value: Any, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ProseCheckError(f"{name} must be a string; got {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise ProseCheckError(f"{name} must not be empty")
    return value


@dataclass(frozen=True)
class Finding:
    """One prose-check finding, shaped for direct ledger ingestion."""

    check_id: str
    mode: CheckMode
    block_id: str
    source: str
    severity: Severity
    evidence: str
    suggestion: str | None = None
    question: str | None = None

    def __post_init__(self) -> None:
        _require_text("check_id", self.check_id)
        _require_text("block_id", self.block_id)
        _require_text("source", self.source)
        object.__setattr__(self, "mode", _coerce_enum(CheckMode, self.mode, "mode"))
        object.__setattr__(
            self, "severity", _coerce_enum(Severity, self.severity, "severity")
        )
        _require_text("evidence", self.evidence, allow_empty=True)
        if len(self.evidence) > EVIDENCE_MAX_CHARS:
            raise ProseCheckError(
                f"evidence must be at most {EVIDENCE_MAX_CHARS} characters; "
                f"got {len(self.evidence)}"
            )
        if self.suggestion is not None:
            _require_text("suggestion", self.suggestion)
        if self.question is not None:
            _require_text("question", self.question)

    def to_dict(self) -> dict[str, Any]:
        """Plain dict with enum values as strings, ready for JSON or ledger rows."""
        return {
            "check_id": self.check_id,
            "mode": self.mode.value,
            "block_id": self.block_id,
            "source": self.source,
            "severity": self.severity.value,
            "evidence": self.evidence,
            "suggestion": self.suggestion,
            "question": self.question,
        }


@dataclass(frozen=True)
class ProseCheck:
    """A registered check. ``run(block)`` returns the findings for one block."""

    check_id: str
    mode: CheckMode
    run: Callable[[Any], Iterable[Finding] | None]

    def __post_init__(self) -> None:
        _require_text("check_id", self.check_id)
        object.__setattr__(self, "mode", _coerce_enum(CheckMode, self.mode, "mode"))
        if not callable(self.run):
            raise ProseCheckError(f"check {self.check_id!r} run must be callable")


def _block_value(block: Any, name: str, default: Any = None) -> Any:
    if isinstance(block, Mapping):
        return block.get(name, default)
    return getattr(block, name, default)


def _validate_emitted(
    check: ProseCheck, block_id: str, emitted: Iterable[Finding] | None
) -> list[Finding]:
    if emitted is None:
        return []
    findings: list[Finding] = []
    for finding in emitted:
        if not isinstance(finding, Finding):
            raise ProseCheckError(
                f"check {check.check_id!r} emitted {type(finding).__name__}, "
                "not a Finding"
            )
        if finding.check_id != check.check_id:
            raise ProseCheckError(
                f"check {check.check_id!r} emitted a finding for "
                f"{finding.check_id!r}"
            )
        if finding.block_id != block_id:
            raise ProseCheckError(
                f"check {check.check_id!r} emitted a finding for block "
                f"{finding.block_id!r} while checking {block_id!r}"
            )
        if finding.mode is not check.mode:
            raise ProseCheckError(
                f"check {check.check_id!r} is {check.mode.value} but emitted a "
                f"{finding.mode.value} finding"
            )
        if check.mode is CheckMode.MODEL_JUDGEMENT:
            if finding.severity is not Severity.INFO:
                raise ProseCheckError(
                    f"model_judgement check {check.check_id!r} emitted severity "
                    f"{finding.severity.value!r}; judgement candidates must be info"
                )
            if not finding.question:
                raise ProseCheckError(
                    f"model_judgement check {check.check_id!r} emitted a candidate "
                    "without a question"
                )
        findings.append(finding)
    return findings


def run_checks(
    blocks: Iterable[Any], checks: Iterable[ProseCheck]
) -> list[Finding]:
    """Run every check over every non-skipped block and return sorted findings.

    Blocks flagged ``skip`` are not passed to any check. Each remaining block is
    checked once per registered check. Output is ordered by block position, then
    ``check_id``. A contract violation raises :class:`ProseCheckError` and no
    partial output is returned.
    """
    block_list = list(blocks)
    check_list = list(checks)

    seen: set[str] = set()
    duplicates: set[str] = set()
    for check in check_list:
        if not isinstance(check, ProseCheck):
            raise ProseCheckError(
                f"checks must be ProseCheck instances; got {type(check).__name__}"
            )
        if check.check_id in seen:
            duplicates.add(check.check_id)
        seen.add(check.check_id)
    if duplicates:
        raise ProseCheckError(
            "duplicate check_id(s): " + ", ".join(sorted(duplicates))
        )

    ordered_checks = sorted(check_list, key=lambda check: check.check_id)
    results: list[Finding] = []
    for block in block_list:
        block_id = _require_text("block_id", _block_value(block, "block_id"))
        skip = _block_value(block, "skip", False)
        if not isinstance(skip, bool):
            raise ProseCheckError(f"skip on block {block_id!r} must be a boolean")
        if skip:
            continue
        for check in ordered_checks:
            emitted = check.run(block)
            results.extend(_validate_emitted(check, block_id, emitted))
    return results
