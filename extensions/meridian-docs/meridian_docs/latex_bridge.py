"""Loss-aware conversion helpers between LaTeX, OMML, and :mod:`math_ir`.

The bridge intentionally reports unsupported constructs instead of flattening
them silently.  OMML stays authoritative for DOCX; the intermediate tree and
LaTeX are review/interchange representations.
"""
from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

from . import docs_intel
from .math_ir import MathNode, from_dict, make_node, opaque, sequence


_MATHML_NS = "http://www.w3.org/1998/Math/MathML"
_OMML_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _digest(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class ConversionResult:
    """A machine-readable conversion result with explicit loss reporting."""

    source_format: str
    target_format: str
    success: bool
    value: Any = None
    warnings: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()
    source_sha256: str | None = None
    result_sha256: str | None = None
    ir_sha256: str | None = None

    @property
    def lossy(self) -> bool:
        """Whether warnings or unsupported constructs were reported."""

        return bool(self.warnings or self.unsupported)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_format": self.source_format,
            "target_format": self.target_format,
            "success": self.success,
            "value": self.value,
            "warnings": list(self.warnings),
            "unsupported": list(self.unsupported),
            "source_sha256": self.source_sha256,
            "result_sha256": self.result_sha256,
            "ir_sha256": self.ir_sha256,
            "lossy": self.lossy,
        }


def _children(node: ET.Element) -> list[ET.Element]:
    """Return semantic children, omitting MathML annotation payloads."""

    if _local(node.tag) == "semantics":
        return list(node)[:1]
    return [
        child
        for child in node
        if _local(child.tag) not in {"annotation", "annotation-xml"}
    ]


def _mathml_to_ir(
    node: ET.Element,
    warnings: list[str],
    unsupported: list[str],
) -> MathNode:
    tag = _local(node.tag)
    kids = _children(node)
    text = "".join(node.itertext()).strip()

    if tag in {"math", "mrow", "mstyle", "mpadded", "mphantom", "semantics"}:
        return sequence(_mathml_to_ir(child, warnings, unsupported) for child in kids)
    if tag == "mi":
        return make_node("symbol", text=text)
    if tag == "mn":
        return make_node("number", text=text)
    if tag == "mo":
        return make_node("operator", text=text)
    if tag == "mtext":
        return make_node("text", text=text)
    if tag == "mspace":
        return make_node("text", text=" ")
    if tag in {"msub", "msup", "msubsup"}:
        expected = {"msub": 2, "msup": 2, "msubsup": 3}[tag]
        if len(kids) != expected:
            warnings.append(f"MathML {tag} has {len(kids)} children; expected {expected}")
            unsupported.append(tag)
            return opaque(f"malformed MathML {tag}", source_format="mathml")
        kind = {"msub": "subscript", "msup": "superscript", "msubsup": "subsup"}[tag]
        return make_node(kind, *(_mathml_to_ir(child, warnings, unsupported) for child in kids))
    if tag == "mfrac":
        if len(kids) != 2:
            warnings.append("MathML mfrac must have exactly two children")
            unsupported.append(tag)
            return opaque("malformed MathML fraction", source_format="mathml")
        return make_node("fraction", *(_mathml_to_ir(child, warnings, unsupported) for child in kids))
    if tag in {"msqrt", "mroot"}:
        if not kids or (tag == "mroot" and len(kids) != 2):
            warnings.append(f"MathML {tag} has an invalid child count")
            unsupported.append(tag)
            return opaque(f"malformed MathML {tag}", source_format="mathml")
        converted = [_mathml_to_ir(child, warnings, unsupported) for child in kids]
        return make_node("radical", *converted)
    if tag == "mfenced":
        converted = [_mathml_to_ir(child, warnings, unsupported) for child in kids]
        return make_node(
            "delimiter",
            *converted,
            open=node.attrib.get("open", "("),
            close=node.attrib.get("close", ")"),
            separators=node.attrib.get("separators", ","),
        )
    if tag in {"munder", "mover", "munderover"}:
        converted = [_mathml_to_ir(child, warnings, unsupported) for child in kids]
        operator = "".join(child.itertext()).strip() if kids else ""
        if tag == "mover" and len(converted) == 2 and operator in {"^", "~", "¯", "→", "⃗", "ˆ"}:
            return make_node("accent", converted[0], accent=operator)
        return make_node("nary", *converted, operator=operator or "unknown")
    if tag == "mtable":
        rows = [_mathml_to_ir(child, warnings, unsupported) for child in kids]
        return make_node("matrix", *rows)
    if tag in {"mtr", "mtd"}:
        return sequence(_mathml_to_ir(child, warnings, unsupported) for child in kids)

    warnings.append(f"unsupported MathML construct: {tag}")
    unsupported.append(tag)
    return opaque(
        f"unsupported MathML construct {tag}",
        source_format="mathml",
        raw_digest=_digest(ET.tostring(node, encoding="unicode")),
    )


