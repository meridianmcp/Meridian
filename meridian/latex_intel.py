"""OOXML-Graph — native LaTeX source intelligence, Phase 3 (106118cd).

Mirror of ``docs_intel`` (the DOCX Phase-1 layer) for ``.tex`` sources: parse a
LaTeX document's *structure* — the ``\\part`` / ``\\chapter`` / ``\\section`` /
``\\subsection`` / ``\\subsubsection`` / ``\\paragraph`` heading tree — directly
from source, with **no PDF intermediary**. Where docs_intel reads OOXML with the
stdlib, this layer uses ``pylatexenc.latexwalker`` (a pure-Python LaTeX parser)
plus ``pylatexenc.latex2text`` to render heading titles that themselves contain
markup (``\\section{The \\textbf{Bold} Title}`` -> ``The Bold Title``).

Public surface (parallels docs_intel.document_outline / get_structure):

* ``parse_latex_structure(source) -> dict`` — a nested document tree
  (``level`` + ``text`` + ``children``) plus a flat ``headings`` outline, the
  SAME shape docs_intel exposes so the ``get_*_structure`` MCP tools are
  consistent across formats.
* ``get_bibliography(source) -> list[dict]`` — bibliography entries from either a
  ``thebibliography`` environment (``\\bibitem{key} ...``) or ``\\bibliography{..}``
  + a sibling ``.bib`` file (bibtex/biblatex ``@article{key, ...}``).
* ``analyze_latex(path_or_source) -> dict`` — structure + bibliography + counts.

Robustness contract: every public function catches parse errors and returns a
partial / empty result. **None of them raise to the caller** — the MCP tool must
never crash on malformed LaTeX.

Pure library — deterministic and unit-tested against small in-memory .tex
strings (see tests/test_latex_intel.py). No network, no shell-outs.
"""
from __future__ import annotations

import os
import re
from typing import Any

# Heading macros in outline order. The index is the nesting depth (level) so the
# tree builder knows how to nest \subsection under \section, etc. Mirrors the
# integer "level" docs_intel derives from a Word "HeadingN" style.
_SECTION_LEVELS: dict[str, int] = {
    "part": 0,
    "chapter": 1,
    "section": 2,
    "subsection": 3,
    "subsubsection": 4,
    "paragraph": 5,
    "subparagraph": 6,
}


def _lazy_latexwalker():
    """Import pylatexenc lazily so a missing/optional dep degrades gracefully.

    Returns ``(latexwalker_module, LatexNodes2Text_instance)`` or ``(None, None)``
    if pylatexenc is unavailable. Callers treat ``None`` as "cannot parse" and
    return an empty/partial result rather than raising.
    """
    try:
        from pylatexenc import latexwalker  # noqa: PLC0415
        from pylatexenc.latex2text import LatexNodes2Text  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — optional dependency, never crash
        return None, None
    return latexwalker, LatexNodes2Text()


def _read_source(path_or_source: str) -> tuple[str, str | None]:
    """Resolve ``path_or_source`` to raw LaTeX text.

    Heuristic mirror of how docs_intel accepts a path *or* content: if the string
    looks like an existing ``.tex`` file path we read it (and return its dir so
    ``\\bibliography`` can find a sibling ``.bib``); otherwise we treat it as the
    LaTeX source itself. Returns ``(source_text, base_dir_or_None)``.
    """
    try:
        looks_like_path = (
            "\n" not in path_or_source
            and len(path_or_source) < 4096
            and os.path.isfile(path_or_source)
        )
    except (ValueError, TypeError):
        looks_like_path = False
    if looks_like_path:
        with open(path_or_source, encoding="utf-8", errors="replace") as handle:
            return handle.read(), os.path.dirname(os.path.abspath(path_or_source))
    return path_or_source, None


def _node_text(node2text: Any, nodes: list[Any]) -> str:
    """Render a list of latexwalker nodes to plain text, best-effort."""
    try:
        return node2text.nodelist_to_text(nodes).strip()
    except Exception:  # noqa: BLE001 — a bad title must not sink the whole parse
        # Fallback: concatenate raw chars from any LatexCharsNode we can see.
        out = []
        for n in nodes or []:
            chars = getattr(n, "chars", None)
            if chars:
                out.append(chars)
        return "".join(out).strip()


