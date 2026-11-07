"""Presets and input normalisers: what each preset turns on, and how markup is cleaned."""

import pytest

from meridian.prose import PRESET_NAMES, get_preset
from meridian.prose.presets import PRESETS, normalize


def test_the_four_documented_presets_exist_and_general_is_the_default():
    assert PRESET_NAMES == ("general", "academic", "overleaf", "docx")
    assert get_preset(None).name == "general"
    assert get_preset("  Academic ").name == "academic"


def test_unknown_preset_is_rejected_with_the_choices_listed():
    with pytest.raises(ValueError, match="general, academic, overleaf, docx"):
        get_preset("strict")


def test_academic_turns_off_dash_and_triad_groups_but_keeps_stock_phrases():
    assert PRESETS["academic"].rule_groups == ("stock_phrases",)
    assert set(PRESETS["general"].rule_groups) == {"stock_phrases", "dash_marks", "triad_lists"}


def test_none_normaliser_returns_the_input_unchanged():
    text = "Plain text, with 40% and [1]."
    assert normalize(text, "none") is text


def test_latex_normaliser_strips_comments_commands_and_math_and_marks_citations():
    source = (
        "Results\\cite{lee2023} are clear. % a hidden comment\n"
        "\\textbf{Bold} claim \\ref{fig:1} with $x = 3$ end."
    )
    cleaned = normalize(source, "latex")
    assert "[cite]" in cleaned
    assert "hidden" not in cleaned
    assert "\\" not in cleaned and "$" not in cleaned
    assert "Bold" in cleaned and "lee2023" not in cleaned
    assert "3" not in cleaned


def test_latex_normaliser_turns_double_and_triple_hyphens_into_dashes():
    assert normalize("a --- b -- c", "latex") == "a — b – c"


def test_docx_normaliser_straightens_quotes_and_replaces_non_breaking_spaces():
    cleaned = normalize("It’s a “quote” here", "docx")
    assert cleaned == "It's a \"quote\" here"


def test_unknown_normaliser_is_rejected():
    with pytest.raises(ValueError):
        normalize("text", "pdf")
