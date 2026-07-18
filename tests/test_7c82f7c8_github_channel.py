"""7c82f7c8 — channel labeling mechanism for auto-filed GitHub issues.

Covers:
  * patch_sprint_item's new ``github_channel`` field (set / clear / validate
    the {nightly, stable, graduated} enum), mirroring the existing
    blocker_kind pattern.
  * The .github/ISSUE_TEMPLATE/ files that apply channel:nightly /
    channel:stable via which template a reporter picks, rather than manual
    tagging.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from meridian import db as db_module

REPO_ROOT = Path(__file__).resolve().parents[1]
ISSUE_TEMPLATE_DIR = REPO_ROOT / ".github" / "ISSUE_TEMPLATE"


@pytest.mark.asyncio
async def test_patch_sprint_item_sets_github_channel(db):
    p = await db_module.create_project(db, "channel-set-test")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Investigate flaky retry bug")
    assert item.get("github_channel") is None

    updated = await db_module.patch_sprint_item(
        db, p["id"], item["id"], github_channel="nightly",
    )
    assert updated["github_channel"] == "nightly"

    fetched = await db_module.get_sprint_item(db, item["id"])
    assert fetched["github_channel"] == "nightly"


@pytest.mark.asyncio
async def test_patch_sprint_item_github_channel_graduated(db):
    """The third state: started nightly-only, now confirmed on stable too."""
    p = await db_module.create_project(db, "channel-graduate-test")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Investigate flaky retry bug")
    await db_module.patch_sprint_item(db, p["id"], item["id"], github_channel="nightly")

    graduated = await db_module.patch_sprint_item(
        db, p["id"], item["id"], github_channel="graduated",
    )
    assert graduated["github_channel"] == "graduated"


@pytest.mark.asyncio
async def test_patch_sprint_item_clears_github_channel(db):
    p = await db_module.create_project(db, "channel-clear-test")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Some bug")
    await db_module.patch_sprint_item(db, p["id"], item["id"], github_channel="stable")

    cleared = await db_module.patch_sprint_item(db, p["id"], item["id"], github_channel="")
    assert cleared["github_channel"] is None


@pytest.mark.asyncio
async def test_patch_sprint_item_github_channel_omitted_leaves_unchanged(db):
    p = await db_module.create_project(db, "channel-omit-test")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Some bug")
    await db_module.patch_sprint_item(db, p["id"], item["id"], github_channel="stable")

    unchanged = await db_module.patch_sprint_item(db, p["id"], item["id"], title="renamed")
    assert unchanged["github_channel"] == "stable"


@pytest.mark.asyncio
async def test_patch_sprint_item_github_channel_rejects_bad_value(db):
    p = await db_module.create_project(db, "channel-bad-value-test")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Some bug")

    with pytest.raises(ValueError, match="github_channel"):
        await db_module.patch_sprint_item(
            db, p["id"], item["id"], github_channel="beta",
        )


def _load_template(name: str) -> dict:
    path = ISSUE_TEMPLATE_DIR / name
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_bug_report_stable_template_labels_channel_stable():
    tpl = _load_template("bug_report.yml")
    assert "channel:stable" in tpl["labels"]
    assert "bug" in tpl["labels"]


def test_bug_report_nightly_template_labels_channel_nightly():
    tpl = _load_template("bug_report_nightly.yml")
    assert "channel:nightly" in tpl["labels"]
    assert "bug" in tpl["labels"]
    # Distinct template name so the picker page makes the channel choice
    # explicit to the reporter (the actual tagging mechanism).
    assert tpl["name"] != _load_template("bug_report.yml")["name"]


def test_channel_issue_templates_are_valid_and_have_required_fields():
    for name in ("bug_report.yml", "bug_report_nightly.yml"):
        tpl = _load_template(name)
        field_ids = {field.get("id") for field in tpl["body"] if isinstance(field, dict)}
        assert {"os", "client", "version", "steps", "expected", "actual"} <= field_ids
