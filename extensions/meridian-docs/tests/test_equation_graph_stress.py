"""Bounded stress and determinism coverage for the equation graph."""
from __future__ import annotations

import hashlib
import io
import time
import zipfile

from meridian_docs.equation_graph import build_equation_graph


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"


def _docx(*body_children: str) -> bytes:
    document = (
        f'<w:document xmlns:w="{W}" xmlns:w14="{W14}" xmlns:m="{M}">'
        f"<w:body>{''.join(body_children)}<w:sectPr /></w:body></w:document>"
    ).encode()
    buffer = io.BytesIO()
    info = zipfile.ZipInfo("word/document.xml", date_time=(2020, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(info, document)
    return buffer.getvalue()


def _math(text: str, *, display: bool = False) -> str:
    raw = f'<m:oMath><m:r><m:t>{text}</m:t></m:r></m:oMath>'
    return f"<m:oMathPara>{raw}</m:oMathPara>" if display else raw


def _paragraph(text: str, formula: str, *, display: bool, para_id: str) -> str:
    return (
        f'<w:p w14:paraId="{para_id}">'
        f'<w:r><w:t>{text}</w:t></w:r>{_math(formula, display=display)}</w:p>'
    )


def _table_equation(
    formula: str,
    *,
    display: bool,
    number: str | None = None,
    para_id: str,
    text: str = "",
) -> str:
    number_cell = ""
    if number is not None:
        number_cell = f'<w:r><w:t>({number})</w:t></w:r>'
    return (
        "<w:tbl><w:tr>"
        f'<w:tc><w:p w14:paraId="{para_id}">'
        f'{f"<w:r><w:t>{text}</w:t></w:r>" if text else ""}'
        f"{_math(formula, display=display)}</w:p></w:tc>"
        f"<w:tc><w:p>{number_cell}</w:p></w:tc>"
        "</w:tr></w:tbl>"
    )


def _stress_docx(equation_count: int) -> bytes:
    """Return a deterministic mixed-placement document with ``equation_count`` equations."""
    if equation_count < 4 or equation_count % 4:
        raise ValueError("equation_count must be a positive multiple of four")

    body: list[str] = []
    quarter = equation_count // 4
    for index in range(quarter):
        number = str(index % 32 + 1)
        reference = f" See Equation ({number})." if index % 16 == 0 else ""
        body.append(
            _paragraph(
                f"x_{index} is defined.{reference}",
                f"x_{index}=a_{index}+b_{index}",
                display=True,
                para_id=f"D{index}",
            )
        )
        body.append(
            _paragraph(
                f"Inline x_{index} remains in prose.",
                f"x_{index}+1",
                display=False,
                para_id=f"I{index}",
            )
        )
        body.append(
            _table_equation(
                f"u_{index}/v_{index}",
                display=False,
                para_id=f"E{index}",
                text=f"Table relation {index}: ",
            )
        )
        body.append(
            _table_equation(
                f"y_{index}={{c_{index}}}",
                display=True,
                number=number,
                para_id=f"N{index}",
            )
        )
    body.append('<w:p w14:paraId="REF"><w:r><w:t>See Equation (9999).</w:t></w:r></w:p>')
    return _docx(*body)


def _build(path, *, max_nodes: int = 10000) -> tuple[dict, float]:
    started = time.perf_counter()
    result = build_equation_graph(
        str(path),
        {"symbols": [{"symbol": "x", "role": "state"}]},
        max_nodes=max_nodes,
    )
    return result, time.perf_counter() - started


def test_large_mixed_docx_graph_is_stable_read_only_and_complete(tmp_path):
    path = tmp_path / "equation-graph-1024.docx"
    fixture = _stress_docx(1024)
    assert fixture == _stress_docx(1024)
    path.write_bytes(fixture)
    before_bytes = path.read_bytes()
    before_entries = sorted(item.name for item in tmp_path.iterdir())

    first, _ = _build(path)
    second, _ = _build(path)

    assert first == second
    assert first["graph_sha256"]
    assert first["source_fingerprint"] == hashlib.sha256(before_bytes).hexdigest()
    assert first["equation_count"] == 1024
    assert first["node_count"] > 2000
    assert len({node["id"] for node in first["nodes"]}) == first["node_count"]
    assert sum(len(equations) for equations in first["placements"].values()) == 1024
    assert set(first["placements"]) == {"inline", "line_separated", "table_embedded", "table_numbered"}
    assert set(first["containers"]) == {"body_paragraph", "table_cell"}
    assert first["numbering"]["numbered_equation_count"] == 256
    assert first["numbering"]["visible_numbers"] == sorted(
        (str(number) for number in range(1, 33)),
        key=lambda value: (value.casefold(), value),
    )
    duplicate_numbers = [
        conflict
        for conflict in first["conflicts"]
        if conflict["type"] == "duplicate_equation_number"
    ]
    assert len(duplicate_numbers) == 32
    assert any(
        conflict["type"] == "unresolved_equation_reference"
        and conflict["reference"] == "9999"
        for conflict in first["conflicts"]
    )
    assert path.read_bytes() == before_bytes
    assert sorted(item.name for item in tmp_path.iterdir()) == before_entries


def test_max_nodes_is_a_hard_deterministic_boundary_without_writes(tmp_path):
    path = tmp_path / "equation-graph-limit.docx"
    path.write_bytes(_stress_docx(256))
    before_bytes = path.read_bytes()

    complete, _ = _build(path)
    at_limit, _ = _build(path, max_nodes=complete["node_count"])
    below_limit, _ = _build(path, max_nodes=complete["node_count"] - 1)
    invalid_low, _ = _build(path, max_nodes=0)
    invalid_high, _ = _build(path, max_nodes=50001)

    assert at_limit == complete
    assert below_limit["error_type"] == "limit_exceeded"
    assert below_limit["error"] == "graph node limit exceeded"
    assert below_limit["source_fingerprint"] == complete["source_fingerprint"]
    assert invalid_low["error_type"] == "options_invalid"
    assert invalid_high["error_type"] == "options_invalid"
    assert "max_nodes must be an integer" in invalid_low["error"]
    assert "max_nodes must be an integer" in invalid_high["error"]
    assert path.read_bytes() == before_bytes
    assert sorted(item.name for item in tmp_path.iterdir()) == [path.name]


def test_bounded_graph_growth_is_not_quadratic_for_mixed_fixtures(tmp_path):
    small_path = tmp_path / "equation-graph-256.docx"
    large_path = tmp_path / "equation-graph-1024.docx"
    small_path.write_bytes(_stress_docx(256))
    large_path.write_bytes(_stress_docx(1024))

    small, small_seconds = _build(small_path)
    large, large_seconds = _build(large_path)

    assert small["equation_count"] == 256
    assert large["equation_count"] == 1024
    assert large["node_count"] == 4 * small["node_count"] - 15
    assert large["edge_count"] >= 2 * small["edge_count"]
    # A 4x fixture should stay comfortably below an 8x wall-time increase;
    # the additive allowance covers interpreter and ZIP setup noise on CI.
    assert large_seconds < (8 * small_seconds) + 0.25
