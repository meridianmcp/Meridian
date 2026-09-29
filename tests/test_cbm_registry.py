"""55d48d69 -- codebase-memory registry snapshot builder + resolver (meridian/cbm_registry.py).

Every DB here is a temp SQLite file built by the test with the same schema as
codebase-memory-mcp's ``<project>.db`` (projects / nodes via AUTOINCREMENT so
``sqlite_sequence`` is populated / file_hashes). The real cache directory is
never read.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from meridian import cbm_registry as reg
from meridian.cbm_registry import DictFS, RealFS

NOW = 1_790_452_800  # 2026-09-26T20:00:00Z


def _iso(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def make_db(cache: Path, name: str, root: Path | str, *, indexed_at: str = "2026-09-26T18:00:00Z", nodes: int = 5,
            deleted: int = 0, rel_paths: tuple[str, ...] = ("a.py", "pkg/b.py", "tests/t.py"), file_hashes: bool = True) -> Path:
    db = cache / f"{name}.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE projects (name TEXT PRIMARY KEY, indexed_at TEXT, root_path TEXT)")
        conn.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT, name TEXT)")
        if file_hashes:
            conn.execute("CREATE TABLE file_hashes (project TEXT, rel_path TEXT, sha256 TEXT, mtime_ns INTEGER, size INTEGER)")
            for rp in rel_paths:
                conn.execute("INSERT INTO file_hashes VALUES (?, ?, 'x', 0, 0)", (name, rp))
        conn.execute("INSERT INTO projects VALUES (?, ?, ?)", (name, indexed_at, str(root).replace("\\", "/")))
        for i in range(nodes):
            conn.execute("INSERT INTO nodes (project, name) VALUES (?, ?)", (name, f"n{i}"))
        if deleted:
            conn.execute("DELETE FROM nodes WHERE id <= ?", (deleted,))
        conn.commit()
    finally:
        conn.close()
    return db


@pytest.fixture
def cache(tmp_path) -> Path:
    c = tmp_path / "cbm-cache"
    c.mkdir()
    return c


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "Repo"
    (r / ".git").mkdir(parents=True)
    (r / "pkg").mkdir()
    return r


def _env(tmp_path) -> dict:
    return {"USERPROFILE": str(tmp_path / "home"), "LOCALAPPDATA": str(tmp_path / "lad")}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("C:\\Users\\13144\\Documents", "C:/Users/13144/Documents"),
    ("c:/users/x/", "C:/users/x"),
    ("C:", "C:/"),
    ("c:\\", "C:/"),
    ("C:/a/./b/../c", "C:/a/c"),
    ("C:/..", "C:/"),
    ("/home/u//x/", "/home/u/x"),
    ("/", "/"),
    ("\\\\server\\share\\dir", "//server/share/dir"),
    ("//server", "//server"),
    ("", None),
    ("   ", None),
    (None, None),
    (5, None),
])
def test_norm_path(raw, expected):
    assert reg.norm_path(raw) == expected


def test_norm_path_relative_and_msys():
    assert reg.norm_path("pkg/x.py", "C:\\repo") == "C:/repo/pkg/x.py"
    assert reg.norm_path("../y", "C:/repo/a") == "C:/repo/y"
    assert reg.norm_path("rel", None) is None
    assert reg.norm_path("rel", "also-relative") is None
    assert reg.norm_path("/c/Users/x", msys=True) == "C:/Users/x"
    assert reg.norm_path("/mnt/d/data", msys=True) == "D:/data"
    assert reg.norm_path("/c/Users/x") == "/c/Users/x", "no MSYS mapping unless asked"
    assert reg.norm_path("/c", msys=True) == "C:/"


def test_parent_is_under_rel():
    assert reg.parent_path("C:/a/b") == "C:/a"
    assert reg.parent_path("C:/a") == "C:/"
    assert reg.parent_path("C:/") == "C:/"
    assert reg.parent_path("/a") == "/"
    assert reg.parent_path("/") == "/"
    assert reg.is_under("c:/a/b", "c:/a") and reg.is_under("c:/a", "c:/a")
    assert not reg.is_under("c:/ab", "c:/a")
    assert reg.is_under("c:/x", "c:/")
    assert not reg.is_under(None, "c:/a") and not reg.is_under("c:/a", "")
    assert reg.rel_path("C:/A/b/c", "c:/a") == "b/c"
    assert reg.rel_path("C:/A", "c:/a") == ""
    assert reg.rel_path("D:/x", "c:/a") == "D:/x"


@pytest.mark.parametrize("root,expected", [
    ("C:/Users/13144/Documents/dnabert-error-correction", "C-Users-13144-Documents-dnabert-error-correction"),
    ("C:/Users/13144/Documents/Meridian/repository", "C-Users-13144-Documents-Meridian-repository"),
    ("C:\\Users\\13144\\Documents\\Masters_Thesis\\CURRENT_PROJECT_CODE", "C-Users-13144-Documents-Masters_Thesis-CURRENT_PROJECT_CODE"),
    ("/home/u/my project", "home-u-my-project"),
    ("/home/u/a..b/.hidden", "home-u-a.b-.hidden"),
    ("/", "root"),
    ("C:/", "C"),
    ("", "root"),
    (None, "root"),
    ("---", "root"),
    ("/home/\u00e9t\u00e9", "home-c3a9tc3a9"),
])
def test_slug_mirrors_cbm_project_name_from_path(root, expected):
    assert reg.slug(root) == expected


def test_slug_caps_long_names_with_fnv_hash():
    long_root = "/data/" + "/".join(["segment%02d" % i for i in range(40)])
    s = reg.slug(long_root)
    assert len(s) == 200
    assert s[191] == "-" and all(c in "0123456789abcdef" for c in s[192:])
    other = reg.slug(long_root + "x")
    assert s[:191] == other[:191] and s != other, "shared prefix, distinct hash suffix"


def test_parse_indexed_at():
    assert reg.parse_indexed_at("2026-09-26T20:00:00Z") == NOW
    assert reg.parse_indexed_at("2026-09-26T20:00:00") == NOW
    assert reg.parse_indexed_at("garbage") == 0
    assert reg.parse_indexed_at(None) == 0
    assert reg.parse_indexed_at("2026-13-45T99:99:99Z") == 0, "an impossible date is unparseable, not an exception"


def test_env_dirs():
    assert reg.guard_dir({"LOCALAPPDATA": "C:\\U\\AppData\\Local"}) == "C:/U/AppData/Local/meridian/guard"
    assert reg.guard_dir({"USERPROFILE": "C:\\U"}) == "C:/U/AppData/Local/meridian/guard"
    assert reg.guard_dir({"HOME": "/home/u"}) == "/home/u/.local/state/meridian/guard"
    assert reg.guard_dir({"HOME": "/home/u", "XDG_STATE_HOME": "/st"}) == "/st/meridian/guard"
    assert reg.guard_dir({}) is None
    assert reg.guard_dir(None) is None
    # guard-integ round 2: MERIDIAN_GUARD_DIR overrides everything else, including
    # when LOCALAPPDATA is also set -- this is the single resolver guard_core uses
    # for PreToolUse/PostToolUse, so this is what makes it agree with session_brief.
    assert reg.guard_dir({"LOCALAPPDATA": "C:\\U\\AppData\\Local", "MERIDIAN_GUARD_DIR": "D:\\reloc"}) == "D:/reloc"
    assert reg.guard_dir({"meridian_guard_dir": "D:\\reloc"}) == "D:/reloc", "env keys are case-insensitive"
    assert reg.default_cache_dir({"USERPROFILE": "C:\\U"}) == "C:/U/.cache/codebase-memory-mcp"
    assert reg.default_cache_dir({"USERPROFILE": "C:\\U", "cbm_cache_dir": "D:\\cbm"}) == "D:/cbm", "env keys are case-insensitive"
    assert reg.default_cache_dir({}) is None
    assert reg.home_dir({"HOME": "/c/Users/u"}) == "C:/Users/u"
    assert reg.upper_env({"a": "1", 2: "x", "b": 3}) == {"A": "1"}
    assert reg.default_snapshot_path({"LOCALAPPDATA": "C:\\L"}) == "C:/L/meridian/guard/snapshot.json"
    assert reg.default_snapshot_path({}) is None


# ---------------------------------------------------------------------------
# Filesystem probes + git layout
# ---------------------------------------------------------------------------


def test_realfs_probe(tmp_path):
    fs = RealFS()
    f = tmp_path / "f.txt"
    f.write_text("hello world", encoding="utf-8")
    assert fs.kind(str(tmp_path)) == "dir"
    assert fs.kind(str(f)) == "file"
    assert fs.kind(str(tmp_path / "nope")) is None
    assert fs.kind(None) is None  # type: ignore[arg-type]
    assert fs.read_text(str(f), 5) == "hello"
    assert fs.read_text(str(tmp_path / "nope")) is None
    assert fs.mtime(str(f)) == pytest.approx(f.stat().st_mtime)
    assert fs.mtime(str(tmp_path / "nope")) is None


def test_dictfs_probe():
    fs = DictFS({"dirs": ["C:/a/b"], "files": {"C:/a/f.txt": "abc", "C:/a/n.bin": None, 5: "x"},
                 "mtimes": {"C:/a/f.txt": 10, "C:/a/bad": "x"}}, overlay={"files": {"C:/z/o": "1"}})
    assert fs.kind("c:\\A\\B") == "dir" and fs.kind("C:/") == "dir" and fs.kind("C:/z") == "dir"
    assert fs.kind("C:/a/F.TXT") == "file" and fs.kind("C:/nope") is None and fs.kind("") is None
    assert fs.read_text("C:/a/f.txt", 2) == "ab" and fs.read_text("C:/a/n.bin") is None and fs.read_text("") is None
    assert fs.mtime("c:/a/f.txt") == 10.0 and fs.mtime("C:/a/bad") is None and fs.mtime("") is None
    assert DictFS("not a dict").kind("C:/") is None  # type: ignore[arg-type]


def test_git_roots_real_layout(tmp_path):
    fs = RealFS()
    main = tmp_path / "main"
    (main / ".git" / "worktrees" / "wt1").mkdir(parents=True)
    (main / "src").mkdir()
    (main / "src" / "x.py").write_text("x", encoding="utf-8")
    wt = tmp_path / "wt1"
    (wt / "src").mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {(main / '.git' / 'worktrees' / 'wt1').as_posix()}\n", encoding="utf-8")
    (main / ".git" / "worktrees" / "wt1" / "commondir").write_text("../..\n", encoding="utf-8")
    sub = tmp_path / "sub"
    (sub / "d").mkdir(parents=True)
    (sub / ".git").write_text("gitdir: ../main/.git/modules/sub\n", encoding="utf-8")
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").write_text("not a gitdir line\n", encoding="utf-8")
    emptycommon = tmp_path / "emptycommon"
    (emptycommon / "g").mkdir(parents=True)
    (emptycommon / ".git").write_text(f"gitdir: {(emptycommon / 'g').as_posix()}\n", encoding="utf-8")
    (emptycommon / "g" / "commondir").write_text("\n", encoding="utf-8")

    m = reg.norm_path(str(main))
    assert reg.git_roots(str(main / "src" / "x.py"), fs) == (m, m, False)
    w = reg.norm_path(str(wt))
    assert reg.git_roots(str(wt / "src"), fs) == (w, m, True)
    s = reg.norm_path(str(sub))
    assert reg.git_roots(str(sub / "d"), fs) == (s, s, False), "a gitfile without commondir is its own repo"
    b = reg.norm_path(str(broken))
    assert reg.git_roots(str(broken), fs) == (b, b, False)
    e = reg.norm_path(str(emptycommon))
    assert reg.git_roots(str(emptycommon), fs) == (e, e, True)
    assert reg.git_roots("", fs) == (None, None, False)


def test_git_roots_none_outside_repo():
    fs = DictFS({"dirs": ["D:/x/y"]})
    assert reg.git_roots("D:/x/y", fs) == (None, None, False)
    assert reg.git_roots("D:/nonexistent/z", fs) == (None, None, False)


# ---------------------------------------------------------------------------
# Builder (temp sqlite DBs)
# ---------------------------------------------------------------------------


def test_read_db_row_uses_sqlite_sequence_not_count(cache, repo):
    db = make_db(cache, "proj", repo, nodes=5, deleted=2)
    info = reg.read_db_row(str(db))
    assert info["nodes"] == 5, "sqlite_sequence seq, not count(*) (which would be 3)"
    assert info["name"] == "proj"
    assert info["root_path"] == str(repo).replace("\\", "/")
    assert info["covered_dirs"] == ["", "pkg", "tests"]


def test_read_db_row_without_file_hashes_or_nodes(cache, repo):
    db = make_db(cache, "bare", repo, nodes=0, file_hashes=False)
    info = reg.read_db_row(str(db))
    assert info["nodes"] == 0 and info["covered_dirs"] is None


def test_read_db_row_backslash_rel_paths(cache, repo):
    db = make_db(cache, "bs", repo, rel_paths=("pkg\\a.py", "top.py"))
    assert reg.read_db_row(str(db))["covered_dirs"] == ["", "pkg"]


def test_read_db_row_is_read_only(cache, repo):
    db = make_db(cache, "ro", repo)
    before = db.read_bytes()
    reg.read_db_row(str(db))
    assert db.read_bytes() == before
    assert not Path(str(db) + "-journal").exists()


def test_read_db_row_raises_on_empty_projects(cache, repo):
    db = cache / "empty.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE projects (name TEXT, indexed_at TEXT, root_path TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError):
        reg.read_db_row(str(db))


def test_build_snapshot_rows_and_skips(cache, repo, tmp_path):
    slug_name = reg.slug(reg.norm_path(str(repo)))
    make_db(cache, slug_name, repo, indexed_at="2026-09-26T18:00:00Z", nodes=10)
    make_db(cache, "old-dup", repo, indexed_at="2026-08-01T00:00:00Z", nodes=500)
    make_db(cache, "_config", repo)
    (cache / "broken.db.corrupt").write_bytes(b"x")
    (cache / "notes.txt").write_text("x", encoding="utf-8")
    (cache / "adir.db").mkdir()
    gone = tmp_path / "gone-root"
    gone.mkdir()
    make_db(cache, "gone", gone)
    gone.rmdir()
    snap = reg.build_snapshot(str(cache), env=_env(tmp_path), now=NOW)
    names = sorted(r["name"] for r in snap["rows"])
    assert names == sorted([slug_name, "old-dup"])
    assert snap["stats"]["dropped_missing_root"] == 1
    assert snap["schema"] == reg.SNAPSHOT_SCHEMA and snap["built_at"] == NOW
    slugrow = next(r for r in snap["rows"] if r["slug_match"])
    assert slugrow["nodes"] == 10 and slugrow["indexed_epoch"] == reg.parse_indexed_at("2026-09-26T18:00:00Z")
    assert slugrow["root_key"] == slugrow["root"].lower()
    assert slugrow["db"].endswith(".db") and slugrow["wal"] == slugrow["db"] + "-wal"
    assert len(slugrow["sig"]) == 4 and slugrow["sig"][2] == -1
    assert reg.valid_snapshot(snap) is not None


def test_build_snapshot_raises_only_for_unlistable_cache(tmp_path):
    with pytest.raises(OSError):
        reg.build_snapshot(str(tmp_path / "missing"), env=_env(tmp_path), now=NOW)


class CountingReader:
    def __init__(self, fail: set[str] | None = None):
        self.calls: list[str] = []
        self.fail = fail or set()

    def __call__(self, path: str) -> dict:
        self.calls.append(os.path.basename(path))
        if os.path.basename(path) in self.fail:
            raise sqlite3.OperationalError("database is locked")
        return reg.read_db_row(path)


def test_signature_reuse_shm_touch_and_wal_change(cache, repo, tmp_path):
    db = make_db(cache, "p", repo)
    r1 = CountingReader()
    s1 = reg.build_snapshot(str(cache), env=_env(tmp_path), now=NOW, reader=r1)
    assert r1.calls == ["p.db"]
    # a -shm touch (every reader does this) must NOT trigger a re-read
    shm = Path(str(db) + "-shm")
    shm.write_bytes(b"\0" * 64)
    os.utime(shm, (NOW + 100, NOW + 100))
    r2 = CountingReader()
    s2 = reg.build_snapshot(str(cache), s1, env=_env(tmp_path), now=NOW + 1, reader=r2)
    assert r2.calls == [] and s2["stats"]["reused"] == 1
    # a .db-wal change DOES trigger a re-read
    wal = Path(str(db) + "-wal")
    wal.write_bytes(b"")
    os.utime(wal, (NOW + 200, NOW + 200))
    r3 = CountingReader()
    s3 = reg.build_snapshot(str(cache), s2, env=_env(tmp_path), now=NOW + 2, reader=r3)
    assert r3.calls == ["p.db"]
    row = s3["rows"][0]
    assert row["sig"][2] == 0 and row["sig"][3] == int(wal.stat().st_mtime_ns)


def test_locked_db_keeps_last_good_row(cache, repo, tmp_path):
    make_db(cache, "p", repo, nodes=7)
    s1 = reg.build_snapshot(str(cache), env=_env(tmp_path), now=NOW)
    s1["rows"][0]["sig"] = [0, 0, 0, 0]  # force a re-read
    s2 = reg.build_snapshot(str(cache), s1, env=_env(tmp_path), now=NOW + 1, reader=CountingReader(fail={"p.db"}))
    assert s2["stats"]["kept_last_good"] == 1
    assert s2["rows"][0]["nodes"] == 7
    s3 = reg.build_snapshot(str(cache), None, env=_env(tmp_path), now=NOW + 1, reader=CountingReader(fail={"p.db"}))
    assert s3["rows"] == [] and s3["stats"]["skipped"] == 1


def test_really_locked_db_times_out_fast(cache, repo):
    db = make_db(cache, "locked", repo)
    holder = sqlite3.connect(str(db), isolation_level=None, timeout=0)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        with pytest.raises(sqlite3.OperationalError):
            reg.read_db_row(str(db))
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_servers_pins_automem_and_no_env_values(cache, repo, tmp_path):
    make_db(cache, "p", repo)
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {
            "codebase-memory-mcp": {"command": "C:/Users/x/.local/bin/codebase-memory-mcp.exe", "env": {"TOKEN": "sk_live_SECRET_1"}},
            "remote": {"type": "http", "url": "https://example/mcp", "headers": {"Authorization": "Bearer SECRET_2"}},
            "other": {"command": "node", "args": ["x"]},
            "renamed": {"command": "/opt/bin/codebase-memory-mcp", "env": {"K": "SECRET_3"}},
        },
        "projects": {str(repo): {"mcpServers": {"cbm-local": {"command": "codebase-memory-mcp"}}}, "bad": 5},
    }), encoding="utf-8")
    (home / ".claude" / "settings.json").write_text(json.dumps({"autoMemoryDirectory": "~/mem-dir"}), encoding="utf-8")
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"codebase-memory": {"command": "codebase-memory-mcp",
                                                                                   "env": {"X": "SECRET_4"}}}}), encoding="utf-8")
    gdir = tmp_path / "lad" / "meridian" / "guard"
    gdir.mkdir(parents=True)
    (gdir / "pins.json").write_text(json.dumps({str(repo): "p", "C:/bad": "has space", 7: "x"}), encoding="utf-8")
    snap = reg.build_snapshot(str(cache), env=_env(tmp_path), now=NOW, project_dirs=[str(repo)])
    dumped = json.dumps(snap)
    for secret in ("SECRET_1", "SECRET_2", "SECRET_3", "SECRET_4", "sk_live"):
        assert secret not in dumped
    assert snap["servers"]["user"] == ["codebase-memory-mcp", "renamed"]
    assert snap["servers"]["projects"][reg.norm_path(str(repo)).lower()] == ["codebase-memory"], ".mcp.json wins"
    assert snap["pins"] == {reg.norm_path(str(repo)).lower(): "p"}
    assert snap["automem_dirs"] == [reg.norm_path(str(home)) + "/mem-dir"]


def test_detect_servers_tolerates_garbage(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    (home / ".claude.json").write_text("{not json", encoding="utf-8")
    assert reg.detect_servers(reg.norm_path(str(home)), [str(tmp_path / "nope"), None, ""]) == {"user": [], "projects": {}}
    assert reg.detect_servers(None, []) == {"user": [], "projects": {}}


def test_write_snapshot_atomic_and_load(tmp_path):
    out = tmp_path / "g" / "snapshot.json"
    snap = {"schema": reg.SNAPSHOT_SCHEMA, "rows": []}
    reg.write_snapshot_atomic(str(out), snap)
    assert reg.load_snapshot(str(out))["rows"] == []
    assert [p.name for p in out.parent.iterdir()] == ["snapshot.json"], "no temp files left behind"
    out.write_text("{broken", encoding="utf-8")
    assert reg.load_snapshot(str(out)) is None


def test_write_snapshot_atomic_cleans_up_on_failure(tmp_path, monkeypatch):
    out = tmp_path / "g" / "snapshot.json"

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(reg.os, "replace", boom)
    with pytest.raises(OSError):
        reg.write_snapshot_atomic(str(out), {"schema": reg.SNAPSHOT_SCHEMA, "rows": []})
    assert list(out.parent.iterdir()) == []


def test_refresh_writes_and_keeps_last_good(cache, repo, tmp_path, monkeypatch):
    make_db(cache, "p", repo)
    out = tmp_path / "snap" / "snapshot.json"
    code, snap = reg.refresh(str(out), cache_dir=str(cache), env=_env(tmp_path), now=NOW)
    assert code == 0 and snap["rows"][0]["name"] == "p"
    good = out.read_text(encoding="utf-8")
    # an unreadable cache dir keeps the previous snapshot untouched
    code2, prev = reg.refresh(str(out), cache_dir=str(tmp_path / "no-cache"), env=_env(tmp_path), now=NOW + 1)
    assert code2 == 1 and prev is not None and out.read_text(encoding="utf-8") == good
    # a failing write keeps it too
    monkeypatch.setattr(reg, "write_snapshot_atomic", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    code3, _ = reg.refresh(str(out), cache_dir=str(cache), env=_env(tmp_path), now=NOW + 2)
    assert code3 == 1 and out.read_text(encoding="utf-8") == good


def test_refresh_without_paths():
    assert reg.refresh(None, env={}) == (2, None)
    code, snap = reg.refresh("unused.json", env={"LOCALAPPDATA": "C:\\nowhere"})
    assert code == 1 and snap is None


def test_refresh_default_paths_from_env(cache, repo, tmp_path):
    make_db(cache, "p", repo)
    env = dict(_env(tmp_path), CBM_CACHE_DIR=str(cache))
    code, snap = reg.refresh(env=env, now=NOW)
    assert code == 0
    assert (tmp_path / "lad" / "meridian" / "guard" / "snapshot.json").is_file()


def test_cli_refresh_and_resolve(cache, repo, tmp_path, monkeypatch, capsys):
    make_db(cache, "p", repo, indexed_at=_iso(int(time.time())))
    out = tmp_path / "s.json"
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    monkeypatch.delenv("MERIDIAN_CBM_PROJECT", raising=False)
    assert reg.main(["--refresh", "--out", str(out), "--cache-dir", str(cache)]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["rows"] == 1 and first["exit"] == 0
    assert reg.main(["--out", str(out), "--resolve", str(repo / "pkg")]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["mode"] == "own" and res["project"] == "p" and res["freshness"]["fresh"] is True
    assert reg.main(["--out", str(tmp_path / "missing.json"), "--resolve", str(repo)]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "none"
    assert reg.main(["--refresh", "--out", str(out), "--cache-dir", str(tmp_path / "nope")]) == 1
    assert json.loads(capsys.readouterr().out)["exit"] == 1
    assert reg.main([]) == 0
    assert "usage" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def _row(name: str, root: str, epoch: int, nodes: int = 1, slug_match: bool | None = None, covered=None) -> dict:
    return {"name": name, "root": root, "root_key": root.lower(), "indexed_at": _iso(epoch), "indexed_epoch": epoch,
            "nodes": nodes, "slug_match": reg.slug(root) == name if slug_match is None else slug_match,
            "covered_dirs": covered or [""], "db": f"C:/cache/{name}.db", "wal": f"C:/cache/{name}.db-wal", "sig": [1, 1, -1, -1]}


def _snap(rows, pins=None, servers=None) -> dict:
    return {"schema": reg.SNAPSHOT_SCHEMA, "rows": rows, "pins": pins or {}, "servers": servers or {}}


VFS = DictFS({
    "dirs": ["C:/R/.git/worktrees/w", "C:/R/src/deep", "C:/W/src", "C:/NG/sub", "C:/S/pkg", "C:/S/.git"],
    "files": {"C:/W/.git": "gitdir: C:/R/.git/worktrees/w\n", "C:/R/.git/worktrees/w/commondir": "../..\n",
              "C:/R/src/f.py": None},
})


def test_tiebreak_order():
    a = _row("zzz", "C:/R", NOW - 10, nodes=1)
    b = _row("aaa", "C:/R", NOW - 10, nodes=1)
    c = _row("big", "C:/R", NOW - 10, nodes=9)
    d = _row("new", "C:/R", NOW - 1, nodes=1)
    s = _row("C-R", "C:/R", NOW - 999999, nodes=0)
    assert s["slug_match"] is True
    assert reg.pick([a, b])[0]["name"] == "aaa", "smallest name last"
    assert reg.pick([a, b, c])[0]["name"] == "big", "most nodes before name"
    assert reg.pick([a, b, c, d])[0]["name"] == "new", "newest before nodes"
    w, why, shadowed = reg.pick([a, b, c, d, s])
    assert w["name"] == "C-R" and why == "slug-name match" and shadowed == ["aaa", "big", "new", "zzz"]
    w2, why2, _ = reg.pick([a, b, c, d, s], pin_name="ZZZ")
    assert w2["name"] == "zzz" and why2 == "pin"
    assert reg.pick([a])[1] == "only index"


def test_resolve_modes():
    rows = [_row("own", "C:/R", NOW), _row("sub", "C:/R/src", NOW), _row("ng", "C:/NG", NOW)]
    snap = _snap(rows)
    r = reg.resolve("c:\\r\\src\\deep", snap, VFS)
    assert r["mode"] == "own" and r["winner"]["name"] == "own" and r["rel"] == "src/deep", "repo-root index beats subdir index"
    f = reg.resolve("C:/R/src/f.py", snap, VFS)
    assert f["mode"] == "own" and f["rel"] == "src/f.py"
    w = reg.resolve("C:/W/src", snap, VFS)
    assert w["mode"] == "canonical" and w["worktree_root"] == "C:/W" and w["canonical_root"] == "C:/R" and w["rel"] == "src"
    n = reg.resolve("C:/NG/sub", snap, VFS)
    assert n["mode"] == "ancestor" and n["winner"]["name"] == "ng" and n["rel"] == "sub"
    assert reg.resolve("D:/x", snap, VFS)["mode"] == "none"
    assert reg.resolve("C:", snap, VFS)["mode"] == "none", "a drive root never resolves to the cwd"
    assert reg.resolve("", snap, VFS)["why"] == "no target"
    assert reg.resolve("C:/R", None, VFS)["why"] == "snapshot missing or corrupt"
    assert reg.resolve("C:/R", {"schema": "x"}, VFS)["mode"] == "none"


def test_resolve_subdir_only_index_is_ancestor_mode():
    snap = _snap([_row("sub", "C:/S/pkg", NOW)])
    r = reg.resolve("C:/S/pkg", snap, VFS)
    assert r["mode"] == "ancestor" and r["worktree_root"] == "C:/S"


def test_resolve_worktree_with_own_index_beats_canonical():
    snap = _snap([_row("canon", "C:/R", NOW), _row("wt", "C:/W", NOW)])
    r = reg.resolve("C:/W/src", snap, VFS)
    assert r["mode"] == "own" and r["winner"]["name"] == "wt"


def test_resolve_pins():
    rows = [_row("C-R", "C:/R", NOW), _row("old", "C:/R", NOW - 99999), _row("x", "C:/X", NOW)]
    pinned = reg.resolve("C:/R/src", _snap(rows, pins={"c:/r": "old"}), VFS)
    assert pinned["mode"] == "pin" and pinned["winner"]["name"] == "old" and pinned["shadowed"] == ["C-R"]
    via_canon = reg.resolve("C:/W/src", _snap(rows, pins={"c:/r": "old"}), VFS)
    assert via_canon["mode"] == "canonical" and via_canon["winner"]["name"] == "old" and via_canon["rel"] == "src", (
        "a canonical-root pin never enforces inside a linked worktree")
    wt_pin = reg.resolve("C:/W/src", _snap(rows, pins={"c:/w": "old"}), VFS)
    assert wt_pin["mode"] == "pin" and wt_pin["winner"]["name"] == "old", "a worktree-root pin is explicit and enforces"
    env_canon = reg.resolve("C:/W/src", _snap(rows), VFS, {"MERIDIAN_CBM_PROJECT": "old"})
    assert env_canon["mode"] == "canonical"
    missing = reg.resolve("C:/R", _snap(rows, pins={"c:/r": "ghost"}), VFS)
    assert missing["mode"] == "none" and "ghost" in missing["why"]
    env_pin = reg.resolve("C:/R", _snap(rows), VFS, {"MERIDIAN_CBM_PROJECT": "old"})
    assert env_pin["mode"] == "pin" and env_pin["winner"]["name"] == "old"
    other_root = reg.resolve("C:/R", _snap(rows), VFS, {"MERIDIAN_CBM_PROJECT": "x"})
    assert other_root["mode"] == "own" and other_root["winner"]["name"] == "C-R"
    bad = reg.resolve("C:/R", _snap(rows), VFS, {"MERIDIAN_CBM_PROJECT": "bad name!"})
    assert bad["winner"]["name"] == "C-R"


def test_valid_snapshot_filters_bad_rows():
    good = _row("g", "C:/R", NOW)
    snap = {"schema": reg.SNAPSHOT_SCHEMA, "rows": [good, {"name": ""}, {"name": "x", "root": "C:/", "root_key": "c:/", "db": "d",
                                                                      "indexed_epoch": "no"}, 5], "pins": [], "servers": 1}
    v = reg.valid_snapshot(snap)
    assert v["rows"] == [good] and v["pins"] == {} and v["servers"] == {}
    assert reg.valid_snapshot({"schema": reg.SNAPSHOT_SCHEMA, "rows": "x"}) is None
    assert reg.valid_snapshot([]) is None
    assert reg.row_by_name(v, "G")["name"] == "g"
    assert reg.row_by_name(v, "") is None and reg.row_by_name(v, "nope") is None


def test_freshness():
    row = _row("p", "C:/R", NOW - 30 * 86400)
    fs = DictFS({"files": {"C:/cache/p.db": None}, "mtimes": {"C:/cache/p.db": NOW - 30 * 86400}})
    f = reg.freshness(row, fs, NOW)
    assert f["present"] and not f["fresh"] and f["age_days"] == 30.0
    fs2 = DictFS({"files": {"C:/cache/p.db": None, "C:/cache/p.db-wal": None},
                  "mtimes": {"C:/cache/p.db": NOW - 30 * 86400, "C:/cache/p.db-wal": NOW - 7 * 86400 + 60}})
    assert reg.freshness(row, fs2, NOW)["fresh"], "WAL activity within 7 days keeps it fresh"
    gone = reg.freshness(row, DictFS({}), NOW)
    assert gone["present"] is False
    zero = reg.freshness(dict(row, indexed_epoch=0, db="C:/none.db", wal=None), DictFS({}), NOW)
    assert zero["fresh"] is False and zero["age_days"] is None


def test_server_prefix():
    servers = {"user": ["codebase-memory-mcp"], "projects": {"c:/r": ["codebase-memory"]}}
    snap = _snap([], servers=servers)
    assert reg.server_prefix(snap, [None, "C:/R"]) == "mcp__codebase-memory__"
    assert reg.server_prefix(snap, ["C:/other"]) == "mcp__codebase-memory-mcp__"
    assert reg.server_prefix(_snap([]), ["C:/R"]) == reg.DEFAULT_SERVER_PREFIX
    assert reg.server_prefix(None, []) == reg.DEFAULT_SERVER_PREFIX
    assert reg.server_prefix({"servers": {"projects": {"c:/r": [5]}, "user": "x"}}, ["C:/R"]) == reg.DEFAULT_SERVER_PREFIX


def test_resolver_against_built_snapshot_end_to_end(cache, tmp_path):
    """Build from real temp sqlite files, then resolve with the real filesystem."""
    root = tmp_path / "Proj"
    (root / ".git").mkdir(parents=True)
    (root / "pkg").mkdir()
    slug_name = reg.slug(reg.norm_path(str(root)))
    make_db(cache, slug_name, root, indexed_at=_iso(NOW - 3600), nodes=30)  # 3 would be "partial" next to 900
    make_db(cache, "dup", root, indexed_at=_iso(NOW - 60), nodes=900)
    snap = reg.build_snapshot(str(cache), env=_env(tmp_path), now=NOW)
    res = reg.resolve(str(root / "pkg"), snap, RealFS(), {})
    assert res["mode"] == "own" and res["winner"]["name"] == slug_name and res["shadowed"] == ["dup"]
    fr = reg.freshness(res["winner"], RealFS(), NOW)
    assert fr["present"] is True


# ---------------------------------------------------------------------------
# Fix round 1 (55d48d69): partial indexes and code-only coverage
# ---------------------------------------------------------------------------


def test_read_db_row_covers_only_dirs_holding_code(cache, repo):
    """results/ (json, npz) and paper/ (png, pdf, log) are hashed but hold no code:
    they must not be covered, or G1/G3 deny data-only searches (verification finding 6)."""
    db = make_db(cache, "cov", repo, rel_paths=(
        "a.py", "results/metrics_summary.json", "results/test_predictions.npz", "paper/fig1.png",
        "paper/main.pdf", "paper/build.log", "src\\model.py", ".github/workflows/ci.yml", "docs/x.md",
    ))
    info = reg.read_db_row(str(db))
    assert info["covered_dirs"] == ["", "src"]
    assert info["files"] == 9


def test_mark_partial_fewer_nodes_than_files_and_dwarfed():
    rows = [
        {"name": "slug", "root_key": "c:/r", "nodes": 90, "files": 1146},     # the live 2026-09-27 broken build
        {"name": "whole", "root_key": "c:/r", "nodes": 126821, "files": 4908},
        {"name": "tiny", "root_key": "c:/r", "nodes": 700, "files": None},     # < 1000 and 100x smaller
        {"name": "small-ok", "root_key": "c:/r", "nodes": 5000, "files": None},  # dwarfed but not tiny
        {"name": "alone", "root_key": "c:/other", "nodes": 3, "files": 3},
        {"name": "nofiles", "root_key": "c:/other2", "nodes": 0, "files": 0},
    ]
    reg.mark_partial(rows)
    assert {r["name"]: r["partial"] for r in rows} == {
        "slug": True, "whole": False, "tiny": True, "small-ok": False, "alone": False, "nofiles": False}


def test_pick_never_lets_a_partial_index_win_unless_pinned_or_alone():
    slug = dict(_row("C-R", "C:/R", NOW - 60, nodes=90, slug_match=True), partial=True, files=1146)
    whole = _row("meridian-repo", "C:/R", NOW - 55 * 86400, nodes=126821, slug_match=False)
    w, why, shadowed = reg.pick([slug, whole])
    assert w["name"] == "meridian-repo" and shadowed == ["C-R"]
    assert reg.pick([slug, whole], pin_name="C-R")[0]["name"] == "C-R", "an explicit pin still wins"
    assert reg.pick([slug])[0]["name"] == "C-R", "a lone partial index is still the (advisory-only) winner"


def test_build_snapshot_demotes_unfinished_slug_index(cache, tmp_path):
    root = tmp_path / "Proj"
    (root / ".git").mkdir(parents=True)
    (root / "pkg").mkdir()
    slug_name = reg.slug(reg.norm_path(str(root)))
    many = tuple(f"pkg/m{i}.py" for i in range(40))
    make_db(cache, slug_name, root, indexed_at=_iso(NOW - 600), nodes=2, rel_paths=many)
    make_db(cache, "whole", root, indexed_at=_iso(NOW - 40 * 86400), nodes=900, rel_paths=many)
    snap = reg.build_snapshot(str(cache), env=_env(tmp_path), now=NOW)
    by = {r["name"]: r for r in snap["rows"]}
    assert by[slug_name]["partial"] is True and by[slug_name]["files"] == 40
    assert by["whole"]["partial"] is False
    res = reg.resolve(str(root / "pkg"), snap, RealFS(), {})
    assert res["winner"]["name"] == "whole" and res["shadowed"] == [slug_name]
