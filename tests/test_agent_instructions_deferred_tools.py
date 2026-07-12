"""Regression test for 1c81fee6.

DEFAULT_AGENT_INSTRUCTIONS' code-intel guidance used to be conditioned on the
tools being "in your tool list" and told the reader to "ignore this section" when
they were absent. Under deferred / tool-search tool loading (claude.ai, Desktop)
a tool is invisible until explicitly searched for, so a cold-start session
honestly saw the tools absent and skipped the whole protocol. The v5 rewording
must instead treat absence as a cue to run a discovery search first.
"""
from meridian.agent_defaults import (
    DEFAULT_AGENT_INSTRUCTIONS,
    AGENT_INSTRUCTIONS_STANDARD_VERSION,
)


def test_code_intel_protocol_survives_deferred_tool_loading():
    text = DEFAULT_AGENT_INSTRUCTIONS

    # The mandatory protocol heading is still present.
    assert "MANDATORY CODE INTEL PROTOCOL" in text

    # The flawed "skip it if the tool isn't listed" phrasing is gone.
    assert "ignore this section" not in text
    assert "If `trace_path` is not in your tool list" not in text

    # It now explicitly accounts for deferred / tool-search loading and tells the
    # reader to run a discovery search before concluding a tool is unavailable.
    lowered = text.lower()
    assert "tool-search" in lowered or "tool search" in lowered
    assert "deferred" in lowered
    assert "discovery" in lowered or "searched for" in lowered


def test_standard_version_bumped_for_rewording():
    # A meaningful default change re-syncs stored per-project copies via the marker.
    assert AGENT_INSTRUCTIONS_STANDARD_VERSION >= 5
    assert (
        f"meridian-executor-standard: v{AGENT_INSTRUCTIONS_STANDARD_VERSION}"
        in DEFAULT_AGENT_INSTRUCTIONS
    )