_LATEX_COMMAND_SYMBOLS = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ϵ",
    "theta": "θ", "lambda": "λ", "mu": "μ", "pi": "π", "rho": "ρ",
    "sigma": "σ", "tau": "τ", "phi": "ϕ", "omega": "ω", "Gamma": "Γ",
    "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Pi": "Π", "Sigma": "Σ",
    "Phi": "Φ", "Omega": "Ω", "infty": "∞", "partial": "∂", "nabla": "∇",
}
_LATEX_OPERATOR_SYMBOLS = {
    "cdot": "·", "times": "×", "pm": "±", "leq": "≤", "geq": "≥",
    "neq": "≠", "to": "→", "rightarrow": "→", "leftarrow": "←", "mid": "∣",
    "sum": "∑", "prod": "∏", "int": "∫", "oint": "∮", "lim": "lim",
}
_LATEX_FUNCTIONS = {"sin", "cos", "tan", "sinh", "cosh", "log", "ln", "exp", "min", "max", "argmin", "argmax"}


def _collapse_nodes(nodes: list[MathNode]) -> MathNode:
    if len(nodes) == 1:
        return nodes[0]
    return sequence(nodes)


class _LatexParser:
    """Small parser for the stable subset used by document workflows.

    It is intentionally conservative.  Unknown macros become opaque nodes
    and are reported instead of being treated as ordinary letters.
    """

    def __init__(self, source: str) -> None:
        self.source = source
        self.pos = 0
        self.warnings: list[str] = []
        self.unsupported: list[str] = []

    def parse(self) -> MathNode:
        node = self._parse_sequence()
        if self.pos < len(self.source) and self.source[self.pos] == "}":
            self.warnings.append("unmatched closing brace in LaTeX")
        return node

    def _skip_space(self) -> None:
        while self.pos < len(self.source) and self.source[self.pos].isspace():
            self.pos += 1

    def _parse_sequence(self, stop: str | None = None, stop_right: bool = False) -> MathNode:
        nodes: list[MathNode] = []
        while self.pos < len(self.source):
            if stop is None and self.source[self.pos] == "}":
                self.warnings.append("unmatched closing brace in LaTeX")
                self.pos += 1
                break
            if stop is not None and self.source[self.pos] == stop:
                self.pos += 1
                break
            if stop_right and self.source.startswith(r"\right", self.pos):
                break
            if self.source[self.pos].isspace():
                self._skip_space()
                continue
            nodes.append(self._parse_atom_with_scripts())
        if stop is not None and (self.pos >= len(self.source) or (self.pos == len(self.source) and not self.source.endswith(stop))):
            # A matched stop is consumed above; reaching EOF without it means
            # the group was unterminated.  The warning makes the conversion
            # fail closed instead of treating malformed TeX as valid input.
            if not self.source[: self.pos].endswith(stop):
                self.warnings.append(f"missing closing {stop!r} in LaTeX")
        return _collapse_nodes(nodes)

    def _parse_group(self) -> MathNode:
        if self.pos >= len(self.source) or self.source[self.pos] != "{":
            return self._parse_atom_with_scripts()
        self.pos += 1
        return self._parse_sequence(stop="}")

    def _parse_required(self) -> MathNode:
        self._skip_space()
        if self.pos < len(self.source) and self.source[self.pos] == "{":
            return self._parse_group()
        return self._parse_atom_with_scripts()

    def _parse_delimiter(self) -> str:
        self._skip_space()
        if self.pos >= len(self.source):
            self.warnings.append("missing delimiter after \\left or \\right")
            return "."
        if self.source[self.pos] != "\\":
            value = self.source[self.pos]
            self.pos += 1
            return value
        command = self._read_command()
        return {"langle": "⟨", "rangle": "⟩", "lbrace": "{", "rbrace": "}", "vert": "|", "Vert": "‖"}.get(command, command)

    def _read_command(self) -> str:
        self.pos += 1  # backslash
        start = self.pos
        while self.pos < len(self.source) and self.source[self.pos].isalpha():
            self.pos += 1
        if start == self.pos and self.pos < len(self.source):
            self.pos += 1
        return self.source[start:self.pos]

    def _parse_atom_with_scripts(self) -> MathNode:
        base = self._parse_atom()
        sub: MathNode | None = None
        sup: MathNode | None = None
        while True:
            self._skip_space()
            if self.pos >= len(self.source) or self.source[self.pos] not in "_^":
                break
            marker = self.source[self.pos]
            self.pos += 1
            value = self._parse_required()
            if marker == "_":
                sub = value
            else:
                sup = value
        if base.kind == "nary" and (sub is not None or sup is not None):
            operator = base.attrs.get("operator", "unknown")
            children = list(base.children[:1])
            if sub is not None:
                children.append(sub)
            if sup is not None:
                if sub is None:
                    children.append(make_node("text", text=""))
                children.append(sup)
            return make_node("nary", *children, operator=operator)
        if sub is not None and sup is not None:
            return make_node("subsup", base, sub, sup)
        if sub is not None:
            return make_node("subscript", base, sub)
        if sup is not None:
            return make_node("superscript", base, sup)
        return base

    def _parse_atom(self) -> MathNode:
        self._skip_space()
        if self.pos >= len(self.source):
            return make_node("text", text="")
        char = self.source[self.pos]
        if char == "{":
            return self._parse_group()
        if char == "\\":
            return self._parse_command_atom()
        if char.isdigit():
            start = self.pos
            while self.pos < len(self.source) and (self.source[self.pos].isdigit() or self.source[self.pos] == "."):
                self.pos += 1
            return make_node("number", text=self.source[start:self.pos])
        self.pos += 1
        if char in "+-=*/(),[]<>|:;.!?":
            return make_node("operator", text=char)
        return make_node("symbol", text=char)

    def _parse_command_atom(self) -> MathNode:
        command = self._read_command()
        if command == "frac":
            return make_node("fraction", self._parse_required(), self._parse_required())
        if command == "sqrt":
            self._skip_space()
            index = None
            if self.pos < len(self.source) and self.source[self.pos] == "[":
                self.pos += 1
                index = self._parse_sequence(stop="]")
            radicand = self._parse_required()
            return make_node("radical", radicand, *( [index] if index is not None else [] ))
        if command in {"text", "mathrm", "mathbf", "mathit", "operatorname"}:
            value = self._parse_required()
            return make_node("text", text=_ir_text(value), style=command)
        if command == "left":
            opening = self._parse_delimiter()
            body = self._parse_sequence(stop_right=True)
            closing = "."
            if self.source.startswith(r"\right", self.pos):
                self._read_command()
                closing = self._parse_delimiter()
            else:
                self.warnings.append("\\left delimiter has no matching \\right")
            return make_node("delimiter", body, open=opening, close=closing)
        if command == "begin":
            environment = _ir_text(self._parse_required()).strip()
            self.unsupported.append(f"environment:{environment}")
            self.warnings.append(f"LaTeX environment {environment!r} is preserved as opaque")
            end_marker = rf"\end{{{environment}}}"
            end = self.source.find(end_marker, self.pos)
            if end >= 0:
                self.pos = end + len(end_marker)
            else:
                self.pos = len(self.source)
                self.warnings.append(f"LaTeX environment {environment!r} has no matching end")
            return opaque(f"unsupported LaTeX environment {environment}", source_format="latex")
        if command in _LATEX_COMMAND_SYMBOLS:
            return make_node("symbol", text=_LATEX_COMMAND_SYMBOLS[command])
        if command in _LATEX_OPERATOR_SYMBOLS:
            operator = _LATEX_OPERATOR_SYMBOLS[command]
            if command in {"sum", "prod", "int", "oint", "lim"}:
                return make_node("nary", make_node("operator", text=operator), operator=operator)
            return make_node("operator", text=operator)
        if command in _LATEX_FUNCTIONS:
            return make_node("function", name=command)
        if command in {"quad", "qquad", ",", ";", ":", "!"}:
            return make_node("text", text=" ")
        if command in {"{", "}", "_", "%", "&", "#", "$"}:
            return make_node("text", text=command)
        self.unsupported.append(command)
        self.warnings.append(f"unsupported LaTeX command: \\{command}")
        return opaque(f"unsupported LaTeX command \\{command}", source_format="latex")


