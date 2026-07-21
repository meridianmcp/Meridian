"""Coverage for the flag registry (45802b67): an AST-based scanner that walks
a source tree and inventories every ``os.environ.get(...)``/``os.getenv(...)``
call site into ``{flag_name, file, line, default}``.

Exercises:

* **scanner correctness** — a synthetic fixture file with a plain flag, a
  flag with a literal default, an ``os.getenv`` call, and a decoy DYNAMIC
  call (``os.environ.get(some_var)``) — the dynamic call must be skipped
  gracefully, never raise, never invent a name.
* **tree walking** — nested directories are walked, vendored/cache dirs
  (``__pycache__``, ``.git``, ``node_modules``) are pruned, non-.py files are
  ignored, an unparsable .py file doesn't abort the whole scan.
* **get_flag_registry** — the top-level summary function (also the MCP tool
  implementation): repo_root default, count/unique_count, quoted-path
  normalization.
* **dispatch + registration** — ``get_flag_registry`` routes through
  ``_dispatch_mcp_tool`` and is registered read-only across the tool schema.
"""
from __future__ import annotations

import pytest

from meridian import flag_registry as fr


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


_FIXTURE = '''import os

def load_config():
    a = os.environ.get("FLAG_A")
    b = os.environ.get("FLAG_B", "default")
    c = os.getenv("FLAG_C")
    d = os.environ.get(some_var)  # decoy: dynamic name, must be skipped
    return a, b, c, d
'''


# ===========================================================================
# scan_file — synthetic fixture
# ===========================================================================

def test_scan_file_extracts_literal_flags_and_skips_dynamic(tmp_path):
    fixture = tmp_path / "config_reader.py"
    _write(fixture, _FIXTURE)

    hits = fr.scan_file(fixture)
    by_name = {h["flag_name"]: h for h in hits}

    # Exactly the three literal-named calls — the dynamic decoy is absent.
    assert set(by_name) == {"FLAG_A", "FLAG_B", "FLAG_C"}
    assert len(hits) == 3

    assert by_name["FLAG_A"]["default"] is None
    assert by_name["FLAG_A"]["file"] == str(fixture)
    assert by_name["FLAG_A"]["line"] == 4

    assert by_name["FLAG_B"]["default"] == "default"
    assert by_name["FLAG_B"]["line"] == 5

    assert by_name["FLAG_C"]["default"] is None
    assert by_name["FLAG_C"]["line"] == 6


def test_scan_file_nonexistent_file_returns_empty():
    assert fr.scan_file("Z:/definitely/does/not/exist.py") == []


def test_scan_file_syntax_error_returns_empty_not_raise(tmp_path):
    bad = tmp_path / "broken.py"
    _write(bad, "def f(:\n    this is not python\n")
    assert fr.scan_file(bad) == []


def test_scan_file_ignores_unrelated_calls(tmp_path):
    f = tmp_path / "unrelated.py"
    _write(f, (
        "import os\n"
        "os.path.join('a', 'b')\n"
        "os.system('echo hi')\n"
        "environ.get('NOT_OS_PREFIXED')\n"
    ))
    assert fr.scan_file(f) == []


def test_scan_file_default_from_keyword_arg(tmp_path):
    f = tmp_path / "kw_default.py"
    _write(f, 'import os\nos.environ.get("FLAG_KW", default="kwval")\n')
    hits = fr.scan_file(f)
    assert len(hits) == 1
    assert hits[0]["flag_name"] == "FLAG_KW"
    assert hits[0]["default"] == "kwval"


def test_scan_file_non_literal_default_is_none(tmp_path):
    f = tmp_path / "computed_default.py"
    _write(f, 'import os\nFALLBACK = "x"\nos.environ.get("FLAG_D", FALLBACK)\n')
    hits = fr.scan_file(f)
    assert len(hits) == 1
    assert hits[0]["flag_name"] == "FLAG_D"
    assert hits[0]["default"] is None  # non-literal — best-effort skip, not a crash


# ===========================================================================
# scan_env_flags — tree walking
# ===========================================================================

def test_scan_env_flags_walks_nested_dirs(tmp_path):
    _write(tmp_path / "top.py", 'import os\nos.environ.get("TOP_FLAG")\n')
    _write(tmp_path / "pkg" / "sub.py", 'import os\nos.getenv("NESTED_FLAG", "n")\n')
    hits = fr.scan_env_flags(tmp_path)
    names = {h["flag_name"] for h in hits}
    assert names == {"TOP_FLAG", "NESTED_FLAG"}
    # sorted by (file, line)
    assert hits == sorted(hits, key=lambda r: (r["file"], r["line"]))


def test_scan_env_flags_prunes_vendored_dirs(tmp_path):
    _write(tmp_path / "real.py", 'import os\nos.environ.get("REAL_FLAG")\n')
    _write(tmp_path / "node_modules" / "vendored.py", 'import os\nos.environ.get("VENDORED_FLAG")\n')
    _write(tmp_path / "__pycache__" / "cached.py", 'import os\nos.environ.get("CACHED_FLAG")\n')
    _write(tmp_path / ".git" / "hooks.py", 'import os\nos.environ.get("GIT_FLAG")\n')
    hits = fr.scan_env_flags(tmp_path)
    names = {h["flag_name"] for h in hits}
    assert names == {"REAL_FLAG"}


