"""Presets: which built-in rule groups run, and how the input is cleaned first.

A preset never changes what a check means. It chooses the groups, and a
normaliser may clean markup before the checks run. Locations in the result refer
to the normalised text, and the result names the normaliser that was used. Only
``latex`` changes offsets; ``none`` and ``docx`` keep the input's offsets.

Presets (names are the values of the ``preset`` argument):

- ``general``: any plain prose, Markdown included. The default.
- ``academic``: stock phrases only. Dash and three-item-list flags are off.
- ``overleaf``: LaTeX source. Comments and commands are stripped, citations become
  ``[cite]`` markers, math is blanked, and ``---``/``--`` become dashes.
- ``docx``: text exported from Word. Curly quotes and non-breaking spaces are
  normalised first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .rules import GROUP_NAMES


@dataclass(frozen=True)
class Preset:
    name: str
    summary: str
    normalizer: str
    rule_groups: tuple[str, ...]


PRESETS: dict[str, Preset] = {
    preset.name: preset
    for preset in (
        Preset(
            "general",
            "Any plain prose, Markdown included. The default.",
            "none",
            ("stock_phrases", "dash_marks", "triad_lists"),
        ),
        Preset(
            "academic",
            "Academic prose. Stock phrases only; dash and three-item-list flags are off.",
            "none",
            ("stock_phrases",),
        ),
        Preset(
            "overleaf",
            "LaTeX source from Overleaf. Strips markup and comments; reads \\cite as [cite].",
            "latex",
            ("stock_phrases", "dash_marks", "triad_lists"),
        ),
        Preset(
            "docx",
            "Text exported from Word. Normalises curly quotes and non-breaking spaces.",
            "docx",
            ("stock_phrases", "dash_marks", "triad_lists"),
        ),
    )
}

PRESET_NAMES: tuple[str, ...] = tuple(PRESETS)

for _preset in PRESETS.values():
    for _group in _preset.rule_groups:
        if _group not in GROUP_NAMES:
            raise ValueError(f"preset {_preset.name} names unknown group {_group}")


def get_preset(name: str) -> Preset:
    """Return the preset called ``name``. Raises ValueError for an unknown name."""
    key = (name or "general").strip().lower()
    if key not in PRESETS:
        raise ValueError(f"unknown preset '{name}'. Choose one of: {', '.join(PRESET_NAMES)}")
    return PRESETS[key]


_COMMENT_RE = re.compile(r"(?<!\\)%[^\n]*")
_CITE_RE = re.compile(
    r"\\(?:cite[a-zA-Z]*|parencite|textcite|footcite)\*?(?:\[[^\]]*\]){0,2}\{[^}]*\}"
)
_REF_RE = re.compile(r"\\(?:ref|eqref|autoref|cref|Cref|pageref)\*?\{[^}]*\}")
_LABEL_RE = re.compile(r"\\label\{[^}]*\}")
_ENV_RE = re.compile(r"\\(?:begin|end)\{[^}]*\}")
_MATH_RE = re.compile(r"\$\$.*?\$\$|\$[^$\n]*\$", re.DOTALL)
_CMD_ARG_RE = re.compile(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?\{([^{}]*)\}")
_BARE_CMD_RE = re.compile(r"\\[a-zA-Z]+\*?")

_DOCX_MAP = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u00a0": " ",
        "\u202f": " ",
        "\u2007": " ",
        "\u2011": "-",
    }
)


def _normalize_latex(text: str) -> str:
    text = _COMMENT_RE.sub("", text)
    text = _CITE_RE.sub(" [cite] ", text)
    text = _REF_RE.sub(" ", text)
    text = _LABEL_RE.sub("", text)
    text = _MATH_RE.sub(" ", text)
    text = _ENV_RE.sub("", text)
    for _ in range(5):
        text, count = _CMD_ARG_RE.subn(r"\1", text)
        if count == 0:
            break
    text = _BARE_CMD_RE.sub("", text)
    text = text.replace("\\\\", "\n").replace("~", " ")
    text = text.replace("{", "").replace("}", "")
    text = text.replace("---", "\u2014").replace("--", "\u2013")
    return text


def _normalize_docx(text: str) -> str:
    return text.translate(_DOCX_MAP).replace("\r\n", "\n").replace("\r", "\n")


def normalize(text: str, normalizer: str) -> str:
    """Apply a named normaliser. ``none`` returns the text unchanged."""
    if normalizer == "none":
        return text
    if normalizer == "latex":
        return _normalize_latex(text)
    if normalizer == "docx":
        return _normalize_docx(text)
    raise ValueError(f"unknown normalizer '{normalizer}'")