def _ir_text(node: MathNode) -> str:
    if node.kind in {"text", "symbol", "number", "operator"}:
        return node.text or ""
    return "".join(_ir_text(child) for child in node.children)


def latex_to_ir(latex: str) -> ConversionResult:
    """Parse the supported LaTeX subset into the bounded Meridian IR.

    The parser deliberately does not claim complete TeX compatibility.  An
    unsupported macro remains visible as an opaque node and is listed in the
    result instead of being silently flattened into letters.
    """

    source_sha = _digest(latex) if isinstance(latex, str) else None
    if not isinstance(latex, str) or not latex.strip():
        return ConversionResult("latex", "ir", False, warnings=("LaTeX source is empty",), source_sha256=source_sha)
    parser = _LatexParser(latex)
    ir = parser.parse()
    warnings = list(parser.warnings)
    warnings.extend(f"IR validation: {issue}" for issue in ir.validate())
    canonical = ir.canonical_json()
    return ConversionResult(
        "latex", "ir", not parser.unsupported and not any(issue.startswith("IR validation:") for issue in warnings),
        value=ir.to_dict(), warnings=tuple(dict.fromkeys(warnings)),
        unsupported=tuple(dict.fromkeys(parser.unsupported)), source_sha256=source_sha,
        result_sha256=_digest(canonical), ir_sha256=ir.digest(),
    )


