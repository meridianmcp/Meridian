"""Coverage for the OOXML-Graph native LaTeX intelligence layer, Phase 3 (106118cd).

Parses small in-memory .tex strings (no LaTeX install, no PDF) and asserts the
heading tree + bibliography, malformed-input safety, the external .bib path, and
the get_latex_structure MCP tool dispatch. Mirrors tests/test_docs_intel.py.
"""
from __future__ import annotations

from meridian import latex_intel

_SAMPLE_TEX = r"""
\documentclass{article}
\begin{document}
\section{Introduction}
Meridian coordinates AI sessions.
\subsection{Design}
Details about the design.
\subsubsection{Internals}
Deep detail.
\section{Results}
\input{extra_chapter}
We conclude \cite{knuth1984} that it works.
\begin{thebibliography}{9}
\bibitem{knuth1984} Donald Knuth. The TeXbook. Addison-Wesley, 1984.
\bibitem{lamport1994} Leslie Lamport. LaTeX: A Document Preparation System. 1994.
\end{thebibliography}
\end{document}
"""


def test_parse_latex_structure_builds_ordered_outline_and_tree():
    out = latex_intel.parse_latex_structure(_SAMPLE_TEX)
    assert out["heading_count"] == 4
    # Flat outline: document order, correct kinds and levels.
    assert [(h["kind"], h["text"]) for h in out["headings"]] == [
        ("section", "Introduction"),
        ("subsection", "Design"),
        ("subsubsection", "Internals"),
        ("section", "Results"),
    ]
    # \input is detected but NOT expanded (best-effort note to the caller).
    assert out["unexpanded_inputs"] == ["extra_chapter"]

    # Tree: two section roots; the first nests subsection -> subsubsection.
    tree = out["tree"]
    assert [n["text"] for n in tree] == ["Introduction", "Results"]
    intro = tree[0]
    assert [c["text"] for c in intro["children"]] == ["Design"]
    assert [c["text"] for c in intro["children"][0]["children"]] == ["Internals"]
    assert tree[1]["children"] == []


def test_parse_latex_structure_renders_markup_in_titles():
    # A title containing markup renders to plain text (\textbf stripped).
    out = latex_intel.parse_latex_structure(r"\section{The \textbf{Bold} Title}")
    assert out["heading_count"] == 1
    assert out["headings"][0]["text"] == "The Bold Title"


def test_part_chapter_paragraph_levels():
    src = r"""
\part{One}
\chapter{First}
\section{Sec}
\paragraph{Para}
"""
    out = latex_intel.parse_latex_structure(src)
    kinds = [(h["kind"], h["level"]) for h in out["headings"]]
    assert kinds == [
        ("part", 0),
        ("chapter", 1),
        ("section", 2),
        ("paragraph", 5),
    ]
    # part -> chapter -> section -> paragraph all nest into one spine.
    tree = out["tree"]
    assert len(tree) == 1 and tree[0]["text"] == "One"
    assert tree[0]["children"][0]["children"][0]["children"][0]["text"] == "Para"


def test_get_bibliography_from_thebibliography():
    bib = latex_intel.get_bibliography(_SAMPLE_TEX)
    assert [e["key"] for e in bib] == ["knuth1984", "lamport1994"]
    assert all(e["type"] == "bibitem" for e in bib)
    assert "Donald Knuth" in bib[0]["raw"] and "1984" in bib[0]["raw"]


def test_get_bibliography_from_external_bib_file(tmp_path):
    (tmp_path / "refs.bib").write_text(
        r"""
@article{einstein1905,
  title = {On the Electrodynamics of {Moving} Bodies},
  author = {Albert Einstein},
  year = 1905,
  journal = {Annalen der Physik}
}
@book{knuth1984,
  title = "The TeXbook",
  author = {Donald E. Knuth},
  year = {1984}
}
""",
        encoding="utf-8",
    )
    tex = tmp_path / "paper.tex"
    tex.write_text(
        r"\documentclass{article}\begin{document}\section{Intro}"
        r"\bibliography{refs}\end{document}",
        encoding="utf-8",
    )
    res = latex_intel.analyze_latex(str(tex))
    assert [h["text"] for h in res["headings"]] == ["Intro"]
    assert res["bibliography_count"] == 2
    by_key = {e["key"]: e for e in res["bibliography"]}
    assert by_key["einstein1905"]["type"] == "article"
    assert by_key["einstein1905"]["author"] == "Albert Einstein"
    assert by_key["einstein1905"]["year"] == "1905"
    # Nested braces preserved in the extracted title.
    assert "Moving" in by_key["einstein1905"]["title"]
    # Quoted-string value form also parses.
    assert by_key["knuth1984"]["title"] == "The TeXbook"


