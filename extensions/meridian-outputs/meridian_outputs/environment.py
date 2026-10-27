"""meridian_outputs.environment -- the runtime a record was written under.

A provenance record already says which script produced an output, with what
parameters and which script content hash. To reproduce the output, a reader
also needs the interpreter and the library versions that ran it. This module
captures that once per process:

    {"schema_version": 1,
     "python_version": "3.12.13",
     "platform": "Windows-11-10.0.26200-SP0",
     "package_name": "meridian_outputs",
     "package_version": "0.1.0",
     "dependencies": {"duckdb": "1.5.4", ..., "pypdf": None}}

``annotate.record_provenance`` stores it on every record (``reproduction``)
and ``run_manifest.start_run_manifest`` builds its ``package_identity`` from
the same call, so the two records cannot drift.

Design choices:

* Dependency versions come from ``importlib.metadata`` (the installed
  distribution), not ``module.__version__``: several of these packages do not
  set ``__version__``, and reading metadata never imports the package.
* The default list is the eight direct runtime dependencies declared in
  ``pyproject.toml``. Transitive dependencies are not walked.
* A dependency that is not installed, or whose metadata cannot be read, is
  ``None``; capture never raises for one dependency.
* The result is cached per process (per dependency list). The libraries a
  process has imported do not change while it runs, so a later re-read could
  only report an on-disk upgrade the process is not actually using.
* No command line, environment variable, user name or absolute path is
  captured: those can hold secrets or machine-local paths, which do not
  belong in a shared ledger.

NO hosted call is made anywhere in this module -- fully local, matching the
rest of the ``meridian_outputs`` package.
"""
from __future__ import annotations

import copy
import importlib.metadata
import logging
import platform
import sys
import threading
from typing import Any, Iterable

_log = logging.getLogger(__name__)

__all__ = [
    "DIRECT_RUNTIME_DEPENDENCIES",
    "ENVIRONMENT_SCHEMA_VERSION",
    "capture_environment",
]

ENVIRONMENT_SCHEMA_VERSION = 1
PACKAGE_NAME = "meridian_outputs"

# The direct runtime dependencies declared in pyproject.toml [project]
# dependencies, by distribution name.
DIRECT_RUNTIME_DEPENDENCIES: tuple[str, ...] = (
    "duckdb",
    "tantivy",
    "xxhash",
    "mcp",
    "watchdog",
    "pyarrow",
    "psutil",
    "pypdf",
)

_cache: dict[tuple[str, ...], dict[str, Any]] = {}
_cache_lock = threading.Lock()


def _dependency_version(name: str) -> str | None:
    """Installed distribution version of ``name``, or ``None``. Never raises."""
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:  # noqa: BLE001 -- broken metadata must not break a record
        _log.debug("environment: could not read the version of %r", name, exc_info=True)
        return None


def _platform_string() -> str | None:
    try:
        return platform.platform()
    except Exception:  # noqa: BLE001
        _log.debug("environment: platform.platform() failed", exc_info=True)
        return None


def _package_version() -> str | None:
    try:
        from . import __version__  # noqa: PLC0415 -- avoid an import cycle at load
    except Exception:  # noqa: BLE001
        return None
    return str(__version__)


def capture_environment(dep_names: Iterable[str] | None = None) -> dict[str, Any]:
    """Snapshot of the interpreter, platform, this package's version and the
    installed versions of ``dep_names`` (default
    :data:`DIRECT_RUNTIME_DEPENDENCIES`).

    Cached per process for each distinct dependency list; every call returns
    a fresh deep copy, so a caller can never alter the cached snapshot.
    Never raises because of one dependency: a missing or unreadable one is
    ``None``.
    """
    names = tuple(DIRECT_RUNTIME_DEPENDENCIES if dep_names is None else dep_names)
    key = tuple(sorted(set(str(n) for n in names)))
    with _cache_lock:
        cached = _cache.get(key)
    if cached is None:
        cached = {
            "schema_version": ENVIRONMENT_SCHEMA_VERSION,
            "python_version": sys.version.split()[0],
            "platform": _platform_string(),
            "package_name": PACKAGE_NAME,
            "package_version": _package_version(),
            "dependencies": {name: _dependency_version(name) for name in key},
        }
        with _cache_lock:
            cached = _cache.setdefault(key, cached)
    return copy.deepcopy(cached)