def _is_group(node: Any) -> bool:
    """True for a ``{...}`` group node (latexwalker LatexGroupNode)."""
    return getattr(node, "nodelist", None) is not None and getattr(
        node, "delimiters", None
    ) == ("{", "}")


def _macro_title(node2text: Any, macro_node: Any, siblings: list[Any], idx: int) -> str:
    """Extract a section macro's title.

    The default latexwalker context defines an argument spec (``*[{``) for
    ``\\chapter`` / ``\\section`` / ``\\subsection`` / ``\\subsubsection`` — there
    the mandatory title is the last non-None ``nodeargd`` argument group. But it
    does NOT know ``\\part`` / ``\\paragraph`` / ``\\subparagraph``, so for those
    the ``{title}`` is parsed as the next sibling ``LatexGroupNode``. We handle
    both: try the parsed arg first, then fall back to the next sibling group.
    """
    nodeargd = getattr(macro_node, "nodeargd", None)
    argnlist = (getattr(nodeargd, "argnlist", None) or []) if nodeargd else []
    for arg in reversed(argnlist):
        if arg is None:
            continue
        inner = getattr(arg, "nodelist", None)
        if inner is not None:
            return _node_text(node2text, inner)
        return _node_text(node2text, [arg])
    # Fallback: no parsed argument — the title is the immediately-following
    # ``{...}`` group (skipping only pure whitespace char nodes in between).
    j = idx + 1
    while j < len(siblings):
        nxt = siblings[j]
        chars = getattr(nxt, "chars", None)
        if chars is not None and chars.strip() == "":
            j += 1
            continue
        if _is_group(nxt):
            return _node_text(node2text, getattr(nxt, "nodelist", []) or [])
        break
    return ""


def _iter_headings(latexwalker: Any, node2text: Any, nodes: list[Any], out: list[dict]) -> None:
    """Walk the node tree depth-first, appending an ordered flat heading list.

    Recurses into environments (and group bodies) so ``\\section`` commands nested
    inside e.g. a ``figure`` or ``document`` environment are still discovered.
    """
    siblings = nodes or []
    for i, node in enumerate(siblings):
        macroname = getattr(node, "macroname", None)
        if macroname in _SECTION_LEVELS:
            out.append(
                {
                    "level": _SECTION_LEVELS[macroname],
                    "kind": macroname,
                    "text": _macro_title(node2text, node, siblings, i),
                }
            )
        # Recurse into containers so deeply-nested sectioning is not missed.
        child_nodes = getattr(node, "nodelist", None)
        if child_nodes:
            _iter_headings(latexwalker, node2text, child_nodes, out)
        nodeargd = getattr(node, "nodeargd", None)
        if nodeargd is not None:
            for arg in getattr(nodeargd, "argnlist", None) or []:
                if arg is None:
                    continue
                arg_nodes = getattr(arg, "nodelist", None)
                if arg_nodes and macroname not in _SECTION_LEVELS:
                    # Don't re-descend a section's own title group (already read).
                    _iter_headings(latexwalker, node2text, arg_nodes, out)


def _build_tree(headings: list[dict]) -> list[dict]:
    """Nest a flat ``[{level, kind, text}]`` outline into a heading tree.

    Each node is ``{level, kind, text, children}`` — the same recursive shape
    docs_intel would produce, so downstream consumers treat DOCX and LaTeX
    structure uniformly. A heading attaches under the nearest preceding heading
    of a strictly smaller level; otherwise it is a root.
    """
    roots: list[dict] = []
    stack: list[dict] = []
    for h in headings:
        node = {"level": h["level"], "kind": h["kind"], "text": h["text"], "children": []}
        while stack and stack[-1]["level"] >= node["level"]:
            stack.pop()
        if stack:
            stack[-1]["children"].append(node)
        else:
            roots.append(node)
        stack.append(node)
    return roots