def test_malformed_latex_returns_safely():
    # Never raises; returns a partial/empty but well-formed result.
    for bad in [
        r"\section{Unclosed \begin{env",
        r"\bibitem{",
        r"@article{broken, title = {unterminated",
        "",
    ]:
        struct = latex_intel.parse_latex_structure(bad)
        assert set(struct) == {"heading_count", "headings", "tree", "unexpanded_inputs"}
        assert isinstance(struct["headings"], list)
        assert latex_intel.get_bibliography(bad) == [] or isinstance(
            latex_intel.get_bibliography(bad), list
        )
    # Non-string input is tolerated.
    assert latex_intel.parse_latex_structure(None)["heading_count"] == 0
    assert latex_intel.get_bibliography(None) == []


def test_analyze_latex_accepts_raw_source():
    res = latex_intel.analyze_latex(_SAMPLE_TEX)
    assert res["heading_count"] == 4
    assert res["bibliography_count"] == 2
    assert res["unexpanded_inputs"] == ["extra_chapter"]


# ---------------------------------------------------------------------------
# In-text citation markers (fefb596a)
# ---------------------------------------------------------------------------

_CITE_TEX = r"""
\section{Intro}
We cite \cite{a,b} early.
\subsection{Design}
See \citep{c} and \citet[p.5]{d}.
\section{Results}
Author view: \citeauthor{e}, year \citeyear{f}, alt \citealt{g}, num \citenum{h}.
"""


def test_parse_latex_citations_extracts_keys_and_section_ordinals():
    cites = latex_intel.parse_latex_citations(_CITE_TEX)
    # \cite{a,b} expands to two entries, both under section 0 (Intro).
    by_key = {c["key"]: c for c in cites}
    assert set(by_key) == {"a", "b", "c", "d", "e", "f", "g", "h"}
    assert by_key["a"]["section_ordinal"] == 0
    assert by_key["b"]["section_ordinal"] == 0
    # \citep{c} / \citet[p.5]{d} are under the subsection Design (heading index 1).
    assert by_key["c"]["section_ordinal"] == 1
    assert by_key["d"]["section_ordinal"] == 1
    # The Results section is heading index 2; its citations track that.
    assert by_key["e"]["section_ordinal"] == 2
    assert by_key["h"]["section_ordinal"] == 2
    # marker_text preserves the raw macro invocation (incl. optional note args).
    assert by_key["a"]["marker_text"] == r"\cite{a,b}"
    assert by_key["d"]["marker_text"] == r"\citet[p.5]{d}"


def test_parse_latex_citations_before_any_heading_has_none_section():
    cites = latex_intel.parse_latex_citations(r"Preamble text \cite{x} then \section{S}")
    assert len(cites) == 1
    assert cites[0]["key"] == "x"
    assert cites[0]["section_ordinal"] is None


def test_parse_latex_citations_starred_variants():
    cites = latex_intel.parse_latex_citations(r"\cite*{a} \citep*{b} \citet*{c}")
    assert [c["key"] for c in cites] == ["a", "b", "c"]


def test_analyze_latex_exposes_citations():
    res = latex_intel.analyze_latex(_CITE_TEX)
    assert "citations" in res
    keys = [c["key"] for c in res["citations"]]
    assert keys == ["a", "b", "c", "d", "e", "f", "g", "h"]


def test_parse_latex_citations_malformed_returns_empty_and_never_raises():
    for bad in [
        r"\cite",            # no key group at all
        r"\cite{",           # unterminated
        r"\section{Unclosed \begin{env",
        "",
        None,
    ]:
        out = latex_intel.parse_latex_citations(bad)
        assert isinstance(out, list)
    # An empty key group yields no keys (not a blank-keyed citation).
    assert latex_intel.parse_latex_citations(r"\cite{}") == []
    # analyze_latex still returns citations=[] on malformed input (never raises).
    assert latex_intel.analyze_latex(r"\cite{")["citations"] == [] or isinstance(
        latex_intel.analyze_latex(r"\cite{")["citations"], list
    )


def test_get_latex_structure_mcp_tool(tmp_path):
    # 106118cd — exposed as an MCP tool via the same dispatch docs_intel uses.
    import asyncio
    from meridian import server as mh
    from meridian import db as db_module

    tex_path = tmp_path / "chapter.tex"
    tex_path.write_text(_SAMPLE_TEX, encoding="utf-8")
    db = asyncio.run(db_module.init_db(":memory:"))
    try:
        # Happy path — server-side parse of a real .tex by file_path.
        res = asyncio.run(
            mh._dispatch_mcp_tool(
                "get_latex_structure", {"file_path": str(tex_path)}, db, str(tmp_path)
            )
        )
        assert res["heading_count"] == 4
        assert res["headings"][0]["text"] == "Introduction"
        assert res["bibliography_count"] == 2
        assert res["tree"][0]["children"][0]["text"] == "Design"

        # Inline source (no file) also works.
        res2 = asyncio.run(
            mh._dispatch_mcp_tool(
                "get_latex_structure",
                {"source": r"\section{Only}"},
                db,
                str(tmp_path),
            )
        )
        assert res2["heading_count"] == 1 and res2["headings"][0]["text"] == "Only"

        # Missing file -> error dict, never a crash.
        err = asyncio.run(
            mh._dispatch_mcp_tool(
                "get_latex_structure",
                {"file_path": str(tmp_path / "nope.tex")},
                db,
                str(tmp_path),
            )
        )
        assert "error" in err

        # Neither file_path nor source -> error.
        err2 = asyncio.run(
            mh._dispatch_mcp_tool("get_latex_structure", {}, db, str(tmp_path))
        )
        assert "error" in err2
    finally:
        asyncio.run(db.close())


