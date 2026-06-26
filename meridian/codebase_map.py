"""5813affe — package-level codebase dependency map rendering (graphviz).

Core logic shared by ``scripts/gen_codebase_map.py`` (CLI) and the
``POST /projects/{id}/codebase-map`` route. The codebase graph is huge (~6k
nodes), so this renders the PACKAGE-level graph (optionally just the top-N
hotspot subgraph). Input is the already-aggregated graph the dashboard fetches
via ``codebase__query_graph``::

    {"packages": [{"name": "meridian.db", "node_count": 120, "layer": 0}, ...],
     "edges":    [{"source": "meridian.routes", "target": "meridian.db",
                   "value": 12}, ...]}

``build_dot`` (pure) turns that into Graphviz DOT source; ``render_map`` shells
``dot``. ``dot`` is an optional system dependency — its absence raises
:class:`GraphvizMissingError` so callers surface an actionable message.
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Any

# Layer index → fill colour (matches the dashboard force-graph palette).
_LAYER_COLORS = (
    "#60a5fa", "#34d399", "#fbbf24", "#f87171", "#a78bfa", "#f472b6", "#22d3ee",
)


class GraphvizMissingError(RuntimeError):
    """Raised when the ``dot`` binary is not on PATH."""


def _esc(label: str) -> str:
    """Escape a DOT string literal."""
    return str(label).replace("\\", "\\\\").replace('"', '\\"')


def build_dot(graph: dict[str, Any], *, hotspots_only: bool = False, top_n: int = 20) -> str:
    """Build Graphviz DOT source for a package dependency graph (pure).

    ``hotspots_only`` keeps just the *top_n* packages by total edge weight (the
    busiest packages + the edges among them) so the map stays legible on a large
    codebase. Unknown/blank packages and self/dangling edges are dropped.
    """
    packages = [p for p in (graph.get("packages") or []) if isinstance(p, dict) and p.get("name")]
    edges = [
        e for e in (graph.get("edges") or [])
        if isinstance(e, dict) and e.get("source") and e.get("target")
    ]

    degree: dict[str, float] = {}
    for e in edges:
        w = float(e.get("value") or 1)
        degree[str(e["source"])] = degree.get(str(e["source"]), 0.0) + w
        degree[str(e["target"])] = degree.get(str(e["target"]), 0.0) + w

    if hotspots_only and len(packages) > top_n:
        ranked = sorted(packages, key=lambda p: degree.get(str(p["name"]), 0.0), reverse=True)
        keep = {str(p["name"]) for p in ranked[:top_n]}
        packages = [p for p in packages if str(p["name"]) in keep]

    names = {str(p["name"]) for p in packages}
    max_nc = max((float(p.get("node_count") or 0) for p in packages), default=1.0) or 1.0

    lines: list[str] = [
        "digraph codebase {",
        '  graph [bgcolor="#0b0e14", rankdir=LR, splines=true, overlap=false, pad=0.3];',
        '  node [shape=box, style="rounded,filled", fontname="Helvetica", '
        'fontsize=10, fontcolor="#0b0e14", color="#1f2937"];',
        '  edge [color="#3b4252", arrowsize=0.6];',
    ]
    for p in packages:
        name = str(p["name"])
        nc = float(p.get("node_count") or 0)
        layer = p.get("layer")
        try:
            color = _LAYER_COLORS[int(layer) % len(_LAYER_COLORS)] if layer is not None else "#9ca3af"
        except (TypeError, ValueError):
            color = "#9ca3af"
        fs = 9 + int(9 * (nc / max_nc))  # 9..18 by node_count
        lines.append(
            f'  "{_esc(name)}" [fillcolor="{color}", fontsize={fs}, '
            f'tooltip="{_esc(name)} ({int(nc)} nodes)"];'
        )
    for e in edges:
        s, t = str(e["source"]), str(e["target"])
        if s in names and t in names and s != t:
            w = float(e.get("value") or 1)
            pen = min(6.0, 0.6 + w / 4.0)
            lines.append(f'  "{_esc(s)}" -> "{_esc(t)}" [penwidth={pen:.1f}];')
    lines.append("}")
    return "\n".join(lines)


def graphviz_available() -> bool:
    """True if the ``dot`` binary is on PATH."""
    return bool(shutil.which("dot"))


def render_map(graph: dict[str, Any], out_path: str, *, hotspots_only: bool = False,
               fmt: str = "png") -> str:
    """Render *graph* to *out_path* via ``dot``. Returns the output path.

    Raises :class:`GraphvizMissingError` when ``dot`` isn't installed.
    """
    dot_bin = shutil.which("dot")
    if not dot_bin:
        raise GraphvizMissingError(
            "Graphviz 'dot' is not on PATH. Install Graphviz "
            "(https://graphviz.org/download/) — e.g. `winget install graphviz`, "
            "`brew install graphviz`, or `apt install graphviz`."
        )
    dot_src = build_dot(graph, hotspots_only=hotspots_only)
    proc = subprocess.run(
        [dot_bin, f"-T{fmt}", "-o", out_path],
        input=dot_src.encode("utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"dot failed: {proc.stderr.decode('utf-8', 'replace')[:500]}")
    return out_path