_OPERATOR_LATEX = {
    "×": r"\times",
    "−": "-",
    "≤": r"\leq",
    "≥": r"\geq",
    "≠": r"\neq",
    "∞": r"\infty",
    "∑": r"\sum",
    "∫": r"\int",
    "∂": r"\partial",
    "∇": r"\nabla",
    "·": r"\cdot",
}


def _latex_group(value: str) -> str:
    return value if len(value) == 1 and value.isalnum() else "{" + value + "}"


def _ir_to_latex(node: MathNode, warnings: list[str], unsupported: list[str]) -> str:
    attrs = node.attrs
    if node.kind == "sequence":
        return "".join(_ir_to_latex(child, warnings, unsupported) for child in node.children)
    if node.kind == "text":
        style = attrs.get("style")
        if style == "operatorname":
            return r"\operatorname{" + (node.text or "") + "}"
        if style in {"mathrm", "mathbf", "mathit"}:
            return rf"\{style}" + _latex_group(node.text or "")
        return node.text or ""
    if node.kind in {"symbol", "number"}:
        return node.text or ""
    if node.kind == "operator":
        return _OPERATOR_LATEX.get(node.text or "", node.text or "")
    if node.kind == "fraction" and len(node.children) == 2:
        return r"\frac" + _latex_group(_ir_to_latex(node.children[0], warnings, unsupported)) + _latex_group(
            _ir_to_latex(node.children[1], warnings, unsupported)
        )
    if node.kind == "superscript" and len(node.children) == 2:
        return _latex_group(_ir_to_latex(node.children[0], warnings, unsupported)) + "^" + _latex_group(
            _ir_to_latex(node.children[1], warnings, unsupported)
        )
    if node.kind == "subscript" and len(node.children) == 2:
        return _latex_group(_ir_to_latex(node.children[0], warnings, unsupported)) + "_" + _latex_group(
            _ir_to_latex(node.children[1], warnings, unsupported)
        )
    if node.kind == "subsup" and len(node.children) == 3:
        return (
            _latex_group(_ir_to_latex(node.children[0], warnings, unsupported))
            + "_"
            + _latex_group(_ir_to_latex(node.children[1], warnings, unsupported))
            + "^"
            + _latex_group(_ir_to_latex(node.children[2], warnings, unsupported))
        )
    if node.kind == "radical" and node.children:
        radicand = _latex_group(_ir_to_latex(node.children[0], warnings, unsupported))
        if len(node.children) == 2:
            return r"\sqrt[" + _ir_to_latex(node.children[1], warnings, unsupported) + "]" + radicand
        return r"\sqrt" + radicand
    if node.kind == "function":
        name = attrs.get("name", "operator")
        body = "".join(_ir_to_latex(child, warnings, unsupported) for child in node.children)
        return rf"\operatorname{{{name}}}" + (_latex_group(body) if body else "")
    if node.kind == "delimiter":
        body = "".join(_ir_to_latex(child, warnings, unsupported) for child in node.children)
        return rf"\left{attrs.get('open', '(')}{body}\right{attrs.get('close', ')')}"
    if node.kind == "accent" and node.children:
        accent = {"^": "hat", "ˆ": "hat", "~": "tilde", "→": "vec"}.get(attrs.get("accent", ""), "hat")
        return rf"\{accent}" + _latex_group(_ir_to_latex(node.children[0], warnings, unsupported))
    if node.kind == "nary":
        operator = _OPERATOR_LATEX.get(attrs.get("operator", ""), attrs.get("operator", r"\mathop{}"))
        return operator + "".join(_latex_group(_ir_to_latex(child, warnings, unsupported)) for child in node.children[1:])
    if node.kind in {"matrix", "cases"}:
        rows = []
        for row in node.children:
            rows.append(_ir_to_latex(row, warnings, unsupported))
        env = "cases" if node.kind == "cases" else "matrix"
        return rf"\begin{{{env}}}" + r" \\ ".join(rows) + rf"\end{{{env}}}"
    if node.kind == "opaque":
        reason = attrs.get("reason", "unsupported construct")
        warnings.append(f"lossy LaTeX placeholder emitted: {reason}")
        unsupported.append(reason)
        return r"\text{[unsupported math]}"
    warnings.append(f"unsupported IR node: {node.kind}")
    unsupported.append(node.kind)
    return r"\text{[unsupported math]}"


