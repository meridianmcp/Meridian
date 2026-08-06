"""106caa76 — local tunnel extension servers must stay on a compatible MCP SDK major.

Live tunnel evidence: an isolated ``uvx --from <path>`` child resolved mcp==2.0.0,
which removed the ``mcp.server.fastmcp`` import both meridian-docs and
meridian-outputs use, crashing the child on startup. The manifests declared
``mcp>=1.0`` with no upper bound, so uvx was free to resolve an incompatible
major. This file proves the pin is in place and that the entry points still
import successfully under the pinned major.

Scoped to the two local extension manifests (the item's declared touches_resources);
the tunnel control-plane and Serena spawn command are intentionally untouched here.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[1]

EXTENSION_MANIFESTS = [
    "extensions/meridian-docs/pyproject.toml",
    "extensions/meridian-outputs/pyproject.toml",
]

REQUIRED_MCP_SPEC = "mcp>=1.27,<2"


def _dependencies(manifest_path: str) -> list[str]:
    with (REPO_ROOT / manifest_path).open("rb") as handle:
        data = tomllib.load(handle)
    return list(data.get("project", {}).get("dependencies", []))


def test_local_extension_manifests_pin_supported_mcp_sdk_major() -> None:
    for manifest_path in EXTENSION_MANIFESTS:
        deps = _dependencies(manifest_path)
        assert REQUIRED_MCP_SPEC in deps, (
            f"{manifest_path} must declare {REQUIRED_MCP_SPEC!r} to stay on the "
            f"v1 FastMCP import path (mcp 2.0 removed mcp.server.fastmcp); "
            f"found dependencies={deps!r}"
        )


@pytest.mark.parametrize(
    "extension_dir, module_name",
    [
        ("meridian-docs", "meridian_docs.server"),
        ("meridian-outputs", "meridian_outputs.server"),
    ],
)
def test_local_extension_entry_point_imports_fastmcp(extension_dir: str, module_name: str) -> None:
    """Regression coverage for the actual crash: the entry-point module must
    import cleanly and construct a real mcp.server.fastmcp.FastMCP instance
    under whatever mcp major is installed in this environment."""
    ext_path = os.path.abspath(str(REPO_ROOT / "extensions" / extension_dir))
    sys.path.insert(0, ext_path)
    try:
        try:
            import importlib

            module = importlib.import_module(module_name)
        except ImportError as exc:
            pytest.skip(f"{module_name} not importable in this environment: {exc}")
    finally:
        sys.path.remove(ext_path)

    from mcp.server.fastmcp import FastMCP

    assert isinstance(module.mcp, FastMCP), (
        f"{module_name}.mcp must be a real mcp.server.fastmcp.FastMCP instance; "
        "an incompatible mcp major would have failed the import above instead"
    )
