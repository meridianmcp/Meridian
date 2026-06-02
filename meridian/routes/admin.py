"""Admin routes (shutdown, restart, snapshot, health, git-status) — extracted from server.py."""
from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .._deps import _db, _hosted_mode, _is_demo_request
from .. import db as db_module

router = APIRouter()


@router.get("/admin/health")
async def admin_health_json(request: Request) -> dict[str, Any]:
    """JSON health check for ops/curl — restricted to admin users."""
    from ..hosted import get_current_tenant, is_admin_db, check_admin_password  # noqa: PLC0415
    try:
        tenant = await get_current_tenant(request)
    except HTTPException:
        raise HTTPException(status_code=403, detail="not authenticated")
    if not await is_admin_db(tenant.get("email", ""), request.app.state.db):
        raise HTTPException(status_code=403, detail="admin only")
    if not check_admin_password(request):
        raise HTTPException(status_code=403, detail="admin password required")

    db = request.app.state.db

    async def _count(sql: str) -> int:
        async with db.execute(sql) as cur:
            row = await cur.fetchone()
        return (row[0] if row else 0) or 0

    tenants_total = await _count("SELECT COUNT(*) FROM tenants")
    tenants_pro = await _count("SELECT COUNT(*) FROM tenants WHERE plan='pro'")
    tasks_today = await _count(
        "SELECT COUNT(*) FROM task_log WHERE created_at >= date('now')"
    )
    sessions_active = await _count(
        "SELECT COUNT(*) FROM sessions WHERE status='in_progress'"
    )
    sprint_pending = await _count(
        "SELECT COUNT(*) FROM sprint_items WHERE status='pending'"
    )
    try:
        from meridian.server import _REPO_ROOT  # noqa: PLC0415
        version_path = _REPO_ROOT / "pyproject.toml"
        ver_text = version_path.read_text(encoding="utf-8")
        import re as _re
        ver_m = _re.search(r'version\s*=\s*"([^"]+)"', ver_text)
        version = ver_m.group(1) if ver_m else "unknown"
    except Exception:
        version = "unknown"
    return {
        "version": version,
        "tenants_total": tenants_total,
        "tenants_pro": tenants_pro,
        "sessions_active": sessions_active,
        "tasks_today": tasks_today,
        "sprint_pending": sprint_pending,
        "hosted_mode": _hosted_mode(),
    }


@router.get("/admin/git-status")
async def git_status() -> dict[str, Any]:
    """Check if local repo is behind/ahead of remote."""
    import subprocess as sp
    try:
        cwd = str(Path(__file__).parent.parent.parent)
        sp.run(["git", "fetch", "origin"], cwd=cwd, capture_output=True, timeout=10)
        result = sp.run(
            ["git", "rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
            cwd=cwd, capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split()
            ahead = int(parts[0]) if parts else 0
            behind = int(parts[1]) if len(parts) > 1 else 0
        else:
            ahead, behind = 0, 0
        branch = sp.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd, capture_output=True, text=True
        ).stdout.strip()
        local_hash = sp.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd, capture_output=True, text=True
        ).stdout.strip()
        remote_hash = sp.run(
            ["git", "rev-parse", "--short", "@{upstream}"],
            cwd=cwd, capture_output=True, text=True
        ).stdout.strip()
        return {
            "ok": True, "branch": branch,
            "ahead": ahead, "behind": behind,
            "local_hash": local_hash, "remote_hash": remote_hash,
            "up_to_date": behind == 0,
            "warning": f"{behind} commit(s) behind origin/{branch}" if behind > 0 else None,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "behind": 0, "ahead": 0}


@router.post("/admin/shutdown")
async def admin_shutdown(request: Request) -> Response:
    """Gracefully stop the server process."""
    if _is_demo_request(request):
        return JSONResponse(
            {"detail": "Not available in demo mode. Sign up at usemeridian.us"},
            status_code=403,
        )

    async def _delayed_shutdown() -> None:
        await asyncio.sleep(0.5)
        os.kill(os.getpid(), signal.SIGINT)

    asyncio.create_task(_delayed_shutdown())
    return JSONResponse({"ok": True})


@router.post("/admin/restart")
async def admin_restart(request: Request) -> Response:
    """Restart the server by spawning a new process then shutting down."""
    if _is_demo_request(request):
        return JSONResponse(
            {"detail": "Not available in demo mode. Sign up at usemeridian.us"},
            status_code=403,
        )
    import subprocess
    import sys

    async def _delayed_restart() -> None:
        await asyncio.sleep(0.5)
        cwd = str(Path(__file__).parent.parent.parent)
        python_exe = sys.executable
        spawn_script = (
            f"import time, subprocess, os; time.sleep(2); "
            f"env = {{k: v for k, v in os.environ.items()}}; "
            f"env['CONDA_SHLVL'] = '1'; "
            f"[env.pop(k, None) for k in list(env) if k.startswith('CONDA_ENV_SHLVL_')]; "
            f"subprocess.Popen([{python_exe!r}, '-m', 'meridian'], cwd={cwd!r}, env=env)"
        )
        subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", spawn_script],
            cwd=cwd,
        )
        await asyncio.sleep(0.5)
        os.kill(os.getpid(), signal.SIGINT)

    asyncio.create_task(_delayed_restart())
    return JSONResponse({"ok": True})


@router.get("/admin/snapshot")
async def download_snapshot(request: Request) -> Response:
    """Download the current DB as a SQLite snapshot file."""
    headers = {"Content-Disposition": "attachment; filename=meridian-snapshot.db"}
    db = await _db(request)
    db_url = os.environ.get("MERIDIAN_DB_URL")

    if not db_url:
        db_path = os.environ.get("MERIDIAN_DB", str(Path("data") / "meridian.db"))
        if db_path == ":memory:":
            raise HTTPException(400, "Cannot snapshot in-memory database")
        try:
            data = Path(db_path).read_bytes()
        except OSError as exc:
            raise HTTPException(500, f"Could not read DB file: {exc}") from exc
        return Response(content=data, media_type="application/x-sqlite3", headers=headers)

    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        async with aiosqlite.connect(tmp.name) as sdb:
            await db_module.init_db(tmp.name)
            tables = [
                "projects", "goal_states", "sessions", "sessions_archived",
                "task_log", "sprint_items", "chat_sessions", "chat_messages",
            ]
            for table in tables:
                try:
                    rows = await db.execute_fetchall(f"SELECT * FROM {table}")  # type: ignore[attr-defined]
                    if rows:
                        cols = list(rows[0].keys())
                        placeholders = ",".join("?" * len(cols))
                        sql = (
                            f"INSERT OR IGNORE INTO {table} "
                            f"({','.join(cols)}) VALUES ({placeholders})"
                        )
                        await sdb.executemany(sql, [list(r.values()) for r in rows])
                except Exception:  # noqa: BLE001
                    pass
            await sdb.commit()
        data = Path(tmp.name).read_bytes()
    finally:
        try:
            Path(tmp.name).unlink()
        except OSError:
            pass
    return Response(content=data, media_type="application/x-sqlite3", headers=headers)
