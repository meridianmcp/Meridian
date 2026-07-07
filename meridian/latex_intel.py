"""Compatibility shim (d45c2cc8).

The implementation moved OUT of the Meridian package into the detachable,
standalone-installable ``packages/docparse`` sub-package. This shim re-exports the
full ``docparse.latex_intel`` namespace so existing ``meridian.latex_intel`` /
``from ..latex_intel import X`` importers (and tests reaching for internal helpers)
keep working unchanged. New code should import from ``docparse.latex_intel`` directly.
"""
from __future__ import annotations

from docparse import latex_intel as _impl

# Re-export EVERYTHING (public + private helpers, not dunders) so no caller breaks.
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})

del _impl
