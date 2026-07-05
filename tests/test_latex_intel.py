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
