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
import hashlib
from typing import Any

from .structural_parser import StructuralParser

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

# In-text citation macros we recognise as citation markers (fefb596a). Each takes
# a mandatory ``{key1,key2,...}`` argument (plus optional ``[..]`` pre/post note
# args we ignore). The default pylatexenc context parses ``\cite*`` / ``\citep*``
# etc. as the base macro (``cite`` / ``citep``) with a leading star flag, so the
# base names below cover the starred variants too. We locate the key group
# structurally (the last ``{..}``-delimited argument) rather than by arg index,
# which is robust across the differing arg specs pylatexenc assigns each macro.
_CITATION_MACROS: frozenset[str] = frozenset(
    {
        "cite",
        "citep",
        "citet",
        "citeauthor",
        "citeyear",
        "citealt",
        "citealp",
        "citenum",
    }
)


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


# --- source pre-expansion: \input/\include (da9815ef) + \newcommand (fae29498) --

_INPUT_RE = re.compile(r"\\(?:input|include)\s*\{([^}]*)\}")
_MAX_INPUT_DEPTH = 20  # runaway/cyclic \input guard

# A \newcommand/\renewcommand definition head, up to the opening brace of its
# body: captures the defined macro name. Handles the star form, an optional
# [nargs] count and an optional [default] first-arg value.
_NEWCOMMAND_HEAD_RE = re.compile(
    r"\\(?:re)?newcommand\*?\s*\{\s*\\([A-Za-z@]+)\s*\}"
    r"\s*(?:\[\d+\])?\s*(?:\[[^\]]*\])?\s*\{"
)
_SECTION_MACRO_ALT = "|".join(re.escape(k) for k in _SECTION_LEVELS)
_SECTION_IN_BODY_RE = re.compile(r"\\(?:" + _SECTION_MACRO_ALT + r")\b")


def _brace_match(text: str, open_idx: int) -> tuple[str, int]:
    """Given ``text[open_idx] == '{'``, return ``(inner, index_after_close)``.

    Depth-aware so nested ``{...}`` inside the group are handled. If the brace is
    never closed (malformed), returns everything after it and ``len(text)``."""
    depth, j, n = 0, open_idx, len(text)
    while j < n:
        c = text[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : j], j + 1
        j += 1
    return text[open_idx + 1 :], n


def _expand_inputs(
    source: str,
    base_dir: str,
    *,
    _seen: set | None = None,
    _depth: int = 0,
    _unexpanded: list | None = None,
) -> tuple[str, list[str]]:
    """Recursively splice ``\\input{f}`` / ``\\include{f}`` file contents inline.

    da9815ef — real multi-file arXiv papers keep chapters/sections in separate
    files; without this the heading tree is incomplete. Each referenced name
    resolves to ``<name>.tex`` under ``base_dir`` (or the including file's dir for
    nested inputs); a file that exists is read and its content expanded in turn
    (depth- and cycle-guarded). A name that can't be resolved/read is left in
    place and recorded in the returned ``unexpanded`` list. Never raises — any
    error leaves that reference unexpanded."""
    if _seen is None:
        _seen = set()
    if _unexpanded is None:
        _unexpanded = []
    if _depth > _MAX_INPUT_DEPTH:
        return source, _unexpanded

    def _repl(m: "re.Match") -> str:
        name = m.group(1).strip()
        if not name:
            return m.group(0)
        candidate = name if name.lower().endswith(".tex") else name + ".tex"
        path = candidate if os.path.isabs(candidate) else os.path.join(base_dir, candidate)
        try:
            real = os.path.abspath(path)
            if real in _seen:  # cycle: already included once, drop the re-include
                return ""
            if os.path.isfile(real):
                with open(real, encoding="utf-8", errors="replace") as handle:
                    inner = handle.read()
                _seen.add(real)
                expanded_inner, _ = _expand_inputs(
                    inner, os.path.dirname(real),
                    _seen=_seen, _depth=_depth + 1, _unexpanded=_unexpanded,
                )
                return expanded_inner
        except Exception:  # noqa: BLE001 — a bad include must not sink the parse
            pass
        if name not in _unexpanded:
            _unexpanded.append(name)
        return m.group(0)

    try:
        return _INPUT_RE.sub(_repl, source), _unexpanded
    except Exception:  # noqa: BLE001
        return source, _unexpanded


