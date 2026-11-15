"""Tests for meridian_desktop/client_guides.py. The guides are plain data: no Qt, no network, no files.

The token checks read render_text output, which is the text a user copies, not only the
data fields. A token-shaped string added to any field fails the check.
"""

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

from meridian_desktop.client_guides import (
    CONFIRM_ON_INSTALL_LINE,
    GUIDES,
    KIND_HOSTED,
    KINDS,
    MCP_ENDPOINT,
    TOKEN_PLACEHOLDER,
    ClientGuide,
    get_guide,
    render_text,
)

REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "meridian_desktop" / "client_guides.py"

# Shapes a live token would have. The guides must match none of them.
TOKEN_SHAPES = (
    re.compile(r"sk_meridian_", re.IGNORECASE),
    re.compile(r"\bsk_[A-Za-z0-9_-]{8,}"),
    re.compile(r"bearer", re.IGNORECASE),
)
QT_ROOTS = {"PySide6", "PySide2", "PyQt5", "PyQt6", "PyQt4", "shiboken6", "shiboken2"}
NETWORK_MODULES = {"socket", "ssl", "urllib", "http", "requests", "httpx", "aiohttp", "ftplib", "smtplib"}
# Guides whose path, flow or key names are not yet confirmed on install (see the module docstring).
UNCONFIRMED_IDS = {"grok", "zed", "windsurf"}


def test_guides_contain_no_token_and_mark_unconfirmed_paths():
    assert {guide.client_id for guide in GUIDES} == {"chatgpt", "grok", "zed", "windsurf"}
    for guide in GUIDES:
        text = render_text(guide)
        for shape in TOKEN_SHAPES:
            assert not shape.search(text), f"{guide.client_id} guide matches {shape.pattern!r}"
        if guide.client_id in UNCONFIRMED_IDS:
            assert not guide.confirmed, f"{guide.client_id} must be unconfirmed"
            assert CONFIRM_ON_INSTALL_LINE in text, f"{guide.client_id} guide is not marked confirm on install"
        else:
            assert guide.confirmed, f"{guide.client_id} must be confirmed"
            assert CONFIRM_ON_INSTALL_LINE not in text


def test_chatgpt_guide_names_public_https_endpoint_and_dashboard_tokens():
    guide = get_guide("chatgpt")
    assert guide is not None
    text = render_text(guide)
    assert MCP_ENDPOINT == "https://usemeridian.us/mcp"
    assert MCP_ENDPOINT.startswith("https://")
    assert MCP_ENDPOINT in text
    assert "Tokens are created in the dashboard" in text
    assert guide.kind == KIND_HOSTED


def test_snippets_hold_the_placeholder_not_a_value():
    snippets = [guide.snippet for guide in GUIDES if guide.snippet is not None]
    assert len(snippets) == 2  # Zed and Windsurf
    for snippet in snippets:
        assert TOKEN_PLACEHOLDER in snippet
        assert MCP_ENDPOINT in snippet


def test_module_imports_no_qt_and_no_network_module():
    tree = ast.parse(MODULE_PATH.read_bytes(), filename=str(MODULE_PATH))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & QT_ROOTS
    assert not imported & NETWORK_MODULES


def test_import_and_render_make_no_network_call_and_load_no_qt():
    # A fresh interpreter: socket creation is refused, so any network use raises.
    script = (
        "import socket, sys\n"
        "def _refuse(*args, **kwargs):\n"
        "    raise RuntimeError('network use')\n"
        "socket.socket = _refuse\n"
        "socket.create_connection = _refuse\n"
        "socket.getaddrinfo = _refuse\n"
        "from meridian_desktop.client_guides import GUIDES, render_text\n"
        "for guide in GUIDES:\n"
        "    render_text(guide)\n"
        "assert not any(m.startswith(('PySide', 'shiboken')) for m in sys.modules)\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=REPO, capture_output=True, text=True, check=False, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_unknown_client_id_gives_none():
    assert get_guide("no-such-client") is None


def test_guide_rejects_unknown_kind_and_empty_steps():
    with pytest.raises(ValueError):
        ClientGuide(client_id="x", name="X", kind="writer", confirmed=True, summary="s", steps=("a",))
    with pytest.raises(ValueError):
        ClientGuide(client_id="x", name="X", kind=KINDS[0], confirmed=True, summary="s", steps=())
    with pytest.raises(ValueError):
        ClientGuide(client_id="Not A Slug", name="X", kind=KINDS[0], confirmed=True, summary="s", steps=("a",))
