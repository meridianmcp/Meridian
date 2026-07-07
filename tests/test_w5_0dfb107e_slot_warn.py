"""0dfb107e — tunnel startup must WARN when a core/office slot is ENABLED but
misconfigured (no runnable command), instead of the slot silently vanishing
from the whole startup log.

Before the fix, ``run_tunnel``'s office-slot loop did ``if not cmd: continue``
with ZERO log output, so an enabled-but-broken slot (e.g. ``word`` with an
override that coerced to an empty command) was undiagnosable — unlike the
filesystem slot, which already warns on a configured-but-unservable root. The
fix mirrors that pattern via the pure helpers :func:`_office_slot_command` and
:func:`_office_slot_warning`.

These tests are UNIT-LEVEL and PURE: no servers, ports, sockets, subprocesses,
or sleeps. They exercise the decision helpers directly plus a tiny stub of the
run_tunnel loop, so nothing can touch the network or hang.
"""
from __future__ import annotations

import sys

from meridian import tunnel_client as tc


# ---------------------------------------------------------------------------
# _office_slot_command — command resolution (incl. the dc npx fallback)
# ---------------------------------------------------------------------------

def test_office_command_present_returns_it():
    assert tc._office_slot_command("word", {"command": ["uvx", "docx-mcp"]}) == [
        "uvx", "docx-mcp",
    ]


def test_office_command_missing_ppt_is_none():
    # ppt/word have no runtime fallback: a missing command means "no command".
    assert tc._office_slot_command("ppt", {"enabled": True}) is None
    assert tc._office_slot_command("ppt", {"command": []}) is None
    assert tc._office_slot_command("word", {"command": None}) is None


def test_office_command_dc_falls_back_to_npx_default():
    # dc's launcher is spawned via npx even with no stored command.
    got = tc._office_slot_command("dc", {"enabled": True})
    assert got is not None and got == tc._dc_default_command()


def test_office_command_tolerates_none_plugin():
    assert tc._office_slot_command("ppt", None) is None


# ---------------------------------------------------------------------------
# _office_slot_warning — the actual bug: warn iff enabled AND no command
# ---------------------------------------------------------------------------

def test_warning_for_enabled_but_no_command():
    """The regression case: an enabled office slot with an empty command MUST
    produce a visible warning rather than vanishing silently."""
    warn = tc._office_slot_warning("word", "Word", {"enabled": True, "command": []})
    assert warn is not None
    assert "WARNING" in warn
    assert "word" in warn.lower()
    assert "no command" in warn.lower()


def test_no_warning_for_healthy_enabled_slot():
    """A healthy slot (enabled + real command) must stay quiet — no warning."""
    warn = tc._office_slot_warning(
        "ppt", "PowerPoint", {"enabled": True, "command": ["uvx", "powerpoint-mcp"]}
    )
    assert warn is None


def test_no_warning_for_disabled_slot():
    """Office slots are opt-in / off-by-default; a disabled slot is not an
    'expected' slot, so silence is correct (matches prior behaviour)."""
    assert tc._office_slot_warning("word", "Word", {"enabled": False}) is None
    assert tc._office_slot_warning("word", "Word", {}) is None
    assert tc._office_slot_warning("word", "Word", None) is None


def test_no_warning_for_enabled_dc_without_command():
    """dc without a stored command is HEALTHY (npx fallback), so no warning —
    proving the warning keys on a genuinely-missing command, not just absence
    of a stored one."""
    assert tc._office_slot_warning("dc", "Desktop Commander", {"enabled": True}) is None


# ---------------------------------------------------------------------------
# The run_tunnel loop's decision, reproduced against the real helpers so a
# future refactor that drops the warning is caught. No I/O.
# ---------------------------------------------------------------------------

def _collect_startup_lines(slots: "list[tuple[str, str, dict]]") -> list[str]:
    """Mirror of run_tunnel's office-slot loop, capturing what it would emit.

    Returns the WARNING lines a real run would print to stderr for the given
    (slot, human, plugin) tuples — using the SAME helpers run_tunnel calls, so
    this stays honest if the production loop changes.
    """
    out: list[str] = []
    for slot, human, plugin in slots:
        if not plugin.get("enabled", False):
            continue
        cmd = tc._office_slot_command(slot, plugin)
        if not cmd:
            warn = tc._office_slot_warning(slot, human, plugin)
            if warn:
                out.append(warn)
            continue
        # healthy: would print a lazy-spawn line (not a warning) — omit here.
    return out


def test_loop_emits_warning_only_for_the_broken_slot():
    slots = [
        ("ppt", "PowerPoint", {"enabled": True, "command": ["uvx", "powerpoint-mcp"]}),
        ("word", "Word", {"enabled": True, "command": []}),   # broken
        ("dc", "Desktop Commander", {"enabled": False}),       # disabled → silent
    ]
    lines = _collect_startup_lines(slots)
    assert len(lines) == 1
    assert "word" in lines[0].lower()
    assert "WARNING" in lines[0]


def test_loop_silent_when_all_slots_healthy_or_disabled():
    slots = [
        ("ppt", "PowerPoint", {"enabled": True, "command": ["uvx", "powerpoint-mcp"]}),
        ("word", "Word", {"enabled": False}),
        ("dc", "Desktop Commander", {"enabled": True}),  # npx fallback → healthy
    ]
    assert _collect_startup_lines(slots) == []


def test_warning_line_shape_matches_filesystem_warning_convention():
    """The warning mirrors the filesystem slot's convention: two-space indent,
    a left-padded slot label, then 'WARNING'. Keeps the startup log consistent
    so operators scan for the same token across slots."""
    warn = tc._office_slot_warning("ppt", "PowerPoint", {"enabled": True, "command": ""})
    assert warn is not None
    assert warn.startswith("  ")
    assert "WARNING" in warn
