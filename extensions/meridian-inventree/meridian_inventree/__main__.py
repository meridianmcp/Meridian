"""Run the MCP server over stdio: python -m meridian_inventree."""

from __future__ import annotations

import sys

from meridian_inventree.server import main

if __name__ == "__main__":
    sys.exit(main())