def _expand_section_macros(source: str) -> str:
    """Rewrite section-aliasing ``\\newcommand`` macros to their base section macro.

    fae29498 — a paper that defines ``\\newcommand{\\mysection}[1]{\\section{#1}}``
    and writes ``\\mysection{Foo}`` is invisible to the fixed-name heading walker.
    We detect definitions whose body contains a sectioning macro and uses ``#1``,
    strip those definitions, then rewrite each use ``\\mysection`` -> ``\\section``
    so the walker sees a real heading. Full TeX macro expansion is out of scope;
    this handles the common single-argument section-alias case. Never raises."""
    try:
        aliases: dict[str, str] = {}
        for m in _NEWCOMMAND_HEAD_RE.finditer(source):
            name = m.group(1)
            body, _end = _brace_match(source, m.end() - 1)
            bm = _SECTION_IN_BODY_RE.search(body)
            if bm and "#1" in body:
                # bm.group(0) is like '\section'; strip the leading backslash.
                aliases[name] = bm.group(0)[1:]
        if not aliases:
            return source
        result = source
        for name, target in aliases.items():
            # 1. Remove this macro's definition(s) so its self-reference inside
            #    \newcommand{\name}{...} isn't rewritten into \newcommand{\section}.
            defpat = re.compile(
                r"\\(?:re)?newcommand\*?\s*\{\s*\\" + re.escape(name) + r"\s*\}"
                r"\s*(?:\[\d+\])?\s*(?:\[[^\]]*\])?\s*\{"
            )
            pieces: list[str] = []
            i = 0
            while True:
                dm = defpat.search(result, i)
                if not dm:
                    pieces.append(result[i:])
                    break
                pieces.append(result[i : dm.start()])
                _body, end = _brace_match(result, dm.end() - 1)
                i = end
            result = "".join(pieces)
            # 2. Rewrite uses \name -> \target (word-boundary so \namex is safe).
            result = re.sub(
                r"\\" + re.escape(name) + r"(?![A-Za-z@])", "\\\\" + target, result
            )
        return result
    except Exception:  # noqa: BLE001 — expansion is best-effort; fall back to raw
        return source


def _expand_source(source: str, base_dir: str | None) -> tuple[str, list[str]]:
    """Pre-expand a LaTeX source: splice \\input/\\include (when ``base_dir`` lets
    us resolve them) then rewrite section-alias \\newcommand macros. Returns
    ``(expanded_source, unexpanded_input_names)``. With no ``base_dir`` the inputs
    can't be resolved, so all referenced names are reported unexpanded (the prior
    behaviour) — only macro rewriting still applies."""
    unexpanded: list[str] = []
    if base_dir:
        source, unexpanded = _expand_inputs(source, base_dir)
    else:
        for m in _INPUT_RE.finditer(source):
            name = m.group(1).strip()
            if name and name not in unexpanded:
                unexpanded.append(name)
    source = _expand_section_macros(source)
    return source, unexpanded


