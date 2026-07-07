"""Coverage tests for meridian/symbols.py — symbol extraction for symbol-level
claim protection (sprint 4bac57ff / 1115046c).

Exercises the uncovered branches of the extractor:

- ``_extract_python`` decorator-line inclusion in a symbol's span (line 75).
- ``_get_ts_parser`` build path for every non-JS tree-sitter language
  (typescript/tsx/c/cpp/go/rust/java/c_sharp) plus its ``except`` fallback.
- ``_node_name`` fallback to the first identifier-like named child, and the
  ``return None`` when no name can be found.
- ``_extract_tree_sitter`` guards: parser is None, empty def_types, and a
  ``parser.parse`` exception.
- ``extract_symbols`` empty-source guard.

All tree-sitter grammars ship in this repo's env, so the real parsers run.
Fallback/None paths are driven with monkeypatch + lightweight stubs so we never
weaken production code to reach a defensive line.
"""
from __future__ import annotations

import pytest

from meridian import symbols


# ── _extract_python: decorators fold into the symbol span (line 75) ──────────

def test_python_decorator_included_in_span():
    src = (
        "import functools\n"
        "\n"
        "@functools.cache\n"          # line 3 — decorator on a function
        "@staticmethod\n"             # line 4 — second decorator
        "def decorated():\n"          # line 5 — the def keyword
        "    return 1\n"
    )
    out = symbols.extract_symbols("mod.py", src)
    fn = {s["name"]: s for s in out}["decorated"]
    # line_start must be pulled up to the first decorator (line 3), not the def.
    assert fn["type"] == "function"
    assert fn["line_start"] == 3
    assert fn["line_end"] == 6


def test_python_decorated_method_span():
    src = (
        "class C:\n"
        "    @property\n"             # line 2 — decorator on a method
        "    def value(self):\n"      # line 3
        "        return 42\n"
    )
    out = symbols.extract_symbols("c.py", src)
    by = {s["name"]: s for s in out}
    assert by["C.value"]["type"] == "method"
    # Method span starts at the decorator line, not the def line.
    assert by["C.value"]["line_start"] == 2


def test_python_async_function_extracted():
    src = "async def fetch():\n    return None\n"
    out = symbols.extract_symbols("a.py", src)
    assert out and out[0]["name"] == "fetch"
    assert out[0]["type"] == "function"


# ── _get_ts_parser: real build path for every non-JS grammar ─────────────────

# (language key, extension, source, expected symbol name) — one real symbol per
# language so we drive the language-specific import branch in _get_ts_parser AND
# the successful walk in _extract_tree_sitter.
_LANG_CASES = [
    ("typescript", "a.ts", "function tsfn(): number { return 1; }", "tsfn"),
    ("tsx", "a.tsx", "function Comp() { return null; }", "Comp"),
    # C exposes struct names directly; a C function's identifier is nested
    # under function_declarator (not a direct named child), so it is not
    # resolvable by _node_name's shallow fallback — struct is the reliable case.
    ("c", "a.c", "struct Point { int x; int y; };", "Point"),
    ("cpp", "a.cpp", "class Widget { public: void draw(); };", "Widget"),
    ("go", "a.go", "package main\nfunc Run() int { return 1 }\n", "Run"),
    ("rust", "a.rs", "fn compute() -> i32 { 1 }", "compute"),
    ("java", "a.java", "class Foo { void bar() {} }", "Foo"),
    ("c_sharp", "a.cs", "class Baz { void Qux() {} }", "Baz"),
]


@pytest.mark.parametrize("lang,path,src,expected", _LANG_CASES)
def test_tree_sitter_languages_extract(lang, path, src, expected):
    # Force a clean cache miss so the language-specific import branch actually
    # runs (rather than returning a previously cached parser).
    symbols._PARSER_CACHE.pop(lang, None)
    out = symbols.extract_symbols(path, src)
    names = {s["name"] for s in out}
    assert expected in names, f"{lang}: {expected} not in {names}"
    # Every symbol carries 1-based inclusive line numbers.
    for s in out:
        assert s["line_start"] >= 1
        assert s["line_end"] >= s["line_start"]


def test_get_ts_parser_caches_result():
    # First real call populates the cache; a second returns the same object.
    symbols._PARSER_CACHE.pop("go", None)
    p1 = symbols._get_ts_parser("go")
    assert "go" in symbols._PARSER_CACHE
    p2 = symbols._get_ts_parser("go")
    assert p1 is p2


def test_get_ts_parser_unknown_language_returns_none():
    # An unknown language hits the ``else: lang_capsule = None`` branch, so no
    # parser is built and None is cached.
    symbols._PARSER_CACHE.pop("klingon", None)
    assert symbols._get_ts_parser("klingon") is None
    assert symbols._PARSER_CACHE["klingon"] is None


