"""Regression checks for the Anthropic submission disclosure contract."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_public_data_handling_matches_github_write_surface() -> None:
    text = (ROOT / "docs" / "data-handling.md").read_text(encoding="utf-8")

    for tool in ("patch_file", "trigger_workflow", "create_issue"):
        assert tool in text
    assert "Read tools are read-only." in text
    assert "does not invoke these writes merely because a repository is connected" in text
    assert "doesn't push commits, open PRs, or modify your repo" not in text


def test_internal_directory_listing_names_github_write_tools() -> None:
    text = (ROOT / "docs" / "mcp-directory-listing.md").read_text(encoding="utf-8")

    for tool in ("patch_file", "trigger_workflow", "create_issue"):
        assert tool in text
    assert "read-only repo tools" not in text
