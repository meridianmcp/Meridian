"""Test package for tools/meridian_fallbacks.

Present (rather than relying on pytest's rootdir-relative discovery alone)
so ``from .conftest import ...`` relative imports inside these test modules
resolve correctly under pytest's default "prepend" import mode -- see
``tools/meridian_fallbacks/tests/conftest.py`` for the shared fixture
helpers this package's tests import from each other.
"""