def ir_to_latex(ir: MathNode | dict[str, Any]) -> ConversionResult:
    """Serialize IR to canonical, explicitly loss-aware LaTeX."""

    try:
        node = from_dict(ir) if isinstance(ir, dict) else ir
        if not isinstance(node, MathNode):
            raise TypeError("ir must be a MathNode or dictionary")
    except (TypeError, ValueError) as exc:
        return ConversionResult("ir", "latex", False, warnings=(f"invalid IR: {exc}",))
    warnings: list[str] = []
    unsupported: list[str] = []
    latex = _ir_to_latex(node, warnings, unsupported)
    return ConversionResult(
        "ir",
        "latex",
        not warnings,
        value=latex,
        warnings=tuple(dict.fromkeys(warnings)),
        unsupported=tuple(dict.fromkeys(unsupported)),
        source_sha256=_digest(node.canonical_json()),
        result_sha256=_digest(latex),
        ir_sha256=node.digest(),
    )


def _omml_text(node: ET.Element) -> str:
    return "".join(node.itertext()).strip()


def _omml(name: str) -> str:
    return f"{{{_OMML_NS}}}{name}"


def _append_ir_to_omml(
    node: MathNode,
    parent: ET.Element,
    warnings: list[str],
    unsupported: list[str],
) -> None:
    if node.kind == "sequence":
        for child in node.children:
            _append_ir_to_omml(child, parent, warnings, unsupported)
        return
    if node.kind in {"text", "symbol", "number", "operator"}:
        run = ET.SubElement(parent, _omml("r"))
        ET.SubElement(run, _omml("t")).text = node.text or ""
        return
    if node.kind == "fraction" and len(node.children) == 2:
        fraction = ET.SubElement(parent, _omml("f"))
        for name, child in (("num", node.children[0]), ("den", node.children[1])):
            slot = ET.SubElement(fraction, _omml(name))
            expr = ET.SubElement(slot, _omml("e"))
            _append_ir_to_omml(child, expr, warnings, unsupported)
        return
    if node.kind in {"superscript", "subscript", "subsup"}:
        names = {"superscript": ("e", "sup"), "subscript": ("e", "sub"), "subsup": ("e", "sub", "sup")}[node.kind]
        if len(node.children) != len(names):
            warnings.append(f"invalid IR {node.kind} child count")
            unsupported.append(node.kind)
            return
        container = ET.SubElement(parent, _omml({"superscript": "sSup", "subscript": "sSub", "subsup": "sSubSup"}[node.kind]))
        for name, child in zip(names, node.children):
            expr = ET.SubElement(container, _omml(name))
            _append_ir_to_omml(child, expr, warnings, unsupported)
        return
    if node.kind == "radical" and node.children:
        radical = ET.SubElement(parent, _omml("rad"))
        if len(node.children) == 1:
            rad_pr = ET.SubElement(radical, _omml("radPr"))
            ET.SubElement(rad_pr, _omml("degHide"), {_omml("val"): "1"})
            ET.SubElement(radical, _omml("deg"))
            radicand = node.children[0]
        elif len(node.children) == 2:
            degree = ET.SubElement(radical, _omml("deg"))
            _append_ir_to_omml(node.children[1], degree, warnings, unsupported)
            radicand = node.children[0]
        else:
            warnings.append("radical has too many children")
            unsupported.append("radical")
            return
        expr = ET.SubElement(radical, _omml("e"))
        _append_ir_to_omml(radicand, expr, warnings, unsupported)
        return
    if node.kind == "function":
        function = ET.SubElement(parent, _omml("func"))
        name = ET.SubElement(function, _omml("fName"))
        run = ET.SubElement(name, _omml("r"))
        ET.SubElement(run, _omml("t")).text = node.attrs.get("name", "operator")
        expr = ET.SubElement(function, _omml("e"))
        for child in node.children:
            _append_ir_to_omml(child, expr, warnings, unsupported)
        return
    if node.kind == "delimiter":
        delimiter = ET.SubElement(parent, _omml("d"))
        dpr = ET.SubElement(delimiter, _omml("dPr"))
        ET.SubElement(dpr, _omml("begChr"), {_omml("val"): node.attrs.get("open", "(")})
        ET.SubElement(dpr, _omml("endChr"), {_omml("val"): node.attrs.get("close", ")")})
        expr = ET.SubElement(delimiter, _omml("e"))
        for child in node.children:
            _append_ir_to_omml(child, expr, warnings, unsupported)
        return
    if node.kind == "accent" and node.children:
        accent = ET.SubElement(parent, _omml("acc"))
        apr = ET.SubElement(accent, _omml("accPr"))
        ET.SubElement(apr, _omml("chr"), {_omml("val"): node.attrs.get("accent", "^")})
        expr = ET.SubElement(accent, _omml("e"))
        _append_ir_to_omml(node.children[0], expr, warnings, unsupported)
        return
    if node.kind == "nary" and node.children:
        nary = ET.SubElement(parent, _omml("nary"))
        npr = ET.SubElement(nary, _omml("naryPr"))
        ET.SubElement(npr, _omml("chr"), {_omml("val"): node.attrs.get("operator", "∑")})
        expr = ET.SubElement(nary, _omml("e"))
        _append_ir_to_omml(node.children[0], expr, warnings, unsupported)
        for name, child in zip(("sub", "sup"), node.children[1:]):
            slot = ET.SubElement(nary, _omml(name))
            _append_ir_to_omml(child, slot, warnings, unsupported)
        return
    if node.kind in {"matrix", "cases"}:
        array = ET.SubElement(parent, _omml("eqArr"))
        for row in node.children:
            expr = ET.SubElement(array, _omml("e"))
            _append_ir_to_omml(row, expr, warnings, unsupported)
        return
    reason = node.attrs.get("reason", f"unsupported IR node {node.kind}")
    warnings.append(f"cannot emit OMML for {reason}")
    unsupported.append(reason)


