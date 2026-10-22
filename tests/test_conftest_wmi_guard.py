"""Regression test for the Windows-only ``platform._wmi_query`` guard added
to ``tests/conftest.py`` (test-runner reliability investigation, 2026-09-27).

See ``tests/conftest.py``'s own comment for the full rationale: xdist worker
startup (``xdist/remote.py::getinfodict``) calls ``platform.platform()``
unconditionally at every worker's ``pytest_sessionstart`` -- no xdist config
flag can skip it. On Windows this tries a real WMI/COM query first, which has
crashed worker processes outright with a non-catchable
``Windows fatal exception: code 0x8007000e`` under host memory pressure --
NOT a regular ``OSError`` the existing fallback code could catch.
``tests/conftest.py`` disables the WMI call for the whole test process by
forcing ``platform._wmi_query`` to raise the same ``OSError`` CPython's own
``platform.py`` already handles gracefully everywhere it is called. This
test proves the guard is actually wired (not just present in source) and
that the documented non-WMI fallback paths still behave correctly with it
in place.
"""
from __future__ import annotations

import platform
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only WMI guard")


def test_wmi_query_disabled_by_conftest():
    """conftest.py must have replaced platform._wmi_query with a fast-failing stub."""
    with pytest.raises(OSError):
        platform._wmi_query("OS", "Version")


def test_win32_ver_still_works_without_wmi():
    """win32_ver()'s registry/sys.getwindowsversion() fallback must still work."""
    release, version, csd, ptype = platform.win32_ver()
    assert isinstance(release, str)
    assert isinstance(version, str)


def test_platform_platform_still_returns_nonempty_string():
    """The exact function xdist's getinfodict() calls at worker startup."""
    assert platform.platform()


def test_processor_does_not_raise():
    """processor() also tries _wmi_query first; must still degrade cleanly."""
    result = platform.processor()
    assert isinstance(result, str)
