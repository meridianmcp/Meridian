"""Tests for draft-prose-check-runner (42e7b4ff).

Covers the finding schema, the skip rule, once-per-block execution, the
model_judgement severity and question rules, and stable output ordering.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from meridian.drafting.prose_checks import (
    EVIDENCE_MAX_CHARS,
    CheckMode,
    Finding,
    ProseCheck,
    ProseCheckError,
    Severity,
    run_checks,
)


@dataclass
class Block:
    block_id: str
    skip: bool = False


_BASE_FIELDS: dict[str, object] = {
    "check_id": "x",
    "mode": CheckMode.DETERMINISTIC,
    "block_id": "b1",
    "source": "test",
    "severity": Severity.MINOR,
    "evidence": "evidence text",
}


def _finding(check_id: str, block_id: str, **overrides: object) -> Finding:
    fields: dict[str, object] = {
        **_BASE_FIELDS,
        "check_id": check_id,
        "block_id": block_id,
    }
    fields.update(overrides)
    return Finding(**fields)  # type: ignore[arg-type]


def _block_id(block: object) -> str:
    if isinstance(block, dict):
        return str(block["block_id"])
    return str(getattr(block, "block_id"))


def _recording_check(check_id: str, mode: CheckMode, calls: list) -> ProseCheck:
    def run(block: object) -> list[Finding]:
        calls.append((check_id, _block_id(block)))
        return []

    return ProseCheck(check_id=check_id, mode=mode, run=run)


# Schema validation


def test_finding_holds_every_schema_field() -> None:
    finding = _finding(
        "hedge-density",
        "b1",
        suggestion="Tighten the claim.",
        question="Is this claim supported?",
        severity=Severity.INFO,
        mode=CheckMode.MODEL_JUDGEMENT,
    )

    assert finding.check_id == "hedge-density"
    assert finding.mode is CheckMode.MODEL_JUDGEMENT
    assert finding.block_id == "b1"
    assert finding.source == "test"
    assert finding.severity is Severity.INFO
    assert finding.suggestion == "Tighten the claim."
    assert finding.question == "Is this claim supported?"


def test_finding_to_dict_uses_plain_vocabulary_strings() -> None:
    finding = _finding("x", "b1", severity=Severity.MAJOR, mode=CheckMode.HYBRID)

    assert finding.to_dict() == {
        "check_id": "x",
        "mode": "hybrid",
        "block_id": "b1",
        "source": "test",
        "severity": "major",
        "evidence": "evidence text",
        "suggestion": None,
        "question": None,
    }


def test_finding_accepts_string_severity_and_mode_from_vocabulary() -> None:
    finding = _finding("x", "b1", severity="blocker", mode="deterministic")

    assert finding.severity is Severity.BLOCKER
    assert finding.mode is CheckMode.DETERMINISTIC


def test_finding_rejects_severity_outside_ledger_vocabulary() -> None:
    with pytest.raises(ProseCheckError, match="severity"):
        _finding("x", "b1", severity="critical")


def test_finding_rejects_unknown_mode() -> None:
    with pytest.raises(ProseCheckError, match="mode"):
        _finding("x", "b1", mode="guess")


def test_evidence_at_the_limit_is_accepted() -> None:
    finding = _finding("x", "b1", evidence="a" * EVIDENCE_MAX_CHARS)

    assert len(finding.evidence) == EVIDENCE_MAX_CHARS


def test_evidence_over_the_limit_is_rejected() -> None:
    with pytest.raises(ProseCheckError, match="evidence"):
        _finding("x", "b1", evidence="a" * (EVIDENCE_MAX_CHARS + 1))


@pytest.mark.parametrize("field", ["check_id", "block_id", "source"])
def test_required_identifier_fields_must_not_be_blank(field: str) -> None:
    with pytest.raises(ProseCheckError, match=field):
        Finding(**{**_BASE_FIELDS, field: "   "})  # type: ignore[arg-type]


def test_optional_suggestion_must_be_non_blank_when_given() -> None:
    with pytest.raises(ProseCheckError, match="suggestion"):
        _finding("x", "b1", suggestion="")


# Skip rule and once-per-block execution


def test_blocks_flagged_skip_are_not_checked() -> None:
    calls: list[tuple[str, str]] = []
    check = _recording_check("c1", CheckMode.DETERMINISTIC, calls)

    run_checks([Block("b1", skip=True), Block("b2")], [check])

    assert calls == [("c1", "b2")]


def test_each_check_runs_exactly_once_per_block() -> None:
    calls: list[tuple[str, str]] = []
    checks = [
        _recording_check("alpha", CheckMode.DETERMINISTIC, calls),
        _recording_check("beta", CheckMode.HYBRID, calls),
    ]
    blocks = [Block("b1"), Block("b2"), Block("b3", skip=True)]

    run_checks(blocks, checks)

    assert sorted(calls) == sorted(
        [(c, b) for c in ("alpha", "beta") for b in ("b1", "b2")]
    )
    assert len(calls) == len(set(calls)) == 4


def test_mapping_blocks_are_supported() -> None:
    calls: list[tuple[str, str]] = []
    check = _recording_check("c1", CheckMode.DETERMINISTIC, calls)

    run_checks(
        [{"block_id": "m1"}, {"block_id": "m2", "skip": True}],
        [check],
    )

    assert calls == [("c1", "m1")]


def test_non_boolean_skip_is_rejected() -> None:
    check = _recording_check("c1", CheckMode.DETERMINISTIC, [])

    with pytest.raises(ProseCheckError, match="skip"):
        run_checks([{"block_id": "b1", "skip": "yes"}], [check])


def test_check_returning_none_yields_no_findings() -> None:
    check = ProseCheck(
        check_id="quiet", mode=CheckMode.DETERMINISTIC, run=lambda block: None
    )

    assert run_checks([Block("b1")], [check]) == []


# model_judgement severity and question rules


@pytest.mark.parametrize("severity", [Severity.MINOR, Severity.MAJOR, Severity.BLOCKER])
def test_judgement_check_rejects_a_higher_severity(severity: Severity) -> None:
    def run(block: Block) -> list[Finding]:
        return [
            _finding(
                "judge",
                block.block_id,
                mode=CheckMode.MODEL_JUDGEMENT,
                severity=severity,
                question="Is this supported?",
            )
        ]

    check = ProseCheck(check_id="judge", mode=CheckMode.MODEL_JUDGEMENT, run=run)

    with pytest.raises(ProseCheckError, match="judgement"):
        run_checks([Block("b1")], [check])


def test_judgement_candidate_without_question_is_rejected() -> None:
    def run(block: Block) -> list[Finding]:
        return [
            _finding(
                "judge",
                block.block_id,
                mode=CheckMode.MODEL_JUDGEMENT,
                severity=Severity.INFO,
            )
        ]

    check = ProseCheck(check_id="judge", mode=CheckMode.MODEL_JUDGEMENT, run=run)

    with pytest.raises(ProseCheckError, match="question"):
        run_checks([Block("b1")], [check])


def test_judgement_info_candidate_with_question_is_accepted() -> None:
    def run(block: Block) -> list[Finding]:
        return [
            _finding(
                "judge",
                block.block_id,
                mode=CheckMode.MODEL_JUDGEMENT,
                severity=Severity.INFO,
                question="Does the paragraph answer the research question?",
            )
        ]

    check = ProseCheck(check_id="judge", mode=CheckMode.MODEL_JUDGEMENT, run=run)

    findings = run_checks([Block("b1")], [check])

    assert len(findings) == 1
    assert findings[0].severity is Severity.INFO
    assert findings[0].question == "Does the paragraph answer the research question?"


def test_hybrid_check_may_emit_a_higher_severity() -> None:
    def run(block: Block) -> list[Finding]:
        return [_finding("hyb", block.block_id, mode=CheckMode.HYBRID, severity="major")]

    check = ProseCheck(check_id="hyb", mode=CheckMode.HYBRID, run=run)

    findings = run_checks([Block("b1")], [check])

    assert findings[0].severity is Severity.MAJOR


def test_finding_whose_mode_disagrees_with_its_check_is_rejected() -> None:
    def run(block: Block) -> list[Finding]:
        return [_finding("c1", block.block_id, mode=CheckMode.HYBRID)]

    check = ProseCheck(check_id="c1", mode=CheckMode.DETERMINISTIC, run=run)

    with pytest.raises(ProseCheckError, match="emitted a hybrid finding"):
        run_checks([Block("b1")], [check])


def test_finding_for_another_block_or_check_is_rejected() -> None:
    wrong_block = ProseCheck(
        check_id="c1",
        mode=CheckMode.DETERMINISTIC,
        run=lambda block: [_finding("c1", "elsewhere")],
    )
    wrong_check = ProseCheck(
        check_id="c1",
        mode=CheckMode.DETERMINISTIC,
        run=lambda block: [_finding("other", block.block_id)],
    )

    with pytest.raises(ProseCheckError, match="block"):
        run_checks([Block("b1")], [wrong_block])
    with pytest.raises(ProseCheckError, match="emitted a finding for"):
        run_checks([Block("b1")], [wrong_check])


def test_check_emitting_a_non_finding_is_rejected() -> None:
    check = ProseCheck(
        check_id="c1",
        mode=CheckMode.DETERMINISTIC,
        run=lambda block: [{"check_id": "c1"}],
    )

    with pytest.raises(ProseCheckError, match="not a Finding"):
        run_checks([Block("b1")], [check])


# Registration and stable ordering


def test_duplicate_check_ids_are_rejected() -> None:
    checks = [
        ProseCheck(check_id="same", mode=CheckMode.DETERMINISTIC, run=lambda b: None),
        ProseCheck(check_id="same", mode=CheckMode.HYBRID, run=lambda b: None),
    ]

    with pytest.raises(ProseCheckError, match="duplicate"):
        run_checks([Block("b1")], checks)


def test_output_is_ordered_by_block_then_check_id() -> None:
    def make(check_id: str) -> ProseCheck:
        return ProseCheck(
            check_id=check_id,
            mode=CheckMode.DETERMINISTIC,
            run=lambda block: [_finding(check_id, block.block_id)],
        )

    # Registered out of order on purpose, and blocks not in id order.
    checks = [make("zeta"), make("alpha")]
    blocks = [Block("b2"), Block("b1")]

    findings = run_checks(blocks, checks)

    assert [(f.block_id, f.check_id) for f in findings] == [
        ("b2", "alpha"),
        ("b2", "zeta"),
        ("b1", "alpha"),
        ("b1", "zeta"),
    ]


def test_repeated_runs_over_the_same_input_are_identical() -> None:
    def make(check_id: str, mode: CheckMode) -> ProseCheck:
        def run(block: Block) -> list[Finding]:
            if mode is CheckMode.MODEL_JUDGEMENT:
                return [
                    _finding(
                        check_id,
                        block.block_id,
                        mode=mode,
                        severity=Severity.INFO,
                        question="Is this clear?",
                    )
                ]
            return [_finding(check_id, block.block_id, mode=mode)]

        return ProseCheck(check_id=check_id, mode=mode, run=run)

    checks = [
        make("det", CheckMode.DETERMINISTIC),
        make("judge", CheckMode.MODEL_JUDGEMENT),
    ]
    blocks = [Block("b1"), Block("b2", skip=True), Block("b3")]

    first = run_checks(blocks, checks)
    second = run_checks(blocks, checks)

    assert first == second
    assert [f.to_dict() for f in first] == [f.to_dict() for f in second]