def ir_to_omml(ir: MathNode | dict[str, Any]) -> ConversionResult:
    """Emit native OMML only for the supported IR subset."""

    try:
        node = from_dict(ir) if isinstance(ir, dict) else ir
        if not isinstance(node, MathNode):
            raise TypeError("ir must be a MathNode or dictionary")
    except (TypeError, ValueError) as exc:
        return ConversionResult("ir", "omml", False, warnings=(f"invalid IR: {exc}",))
    warnings: list[str] = []
    unsupported: list[str] = []
    root = ET.Element(_omml("oMath"))
    _append_ir_to_omml(node, root, warnings, unsupported)
    raw = ET.tostring(root, encoding="unicode")
    try:
        docs_intel._validate_omml_structure(raw)
    except ValueError as exc:
        warnings.append(str(exc))
    return ConversionResult(
        "ir", "omml", not warnings and not unsupported, value=raw,
        warnings=tuple(dict.fromkeys(warnings)), unsupported=tuple(dict.fromkeys(unsupported)),
        source_sha256=_digest(node.canonical_json()), result_sha256=_digest(raw), ir_sha256=node.digest(),
    )


def _omml_to_ir(node: ET.Element, warnings: list[str], unsupported: list[str]) -> MathNode:
    tag = _local(node.tag)
    if tag in {"oMath", "oMathPara", "e", "mr"}:
        return sequence(_omml_to_ir(child, warnings, unsupported) for child in node if _local(child.tag) not in {"rPr", "ctrlPr"})
    if tag == "r":
        text = _omml_text(node)
        return make_node("operator" if text in _OPERATOR_LATEX or text in {"=", "+", "-", "/", "(" , ")"} else "symbol", text=text)
    if tag == "t":
        return make_node("symbol", text=node.text or "")
    if tag == "f":
        parts = { _local(child.tag): child for child in node }
        if "num" not in parts or "den" not in parts:
            warnings.append("OMML fraction is missing num or den")
            unsupported.append("f")
            return opaque("malformed OMML fraction", source_format="omml")
        return make_node("fraction", _omml_to_ir(parts["num"], warnings, unsupported), _omml_to_ir(parts["den"], warnings, unsupported))
    if tag in {"sSub", "sSup", "sSubSup"}:
        parts = { _local(child.tag): child for child in node }
        names = {"sSub": ("e", "sub"), "sSup": ("e", "sup"), "sSubSup": ("e", "sub", "sup")}[tag]
        if any(name not in parts for name in names):
            warnings.append(f"OMML {tag} is missing a required operand")
            unsupported.append(tag)
            return opaque(f"malformed OMML {tag}", source_format="omml")
        kind = {"sSub": "subscript", "sSup": "superscript", "sSubSup": "subsup"}[tag]
        return make_node(kind, *( _omml_to_ir(parts[name], warnings, unsupported) for name in names))
    if tag == "rad":
        parts = { _local(child.tag): child for child in node }
        if "e" not in parts:
            warnings.append("OMML radical is missing its radicand")
            unsupported.append(tag)
            return opaque("malformed OMML radical", source_format="omml")
        values = [_omml_to_ir(parts["e"], warnings, unsupported)]
        degree = parts.get("deg")
        rad_pr = parts.get("radPr")
        hidden_degree = rad_pr is not None and any(
            _local(child.tag) == "degHide" and child.attrib.get(f"{{{_OMML_NS}}}val", child.attrib.get("val")) == "1"
            for child in rad_pr
        )
        if degree is not None and not hidden_degree and list(degree):
            values.append(_omml_to_ir(degree, warnings, unsupported))
        return make_node("radical", *values)
    if tag == "d":
        attrs = {}
        dpr = next((child for child in node if _local(child.tag) == "dPr"), None)
        if dpr is not None:
            for child in dpr:
                name = _local(child.tag)
                if name in {"begChr", "endChr"}:
                    attrs["open" if name == "begChr" else "close"] = child.attrib.get(f"{{{_OMML_NS}}}val", child.attrib.get("val", ""))
        expr = next((child for child in node if _local(child.tag) == "e"), None)
        if expr is None:
            warnings.append("OMML delimiter is missing its expression")
            unsupported.append(tag)
            return opaque("malformed OMML delimiter", source_format="omml")
        return make_node("delimiter", _omml_to_ir(expr, warnings, unsupported), open=attrs.get("open", "("), close=attrs.get("close", ")"))
    if tag == "func":
        name_node = next((child for child in node if _local(child.tag) == "fName"), None)
        expr = next((child for child in node if _local(child.tag) == "e"), None)
        if name_node is None or expr is None:
            warnings.append("OMML function is missing fName or expression")
            unsupported.append(tag)
            return opaque("malformed OMML function", source_format="omml")
        return make_node("function", _omml_to_ir(expr, warnings, unsupported), name=_omml_text(name_node))
    if tag in {"nary", "limLow", "limUpp"}:
        expr = next((child for child in node if _local(child.tag) == "e"), None)
        if expr is None:
            warnings.append(f"OMML {tag} is missing its expression")
            unsupported.append(tag)
            return opaque(f"malformed OMML {tag}", source_format="omml")
        operator = ""
        props = next((child for child in node if _local(child.tag) == "naryPr"), None)
        if props is not None:
            char = next((child for child in props if _local(child.tag) == "chr"), None)
            if char is not None:
                operator = char.attrib.get(f"{{{_OMML_NS}}}val", char.attrib.get("val", ""))
        children = [_omml_to_ir(expr, warnings, unsupported)]
        for child in node:
            if _local(child.tag) in {"sub", "sup", "lim"}:
                children.append(_omml_to_ir(child, warnings, unsupported))
        return make_node("nary", *children, operator=operator or _omml_text(expr) or "unknown")
    if tag == "acc":
        expr = next((child for child in node if _local(child.tag) == "e"), None)
        acc_pr = next((child for child in node if _local(child.tag) == "accPr"), None)
        chr_node = next((child for child in acc_pr or () if _local(child.tag) == "chr"), None)
        if expr is None:
            warnings.append("OMML accent is missing its expression")
            unsupported.append(tag)
            return opaque("malformed OMML accent", source_format="omml")
        accent = chr_node.attrib.get(f"{{{_OMML_NS}}}val", chr_node.attrib.get("val", "")) if chr_node is not None else ""
        return make_node("accent", _omml_to_ir(expr, warnings, unsupported), accent=accent or "^")
    if tag in {"eqArr", "m"}:
        rows = [child for child in node if _local(child.tag) in {"e", "mr"}]
        return make_node("matrix", *(_omml_to_ir(row, warnings, unsupported) for row in rows))
    if tag in {"rPr", "ctrlPr", "fPr", "num", "den", "sub", "sup", "deg", "lim", "fName", "dPr", "naryPr", "accPr"}:
        return sequence(_omml_to_ir(child, warnings, unsupported) for child in node)

    warnings.append(f"unsupported OMML construct: m:{tag}")
    unsupported.append(tag)
    return opaque(
        f"unsupported OMML construct m:{tag}",
        source_format="omml",
        raw_digest=_digest(ET.tostring(node, encoding="unicode")),
    )


