"""The per-project backup helpers: the newest backup time for each project, a backup of selected projects only, and the
time text for the Last backup column. Pure Python, a temp folder, and fake reads only. No socket, no real backups folder."""

from datetime import datetime, timezone

import pytest

from meridian_desktop import projects_backup as pb


def _rows(*ids):
    return [{"id": pid, "name": f"Project {pid}", "created_at": "2026-01-01T00:00:00Z"} for pid in ids]


def _at(day, hour=0, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc)


def test_last_backed_up_keeps_the_newest_time_for_each_project(tmp_path):
    first, second = _at(1, 8), _at(2, 9, 30)
    pb.write_backup(_rows("a", "b"), folder=tmp_path, taken_at=first)
    pb.write_backup(_rows("b"), folder=tmp_path, taken_at=second)

    assert pb.last_backed_up(tmp_path) == {"a": first, "b": second}


def test_a_project_in_no_backup_has_no_time(tmp_path):
    pb.write_backup(_rows("a"), folder=tmp_path, taken_at=_at(1))

    assert "zzz" not in pb.last_backed_up(tmp_path)


def test_a_backup_of_selected_projects_holds_only_those(tmp_path):
    rows = _rows("a", "b", "c")
    written = pb.backup_hosted(
        lambda url, token: rows, "https://example.test", "token", folder=tmp_path, taken_at=_at(2), only_ids={"b"}
    )

    assert written.count == 1
    assert set(pb.last_backed_up(tmp_path)) == {"b"}


def test_a_selection_that_matches_no_project_writes_nothing(tmp_path):
    with pytest.raises(pb.NoUsableProjects):
        pb.backup_hosted(
            lambda url, token: _rows("a"), "https://example.test", "token", folder=tmp_path, taken_at=_at(2), only_ids={"zzz"}
        )

    assert list(tmp_path.iterdir()) == []


def test_a_backup_file_that_cannot_be_read_is_skipped_and_the_others_still_count(tmp_path):
    pb.write_backup(_rows("a"), folder=tmp_path, taken_at=_at(1))
    (tmp_path / "projects-20261103T000000Z.json").write_text("not json", encoding="utf-8")

    assert set(pb.last_backed_up(tmp_path)) == {"a"}


def test_backup_time_text_reads_in_plain_words_in_utc():
    assert pb.backup_time_text(_at(2, 8, 25)) == "2 Nov 2026 08:25 UTC"
