#!/usr/bin/env python
"""5813affe — CLI to render a package-level codebase map (graphviz).

Thin wrapper around :mod:`meridian.codebase_map`. Reads the aggregated package
graph as JSON (from a file arg or stdin) and renders it to ``--out``::

    pixi run python scripts/gen_codebase_map.py graph.json --out data/codebase_map.png
    pixi run python scripts/gen_codebase_map.py graph.json --hotspots --out data/codebase_map_hotspots.png

The graph shape is what the dashboard fetches via ``codebase__query_graph``::

    {"packages": [{"name": "meridian.db", "node_count": 120, "layer": 0}, ...],
     "edges":    [{"source": "meridian.routes", "target": "meridian.db", "value": 12}, ...]}

``dot`` (Graphviz) is an optional system dependency; its absence exits non-zero
with an actionable install hint rather than a stack trace. ``data/`` is
gitignored, so the rendered images are not committed.
"""
from __future__ import annotations

import argparse
import json
import sys

from meridian.codebase_map import GraphvizMissingError, render_map


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="Render a package-level codebase map (graphviz).")
    parser.add_argument("graph_json", nargs="?", help="Path to graph JSON (default: stdin).")
    parser.add_argument("--out", default="data/codebase_map.png", help="Output image path.")
    parser.add_argument("--hotspots", action="store_true", help="Top-20 hotspot subgraph only.")
    parser.add_argument("--format", default="png", help="Output format (png/svg).")
    args = parser.parse_args(argv)

    raw = open(args.graph_json, encoding="utf-8").read() if args.graph_json else sys.stdin.read()
    try:
        graph = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"error: invalid graph JSON: {exc}", file=sys.stderr)
        return 2
    try:
        path = render_map(graph, args.out, hotspots_only=args.hotspots, fmt=args.format)
    except GraphvizMissingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
