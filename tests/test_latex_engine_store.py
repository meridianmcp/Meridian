"""Parity tests for the Python-stage local LaTeX project/provenance store."""

from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "extensions" / "meridian-latex" / "python"),
)

from meridian_latex import store  # noqa: E402


def test_open_store_creates_schema_with_foreign_keys_and_autocommit(tmp_path):
    db_path = tmp_path / "local" / "meridian-latex.db"
    connection = store.open_store(str(db_path))
    try:
        assert connection.isolation_level is None
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"projects", "claims", "provenance"} <= tables
        assert db_path.exists()
    finally:
        connection.close()


def test_project_upsert_and_ensure_preserve_existing_snapshot():
    connection = store.open_store()
    try:
        outline = [{"id": "section-a", "kind": "heading"}]
        store.upsert_project(connection, "overleaf-1", outline)
        initial = store.get_project(connection, "overleaf-1")
        assert initial is not None
        assert json.loads(initial["last_outline"]) == outline

        store.ensure_project_row(connection, "overleaf-1")
        assert store.get_project(connection, "overleaf-1") == initial

        replacement = [{"id": "section-b", "kind": "heading"}]
        store.upsert_project(connection, "overleaf-1", replacement)
        updated = store.get_project(connection, "overleaf-1")
        assert updated is not None
        assert json.loads(updated["last_outline"]) == replacement
        assert store.get_project(connection, "missing") is None
    finally:
        connection.close()


def test_provenance_crud_and_unsynced_filter():
    connection = store.open_store()
    try:
        first = store.record_edit(
            connection,
            project_id="overleaf-1",
            node_id="heading-1",
            kind="heading",
            field="title",
            old_value="Old title",
            new_value="New title",
            holder_token="holder-a",
        )
        second = store.record_edit(
            connection,
            project_id="overleaf-1",
            node_id="citation-1",
            kind="citation",
            field="key",
            old_value="oldkey",
            new_value="newkey",
            holder_token="holder-a",
        )
        assert first["recorded"] is True
        assert second["recorded"] is True

        connection.execute(
            "UPDATE provenance SET recorded_at = '2026-01-01T00:00:00.000Z' WHERE id = ?",
            (first["id"],),
        )
        connection.execute(
            "UPDATE provenance SET recorded_at = '2026-01-02T00:00:00.000Z' WHERE id = ?",
            (second["id"],),
        )
        newest_first = store.list_provenance(connection, project_id="overleaf-1")
        assert [row["id"] for row in newest_first] == [second["id"], first["id"]]
        assert newest_first[0]["old_value"] == "oldkey"
        assert newest_first[0]["synced_to_meridian_outputs"] == 0

        assert store.mark_synced(connection, [first["id"], "missing"]) == {"marked": 1}
        assert [row["id"] for row in store.list_provenance(
            connection, project_id="overleaf-1", unsynced_only=True
        )] == [second["id"]]
        first_row = next(
            row for row in store.list_provenance(connection, project_id="overleaf-1")
            if row["id"] == first["id"]
        )
        assert first_row["synced_to_meridian_outputs"] == 1
        assert first_row["synced_at"]
        assert store.list_provenance(connection, project_id="") == []
    finally:
        connection.close()


def test_record_edit_rejects_invalid_required_values():
    connection = store.open_store()
    try:
        missing = store.record_edit(
            connection,
            project_id="",
            node_id="n1",
            kind="heading",
            field="title",
            new_value="new",
            holder_token="holder-a",
        )
        assert missing["recorded"] is False
        assert "all required" in missing["reason"]

        bad_value = store.record_edit(
            connection,
            project_id="p1",
            node_id="n1",
            kind="heading",
            field="title",
            new_value=7,
            holder_token="holder-a",
        )
        assert bad_value == {"recorded": False, "reason": "new_value must be a string"}
        assert store.mark_synced(connection, "not-a-list") == {"marked": 0}
        assert store.mark_synced(connection, []) == {"marked": 0}
    finally:
        connection.close()


def test_ledger_reads_and_writes_degrade_after_connection_closes():
    connection = store.open_store()
    connection.close()
    result = store.record_edit(
        connection,
        project_id="p1",
        node_id="n1",
        kind="heading",
        field="title",
        new_value="new",
        holder_token="holder-a",
    )
    assert result["recorded"] is False
    assert "internal error" in result["reason"]
    assert store.list_provenance(connection, project_id="p1") == []
    assert store.mark_synced(connection, ["id"])["marked"] == 0
