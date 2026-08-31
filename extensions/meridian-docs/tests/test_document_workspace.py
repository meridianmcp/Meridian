"""Focused tests for the standalone document-workspace lineage primitive."""

from __future__ import annotations

import json

import pytest

from meridian_docs.document_workspace import (
    DocumentWorkspace,
    DocumentWorkspaceError,
    LineageValidationError,
    is_workspace_stale,
    snapshot_sha256,
    validate_lineage,
)


SOURCE_A = snapshot_sha256("source A")
SOURCE_B = snapshot_sha256("source B")


def workspace(workspace_id: str, **kwargs) -> DocumentWorkspace:
    return DocumentWorkspace(
        workspace_id=workspace_id,
        project_id=kwargs.pop("project_id", "project-1"),
        source_snapshot_sha256=kwargs.pop("source_snapshot_sha256", SOURCE_A),
        scope=kwargs.pop("scope", {"section": "methods", "include": [1, 2]}),
        profile=kwargs.pop("profile", {"name": "thesis", "revision": 2}),
        status=kwargs.pop("status", "active"),
        **kwargs,
    )


def test_workspace_round_trips_as_canonical_json_and_is_detached() -> None:
    original_scope = {"section": "methods", "include": [1, 2]}
    record = workspace("ws-2", scope=original_scope, parent_workspace_id="ws-1", supersedes="ws-old")

    original_scope["include"].append(3)
    serialized = record.to_json()
    restored = DocumentWorkspace.from_json(serialized)

    assert restored == record
    assert serialized == record.to_json()
    assert json.loads(serialized) == record.to_dict()
    assert record.parent_workspace_id == "ws-1"
    assert record.supersedes_workspace_id == "ws-old"
    assert record.to_dict()["scope"]["include"] == [1, 2]


def test_stale_detection_compares_current_source_hash() -> None:
    record = workspace("ws-1")

    assert record.is_stale(SOURCE_A) is False
    assert is_workspace_stale(record, SOURCE_B) is True


def test_workspace_rejects_non_json_scope_and_invalid_hash() -> None:
    with pytest.raises(DocumentWorkspaceError, match="JSON-safe"):
        workspace("ws-1", scope={"bad": object()})
    with pytest.raises(DocumentWorkspaceError, match="SHA-256"):
        workspace("ws-1", source_snapshot_sha256="not-a-hash")
    with pytest.raises(DocumentWorkspaceError, match="non-finite"):
        workspace("ws-1", scope={"bad": float("nan")})


def test_lineage_validation_is_deterministic_and_allows_two_relation_types() -> None:
    records = [
        workspace("ws-3", parent="ws-1", supersedes="ws-2"),
        workspace("ws-2"),
        workspace("ws-1"),
    ]

    assert validate_lineage(records) == ("ws-1", "ws-2", "ws-3")
    assert validate_lineage(reversed(records)) == ("ws-1", "ws-2", "ws-3")


@pytest.mark.parametrize(
    ("records", "message"),
    [
        ([workspace("ws-2", parent="missing"), workspace("ws-1")], "missing"),
        (
            [workspace("ws-1"), workspace("ws-1")],
            "duplicate workspace_id",
        ),
        (
            [workspace("ws-1", parent="ws-2"), workspace("ws-2", parent="ws-1")],
            "cycle",
        ),
        (
            [workspace("ws-1"), workspace("ws-2", parent="ws-1", project_id="project-2")],
            "crosses projects",
        ),
    ],
)
def test_lineage_validation_rejects_invalid_dags(records, message: str) -> None:
    with pytest.raises(LineageValidationError, match=message):
        validate_lineage(records)