def parse_latex_structure(source: str) -> dict[str, Any]:
    """Parse a LaTeX ``source`` string into a document structure tree.

    Returns ``{heading_count, headings, tree, unexpanded_inputs}`` where:

    * ``headings`` — the flat, document-ordered outline
      (``level`` / ``kind`` / ``text``), mirroring docs_intel.document_outline's
      ``headings`` list.
    * ``tree`` — the same headings nested by level (``children`` lists).
    * ``unexpanded_inputs`` — filenames referenced via ``\\input`` / ``\\include``
      that were NOT expanded (best-effort note; expansion is out of scope for
      Phase 3, so the caller knows the tree may be incomplete).

    Never raises: on any parse error returns an empty-but-well-formed dict.
    """
    empty = {"heading_count": 0, "headings": [], "tree": [], "unexpanded_inputs": []}
    if not source or not isinstance(source, str):
        return dict(empty)
    latexwalker, node2text = _lazy_latexwalker()
    if latexwalker is None:
        return dict(empty)
    try:
        walker = latexwalker.LatexWalker(source, tolerant_parsing=True)
        nodelist, _pos, _len = walker.get_latex_nodes()
    except Exception:  # noqa: BLE001 — malformed LaTeX -> empty, never crash
        return dict(empty)

    headings: list[dict] = []
    try:
        _iter_headings(latexwalker, node2text, nodelist, headings)
    except Exception:  # noqa: BLE001 — partial outline beats a crash
        pass

    # Best-effort \input / \include detection (we do NOT expand them in Phase 3).
    unexpanded: list[str] = []
    try:
        for m in re.finditer(r"\\(?:input|include)\s*\{([^}]*)\}", source):
            name = m.group(1).strip()
            if name and name not in unexpanded:
                unexpanded.append(name)
    except Exception:  # noqa: BLE001
        unexpanded = []

    return {
        "heading_count": len(headings),
        "headings": headings,
        "tree": _build_tree(headings),
        "unexpanded_inputs": unexpanded,
    }


# --- Bibliography -----------------------------------------------------------

# Fields we lift out of a bibtex entry body into structured keys (everything is
# also preserved in ``raw``). Case-insensitive match on the field name.
_BIB_FIELDS = ("title", "author", "year")


def _parse_bibitems(source: str) -> list[dict[str, Any]]:
    """Extract ``\\bibitem{key} text`` entries from a thebibliography block.

    Regex-based (not AST) because ``\\bibitem`` bodies are free-form LaTeX text
    that runs until the next ``\\bibitem`` or ``\\end{thebibliography}``; this is
    the robust, standard way to slice them. Returns ``{key, type, raw}`` per
    entry (type='bibitem'). Malformed input yields whatever entries parsed.
    """
    entries: list[dict[str, Any]] = []
    # Isolate the thebibliography environment body (if present); else scan whole.
    env = re.search(
        r"\\begin\{thebibliography\}(?:\{[^}]*\})?(.*?)\\end\{thebibliography\}",
        source,
        re.DOTALL,
    )
    body = env.group(1) if env else source
    # Split on \bibitem, keeping the (optional [label]) and mandatory {key}.
    pattern = re.compile(r"\\bibitem\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}", re.DOTALL)
    matches = list(pattern.finditer(body))
    for i, m in enumerate(matches):
        key = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        raw = body[start:end].strip()
        # Normalize internal whitespace for a compact raw citation string.
        raw = re.sub(r"\s+", " ", raw)
        entries.append({"key": key, "type": "bibitem", "raw": raw})
    return entries