def _expand_source_with_provenance(
    source: str,
    base_dir: str,
    source_file: str,
    *,
    _seen: set[str] | None = None,
    _depth: int = 0,
    _unexpanded: list[str] | None = None,
    _graph: list[dict[str, Any]] | None = None,
) -> tuple[str, list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Expand inputs while retaining source-origin segments and file edges.

    The ordinary expander above remains the compatibility path for citation
    parsing.  This companion is used by structural analysis to make each
    heading span traceable to the file that supplied it, without attempting to
    be a TeX engine.  Every file edge is content-hashed and cycles are
    fail-closed in the same way as ``_expand_inputs``.
    """

    seen = _seen if _seen is not None else set()
    unexpanded = _unexpanded if _unexpanded is not None else []
    graph = _graph if _graph is not None else []
    if _depth > _MAX_INPUT_DEPTH:
        return source, unexpanded, [{
            "expanded_start": 0,
            "expanded_end": len(source),
            "source_file": source_file,
            "source_start": 0,
            "source_end": len(source),
            "source_text": source,
        }], graph

    pieces: list[str] = []
    segments: list[dict[str, Any]] = []
    cursor = 0
    expanded_length = 0

    def append_literal(start: int, end: int) -> None:
        nonlocal expanded_length
        if end <= start:
            return
        text = source[start:end]
        expanded_start = expanded_length
        pieces.append(text)
        expanded_length += len(text)
        segments.append(
            {
                "expanded_start": expanded_start,
                "expanded_end": expanded_start + len(text),
                "source_file": source_file,
                "source_start": start,
                "source_end": end,
                "source_text": source,
            }
        )

    try:
        for match in _INPUT_RE.finditer(source):
            append_literal(cursor, match.start())
            name = match.group(1).strip()
            macro = match.group(0).lstrip("\\").split("{", 1)[0].strip()
            if not name:
                append_literal(match.start(), match.end())
                cursor = match.end()
                continue
            candidate = name if name.lower().endswith(".tex") else name + ".tex"
            path = candidate if os.path.isabs(candidate) else os.path.join(base_dir, candidate)
            real = os.path.abspath(path)
            if real in seen:
                graph.append(
                    {
                        "from": source_file,
                        "to": real,
                        "kind": macro,
                        "name": name,
                        "status": "cycle",
                    }
                )
                if name not in unexpanded:
                    unexpanded.append(name)
                append_literal(match.start(), match.end())
                cursor = match.end()
                continue
            if _depth >= _MAX_INPUT_DEPTH:
                graph.append(
                    {
                        "from": source_file,
                        "to": real,
                        "kind": macro,
                        "name": name,
                        "status": "depth_limited",
                    }
                )
                if name not in unexpanded:
                    unexpanded.append(name)
                append_literal(match.start(), match.end())
                cursor = match.end()
                continue
            try:
                if os.path.isfile(real):
                    with open(real, "rb") as handle:
                        raw_inner = handle.read()
                    inner = raw_inner.decode("utf-8", errors="replace")
                    seen.add(real)
                    graph.append(
                        {
                            "from": source_file,
                            "to": real,
                            "kind": macro,
                            "name": name,
                            "sha256": hashlib.sha256(raw_inner).hexdigest(),
                            "status": "resolved",
                        }
                    )
                    offset = expanded_length
                    expanded_inner, _inner_unexpanded, inner_segments, _ = _expand_source_with_provenance(
                        inner,
                        os.path.dirname(real),
                        real,
                        _seen=seen,
                        _depth=_depth + 1,
                        _unexpanded=unexpanded,
                        _graph=graph,
                    )
                    pieces.append(expanded_inner)
                    expanded_length += len(expanded_inner)
                    for segment in inner_segments:
                        segment["expanded_start"] += offset
                        segment["expanded_end"] += offset
                        segments.append(segment)
                    seen.discard(real)
                    cursor = match.end()
                    continue
            except Exception:  # noqa: BLE001 — malformed include must not raise
                pass
            if name not in unexpanded:
                unexpanded.append(name)
            append_literal(match.start(), match.end())
            cursor = match.end()
        append_literal(cursor, len(source))
        expanded_before_aliases = "".join(pieces)
        # Preserve the existing alias behavior.  If an alias rewrites offsets,
        # discard origin segments rather than attributing shifted positions to
        # the wrong file; the span helper then reports logical expanded-source
        # coordinates explicitly.
        expanded = _expand_section_macros(expanded_before_aliases)
        if expanded != expanded_before_aliases:
            segments = []
        return expanded, unexpanded, segments, graph
    except Exception:  # noqa: BLE001 — structural analysis is best-effort
        return source, unexpanded, [{
            "expanded_start": 0,
            "expanded_end": len(source),
            "source_file": source_file,
            "source_start": 0,
            "source_end": len(source),
            "source_text": source,
        }], graph


def _line_for_offset(source: str, offset: int) -> int:
    return source.count("\n", 0, max(0, min(offset, len(source)))) + 1


def _source_span(
    node: Any,
    expanded_source: str,
    provenance: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Convert a latexwalker node position into a stable source span."""

    start = getattr(node, "pos", None)
    length = getattr(node, "len", None)
    if not isinstance(start, int) or not isinstance(length, int) or length < 0:
        return None
    end = start + length
    for segment in provenance or []:
        if segment["expanded_start"] <= start and end <= segment["expanded_end"]:
            source_start = segment["source_start"] + start - segment["expanded_start"]
            source_end = segment["source_start"] + end - segment["expanded_start"]
            source_text = segment.get("source_text", "")
            return {
                "source_file": segment["source_file"],
                "start_offset": source_start,
                "end_offset": source_end,
                "start_line": _line_for_offset(source_text, source_start),
                "end_line": _line_for_offset(source_text, max(source_start, source_end - 1)),
            }
    return {
        "source_file": "<expanded>",
        "start_offset": start,
        "end_offset": end,
        "start_line": _line_for_offset(expanded_source, start),
        "end_line": _line_for_offset(expanded_source, max(start, end - 1)),
    }


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


def _iter_headings(
    latexwalker: Any,
    node2text: Any,
    nodes: list[Any],
    out: list[dict],
    *,
    span_source: str | None = None,
    provenance: list[dict[str, Any]] | None = None,
) -> None:
    """Walk the node tree depth-first, appending an ordered flat heading list.

    Recurses into environments (and group bodies) so ``\\section`` commands nested
    inside e.g. a ``figure`` or ``document`` environment are still discovered.
    """
    siblings = nodes or []
    for i, node in enumerate(siblings):
        macroname = getattr(node, "macroname", None)
        if macroname in _SECTION_LEVELS:
            heading = {
                "level": _SECTION_LEVELS[macroname],
                "kind": macroname,
                "text": _macro_title(node2text, node, siblings, i),
            }
            if span_source is not None:
                span = _source_span(node, span_source, provenance)
                if span is not None:
                    heading["source_span"] = span
            out.append(heading)
        # Recurse into containers so deeply-nested sectioning is not missed.
        child_nodes = getattr(node, "nodelist", None)
        if child_nodes:
            _iter_headings(
                latexwalker,
                node2text,
                child_nodes,
                out,
                span_source=span_source,
                provenance=provenance,
            )
        nodeargd = getattr(node, "nodeargd", None)
        if nodeargd is not None:
            for arg in getattr(nodeargd, "argnlist", None) or []:
                if arg is None:
                    continue
                arg_nodes = getattr(arg, "nodelist", None)
                if arg_nodes and macroname not in _SECTION_LEVELS:
                    # Don't re-descend a section's own title group (already read).
                    _iter_headings(
                        latexwalker,
                        node2text,
                        arg_nodes,
                        out,
                        span_source=span_source,
                        provenance=provenance,
                    )


def _citation_keys(macro_node: Any) -> list[str]:
    """Extract the individual citation keys from a ``\\cite``-family macro node.

    The mandatory key group is the last ``{...}``-delimited argument in the
    macro's parsed ``argnlist`` (optional ``[..]`` note args carry ``[`` / ``]``
    delimiters and are ignored). The group's chars are the raw ``key1,key2``
    string, which we split on commas and trim. A macro with no key group (a bare
    ``\\cite``) or an empty group yields no keys.
    """
    nodeargd = getattr(macro_node, "nodeargd", None)
    argnlist = (getattr(nodeargd, "argnlist", None) or []) if nodeargd else []
    key_group = None
    for arg in argnlist:
        if arg is None:
            continue
        if getattr(arg, "delimiters", None) == ("{", "}"):
            key_group = arg  # last brace group wins (mandatory key arg)
    if key_group is None:
        return []
    inner = getattr(key_group, "nodelist", None) or []
    raw = "".join(getattr(n, "chars", "") or "" for n in inner)
    return [k.strip() for k in raw.split(",") if k.strip()]


def _iter_citations(
    latexwalker: Any,
    nodes: list[Any],
    out: list[dict],
    section_ordinal: int | None,
    counter: dict[str, int],
) -> None:
    """Walk the node tree depth-first, appending in-text citation markers.

    Tracks the *enclosing* section by mirroring the heading walk: whenever a
    sectioning macro (``\\section`` etc.) is seen at this level it becomes the new
    enclosing section for the citations that follow it, identified by its
    document-order ordinal (``counter['heading']`` — the same ordinal the heading
    receives in :func:`_iter_headings`, since both walks visit nodes in identical
    order). Each recognised citation macro emits one entry per key:
    ``{key, marker_text, section_ordinal}``.
    """
    current_section = section_ordinal
    for node in nodes or []:
        macroname = getattr(node, "macroname", None)
        if macroname in _SECTION_LEVELS:
            current_section = counter["heading"]
            counter["heading"] += 1
        elif macroname in _CITATION_MACROS:
            try:
                marker_text = node.latex_verbatim()
            except Exception:  # noqa: BLE001 — verbatim is best-effort
                marker_text = "\\" + str(macroname)
            for key in _citation_keys(node):
                out.append(
                    {
                        "key": key,
                        "marker_text": marker_text,
                        "section_ordinal": current_section,
                    }
                )
        # Recurse into containers so citations nested inside environments (e.g.
        # a ``figure`` caption or the ``document`` env) are still discovered. The
        # enclosing section propagates into children.
        child_nodes = getattr(node, "nodelist", None)
        if child_nodes:
            _iter_citations(
                latexwalker, child_nodes, out, current_section, counter
            )
        nodeargd = getattr(node, "nodeargd", None)
        if nodeargd is not None:
            for arg in getattr(nodeargd, "argnlist", None) or []:
                if arg is None:
                    continue
                arg_nodes = getattr(arg, "nodelist", None)
                if arg_nodes and macroname not in _SECTION_LEVELS:
                    # Skip a section's own title group (mirrors _iter_headings) so
                    # the heading counter is not double-advanced.
                    _iter_citations(
                        latexwalker, arg_nodes, out, current_section, counter
                    )


def parse_latex_citations(source: str) -> list[dict[str, Any]]:
    """Parse in-text citation markers from a LaTeX ``source`` string.

    Returns a document-ordered list of ``{key, marker_text, section_ordinal}``
    dicts — one per citation key (a ``\\cite{a,b}`` expands to two entries).
    ``section_ordinal`` is the document-order ordinal of the enclosing sectioning
    heading (``None`` if the citation precedes any heading), matching the ordinal
    :func:`parse_latex_structure` assigns that heading, so a downstream consumer
    can resolve each citation's parent section.

    Never raises: any parse failure degrades to ``[]`` (honours the module
    robustness contract).
    """
    if not source or not isinstance(source, str):
        return []
    latexwalker, _node2text = _lazy_latexwalker()
    if latexwalker is None:
        return []
    try:
        walker = latexwalker.LatexWalker(source, tolerant_parsing=True)
        nodelist, _pos, _len = walker.get_latex_nodes()
    except Exception:  # noqa: BLE001 — malformed LaTeX -> empty, never crash
        return []
    citations: list[dict[str, Any]] = []
    try:
        _iter_citations(latexwalker, nodelist, citations, None, {"heading": 0})
    except Exception:  # noqa: BLE001 — partial list beats a crash
        pass
    return citations


def _build_tree(headings: list[dict]) -> list[dict]:
    """Nest a flat ``[{level, kind, text}]`` outline into a heading tree.

    Each node is ``{level, kind, text, children}`` — the same recursive shape
    docs_intel would produce, so downstream consumers treat DOCX and LaTeX
    structure uniformly. A heading attaches under the nearest preceding heading
    of a strictly smaller level; otherwise it is a root.

    Thin wrapper over the shared :meth:`StructuralParser.build_tree` (67402ce7):
    the level-nesting algorithm is identical across formats, so it lives once on
    the base. Behaviour is unchanged — for a ``{level, kind, text}`` heading the
    shared helper yields exactly ``{level, kind, text, children}``.
    """
    return StructuralParser.build_tree(headings)


def parse_latex_structure(
    source: str,
    base_dir: str | None = None,
    *,
    source_file: str | None = None,
) -> dict[str, Any]:
    """Parse a LaTeX ``source`` string into a document structure tree.

    Returns ``{heading_count, headings, tree, unexpanded_inputs}`` where:

    * ``headings`` — the flat, document-ordered outline
      (``level`` / ``kind`` / ``text``), mirroring docs_intel.document_outline's
      ``headings`` list.
    * ``tree`` — the same headings nested by level (``children`` lists).
    * ``unexpanded_inputs`` — filenames referenced via ``\\input`` / ``\\include``
      that could NOT be expanded (a missing file, or no ``base_dir`` to resolve
      against). When ``base_dir`` is given, resolvable inputs are spliced in
      first (da9815ef) so multi-file papers get a complete tree, and only the
      genuinely-unresolvable references remain here.

    ``base_dir`` — directory to resolve ``\\input``/``\\include`` (and nested ones)
    against. ``analyze_latex`` passes the ``.tex`` file's own directory; a raw
    source string has none, so its inputs stay unexpanded (prior behaviour).
    Section-aliasing ``\\newcommand`` macros are expanded regardless (fae29498).

    Never raises: on any parse error returns an empty-but-well-formed dict.
    """
    empty = {"heading_count": 0, "headings": [], "tree": [], "unexpanded_inputs": []}
    if not source or not isinstance(source, str):
        return dict(empty)
    # da9815ef + fae29498 — splice \input/\include and rewrite section-alias
    # macros BEFORE the heading walk so the tree reflects the whole document.
    resolved_input_graph: list[dict[str, Any]] = []
    if base_dir:
        root_file = "<root>"
        seen: set[str] = set()
        if source_file:
            try:
                root_file = os.path.abspath(os.fspath(source_file))
                if os.path.isfile(root_file):
                    seen.add(root_file)
            except (TypeError, ValueError, OSError):
                root_file = "<root>"
        source, unexpanded, provenance, resolved_input_graph = _expand_source_with_provenance(
            source,
            base_dir,
            root_file,
            _seen=seen,
        )
    else:
        raw_source = source
        source, unexpanded = _expand_source(source, base_dir)
        provenance_source = source if source != raw_source else raw_source
        provenance = [
            {
                "expanded_start": 0,
                "expanded_end": len(source),
                "source_file": (source_file or "<raw>") if source == raw_source else "<expanded>",
                "source_start": 0,
                "source_end": len(source),
                "source_text": provenance_source,
            }
        ]
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
        _iter_headings(
            latexwalker,
            node2text,
            nodelist,
            headings,
            span_source=source,
            provenance=provenance,
        )
    except Exception:  # noqa: BLE001 — partial outline beats a crash
        pass

    result = {
        "heading_count": len(headings),
        "headings": headings,
        "tree": _build_tree(headings),
        "unexpanded_inputs": unexpanded,
    }
    if base_dir:
        result["resolved_input_graph"] = resolved_input_graph
    return result


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
    bibliography_count, citations}``. Never raises.
    """
    try:
        source, base_dir = _read_source(path_or_source)
    except Exception:  # noqa: BLE001
        source, base_dir = (path_or_source if isinstance(path_or_source, str) else ""), None

    source_file = None
    try:
        if base_dir and os.path.isfile(path_or_source):
            source_file = os.path.abspath(path_or_source)
    except (TypeError, ValueError):
        source_file = None
    structure = parse_latex_structure(source, base_dir=base_dir, source_file=source_file)
    bibliography = get_bibliography(source, base_dir=base_dir)
    # In-text citation markers (fefb596a). parse_latex_citations never raises, but
    # guard defensively so a regression there can never break analyze_latex.
    # da9815ef — parse citations from the SAME expanded source as the structure so
    # citations in \input-ed files are found and their section_ordinal aligns with
    # the (expanded) heading tree.
    try:
        expanded_source, _ = _expand_source(source, base_dir)
        citations = parse_latex_citations(expanded_source)
    except Exception:  # noqa: BLE001 — citation parse must degrade to [], never crash
        citations = []
    return {
        **structure,
        "bibliography": bibliography,
        "bibliography_count": len(bibliography),
        "citations": citations,
    }


# --- Shared structural-parser conformance (67402ce7) ------------------------


class LatexStructuralParser(StructuralParser):
    """LaTeX conformance to the shared :class:`StructuralParser` interface.

    Interface-only (67402ce7): every method delegates to the existing
    module-level functions — the LaTeX ``latexwalker`` parsing logic is untouched.
    This class merely *declares* that the ``.tex`` layer honours the common
    structural contract (``parse_structure`` -> heading outline + tree;
    ``analyze`` -> structure + bibliography + citations), so DOCX and LaTeX are
    provably conformant to one shape. The functional API
    (``parse_latex_structure`` / ``analyze_latex`` / ...) remains the public
    entry point and is unchanged.
    """

    def parse_structure(self, source: Any) -> dict[str, Any]:
        """Delegate to :func:`parse_latex_structure` (unchanged behaviour)."""
        return parse_latex_structure(source)

    def analyze(self, source: Any) -> dict[str, Any]:
        """Delegate to :func:`analyze_latex` (unchanged behaviour)."""
        return analyze_latex(source)