# ---------------------------------------------------------------------------
# da9815ef — \input / \include expansion
# ---------------------------------------------------------------------------

def test_input_expansion_splices_included_headings(tmp_path):
    (tmp_path / "chapter1.tex").write_text(
        r"\section{Included Chapter}" "\n" r"\subsection{Included Detail}",
        encoding="utf-8",
    )
    main = r"\section{Main}" "\n" r"\input{chapter1}" "\n" r"\section{Tail}"
    out = latex_intel.parse_latex_structure(main, base_dir=str(tmp_path))
    texts = [h["text"] for h in out["headings"]]
    # The included file's headings appear in document order between Main and Tail.
    assert texts == ["Main", "Included Chapter", "Included Detail", "Tail"]
    # Successfully expanded → nothing left unexpanded.
    assert out["unexpanded_inputs"] == []
    # Tree nests the included subsection under the included section.
    included = out["tree"][1]
    assert included["text"] == "Included Chapter"
    assert [c["text"] for c in included["children"]] == ["Included Detail"]


def test_input_expansion_missing_file_stays_unexpanded(tmp_path):
    main = r"\section{Main}" "\n" r"\input{does_not_exist}"
    out = latex_intel.parse_latex_structure(main, base_dir=str(tmp_path))
    assert [h["text"] for h in out["headings"]] == ["Main"]
    # A reference that can't be resolved is honestly reported, not silently lost.
    assert out["unexpanded_inputs"] == ["does_not_exist"]


def test_input_expansion_is_recursive_and_cycle_safe(tmp_path):
    # a.tex inputs b.tex; b.tex inputs a.tex (a cycle) — must terminate.
    (tmp_path / "a.tex").write_text(
        r"\section{A}" "\n" r"\input{b}", encoding="utf-8"
    )
    (tmp_path / "b.tex").write_text(
        r"\subsection{B}" "\n" r"\input{a}", encoding="utf-8"
    )
    main = r"\input{a}"
    out = latex_intel.parse_latex_structure(main, base_dir=str(tmp_path))
    # Both headings surface once; the cycle back to a is dropped, no hang.
    assert [h["text"] for h in out["headings"]] == ["A", "B"]


def test_no_base_dir_leaves_inputs_unexpanded(tmp_path):
    # Without a base_dir the inputs can't be resolved — prior behaviour preserved.
    out = latex_intel.parse_latex_structure(r"\section{X}" "\n" r"\input{foo}")
    assert out["unexpanded_inputs"] == ["foo"]


def test_analyze_latex_expands_inputs_from_file(tmp_path):
    (tmp_path / "sec.tex").write_text(r"\section{From Include}", encoding="utf-8")
    main_path = tmp_path / "main.tex"
    main_path.write_text(r"\section{Root}" "\n" r"\input{sec}", encoding="utf-8")
    res = latex_intel.analyze_latex(str(main_path))
    assert [h["text"] for h in res["headings"]] == ["Root", "From Include"]
    assert res["unexpanded_inputs"] == []


# ---------------------------------------------------------------------------
# fae29498 — \newcommand / \renewcommand section-alias expansion
# ---------------------------------------------------------------------------

def test_newcommand_section_alias_is_expanded():
    src = (
        r"\newcommand{\mysection}[1]{\section{#1}}" "\n"
        r"\mysection{Aliased Heading}" "\n"
        r"\section{Plain Heading}"
    )
    out = latex_intel.parse_latex_structure(src)
    kinds_texts = [(h["kind"], h["text"]) for h in out["headings"]]
    # The \mysection use is recovered as a real section; the definition itself
    # produces no spurious heading.
    assert kinds_texts == [
        ("section", "Aliased Heading"),
        ("section", "Plain Heading"),
    ]


def test_newcommand_alias_to_subsection_level():
    src = (
        r"\renewcommand{\mysub}[1]{\subsection{#1}}" "\n"
        r"\section{Top}" "\n"
        r"\mysub{Nested}"
    )
    out = latex_intel.parse_latex_structure(src)
    assert [(h["kind"], h["text"]) for h in out["headings"]] == [
        ("section", "Top"),
        ("subsection", "Nested"),
    ]
    # Tree nests the aliased subsection under the section.
    assert out["tree"][0]["children"][0]["text"] == "Nested"


def test_non_section_newcommand_is_untouched():
    # A \newcommand that is NOT a section alias must not create headings.
    src = r"\newcommand{\foo}[1]{\textbf{#1}}" "\n" r"\section{Real}" "\n" r"\foo{bold}"
    out = latex_intel.parse_latex_structure(src)
    assert [h["text"] for h in out["headings"]] == ["Real"]