def test_scan_env_flags_ignores_non_python_files(tmp_path):
    _write(tmp_path / "notes.txt", 'os.environ.get("TXT_FLAG")\n')
    _write(tmp_path / "real.py", 'import os\nos.environ.get("PY_FLAG")\n')
    hits = fr.scan_env_flags(tmp_path)
    assert {h["flag_name"] for h in hits} == {"PY_FLAG"}


def test_scan_env_flags_single_file_argument(tmp_path):
    f = tmp_path / "solo.py"
    _write(f, 'import os\nos.environ.get("SOLO_FLAG")\n')
    hits = fr.scan_env_flags(f)
    assert {h["flag_name"] for h in hits} == {"SOLO_FLAG"}


def test_scan_env_flags_empty_tree_returns_empty(tmp_path):
    assert fr.scan_env_flags(tmp_path) == []


def test_scan_env_flags_one_bad_file_does_not_abort_scan(tmp_path):
    _write(tmp_path / "broken.py", "def f(:\n")
    _write(tmp_path / "good.py", 'import os\nos.environ.get("GOOD_FLAG")\n')
    hits = fr.scan_env_flags(tmp_path)
    assert {h["flag_name"] for h in hits} == {"GOOD_FLAG"}


# ===========================================================================
# get_flag_registry — top-level summary / MCP tool implementation
# ===========================================================================

def test_get_flag_registry_summary_shape(tmp_path):
    _write(tmp_path / "a.py", 'import os\nos.environ.get("DUP_FLAG")\n')
    _write(tmp_path / "b.py", 'import os\nos.environ.get("DUP_FLAG", "x")\nos.getenv("OTHER_FLAG")\n')
    result = fr.get_flag_registry(str(tmp_path))
    assert result["repo_root"] == str(tmp_path)
    assert result["count"] == 3
    assert result["unique_count"] == 2
    assert result["unique_flag_names"] == ["DUP_FLAG", "OTHER_FLAG"]
    assert len(result["flags"]) == 3
    for entry in result["flags"]:
        assert set(entry) == {"flag_name", "file", "line", "default"}


def test_get_flag_registry_strips_quoted_root(tmp_path):
    _write(tmp_path / "a.py", 'import os\nos.environ.get("QUOTED_FLAG")\n')
    result = fr.get_flag_registry('"' + str(tmp_path) + '"')
    assert result["repo_root"] == str(tmp_path)
    assert result["unique_flag_names"] == ["QUOTED_FLAG"]


def test_get_flag_registry_defaults_to_cwd(tmp_path, monkeypatch):
    _write(tmp_path / "a.py", 'import os\nos.environ.get("CWD_FLAG")\n')
    monkeypatch.chdir(tmp_path)
    result = fr.get_flag_registry(None)
    assert "CWD_FLAG" in result["unique_flag_names"]


# ===========================================================================
# Dispatch + registration
# ===========================================================================

@pytest.mark.asyncio
async def test_get_flag_registry_dispatches_through_dispatch_mcp_tool(tmp_path):
    from meridian import server as srv

    _write(tmp_path / "svc.py", 'import os\nos.environ.get("DISPATCHED_FLAG", "d")\n')
    result = await srv._dispatch_mcp_tool(
        "get_flag_registry", {"root_dir": str(tmp_path)}, None, str(tmp_path),
    )
    assert isinstance(result, dict)
    assert result["repo_root"] == str(tmp_path)
    assert "DISPATCHED_FLAG" in result["unique_flag_names"]
    hit = next(f for f in result["flags"] if f["flag_name"] == "DISPATCHED_FLAG")
    assert hit["default"] == "d"


@pytest.mark.asyncio
async def test_dispatch_get_flag_registry_accepts_quoted_dir(tmp_path):
    from meridian import server as srv

    _write(tmp_path / "svc.py", 'import os\nos.environ.get("QUOTED_DISPATCH_FLAG")\n')
    result = await srv._dispatch_mcp_tool(
        "get_flag_registry", {"root_dir": '"' + str(tmp_path) + '"'}, None, str(tmp_path),
    )
    assert "QUOTED_DISPATCH_FLAG" in result["unique_flag_names"]


@pytest.mark.asyncio
async def test_dispatch_get_flag_registry_defaults_root_dir_when_omitted(tmp_path, monkeypatch):
    from meridian import server as srv

    _write(tmp_path / "svc.py", 'import os\nos.environ.get("OMITTED_ROOT_FLAG")\n')
    monkeypatch.chdir(tmp_path)
    result = await srv._dispatch_mcp_tool("get_flag_registry", {}, None, str(tmp_path))
    assert "OMITTED_ROOT_FLAG" in result["unique_flag_names"]


def test_get_flag_registry_registered_read_only():
    from meridian import mcp_tools as mt

    names = [t["name"] for t in mt._MCP_TOOLS_LIST]
    assert "get_flag_registry" in names
    assert "get_flag_registry" in mt._READ_ONLY_TOOLS
    tool = next(t for t in mt._MCP_TOOLS_LIST if t["name"] == "get_flag_registry")
    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["title"] == "Get Flag Registry"
