"""The per-project Zotero scope file: a binding per project, kept on this computer. Offline, temp files only.

Nothing here touches the real settings folder: every test passes its own path under pytest's temp directory.
"""

import json

import pytest

from meridian_desktop import zotero_scope as zs


def _scope(project_id="proj-1", **changes):
    values = {
        "project_id": project_id,
        "library_type": "user",
        "library_id": "0",
        "library_name": "My Library",
        "collection_key": "AAAABBBB",
        "collection_name": "Thesis",
    }
    values.update(changes)
    return zs.ProjectScope(**values)


def _valid_entry(**changes):
    entry = {
        "library_type": "user",
        "library_id": "0",
        "library_name": "My Library",
        "collection_key": "CCCCDDDD",
        "collection_name": "Papers",
    }
    entry.update(changes)
    return entry


def _write(path, text):
    path.write_text(text, encoding="utf-8")


def _backup(path):
    return path.with_name(path.name + zs.BACKUP_SUFFIX)


def test_the_scope_file_sits_beside_the_settings_file_under_its_own_name():
    assert zs.default_path().name == zs.SCOPE_FILE_NAME
    assert zs.default_path().parent == zs.default_settings_path().parent


def test_a_missing_file_means_no_scopes_and_no_warning(tmp_path):
    read = zs.read_scopes(tmp_path / "scopes.json")
    assert read.scopes == {}
    assert read.warning == ""
    assert read.unreadable is False


def test_a_saved_scope_reads_back_exactly(tmp_path):
    path = tmp_path / "scopes.json"
    zs.save_scope(_scope(), path)
    read = zs.read_scopes(path)
    assert read.scopes == {"proj-1": _scope()}
    assert read.warning == ""
    assert read.unreadable is False