def _split_bibtex_fields(body: str) -> dict[str, str]:
    """Parse ``field = {value}`` / ``field = "value"`` pairs from a bibtex body.

    Brace-aware: values may contain nested balanced braces (common in titles).
    Returns a lowercased-key dict of the fields we care about plus any others.
    """
    fields: dict[str, str] = {}
    i, n = 0, len(body)
    while i < n:
        m = re.match(r"\s*([A-Za-z][A-Za-z0-9_\-]*)\s*=\s*", body[i:])
        if not m:
            # Skip to the next comma and retry (tolerant of junk).
            comma = body.find(",", i)
            if comma == -1:
                break
            i += comma - i + 1
            continue
        name = m.group(1).lower()
        i += m.end()
        if i >= n:
            break
        ch = body[i]
        value = ""
        if ch == "{":
            depth, j = 0, i
            while j < n:
                if body[j] == "{":
                    depth += 1
                elif body[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            value = body[i + 1 : j]
            i = j + 1
        elif ch == '"':
            j = i + 1
            while j < n and body[j] != '"':
                j += 1
            value = body[i + 1 : j]
            i = j + 1
        else:
            # Bare value (e.g. a number or a macro) up to the next comma.
            comma = body.find(",", i)
            j = comma if comma != -1 else n
            value = body[i:j].strip()
            i = j
        fields[name] = re.sub(r"\s+", " ", value).strip()
        # Advance past a trailing comma between fields.
        nxt = body.find(",", i)
        if nxt == -1:
            break
        i = nxt + 1
    return fields


def parse_bibtex(source: str) -> list[dict[str, Any]]:
    """Parse bibtex/biblatex ``@type{key, field=..., ...}`` entries.

    Returns ``[{key, type, title, author, year, raw}]``. Brace-matched so entry
    bodies with nested braces are handled. Tolerant: unknown/@comment entries and
    malformed bodies are skipped, never raised.
    """
    entries: list[dict[str, Any]] = []
    n = len(source)
    for m in re.finditer(r"@([A-Za-z]+)\s*\{", source):
        etype = m.group(1).lower()
        if etype in ("comment", "preamble", "string"):
            continue
        # Brace-match the entry body starting at the '{' the regex consumed.
        start = m.end() - 1  # index of the opening '{'
        depth, j = 0, start
        while j < n:
            if source[j] == "{":
                depth += 1
            elif source[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        raw = source[start + 1 : j]
        # First comma separates the citation key from the fields.
        comma = raw.find(",")
        if comma == -1:
            key = raw.strip()
            fields: dict[str, str] = {}
        else:
            key = raw[:comma].strip()
            fields = _split_bibtex_fields(raw[comma + 1 :])
        entry = {
            "key": key,
            "type": etype,
            "title": fields.get("title", ""),
            "author": fields.get("author", ""),
            "year": fields.get("year", ""),
            "raw": re.sub(r"\s+", " ", raw).strip(),
        }
        entries.append(entry)
    return entries


def get_bibliography(source: str, base_dir: str | None = None) -> list[dict[str, Any]]:
    """Extract bibliography entries from a LaTeX ``source``.

    Handles two mechanisms:

    1. A ``thebibliography`` environment with ``\\bibitem{key} ...`` entries.
    2. ``\\bibliography{refs}`` / ``\\addbibresource{refs.bib}`` pointing at a
       sibling ``.bib`` file — parsed as bibtex/biblatex when ``base_dir`` is
       given and the file exists.

    Returns a list of ``{key, type, title, author, year, raw}`` dicts (bibitem
    entries omit title/author/year, leaving ``raw``). Robust: returns ``[]`` on
    any error and never raises to the caller.
    """
    if not source or not isinstance(source, str):
        return []
    entries: list[dict[str, Any]] = []
    try:
        # 1. Inline thebibliography.
        if "thebibliography" in source or "\\bibitem" in source:
            entries.extend(_parse_bibitems(source))

        # 2. External .bib via \bibliography{...} or \addbibresource{...}.
        bib_names: list[str] = []
        for m in re.finditer(r"\\bibliography\s*\{([^}]*)\}", source):
            bib_names.extend(part.strip() for part in m.group(1).split(","))
        for m in re.finditer(r"\\addbibresource\s*\{([^}]*)\}", source):
            bib_names.extend(part.strip() for part in m.group(1).split(","))

        if base_dir:
            for name in bib_names:
                if not name:
                    continue
                candidate = name if name.lower().endswith(".bib") else name + ".bib"
                path = candidate if os.path.isabs(candidate) else os.path.join(base_dir, candidate)
                try:
                    if os.path.isfile(path):
                        with open(path, encoding="utf-8", errors="replace") as handle:
                            entries.extend(parse_bibtex(handle.read()))
                except Exception:  # noqa: BLE001 — one bad .bib must not sink the rest
                    continue
    except Exception:  # noqa: BLE001 — bibliography is best-effort, never crash
        return entries
    return entries


def analyze_latex(path_or_source: str) -> dict[str, Any]:
    """Top-level entry: structure + bibliography + basic counts for a .tex doc.

    Accepts either a server-accessible ``.tex`` file path (in which case a sibling
    ``.bib`` referenced by ``\\bibliography`` is resolved) or a raw LaTeX string.
    Mirrors docs_intel.document_outline's role as the one-call structural map.

    Returns ``{heading_count, headings, tree, unexpanded_inputs, bibliography,
    bibliography_count}``. Never raises.
    """
    try:
        source, base_dir = _read_source(path_or_source)
    except Exception:  # noqa: BLE001
        source, base_dir = (path_or_source if isinstance(path_or_source, str) else ""), None

    structure = parse_latex_structure(source)
    bibliography = get_bibliography(source, base_dir=base_dir)
    return {
        **structure,
        "bibliography": bibliography,
        "bibliography_count": len(bibliography),
    }
