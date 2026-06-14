"""Symbol extraction for symbol-level parallel protection (sprint 4bac57ff).

``claim_file`` can claim an individual class/function/method instead of the
whole file, so two sessions can safely edit the same file as long as they own
different symbols. To do that the server has to know each symbol's line range.

The Meridian server has no filesystem access to a caller's repo (hosted mode),
so callers pass the file *content* and the extractor runs server-side. Python
is parsed with the stdlib ``ast`` (exact, no third-party dep); every other
supported language is parsed with tree-sitter grammars (added in 1115046c).

``extract_symbols(file_path, source)`` returns a list of
``{"name", "type", "line_start", "line_end"}`` dicts with 1-based inclusive
line numbers. It never raises on a syntax error or a missing grammar — it
returns an empty list so claiming simply falls back to whole-file locking.
"""
from __future__ import annotations

import ast
import os
from typing import Any

# ── Language detection ──────────────────────────────────────────────────────

_EXT_LANG = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".cs": "c_sharp",
}


def detect_language(file_path: str) -> str | None:
    """Return the language key for a path's extension, or None if unsupported."""
    _, ext = os.path.splitext((file_path or "").lower())
    return _EXT_LANG.get(ext)


# ── Python (stdlib ast) ─────────────────────────────────────────────────────

def _extract_python(source: str) -> list[dict[str, Any]]:
    """Extract top-level classes/functions plus methods (``Class.method``).

    Class ranges cover their methods, so a class claim and a method claim in
    the same class overlap by line and correctly conflict — class-level
    granularity first, with finer method claims available when wanted.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    out: list[dict[str, Any]] = []

    def _span(node: ast.AST) -> tuple[int, int]:
        start = getattr(node, "lineno", 1)
        end = getattr(node, "end_lineno", None) or start
        # Include decorators in the claimed range so two sessions can't both
        # grab a decorator line + its target.
        decorators = getattr(node, "decorator_list", []) or []
        for dec in decorators:
            start = min(start, getattr(dec, "lineno", start))
        return start, end

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            cs, ce = _span(node)
            out.append({"name": node.name, "type": "class", "line_start": cs, "line_end": ce})
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    ms, me = _span(child)
                    out.append({
                        "name": f"{node.name}.{child.name}",
                        "type": "method",
                        "line_start": ms,
                        "line_end": me,
                    })
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fs, fe = _span(node)
            out.append({"name": node.name, "type": "function", "line_start": fs, "line_end": fe})
    return out


# ── tree-sitter languages ───────────────────────────────────────────────────

# node type -> symbol type, per language. Anonymous nodes (e.g. arrow funcs)
# are intentionally omitted: a claim needs a stable name.
_TS_DEF_TYPES: dict[str, dict[str, str]] = {
    "javascript": {
        "function_declaration": "function",
        "generator_function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "method",
    },
    "typescript": {
        "function_declaration": "function",
        "generator_function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "method",
        "interface_declaration": "interface",
        "abstract_class_declaration": "class",
        "enum_declaration": "enum",
    },
    "c": {
        "function_definition": "function",
        "struct_specifier": "struct",
    },
    "cpp": {
        "function_definition": "function",
        "class_specifier": "class",
        "struct_specifier": "struct",
    },
    "go": {
        "function_declaration": "function",
        "method_declaration": "method",
        "type_declaration": "type",
    },
    "rust": {
        "function_item": "function",
        "struct_item": "struct",
        "enum_item": "enum",
        "trait_item": "trait",
        "impl_item": "impl",
        "mod_item": "module",
    },
    "java": {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "method_declaration": "method",
        "constructor_declaration": "method",
        "enum_declaration": "enum",
    },
    "c_sharp": {
        "class_declaration": "class",
        "struct_declaration": "struct",
        "interface_declaration": "interface",
        "method_declaration": "method",
        "enum_declaration": "enum",
    },
}
# tsx reuses the typescript grammar's tsx dialect + the typescript def types.
_TS_DEF_TYPES["tsx"] = _TS_DEF_TYPES["typescript"]

_PARSER_CACHE: dict[str, Any] = {}


def _get_ts_parser(language: str):
    """Lazily build + cache a tree-sitter Parser for ``language``.

    Returns None when tree-sitter or the grammar is unavailable so callers
    degrade to whole-file locking instead of crashing.
    """
    if language in _PARSER_CACHE:
        return _PARSER_CACHE[language]
    parser = None
    try:
        from tree_sitter import Language, Parser
        if language in ("javascript",):
            import tree_sitter_javascript as ts_mod
            lang_capsule = ts_mod.language()
        elif language == "typescript":
            import tree_sitter_typescript as ts_mod
            lang_capsule = ts_mod.language_typescript()
        elif language == "tsx":
            import tree_sitter_typescript as ts_mod
            lang_capsule = ts_mod.language_tsx()
        elif language == "c":
            import tree_sitter_c as ts_mod
            lang_capsule = ts_mod.language()
        elif language == "cpp":
            import tree_sitter_cpp as ts_mod
            lang_capsule = ts_mod.language()
        elif language == "go":
            import tree_sitter_go as ts_mod
            lang_capsule = ts_mod.language()
        elif language == "rust":
            import tree_sitter_rust as ts_mod
            lang_capsule = ts_mod.language()
        elif language == "java":
            import tree_sitter_java as ts_mod
            lang_capsule = ts_mod.language()
        elif language == "c_sharp":
            import tree_sitter_c_sharp as ts_mod
            lang_capsule = ts_mod.language()
        else:
            lang_capsule = None
        if lang_capsule is not None:
            parser = Parser(Language(lang_capsule))
    except Exception:
        parser = None
    _PARSER_CACHE[language] = parser
    return parser


def _node_name(node: Any, source_bytes: bytes) -> str | None:
    """Best-effort symbol name for a definition node."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        # Fall back to the first identifier-like named child (covers grammars
        # that don't expose a 'name' field, e.g. C function declarators).
        for child in node.named_children:
            if "identifier" in child.type:
                name_node = child
                break
    if name_node is None:
        return None
    return source_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8", "replace")


def _extract_tree_sitter(language: str, source: str) -> list[dict[str, Any]]:
    parser = _get_ts_parser(language)
    if parser is None:
        return []
    def_types = _TS_DEF_TYPES.get(language, {})
    if not def_types:
        return []
    source_bytes = source.encode("utf-8")
    try:
        tree = parser.parse(source_bytes)
    except Exception:
        return []
    out: list[dict[str, Any]] = []

    def _walk(node: Any) -> None:
        sym_type = def_types.get(node.type)
        if sym_type:
            name = _node_name(node, source_bytes)
            if name:
                out.append({
                    "name": name,
                    "type": sym_type,
                    "line_start": node.start_point[0] + 1,
                    "line_end": node.end_point[0] + 1,
                })
        for child in node.children:
            _walk(child)

    _walk(tree.root_node)
    return out


# ── public API ──────────────────────────────────────────────────────────────

def extract_symbols(file_path: str, source: str) -> list[dict[str, Any]]:
    """Return ``[{name, type, line_start, line_end}]`` for a file's content.

    1-based inclusive line numbers. Returns an empty list for an unsupported
    extension, a parse error, or a missing grammar — callers then fall back to
    whole-file locking. Never raises.
    """
    if not source:
        return []
    language = detect_language(file_path)
    if language is None:
        return []
    if language == "python":
        return _extract_python(source)
    return _extract_tree_sitter(language, source)