def omml_to_ir(omml: str) -> ConversionResult:
    """Convert a native ``m:oMath`` payload into the bounded IR."""

    source_sha = _digest(omml) if isinstance(omml, str) else None
    if not isinstance(omml, str) or not omml.strip():
        return ConversionResult("omml", "ir", False, warnings=("OMML source is empty",), source_sha256=source_sha)
    try:
        root = ET.fromstring(omml)
    except ET.ParseError as exc:
        return ConversionResult("omml", "ir", False, warnings=(f"OMML XML is invalid: {exc}",), source_sha256=source_sha)
    warnings: list[str] = []
    unsupported: list[str] = []
    if _local(root.tag) == "oMathPara":
        warnings.append("OMML oMathPara wrapper accepted for inspection; oMath remains the insertion payload")
    elif _local(root.tag) != "oMath":
        return ConversionResult("omml", "ir", False, warnings=("OMML root must be m:oMath or m:oMathPara",), source_sha256=source_sha)
    if _local(root.tag) == "oMath":
        try:
            docs_intel._validate_omml_structure(omml)
        except ValueError as exc:
            warnings.append(str(exc))
            return ConversionResult("omml", "ir", False, warnings=tuple(warnings), source_sha256=source_sha)
    ir = _omml_to_ir(root, warnings, unsupported)
    warnings.extend(f"IR validation: {issue}" for issue in ir.validate())
    return ConversionResult(
        "omml", "ir", not any(issue.startswith("IR validation:") for issue in warnings), value=ir.to_dict(),
        warnings=tuple(dict.fromkeys(warnings)), unsupported=tuple(dict.fromkeys(unsupported)),
        source_sha256=source_sha, result_sha256=_digest(ir.canonical_json()), ir_sha256=ir.digest(),
    )


