"""Path containment tests for the CodeQL py/path-injection fixes.

Covers the caller-controlled path flows on fix/codeql-path-injection:

- ``handoff.resolve_contained_output_dir`` (containment for promotion targets)
- the regenerate ``output_dir`` override, over MCP and HTTP: refused before any
  write when it cannot name a directory. Containment of the override is an
  open decision, so a path outside the data directory is still accepted as
  before.
- promotion targets joined onto a handoff output dir, recorded as unresolved
  (never hashed) when they escape
- the codebase-map temp file, which no longer embeds the URL ``project_id``
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

from meridian import artifact_declaration as artifact_declaration_module
from meridian import db as db_module
from meridian import handoff as handoff_module

# meridian.server must load before meridian.mcp.handler: handler imports server
# and server imports back from handler, so the reverse order is circular.
import meridian.server  # noqa: F401,E402

routes_handoff = importlib.import_module("meridian.routes.handoff")
mcp_handler = importlib.import_module("meridian.mcp.handler")


# ---------------------------------------------------------------------------
# resolve_contained_output_dir: the shared containment check
# ---------------------------------------------------------------------------


def test_contained_dir_accepts_root_and_nested_children(tmp_path):
    root = tmp_path / "data"
    nested = root / "handoffs" / "nested"
    nested.mkdir(parents=True)
    assert handoff_module.resolve_contained_output_dir(str(root), str(root)) == str(root.resolve())
    assert handoff_module.resolve_contained_output_dir(str(nested), str(root)) == str(nested.resolve())


def test_contained_dir_accepts_not_yet_created_child(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    target = root / "fresh" / "dir"
    assert handoff_module.resolve_contained_output_dir(str(target), str(root)) == str(target.resolve())


@pytest.mark.parametrize(
    "segments",
    [
        ("..", "escape"),
        ("..", "..", "escape"),
        ("nested", "..", "..", "escape"),
    ],
)
def test_contained_dir_rejects_parent_traversal(tmp_path, segments):
    root = tmp_path / "data"
    root.mkdir()
    with pytest.raises(ValueError):
        handoff_module.resolve_contained_output_dir(str(root.joinpath(*segments)), str(root))


def test_contained_dir_rejects_sibling_with_shared_string_prefix(tmp_path):
    # "data-evil" starts with the string "data" but is not inside it.
    root = tmp_path / "data"
    root.mkdir()
    sibling = tmp_path / "data-evil"
    sibling.mkdir()
    with pytest.raises(ValueError):
        handoff_module.resolve_contained_output_dir(str(sibling), str(root))


def test_contained_dir_rejects_absolute_path_outside_root(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    with pytest.raises(ValueError):
        handoff_module.resolve_contained_output_dir(str(outside), str(root))


def test_contained_dir_rejects_filesystem_anchor(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    with pytest.raises(ValueError):
        handoff_module.resolve_contained_output_dir(root.anchor, str(root))


@pytest.mark.skipif(sys.platform != "win32", reason="drive-relative paths are Windows-only")
def test_contained_dir_rejects_drive_relative_path_escaping_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    # "C:escape" is relative to the current directory on drive C; with the cwd
    # set to tmp_path it resolves to tmp_path\escape, which is outside root.
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        handoff_module.resolve_contained_output_dir(root.drive + "escape", str(root))


@pytest.mark.skipif(sys.platform != "win32", reason="drive-relative paths are Windows-only")
def test_contained_dir_accepts_drive_relative_path_inside_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.chdir(root)
    resolved = handoff_module.resolve_contained_output_dir(root.drive + "inside", str(root))
    assert resolved == str((root / "inside").resolve())


def _make_dir_link(link: Path, target: Path) -> None:
    """Create a directory symlink; on Windows fall back to a junction, which needs no privilege."""
    import subprocess

    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            return
    pytest.skip("directory links are unavailable on this platform")


def test_contained_dir_rejects_symlink_escaping_root(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    _make_dir_link(link, outside)
    with pytest.raises(ValueError):
        handoff_module.resolve_contained_output_dir(str(link), str(root))
    with pytest.raises(ValueError):
        handoff_module.resolve_contained_output_dir(str(link / "child"), str(root))


def test_contained_dir_accepts_symlink_that_stays_inside_root(tmp_path):
    root = tmp_path / "data"
    real = root / "real"
    real.mkdir(parents=True)
    alias = root / "alias"
    _make_dir_link(alias, real)
    assert handoff_module.resolve_contained_output_dir(str(alias), str(root)) == str(real.resolve())


@pytest.mark.parametrize("bad", [None, 123, ["x"], {"a": 1}, "bad\x00name"])
def test_contained_dir_rejects_non_path_values(tmp_path, bad):
    with pytest.raises(ValueError):
        handoff_module.resolve_contained_output_dir(bad, str(tmp_path))


# ---------------------------------------------------------------------------
# resolve_output_dir_override: regenerate's shape-only pre-write check
# ---------------------------------------------------------------------------


def test_output_dir_override_defaults_to_data_dir_when_absent(tmp_path):
    default = str(tmp_path / "data")
    assert handoff_module.resolve_output_dir_override(None, default) == default
    assert handoff_module.resolve_output_dir_override("", default) == default


def test_output_dir_override_returns_value_outside_data_dir_unchanged(tmp_path):
    # Pins the pre-existing behaviour. Containment of output_dir is an open
    # decision, so an outside path is accepted rather than refused.
    outside = str(tmp_path / "elsewhere")
    assert handoff_module.resolve_output_dir_override(outside, str(tmp_path / "data")) == outside


@pytest.mark.parametrize("bad", [123, ["x"], {"a": 1}, "bad\x00name"])
def test_output_dir_override_refuses_values_that_cannot_name_a_directory(tmp_path, bad):
    with pytest.raises(ValueError):
        handoff_module.resolve_output_dir_override(bad, str(tmp_path))


# ---------------------------------------------------------------------------
# Promotion targets joined onto the handoff output dir
# ---------------------------------------------------------------------------


def _stub_promotion_item(monkeypatch, uri):
    monkeypatch.setattr(
        artifact_declaration_module, "effective_promotion",
        lambda _item: {"base_sha256": "0" * 64},
    )
    monkeypatch.setattr(
        artifact_declaration_module, "effective_planned_output",
        lambda _item: {"targets": [{"uri": uri}]},
    )


def _spy_preconditions(monkeypatch):
    seen: list[str] = []

    def _fake_check(_item, target_path, **_kwargs):
        seen.append(str(target_path))
        return {"ok": True, "reason": "stub"}

    monkeypatch.setattr(artifact_declaration_module, "check_promotion_preconditions", _fake_check)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("escaping", ["../escape.docx", "ABSOLUTE"])
async def test_promotion_readiness_records_target_escaping_output_dir_as_unresolved(tmp_path, monkeypatch, escaping):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    outside = tmp_path / "escape.docx"
    outside.write_bytes(b"outside")
    uri = str(outside) if escaping == "ABSOLUTE" else escaping
    _stub_promotion_item(monkeypatch, uri)
    seen = _spy_preconditions(monkeypatch)

    result = await handoff_module.build_promotion_readiness_for_handoff(
        None, "proj", [{"id": "item-1"}], output_dir=str(out_dir),
    )

    assert seen == [], "a target outside output_dir must never be hashed or checked"
    assert result == {
        "checked": [{
            "item_id": "item-1",
            "target_docx_path": os.path.join(str(out_dir), uri),
            "ok": False,
            "reason": "target_outside_output_dir",
        }],
        "unresolved_count": 1,
    }


@pytest.mark.asyncio
async def test_promotion_readiness_still_checks_target_inside_output_dir(tmp_path, monkeypatch):
    out_dir = tmp_path / "out"
    (out_dir / "docs").mkdir(parents=True)
    _stub_promotion_item(monkeypatch, "docs/report.docx")
    seen = _spy_preconditions(monkeypatch)

    result = await handoff_module.build_promotion_readiness_for_handoff(
        None, "proj", [{"id": "item-1"}], output_dir=str(out_dir),
    )

    assert seen == [os.path.join(str(out_dir), "docs/report.docx")]
    assert result["checked"][0]["item_id"] == "item-1"
    assert result["unresolved_count"] == 0


# ---------------------------------------------------------------------------
# Regenerate output_dir override: MCP dispatch and HTTP route
# ---------------------------------------------------------------------------


async def _source_handoff_id(db, data_dir: Path) -> tuple[str, str]:
    p = await db_module.create_project(db, "codeql-regen-containment")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    await handoff_module.generate_handoff(
        db, p["id"], str(data_dir), skip_ai_summary=True, mode="full",
    )
    rows = await db_module.get_handoffs(db, p["id"], limit=1)
    return p["id"], rows[0]["id"]


async def _count_rows(db, sql: str, project_id: str) -> int:
    async with db.execute(sql, (project_id,)) as cur:
        row = await cur.fetchone()
    return row["n"]


@pytest.mark.asyncio
async def test_mcp_regenerate_accepts_output_dir_outside_data_dir_pending_decision(db, tmp_path):
    # Pins the pre-existing behaviour. When the containment root is decided this
    # test should flip to a refusal.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    escape = tmp_path / "escape"
    project_id, source_id = await _source_handoff_id(db, data_dir)

    result = await mcp_handler._handle_task_tools(
        "record_handoff_correction",
        {
            "project_id": project_id,
            "source_handoff_id": source_id,
            "blocker_classification": "pointer_unresolved",
            "regenerate": True,
            "output_dir": str(escape),
        },
        db,
        str(data_dir),
        None,
        None,
    )

    assert "error" not in result, result
    assert result["regenerated"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [123, ["x"], "bad\x00name"])
async def test_mcp_regenerate_refuses_non_path_output_dir_before_any_write(db, tmp_path, bad):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    project_id, source_id = await _source_handoff_id(db, data_dir)
    corrections_sql = "SELECT COUNT(*) AS n FROM handoff_corrections WHERE project_id = ?"
    events_sql = "SELECT COUNT(*) AS n FROM ai_log_events WHERE project_id = ?"
    corrections_before = await _count_rows(db, corrections_sql, project_id)
    events_before = await _count_rows(db, events_sql, project_id)

    result = await mcp_handler._handle_task_tools(
        "record_handoff_correction",
        {
            "project_id": project_id,
            "source_handoff_id": source_id,
            "blocker_classification": "pointer_unresolved",
            "regenerate": True,
            "output_dir": bad,
        },
        db,
        str(data_dir),
        None,
        None,
    )

    assert result["regenerated"] is False
    assert result["error"] == "HANDOFF_OUTPUT_DIR_INVALID"
    assert "correction" not in result
    assert await _count_rows(db, corrections_sql, project_id) == corrections_before, (
        "a refused regenerate must not record a correction row"
    )
    assert await _count_rows(db, events_sql, project_id) == events_before, (
        "a refused regenerate must not write an audit event"
    )


@pytest.mark.asyncio
async def test_mcp_regenerate_accepts_output_dir_inside_data_dir(db, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    nested = data_dir / "nested"
    project_id, source_id = await _source_handoff_id(db, data_dir)

    result = await mcp_handler._handle_task_tools(
        "record_handoff_correction",
        {
            "project_id": project_id,
            "source_handoff_id": source_id,
            "blocker_classification": "pointer_unresolved",
            "regenerate": True,
            "output_dir": str(nested),
        },
        db,
        str(data_dir),
        None,
        None,
    )

    assert "error" not in result, result
    assert result["regenerated"] is True
    assert result["new_handoff_id"]


def _install_correction_fakes(monkeypatch):
    regen_calls: list[tuple] = []
    record_calls: list[tuple] = []
    log_calls: list[tuple] = []

    async def _fake_record(*args, **_kwargs):
        record_calls.append(args)
        return {
            "id": "corr-1",
            "source_handoff_id": "src-1",
            "session_id": None,
            "blocker_classification": "pointer_unresolved",
            "status": "draft",
            "version": 1,
        }

    async def _fake_regenerate(*args, **_kwargs):
        regen_calls.append(args)
        return {"new_handoff_id": "new-1", "amended": False, "invalidated_source": None}

    async def _fake_log(*args, **_kwargs):
        log_calls.append(args)
        return None

    monkeypatch.setattr(handoff_module, "record_handoff_correction", _fake_record)
    monkeypatch.setattr(handoff_module, "regenerate_handoff_correction", _fake_regenerate)
    monkeypatch.setattr(routes_handoff, "_log_correction_event", _fake_log)
    return regen_calls, record_calls, log_calls


@pytest.mark.parametrize("bad", [123, ["x"], "bad\u0000name"])
def test_http_regenerate_refuses_non_path_output_dir_before_any_write(client, monkeypatch, tmp_path, bad):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(client.app.state, "data_dir", str(data_dir))
    regen_calls, record_calls, log_calls = _install_correction_fakes(monkeypatch)
    project = client.post("/projects", json={"name": "codeql-http-regen-bad"}).json()

    r = client.post(
        f"/projects/{project['id']}/handoff/corrections",
        json={
            "source_handoff_id": "src-1",
            "blocker_classification": "pointer_unresolved",
            "regenerate": True,
            "output_dir": bad,
        },
    )

    assert r.status_code == 400, r.text
    assert record_calls == [], "a refused regenerate must not record a correction"
    assert log_calls == [], "a refused regenerate must not write an audit event"
    assert regen_calls == [], "regenerate must not run for an invalid output_dir"


@pytest.mark.parametrize("escape_kind", ["absolute", "traversal"])
def test_http_regenerate_accepts_output_dir_outside_data_dir_pending_decision(client, monkeypatch, tmp_path, escape_kind):
    # Pins the pre-existing behaviour until the containment root is decided.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(client.app.state, "data_dir", str(data_dir))
    regen_calls, _, _ = _install_correction_fakes(monkeypatch)
    project = client.post("/projects", json={"name": "codeql-http-regen-outside"}).json()
    if escape_kind == "absolute":
        output_dir = str(tmp_path / "escape")
    else:
        output_dir = str(data_dir / ".." / "escape")

    r = client.post(
        f"/projects/{project['id']}/handoff/corrections",
        json={
            "source_handoff_id": "src-1",
            "blocker_classification": "pointer_unresolved",
            "regenerate": True,
            "output_dir": output_dir,
        },
    )

    assert r.status_code == 200, r.text
    assert len(regen_calls) == 1
    assert regen_calls[0][3] == output_dir


def test_http_regenerate_accepts_output_dir_inside_data_dir(client, monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(client.app.state, "data_dir", str(data_dir))
    regen_calls, _, _ = _install_correction_fakes(monkeypatch)
    project = client.post("/projects", json={"name": "codeql-http-regen-ok"}).json()

    r = client.post(
        f"/projects/{project['id']}/handoff/corrections",
        json={
            "source_handoff_id": "src-1",
            "blocker_classification": "pointer_unresolved",
            "regenerate": True,
            "output_dir": str(data_dir / "nested"),
        },
    )

    assert r.status_code == 200, r.text
    assert len(regen_calls) == 1
    assert regen_calls[0][3] == str(data_dir / "nested")


# ---------------------------------------------------------------------------
# Codebase-map temp file: project_id must not shape the path
# ---------------------------------------------------------------------------


def test_codebase_map_temp_file_ignores_project_id_separators(client, monkeypatch, tmp_path):
    from meridian import codebase_map as cbmap_mod

    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_dir))
    rendered: list[Path] = []

    def _fake_render(_graph, out_path, **_kwargs):
        rendered.append(Path(out_path))
        Path(out_path).write_bytes(b"\x89PNG fake")
        return out_path

    monkeypatch.setattr(cbmap_mod, "render_map", _fake_render)

    r = client.post(
        "/projects/..\\..\\escape-target/codebase-map",
        json={"packages": [{"id": "a"}], "edges": []},
    )

    assert r.status_code == 200, r.text
    assert len(rendered) == 1
    out_path = rendered[0]
    assert out_path.parent == tmp_dir
    assert "\\" not in out_path.name
    assert ".." not in out_path.name
    assert out_path.name.startswith("meridian_codebase_map_")
    assert not out_path.exists(), "the temp PNG must be removed after the response is built"
