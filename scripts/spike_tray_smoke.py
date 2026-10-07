"""Throwaway spike (decision e3bf0981, item 6094a084): can a system-tray icon
process and a Tk window be started AND observed on a GitHub-hosted
windows-latest runner?

GitHub documents no interactive desktop for hosted runners, and
pywinauto / PIL.ImageGrab return empty or black in Session 0, so this records
objective facts instead of assuming: the session id, whether explorer.exe and the
taskbar exist, whether a full-screen screenshot is non-black, whether a pystray
icon with a distinctive tooltip appears in the notification area (visible area
or overflow only), and whether a Tk window is enumerable and visible in a
screenshot.

Never raises: every probe is wrapped and reports its own exception text, and the
exit code is always 0 so the CI job stays green and the evidence is reported.
Standard library plus pystray, Pillow and pywinauto (installed by the workflow).
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

TRAY_TOOLTIP = "MERIDIAN-SPIKE-TRAY"
TK_TITLE = "MERIDIAN-SPIKE-TK"

report: dict = {"facts": {}, "screenshots": {}, "tray": {}, "tk": {}, "errors": {}}


def probe(name: str):
    """Decorator-free helper: run fn, store its result or its exception text."""
    def run(fn):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - the whole point is to record, not raise
            report["errors"][name] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"
            return None
    return run


def run_cmd(args: list[str], timeout: int = 20) -> str:
    try:
        cp = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return (cp.stdout or "") + (cp.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return f"<failed: {type(exc).__name__}: {exc}>"


def gather_facts() -> None:
    f = report["facts"]
    f["python"] = sys.version.split()[0]
    f["platform"] = platform.platform()
    f["windows_version"] = platform.version()
    f["user"] = os.environ.get("USERNAME", "?")
    f["computername"] = os.environ.get("COMPUTERNAME", "?")
    f["session_name_env"] = os.environ.get("SESSIONNAME", "<not set>")

    @probe("session_ids")
    def _sessions():
        k32 = ctypes.windll.kernel32
        sid = ctypes.c_ulong(0)
        k32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
        f["process_session_id"] = int(sid.value)
        f["active_console_session_id"] = int(k32.WTSGetActiveConsoleSessionId())
        f["process_is_in_session_0"] = int(sid.value) == 0

    f["query_session"] = run_cmd(["query", "session"]).strip()[:600]
    f["qwinsta"] = run_cmd(["qwinsta"]).strip()[:600]
    f["explorer_tasklist"] = run_cmd(["tasklist", "/FI", "IMAGENAME eq explorer.exe"]).strip()[:400]
    f["explorer_running"] = "explorer.exe" in f["explorer_tasklist"].lower()

    @probe("screen_and_taskbar")
    def _screen():
        u32 = ctypes.windll.user32
        f["screen_size"] = [int(u32.GetSystemMetrics(0)), int(u32.GetSystemMetrics(1))]
        f["shell_traywnd_handle"] = int(u32.FindWindowW("Shell_TrayWnd", None))
        f["taskbar_found"] = f["shell_traywnd_handle"] != 0
        hwnd = u32.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(256)
        u32.GetWindowTextW(hwnd, buf, 255)
        f["foreground_window"] = {"handle": int(hwnd), "title": buf.value}

    @probe("interactive_window_station")
    def _winsta():
        u32 = ctypes.windll.user32
        hws = u32.GetProcessWindowStation()
        buf = ctypes.create_unicode_buffer(256)
        needed = ctypes.c_ulong(0)
        # UOI_NAME = 2
        u32.GetUserObjectInformationW(hws, 2, buf, ctypes.sizeof(buf), ctypes.byref(needed))
        f["window_station"] = buf.value  # WinSta0 means an interactive station


def screenshot(label: str, out: Path) -> None:
    @probe("screenshot_" + label)
    def _shot():
        from PIL import ImageGrab, ImageStat  # noqa: PLC0415

        img = ImageGrab.grab(all_screens=True)
        path = out / f"{label}.png"
        img.save(path)
        gray = img.convert("L")
        hist = gray.histogram()
        total = sum(hist) or 1
        non_black = sum(hist[9:]) / total  # pixels brighter than ~3 percent
        stat = ImageStat.Stat(gray)
        report["screenshots"][label] = {
            "file": path.name,
            "size": list(img.size),
            "non_black_fraction": round(non_black, 4),
            "mean_brightness": round(stat.mean[0], 2),
            "looks_black": non_black < 0.01,
        }


def list_toolbar_buttons(window_spec: dict, toolbar_chain: list[dict], backend: str) -> list[str]:
    """Return button texts of the first toolbar reachable through toolbar_chain."""
    from pywinauto import Desktop  # noqa: PLC0415

    win = Desktop(backend=backend).window(**window_spec)
    node = win
    for step in toolbar_chain:
        node = node.child_window(**step)
    tb = node.wrapper_object()
    out = []
    for i in range(tb.button_count()):
        try:
            out.append(tb.button(i).text())
        except Exception as exc:  # noqa: BLE001
            out.append(f"<button {i}: {type(exc).__name__}>")
    return out


def enumerate_tray() -> None:
    t = report["tray"]

    @probe("tray_win32_visible_area")
    def _visible():
        t["win32_notification_area_buttons"] = list_toolbar_buttons(
            {"class_name": "Shell_TrayWnd"},
            [{"class_name": "TrayNotifyWnd"}, {"class_name": "ToolbarWindow32"}],
            "win32",
        )

    @probe("tray_win32_overflow_area")
    def _overflow():
        t["win32_overflow_buttons"] = list_toolbar_buttons(
            {"class_name": "NotifyIconOverflowWindow"},
            [{"class_name": "ToolbarWindow32"}],
            "win32",
        )

    @probe("tray_uia_descendants")
    def _uia():
        from pywinauto import Desktop  # noqa: PLC0415

        win = Desktop(backend="uia").window(class_name="Shell_TrayWnd")
        names = []
        for d in win.descendants():
            try:
                n = d.window_text()
            except Exception:  # noqa: BLE001
                continue
            if n:
                names.append(n)
        t["uia_taskbar_descendant_names"] = names[:80]

    needle = TRAY_TOOLTIP.lower()
    vis = [b for b in t.get("win32_notification_area_buttons", []) if needle in str(b).lower()]
    ovf = [b for b in t.get("win32_overflow_buttons", []) if needle in str(b).lower()]
    uia = [b for b in t.get("uia_taskbar_descendant_names", []) if needle in str(b).lower()]
    t["found_in_visible_notification_area"] = bool(vis)
    t["found_in_overflow_only"] = bool(ovf) and not bool(vis)
    t["found_via_uia"] = bool(uia)


def registry_notify_icons() -> None:
    """Windows 11 / Server 2025 records every tray icon the shell has seen under
    HKCU\\Control Panel\\NotifyIconSettings. This proves the shell received the icon
    even when the UI cannot show where it landed."""
    t = report["tray"]

    @probe("registry_notify_icon_settings")
    def _reg():
        import winreg  # noqa: PLC0415

        rows = []
        base = r"Control Panel\NotifyIconSettings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as k:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                row = {"key": sub}
                try:
                    with winreg.OpenKey(k, sub) as sk:
                        for name in ("ExecutablePath", "InitialTooltip", "IsPromoted", "UID"):
                            try:
                                row[name] = winreg.QueryValueEx(sk, name)[0]
                            except OSError:
                                pass
                except OSError as exc:
                    row["error"] = str(exc)
                rows.append(row)
        t["registry_notify_icon_rows"] = rows
        needle = TRAY_TOOLTIP.lower()
        t["registry_has_spike_icon"] = any(
            needle in str(r.get("InitialTooltip", "")).lower()
            or "python" in str(r.get("ExecutablePath", "")).lower()
            for r in rows
        )
        t["registry_spike_rows"] = [
            r for r in rows
            if needle in str(r.get("InitialTooltip", "")).lower()
            or "python" in str(r.get("ExecutablePath", "")).lower()
        ]


def open_overflow_and_list(out: Path) -> None:
    """Click 'Show Hidden Icons' through UIA, screenshot the flyout and list it."""
    t = report["tray"]

    @probe("open_hidden_icons_flyout")
    def _open():
        from pywinauto import Desktop  # noqa: PLC0415

        taskbar = Desktop(backend="uia").window(class_name="Shell_TrayWnd")
        btn = taskbar.child_window(title="Show Hidden Icons", control_type="Button")
        btn.click_input()
        time.sleep(2)
        screenshot("3_hidden_icons_flyout_open", out)
        names: list[str] = []
        for cls in ("TopLevelWindowForOverflowXamlIsland", "NotifyIconOverflowWindow"):
            try:
                fly = Desktop(backend="uia").window(class_name=cls)
                for d in fly.descendants():
                    try:
                        n = d.window_text()
                    except Exception:  # noqa: BLE001
                        continue
                    if n:
                        names.append(f"{cls}: {n}")
            except Exception as exc:  # noqa: BLE001
                names.append(f"<{cls}: {type(exc).__name__}>")
        t["flyout_descendant_names"] = names[:120]
        needle = TRAY_TOOLTIP.lower()
        t["found_in_hidden_icons_flyout"] = any(needle in n.lower() for n in names)
        try:
            from pywinauto.keyboard import send_keys  # noqa: PLC0415

            send_keys("{ESC}")
        except Exception:  # noqa: BLE001
            pass


def window_titles() -> list[str]:
    titles: list[str] = []

    @probe("enum_windows")
    def _enum():
        u32 = ctypes.windll.user32
        proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def cb(hwnd, _lparam):
            if u32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(256)
                u32.GetWindowTextW(hwnd, buf, 255)
                if buf.value:
                    titles.append(buf.value)
            return True

        u32.EnumWindows(proto(cb), 0)

    return titles


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="spike_out")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    gather_facts()
    screenshot("0_before_anything", out)

    icon = None

    @probe("pystray_start")
    def _start_tray():
        nonlocal icon
        import pystray  # noqa: PLC0415
        from PIL import Image, ImageDraw  # noqa: PLC0415

        img = Image.new("RGB", (64, 64), (200, 30, 30))
        ImageDraw.Draw(img).ellipse((8, 8, 56, 56), fill=(250, 250, 250))
        icon = pystray.Icon("meridian_spike", img, TRAY_TOOLTIP,
                            menu=pystray.Menu(pystray.MenuItem("Quit", lambda: None)))
        icon.run_detached()
        report["tray"]["pystray_started_without_exception"] = True

    time.sleep(5)
    screenshot("1_after_tray_icon", out)
    enumerate_tray()
    registry_notify_icons()
    open_overflow_and_list(out)

    @probe("tk_start")
    def _tk():
        import tkinter as tk  # noqa: PLC0415

        root = tk.Tk()
        root.title(TK_TITLE)
        root.geometry("360x120+60+60")
        tk.Label(root, text=TK_TITLE, font=("Segoe UI", 16)).pack(expand=True)
        root.attributes("-topmost", True)
        deadline = time.time() + 3
        while time.time() < deadline:
            root.update()
            time.sleep(0.05)
        report["tk"]["window_started_without_exception"] = True
        report["tk"]["enum_windows_contains_title"] = TK_TITLE in window_titles()
        screenshot("2_with_tk_window", out)
        root.destroy()

    @probe("pystray_stop")
    def _stop():
        if icon is not None:
            icon.stop()

    report["tk"]["visible_window_titles_sample"] = window_titles()[:40]
    report["verdict_inputs"] = {
        "in_session_0": report["facts"].get("process_is_in_session_0"),
        "window_station": report["facts"].get("window_station"),
        "explorer_running": report["facts"].get("explorer_running"),
        "taskbar_found": report["facts"].get("taskbar_found"),
        "any_screenshot_not_black": any(
            not s.get("looks_black", True) for s in report["screenshots"].values()
        ),
        "tray_visible_area": report["tray"].get("found_in_visible_notification_area"),
        "tray_overflow_only": report["tray"].get("found_in_overflow_only"),
        "tray_via_uia": report["tray"].get("found_via_uia"),
        "tray_registered_in_shell_registry": report["tray"].get("registry_has_spike_icon"),
        "tray_in_hidden_icons_flyout": report["tray"].get("found_in_hidden_icons_flyout"),
        "tk_enumerated": report["tk"].get("enum_windows_contains_title"),
    }
    (out / "spike_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except Exception as exc:  # noqa: BLE001 - never fail the job, report instead
        print("SPIKE CRASHED:", type(exc).__name__, exc)
        traceback.print_exc()
        rc = 0
    os._exit(rc)  # pystray's detached thread must not keep the job alive