def omml_to_latex(omml: str) -> ConversionResult:
    parsed = omml_to_ir(omml)
    if not parsed.success:
        return ConversionResult(
            "omml", "latex", False, warnings=parsed.warnings, unsupported=parsed.unsupported,
            source_sha256=parsed.source_sha256,
        )
    rendered = ir_to_latex(parsed.value)
    return ConversionResult(
        "omml", "latex", rendered.success, value=rendered.value,
        warnings=tuple(dict.fromkeys((*parsed.warnings, *rendered.warnings))),
        unsupported=tuple(dict.fromkeys((*parsed.unsupported, *rendered.unsupported))),
        source_sha256=parsed.source_sha256, result_sha256=rendered.result_sha256,
        ir_sha256=parsed.ir_sha256,
    )


def latex_to_omml(latex: str) -> ConversionResult:
    """Emit native OMML from the supported LaTeX subset with diagnostics."""

    parsed = latex_to_ir(latex)
    if not parsed.success and parsed.value is None:
        return ConversionResult(
            "latex", "omml", False, warnings=parsed.warnings, unsupported=parsed.unsupported,
            source_sha256=parsed.source_sha256, ir_sha256=parsed.ir_sha256,
        )
    emitted = ir_to_omml(parsed.value)
    if emitted.value is None:
        return ConversionResult(
            "latex", "omml", False, warnings=tuple(dict.fromkeys((*parsed.warnings, "LaTeX could not be converted to valid OMML"))),
            unsupported=parsed.unsupported, source_sha256=parsed.source_sha256, ir_sha256=parsed.ir_sha256,
        )
    return ConversionResult(
        "latex", "omml", emitted.success and not parsed.unsupported, value=emitted.value,
        warnings=tuple(dict.fromkeys((*parsed.warnings, *emitted.warnings))),
        unsupported=tuple(dict.fromkeys((*parsed.unsupported, *emitted.unsupported))),
        source_sha256=parsed.source_sha256, result_sha256=emitted.result_sha256, ir_sha256=parsed.ir_sha256,
    )


def convert_equation(source: Any, source_format: str, target_format: str) -> ConversionResult:
    """Convert supported representations without claiming universal fidelity."""

    source_format = source_format.casefold().strip()
    target_format = target_format.casefold().strip()
    supported = {"latex", "omml", "ir"}
    if source_format not in supported or target_format not in supported:
        return ConversionResult(
            source_format,
            target_format,
            False,
            warnings=(f"unsupported format: {source_format} -> {target_format}",),
        )
    if source_format == target_format:
        if source_format == "ir":
            try:
                node = from_dict(source) if isinstance(source, dict) else source
                if not isinstance(node, MathNode):
                    raise TypeError("IR source must be an object")
                return ConversionResult("ir", "ir", True, value=node.to_dict(), result_sha256=_digest(node.canonical_json()), ir_sha256=node.digest())
            except (TypeError, ValueError) as exc:
                return ConversionResult("ir", "ir", False, warnings=(f"invalid IR: {exc}",))
        if isinstance(source, str):
            return ConversionResult(source_format, target_format, True, value=source, source_sha256=_digest(source), result_sha256=_digest(source))
    if source_format == "latex" and target_format in {"ir", "omml"}:
        return latex_to_ir(source) if target_format == "ir" else latex_to_omml(source)
    if source_format == "omml" and target_format in {"ir", "latex"}:
        return omml_to_ir(source) if target_format == "ir" else omml_to_latex(source)
    if source_format == "ir" and target_format == "latex":
        return ir_to_latex(source)
    return ConversionResult(
        source_format, target_format, False,
        warnings=(f"unsupported conversion: {source_format} -> {target_format}",),
    )


__all__ = [
    "ConversionResult",
    "convert_equation",
    "ir_to_latex",
    "ir_to_omml",
    "latex_to_ir",
    "latex_to_omml",
    "omml_to_ir",
    "omml_to_latex",
]
