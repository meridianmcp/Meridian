"""PyInstaller entry point for the Meridian desktop executable.

When run as a frozen exe:
- DB lives in ~/.meridian/meridian.db (NOT next to the exe)
- Starts uvicorn on MERIDIAN_PORT (default 7700)
- Opens the browser after a short delay
- Blocks until killed
"""
from __future__ import annotations

import os
import sys
import time
import threading
import webbrowser
from pathlib import Path


def _set_frozen_defaults() -> None:
    """Override default DB/data paths so they land in ~/.meridian, not next to the exe."""
    if getattr(sys, "frozen", False):
        home_dir = Path.home() / ".meridian"
        home_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("MERIDIAN_DB", str(home_dir / "meridian.db"))
        os.environ.setdefault("MERIDIAN_DATA_DIR", str(home_dir / "data"))


def _open_browser(port: int, delay: float = 1.5) -> None:
    """Open the dashboard in the default browser after a short delay."""
    def _open():
        time.sleep(delay)
        webbrowser.open(f"http://localhost:{port}")
    t = threading.Thread(target=_open, daemon=True)
    t.start()


def main() -> None:
    """Start the Meridian server and open the dashboard."""
    _set_frozen_defaults()

    import uvicorn
    from meridian.server import app

    port = int(os.environ.get("MERIDIAN_PORT", 7700))
    _open_browser(port)
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
