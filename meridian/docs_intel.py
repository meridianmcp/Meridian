"""Compatibility shim (d45c2cc8).

The implementation moved OUT of the Meridian package into the detachable,
standalone-installable ``packages/docparse`` sub-package. This shim re-exports the
full ``docparse.docs_intel`` namespace so existing ``meridian.docs_intel`` /
``from ..docs_intel import X`` importers (and tests reaching for internal helpers)
keep working unchanged. New code should import from ``docparse.docs_intel`` directly.
"""
from __future__ import annotations

from docparse import docs_intel as _impl

# Re-export EVERYTHING (public + private helpers, not dunders) so no caller breaks.
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})

del _impl
