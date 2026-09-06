from __future__ import annotations

import hashlib

from packages.docparse.docparse import latex_intel


def test_raw_heading_exposes_deterministic_source_span():
    source = "prefix\n\\section{Intro}\n"

    result = latex_intel.parse_latex_structure(source)
    heading = result["headings"][0]
    span = heading["source_span"]

    assert span["source_file"] == "<raw>"
    assert source[span["start_offset"] : span["end_offset"]].startswith(r"\section")
    assert span["start_line"] == 2
    assert span["end_line"] == 2
    assert result == latex_intel.parse_latex_structure(source)


def test_include_graph_and_heading_spans_preserve_file_origin(tmp_path):
    included = tmp_path / "chapter.tex"
    root = tmp_path / "main.tex"
    included.write_text("\n\\section{Included}\n", encoding="utf-8")
    root.write_text("\\section{Root}\n\\input{chapter}\n", encoding="utf-8")

    result = latex_intel.analyze_latex(str(root))
    spans = [heading["source_span"] for heading in result["headings"]]

    assert [span["source_file"] for span in spans] == [str(root), str(included)]
    assert spans[0]["start_line"] == 1
    assert spans[1]["start_line"] == 2
    assert result["resolved_input_graph"] == [
        {
            "from": str(root),
            "to": str(included),
            "kind": "input",
            "name": "chapter",
            "sha256": hashlib.sha256(included.read_bytes()).hexdigest(),
            "status": "resolved",
        }
    ]


def test_missing_include_remains_unexpanded_and_graph_is_empty(tmp_path):
    root = tmp_path / "main.tex"
    root.write_text(r"\section{Root}\input{missing}", encoding="utf-8")

    result = latex_intel.analyze_latex(str(root))

    assert result["unexpanded_inputs"] == ["missing"]
    assert result["resolved_input_graph"] == []


def test_alias_rewrite_does_not_claim_shifted_offsets_are_file_exact(tmp_path):
    root = tmp_path / "main.tex"
    root.write_text(
        r"\newcommand{\mysection}[1]{\section{#1}}" "\n" r"\mysection{Root}",
        encoding="utf-8",
    )

    result = latex_intel.analyze_latex(str(root))

    assert result["headings"][0]["text"] == "Root"
    assert result["headings"][0]["source_span"]["source_file"] == "<expanded>"


def test_raw_alias_rewrite_does_not_claim_offsets_are_raw_exact():
    source = r"\newcommand{\mysection}[1]{\section{#1}}" "\n" r"\mysection{Root}"

    result = latex_intel.parse_latex_structure(source, source_file="raw.tex")

    assert result["headings"][0]["source_span"]["source_file"] == "<expanded>"


def test_file_root_cycle_is_reported_without_duplicate_headings(tmp_path):
    root = tmp_path / "main.tex"
    child = tmp_path / "child.tex"
    root.write_text(r"\section{Root}" "\n" r"\input{child}", encoding="utf-8")
    child.write_text(r"\section{Child}" "\n" r"\input{main}", encoding="utf-8")

    result = latex_intel.analyze_latex(str(root))

    assert [heading["text"] for heading in result["headings"]] == ["Root", "Child"]
    assert any(edge["status"] == "cycle" for edge in result["resolved_input_graph"])
    assert "main" in result["unexpanded_inputs"]


def test_repeated_include_is_expanded_each_time(tmp_path):
    child = tmp_path / "child.tex"
    root = tmp_path / "main.tex"
    child.write_text(r"\section{Child}", encoding="utf-8")
    root.write_text(r"\input{child}" r"\input{child}", encoding="utf-8")

    result = latex_intel.analyze_latex(str(root))

    assert [heading["text"] for heading in result["headings"]] == ["Child", "Child"]
    assert len([edge for edge in result["resolved_input_graph"] if edge["status"] == "resolved"]) == 2


def test_non_pathlike_source_file_preserves_never_raise_contract():
    result = latex_intel.parse_latex_structure(r"\section{Safe}", source_file=object())

    assert [heading["text"] for heading in result["headings"]] == ["Safe"]
