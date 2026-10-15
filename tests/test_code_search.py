"""Tests for the local BM25 code search (meridian.code_search)."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from meridian import code_search as cs


def _git_init(root: Path) -> None:
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def base(tmp_path: Path) -> Path:
    """Index directory kept separate from the project so nothing leaks into the repo."""
    return tmp_path / "indexes"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git_init(root)
    return root


def test_split_identifiers_breaks_camel_case_and_underscores():
    assert cs.split_identifiers("ReadPipeline_v2") == "read pipeline v2"
    assert cs.split_identifiers("parseHTTPResponse") == "parse http response"


def test_query_terms_keeps_raw_and_split_forms():
    terms = cs.query_terms("ReadPipeline")
    assert "readpipeline" in terms
    assert "read" in terms and "pipeline" in terms


def test_secret_binary_and_oversized_files_are_never_indexed(project, base, monkeypatch):
    _write(project, ".env", "API_TOKEN=hunter2supersecret\n")
    _write(project, "meridian.toml", "password = 'hunter2supersecret'\n")
    _write(project, "keys/server.pem", "hunter2supersecret\n")
    _write(project, "blob.py", "hunter2supersecret\x00\x01\n")
    monkeypatch.setattr(cs, "MAX_FILE_BYTES", 64)
    _write(project, "big.py", "hunter2supersecret " * 20)
    _write(project, "app.py", "def handler():\n    return 1\n")

    report = cs.refresh_index(project, base)

    assert report.total == 1
    assert cs.search(project, "hunter2supersecret", base=base, refresh=False) == []
    assert [h["path"] for h in cs.search(project, "handler", base=base)] == ["app.py"]


def test_bm25_ranks_the_document_with_more_matches_first(project, base):
    _write(project, "alpha.py", "pipeline pipeline pipeline pipeline pipeline\n")
    _write(project, "beta.py", "one mention of pipeline here\n")
    _write(project, "gamma.py", "nothing relevant at all\n")

    hits = cs.search(project, "pipeline", base=base)

    assert [h["path"] for h in hits] == ["alpha.py", "beta.py"]
    assert hits[0]["score"] > hits[1]["score"] > 0


def test_camel_case_query_matches_split_identifiers(project, base):
    _write(project, "reader.py", "class ReadPipeline:\n    pass\n")

    assert [h["path"] for h in cs.search(project, "read pipeline", base=base)] == ["reader.py"]
    assert [h["path"] for h in cs.search(project, "ReadPipeline", base=base)] == ["reader.py"]


def test_underscore_identifiers_match_their_parts(project, base):
    _write(project, "tools.py", "def search_code():\n    return None\n")

    assert [h["path"] for h in cs.search(project, "search", base=base)] == ["tools.py"]


def test_incremental_refresh_picks_up_edits_and_deletions(project, base):
    _write(project, "a.py", "old content here\n")
    _write(project, "b.py", "stays put\n")
    first = cs.refresh_index(project, base)
    assert first.total == 2 and first.updated == 2

    _write(project, "a.py", "brand new wording\n")
    (project / "b.py").unlink()
    second = cs.refresh_index(project, base)

    assert second.updated == 1
    assert second.removed == 1
    assert second.total == 1
    assert cs.search(project, "wording", base=base, refresh=False)[0]["path"] == "a.py"
    assert cs.search(project, "stays", base=base, refresh=False) == []


def test_search_refreshes_before_querying(project, base):
    _write(project, "a.py", "first version\n")
    cs.refresh_index(project, base)
    _write(project, "a.py", "second version with fresh terms\n")

    hits = cs.search(project, "fresh", base=base)

    assert [h["path"] for h in hits] == ["a.py"]


def test_no_terms_or_missing_index_return_empty(project, base):
    assert cs.search(project, "   ", base=base) == []
    assert cs.search(project, "anything", base=base, refresh=False) == []


def test_limit_is_clamped(project, base):
    for i in range(3):
        _write(project, f"f{i}.py", "shared term\n")
    assert len(cs.search(project, "shared", limit=1, base=base)) == 1
    assert len(cs.search(project, "shared", limit=10_000, base=base)) == 3


def test_missing_directory_raises(tmp_path, base):
    with pytest.raises(cs.CodeSearchError):
        cs.refresh_index(tmp_path / "does-not-exist", base)


def test_directory_walk_fallback_skips_vendor_dirs(tmp_path, base):
    root = tmp_path / "plain"
    _write(root, "src/mod.py", "walked content\n")
    _write(root, "node_modules/dep.js", "walked content\n")
    _write(root, ".pixi/envs/x.py", "walked content\n")

    report = cs.refresh_index(root, base)

    assert report.total == 1
    assert [h["path"] for h in cs.search(root, "walked", base=base)] == ["src/mod.py"]


def test_list_indexes_reports_roots_and_counts(project, base):
    _write(project, "a.py", "hello\n")
    cs.refresh_index(project, base)

    listed = cs.list_indexes(base)

    assert len(listed) == 1
    assert listed[0]["root"] == str(project.resolve())
    assert listed[0]["files"] == 1
    assert cs.list_indexes(base / "missing") == []


def test_index_dir_honors_override_and_local_appdata(tmp_path):
    assert cs.index_dir({"MERIDIAN_CODE_SEARCH_DIR": str(tmp_path)}) == tmp_path
    assert cs.index_dir({"LOCALAPPDATA": str(tmp_path)}) == tmp_path / "meridian" / "code-search"


def test_mcp_server_exposes_the_three_tools():
    server = cs.build_server()
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == {"search_code", "index_project", "list_projects"}


def test_cli_index_and_search_roundtrip(project, base, capsys, monkeypatch):
    monkeypatch.setenv("MERIDIAN_CODE_SEARCH_DIR", str(base))
    _write(project, "a.py", "cli roundtrip token\n")

    assert cs.main(["index", str(project)]) == 0
    assert cs.main(["search", str(project), "roundtrip"]) == 0
    out = capsys.readouterr().out
    assert '"path": "a.py"' in out


def test_cli_reports_bad_project_as_error(tmp_path, capsys):
    assert cs.main(["index", str(tmp_path / "nope")]) == 1
    assert "must be an existing directory" in capsys.readouterr().err