def test_get_ts_parser_import_failure_returns_none(monkeypatch):
    # Simulate tree_sitter being unavailable: the import inside the try raises,
    # the ``except Exception`` branch runs, and None is cached (line 202-203).
    import builtins

    real_import = builtins.__import__

    def _boom(name, *args, **kwargs):
        if name == "tree_sitter" or name.startswith("tree_sitter"):
            raise ImportError("tree_sitter not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _boom)
    symbols._PARSER_CACHE.pop("python_never", None)
    symbols._PARSER_CACHE.pop("go", None)
    assert symbols._get_ts_parser("go") is None
    assert symbols._PARSER_CACHE["go"] is None
    # Clear the poisoned cache entry so later tests rebuild a real parser.
    symbols._PARSER_CACHE.pop("go", None)


# ── _node_name: fallback to first identifier-like child + None return ────────

class _FakeNode:
    """Minimal tree-sitter node stand-in for _node_name."""

    def __init__(self, type_, named_children=None, start_byte=0, end_byte=0,
                 name_field=None):
        self.type = type_
        self.named_children = named_children or []
        self.start_byte = start_byte
        self.end_byte = end_byte
        self._name_field = name_field

    def child_by_field_name(self, field):
        assert field == "name"
        return self._name_field


def test_node_name_direct_name_field():
    src = b"hello world"
    name_node = _FakeNode("identifier", start_byte=0, end_byte=5)
    node = _FakeNode("function_definition", name_field=name_node)
    assert symbols._node_name(node, src) == "hello"


def test_node_name_fallback_to_identifier_child():
    # No 'name' field -> scan named_children for one whose type contains
    # 'identifier' (lines 214-217).
    src = b"struct Point {}"
    ident = _FakeNode("type_identifier", start_byte=7, end_byte=12)
    noise = _FakeNode("comment", start_byte=0, end_byte=0)
    node = _FakeNode("struct_specifier", named_children=[noise, ident],
                     name_field=None)
    assert symbols._node_name(node, src) == "Point"


def test_node_name_returns_none_when_no_name(monkeypatch):
    # No name field and no identifier-like child -> return None (line 219).
    node = _FakeNode(
        "function_definition",
        named_children=[_FakeNode("parameter_list"), _FakeNode("body")],
        name_field=None,
    )
    assert symbols._node_name(node, b"") is None


def test_node_name_decode_replaces_invalid_utf8():
    # Invalid UTF-8 bytes decode with 'replace' rather than raising.
    src = b"\xff\xfe"
    name_node = _FakeNode("identifier", start_byte=0, end_byte=2)
    node = _FakeNode("function_definition", name_field=name_node)
    assert symbols._node_name(node, src) == "��"


# ── _extract_tree_sitter guards ──────────────────────────────────────────────

def test_extract_tree_sitter_parser_none(monkeypatch):
    # parser is None -> [] (line 226).
    monkeypatch.setattr(symbols, "_get_ts_parser", lambda language: None)
    assert symbols._extract_tree_sitter("go", "func Run() {}") == []


def test_extract_tree_sitter_empty_def_types(monkeypatch):
    # A truthy parser but a language with no def-type mapping -> [] (line 229).
    monkeypatch.setattr(symbols, "_get_ts_parser", lambda language: object())
    assert symbols._extract_tree_sitter("no_such_lang", "code") == []


def test_extract_tree_sitter_parse_raises(monkeypatch):
    # parser.parse raising is swallowed and yields [] (lines 233-234).
    class _BoomParser:
        def parse(self, data):
            raise RuntimeError("bad parse")

    monkeypatch.setattr(symbols, "_get_ts_parser", lambda language: _BoomParser())
    # 'go' has a real def_types mapping, so we get past the empty-def guard.
    assert symbols._extract_tree_sitter("go", "package main") == []


def test_extract_tree_sitter_skips_unnamed_nodes(monkeypatch):
    # A def node whose name resolves to None must be skipped (the ``if name``
    # guard in _walk), so no symbol is emitted.
    monkeypatch.setattr(symbols, "_node_name", lambda node, sb: None)
    out = symbols._extract_tree_sitter("go", "func Run() int { return 1 }")
    assert out == []


# ── extract_symbols public API guards ────────────────────────────────────────

def test_extract_symbols_empty_source_returns_empty():
    # Falsy source short-circuits before any language detection (line 265).
    assert symbols.extract_symbols("a.py", "") == []
    assert symbols.extract_symbols("a.py", None) == []  # type: ignore[arg-type]


def test_extract_symbols_unsupported_extension():
    assert symbols.extract_symbols("notes.txt", "some text") == []
    assert symbols.extract_symbols("", "content") == []


def test_extract_symbols_python_syntax_error_returns_empty():
    # ast.parse failure -> [] (whole-file fallback), never raises.
    assert symbols.extract_symbols("broken.py", "def (") == []


def test_extract_symbols_routes_python_vs_tree_sitter():
    py = symbols.extract_symbols("x.py", "def f():\n    return 1\n")
    assert {s["name"] for s in py} == {"f"}
    js = symbols.extract_symbols("x.js", "function g(){}\n")
    assert "g" in {s["name"] for s in js}


# ── detect_language ──────────────────────────────────────────────────────────

def test_detect_language_known_and_unknown():
    assert symbols.detect_language("a.py") == "python"
    assert symbols.detect_language("a.TS") == "typescript"   # case-insensitive
    assert symbols.detect_language("a.tsx") == "tsx"
    assert symbols.detect_language("a.unknown") is None
    assert symbols.detect_language("") is None
    assert symbols.detect_language(None) is None  # type: ignore[arg-type]
