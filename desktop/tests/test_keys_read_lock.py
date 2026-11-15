"""The keys backup file is read-locked for agents: the project settings deny a read of it, and AGENTS.md says never to read it."""

from __future__ import annotations

import json
from pathlib import Path

from meridian_desktop.keys_export import EXPORT_NAME

REPO = Path(__file__).resolve().parents[1]
READ_RULE = f"Read(**/{EXPORT_NAME})"
ABSOLUTE_READ_RULE = f"Read(//**/{EXPORT_NAME})"
# The deny entries the project settings must keep. The desktop repo held none before this rule, so the read lock's own two
# rules are the entries it holds now. Any entry added later goes here too, so an edit that replaces the list instead of
# merging into it fails the test below.
EXISTING_DENY_ENTRIES: tuple[str, ...] = (READ_RULE, ABSOLUTE_READ_RULE)
AGENTS_SENTENCE = f"The keys backup file, {EXPORT_NAME}, must never be read by an agent."


def test_the_read_lock_names_the_one_export_file():
    assert EXPORT_NAME == "meridian-keys-backup.json"


def test_agents_md_says_the_keys_backup_file_is_never_read_by_an_agent():
    text = (REPO / "AGENTS.md").read_text(encoding="utf-8")

    assert AGENTS_SENTENCE in text


def test_the_project_settings_are_tracked_and_keep_every_deny_entry():
    # .claude/settings.json is tracked: the .gitignore re-includes it, so every checkout has the read lock. A missing file
    # means the lock is gone from this checkout, and the test fails rather than skipping.
    path = REPO / ".claude" / "settings.json"
    assert path.is_file(), "the read lock is missing: .claude/settings.json must be present in every checkout"
    deny = json.loads(path.read_text(encoding="utf-8"))["permissions"]["deny"]

    assert isinstance(deny, list)
    for entry in EXISTING_DENY_ENTRIES:
        assert entry in deny, f"the deny entry {entry} was dropped from .claude/settings.json"
