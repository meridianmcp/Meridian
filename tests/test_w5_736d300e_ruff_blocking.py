"""736d300e — ruff correctness lint is now BLOCKING in CI (a curated clean subset).

Before this item the CI `lint` job ran `ruff check . || true` with a comment that
"violations do not fail CI yet" — i.e. ruff was purely advisory. This item makes a
scoped, correctness-critical rule set BLOCKING without a huge cleanup:

  * pyproject.toml `[tool.ruff.lint].select` pins a subset of pyflakes rules that are
    ALREADY at 0 violations (undefined names F821/F822/F823, malformed format strings
    F50x, misplaced statements F70x, nonsensical comparisons/asserts F60x/F63x).
  * .github/workflows/test.yml runs plain `ruff check .` (which honors that select set)
    WITHOUT `|| true` — a regression fails the build — plus a separate warn-only broad
    pass (`|| true`) that keeps the noisy style/import backlog visible.

These tests are static: they parse the two config files (no ruff install needed — ruff
is not in the default pixi env; CI installs it via `pip install ruff`), so they encode
the contract and guard against a silent regression back to the non-blocking form.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

# tests/ -> repo root
REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Codes that are known-noisy today (they have violations) and therefore MUST NOT be in
# the blocking select set, or CI would red-wall on push.
NOISY_CODES = {"F401", "F811", "F841", "F405", "F541", "E501"}


def _lint_job() -> dict:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert "lint" in data["jobs"], "expected a `lint` job in test.yml"
    return data["lint"] if "lint" in data else data["jobs"]["lint"]


def _lint_steps() -> list[dict]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return data["jobs"]["lint"]["steps"]


def _ruff_run_lines() -> list[str]:
    """Every `run:` command in the lint job that invokes ruff check."""
    lines: list[str] = []
    for step in _lint_steps():
        run = step.get("run")
        if run and "ruff check" in run:
            lines.append(run)
    return lines


def _ruff_config() -> dict:
    with PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    return data["tool"]["ruff"]


def _select() -> list[str]:
    return _ruff_config()["lint"]["select"]


# ---------------------------------------------------------------------------
# test.yml — the blocking ruff line no longer has `|| true`
# ---------------------------------------------------------------------------

def test_workflow_parses_and_has_lint_job():
    steps = _lint_steps()
    assert isinstance(steps, list) and steps, "lint job must have steps"


def test_there_is_a_blocking_ruff_check():
    """At least one `ruff check` step runs WITHOUT `|| true` (blocking)."""
    ruff_lines = _ruff_run_lines()
    assert ruff_lines, "lint job must still run ruff check"
    blocking = [ln for ln in ruff_lines if "|| true" not in ln]
    assert blocking, (
        "expected a blocking `ruff check` step (no `|| true`); found only "
        f"non-blocking invocations: {ruff_lines!r}"
    )


def test_blocking_line_is_plain_ruff_check():
    """The blocking step is the plain `ruff check .` that honors pyproject select."""
    blocking = [ln.strip() for ln in _ruff_run_lines() if "|| true" not in ln]
    assert any(
        re.fullmatch(r"ruff check \.", ln) for ln in blocking
    ), f"blocking ruff step should be exactly `ruff check .`; got {blocking!r}"


def test_blocking_ruff_has_no_true_suppressor():
    """No blocking ruff line may end with a `|| true` / `; true` success-swallow."""
    for ln in _ruff_run_lines():
        if "|| true" not in ln:
            assert "; true" not in ln, f"blocking ruff line swallows failure: {ln!r}"


def test_stale_nonblocking_comment_is_gone():
    """The old 'violations do not fail CI yet' comment on the blocking line is gone."""
    text = WORKFLOW.read_text(encoding="utf-8")
    # It's fine for the warn-only step to describe itself as non-failing, but the
    # exact stale phrasing that gated ALL of ruff must not be the only ruff step.
    assert not re.search(
        r"violations do not fail CI yet.*\n\s*run:\s*ruff check \. \|\| true",
        text,
    ), "the old warn-only-everything ruff step must be replaced by a blocking one"


def test_warn_only_broad_pass_still_present():
    """A non-blocking broad pass (`|| true`) keeps the style backlog visible."""
    ruff_lines = _ruff_run_lines()
    warn_only = [ln for ln in ruff_lines if "|| true" in ln]
    assert warn_only, (
        "expected a warn-only broad ruff pass (with `|| true`) so the noisy "
        "F401/F405/E-code backlog stays visible without failing CI"
    )


# ---------------------------------------------------------------------------
# pyproject.toml — the selected correctness rule set is encoded
# ---------------------------------------------------------------------------

def test_pyproject_has_ruff_lint_select():
    select = _select()
    assert isinstance(select, list) and select, "[tool.ruff.lint].select must be a non-empty list"


def test_select_includes_undefined_name_rules():
    """F821 (undefined-name) is the crown-jewel correctness rule and must be blocking."""
    select = set(_select())
    for code in ("F821", "F822", "F823"):
        assert code in select, f"correctness rule {code} must be in the blocking select set"


def test_select_includes_more_correctness_families():
    """Blocking set covers malformed format strings, misplaced stmts, bad comparisons."""
    select = set(_select())
    # at least one representative of each family we curated
    assert "F501" in select, "malformed %-format string rule (F50x) should be blocking"
    assert "F632" in select, "use-of-== -on-literal-type rule (F632) should be blocking"
    assert "F706" in select, "return-outside-function rule (F70x) should be blocking"


def test_select_excludes_known_noisy_codes():
    """The blocking set must NOT contain codes that still have violations today,
    otherwise the blocking gate would red-wall CI on every push."""
    select = set(_select())
    overlap = select & NOISY_CODES
    assert not overlap, (
        f"blocking select set includes still-dirty codes {sorted(overlap)}; "
        "these would fail CI. Move them into select only once their count reaches 0."
    )


def test_e501_still_ignored():
    """E501 (line-length backlog, 500+ hits) stays ignored so nothing spams on it."""
    ignore = _ruff_config()["lint"].get("ignore", [])
    assert "E501" in ignore, "E501 must remain ignored (large line-length backlog)"


def test_select_codes_are_wellformed():
    """Every selected code looks like a real ruff rule code (letter(s)+digits)."""
    for code in _select():
        assert re.fullmatch(r"[A-Z]+[0-9]+", code), f"malformed rule code: {code!r}"