def test_the_file_is_versioned_sorted_json_keyed_by_project(tmp_path):
    path = tmp_path / "scopes.json"
    zs.save_scope(_scope("zeta"), path)
    zs.save_scope(_scope("alpha"), path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == zs.FILE_VERSION
    assert list(data["scopes"]) == ["alpha", "zeta"]
    assert data["scopes"]["alpha"]["collection_key"] == "AAAABBBB"


def test_two_projects_keep_their_own_collections_and_clearing_one_keeps_the_other(tmp_path):
    path = tmp_path / "scopes.json"
    zs.save_scope(_scope("p1"), path)
    zs.save_scope(_scope("p2", library_type="group", library_id="123", library_name="Lab Group",
                         collection_key="CCCCDDDD", collection_name="Papers"), path)
    read = zs.clear_scope("p1", path)
    assert set(read.scopes) == {"p2"}
    kept = zs.read_scopes(path).scopes["p2"]
    assert (kept.library_type, kept.library_id, kept.collection_name) == ("group", "123", "Papers")


def test_saving_a_project_again_replaces_its_collection(tmp_path):
    path = tmp_path / "scopes.json"
    zs.save_scope(_scope("p1", collection_key="AAAABBBB", collection_name="Old"), path)
    zs.save_scope(_scope("p1", collection_key="CCCCDDDD", collection_name="New"), path)
    assert zs.read_scopes(path).scopes["p1"].collection_name == "New"


def test_clearing_a_project_with_no_binding_changes_nothing(tmp_path):
    path = tmp_path / "scopes.json"
    zs.save_scope(_scope("p1"), path)
    before = path.read_bytes()
    zs.clear_scope("nobody", path)
    assert set(zs.read_scopes(path).scopes) == {"p1"}
    assert path.read_bytes() == before


def test_a_save_leaves_no_temporary_file_behind(tmp_path):
    path = tmp_path / "scopes.json"
    zs.save_scope(_scope(), path)
    assert sorted(item.name for item in tmp_path.iterdir()) == ["scopes.json"]


@pytest.mark.parametrize(
    "changes",
    [
        {"library_type": "other"},
        {"library_type": "user", "library_id": "5"},
        {"library_type": "group", "library_id": "abc"},
        {"library_type": "group", "library_id": ""},
        {"collection_key": "SHORT"},
        {"collection_key": "aaaabbbb"},
        {"collection_key": "AAAA/../"},
        {"project_id": ""},
        {"project_id": "  padded "},
        {"project_id": "x" * 201},
        {"library_name": "y" * 301},
        {"collection_name": None},
    ],
)
def test_an_invalid_scope_is_refused_when_it_is_made(changes):
    with pytest.raises(zs.ScopeError) as caught:
        _scope(**changes)
    assert caught.value.message == zs.INVALID_TEXT


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", '{"scopes": []}', '{"no": "scopes"}'])
def test_an_unreadable_file_is_reported_and_read_as_empty(tmp_path, text):
    path = tmp_path / "scopes.json"
    _write(path, text)
    read = zs.read_scopes(path)
    assert read.unreadable is True
    assert read.scopes == {}
    assert read.warning == zs.UNREADABLE_TEXT


def test_a_save_over_an_unreadable_file_is_refused_and_a_copy_is_kept(tmp_path):
    path = tmp_path / "scopes.json"
    _write(path, "{not json")
    with pytest.raises(zs.ScopeError) as caught:
        zs.save_scope(_scope(), path)
    assert caught.value.message == zs.UNREADABLE_TEXT
    assert path.read_text(encoding="utf-8") == "{not json"
    assert _backup(path).read_text(encoding="utf-8") == "{not json"


def test_a_clear_over_an_unreadable_file_is_refused_too(tmp_path):
    path = tmp_path / "scopes.json"
    _write(path, "[]")
    with pytest.raises(zs.ScopeError):
        zs.clear_scope("p1", path)
    assert path.read_text(encoding="utf-8") == "[]"


def test_when_the_copy_cannot_be_kept_nothing_is_written(tmp_path, monkeypatch):
    path = tmp_path / "scopes.json"
    _write(path, "{not json")
    monkeypatch.setattr(zs, "_keep_copy", lambda target: False)
    with pytest.raises(zs.ScopeError) as caught:
        zs.save_scope(_scope(), path)
    assert caught.value.message == zs.UNREADABLE_NO_COPY_TEXT
    assert path.read_text(encoding="utf-8") == "{not json"


def test_an_invalid_entry_is_left_out_with_a_warning_and_the_valid_ones_are_kept(tmp_path):
    path = tmp_path / "scopes.json"
    raw = {
        "version": 1,
        "scopes": {
            "good": _valid_entry(),
            "bad-library": _valid_entry(library_id="9", library_type="user"),
            "not-a-dict": "text",
        },
    }
    _write(path, json.dumps(raw))
    read = zs.read_scopes(path)
    assert set(read.scopes) == {"good"}
    assert read.warning == zs.SKIPPED_TEXT.format(2)
    assert read.unreadable is False


def test_a_save_over_a_file_with_invalid_entries_keeps_them_as_they_were(tmp_path):
    path = tmp_path / "scopes.json"
    bad = {"library_type": "nope"}
    raw = {"version": 1, "scopes": {"good": _valid_entry(), "bad": bad}}
    _write(path, json.dumps(raw))
    read = zs.save_scope(_scope("new"), path)
    assert set(zs.read_scopes(path).scopes) == {"good", "new"}
    assert json.loads(path.read_text(encoding="utf-8"))["scopes"]["bad"] == bad
    assert read.skipped == frozenset({"bad"})
    assert read.warning == zs.SKIPPED_TEXT.format(1)
    assert not _backup(path).exists()


def test_a_save_over_a_clean_file_keeps_no_copy_and_says_nothing(tmp_path):
    path = tmp_path / "scopes.json"
    zs.save_scope(_scope("p1"), path)
    read = zs.save_scope(_scope("p2"), path)
    assert not _backup(path).exists()
    assert read.warning == ""


def test_clearing_a_project_whose_entry_is_not_valid_removes_that_entry(tmp_path):
    path = tmp_path / "scopes.json"
    bad = {
        "library_type": "user",
        "library_id": "0",
        "library_name": "My Library",
        "collection_key": "abcd2345",
        "collection_name": "Notes",
    }
    _write(path, json.dumps({"version": 1, "scopes": {"good": _valid_entry(), "P1": bad}}))
    read = zs.clear_scope("P1", path)
    assert "P1" not in json.loads(path.read_text(encoding="utf-8"))["scopes"]
    fresh = zs.read_scopes(path)
    assert set(fresh.scopes) == {"good"}
    assert fresh.skipped == frozenset()
    assert read.skipped == frozenset()
    assert read.warning == ""


def test_binding_a_project_whose_entry_is_not_valid_replaces_that_entry(tmp_path):
    path = tmp_path / "scopes.json"
    bad = {
        "library_type": "user",
        "library_id": "0",
        "library_name": "My Library",
        "collection_key": "abcd2345",
        "collection_name": "Notes",
    }
    _write(path, json.dumps({"version": 1, "scopes": {"P1": bad, "P2": bad}}))
    written = zs.save_scope(_scope("P1"), path)
    fresh = zs.read_scopes(path)
    assert set(fresh.scopes) == {"P1"}
    assert fresh.scopes["P1"].collection_key == _scope("P1").collection_key
    assert fresh.skipped == frozenset({"P2"})
    assert written.skipped == frozenset({"P2"})
    assert written.warning == zs.SKIPPED_TEXT.format(1)
