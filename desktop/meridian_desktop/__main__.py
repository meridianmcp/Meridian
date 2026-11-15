"""Entry point: ``python -m meridian_desktop``.

``--smoke`` builds the window and tray, prints "smoke ok" and exits 0 without
starting the event loop. The Qt code lives in meridian_desktop/ui/. This module
re-exports the names that callers and tests use.
"""

from __future__ import annotations

import sys as _sys

# --install and --uninstall need no Qt, so they run before the UI modules are imported.
if __name__ == "__main__" and {"--install", "--uninstall"} & set(_sys.argv[1:]):
    from meridian_desktop.install import main as _install_main

    raise SystemExit(_install_main(_sys.argv[1:]))

from meridian_desktop.ui.app import main
from meridian_desktop.ui.main_window import TAB_NAMES, MainWindow

__all__ = ["MainWindow", "TAB_NAMES", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
