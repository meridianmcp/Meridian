"""Raise an exception with its exception chain cleared.

``raise ... from None`` clears ``__cause__`` but leaves the exception being
handled in ``__context__``. A ``raise`` inside an ``except`` block therefore
still reaches the original exception, with its arguments and any request it
carries (for example an ``Authorization`` header). Callers record the failure
inside the ``except`` block, leave the block, and then call
:func:`raise_unchained`.
"""
from __future__ import annotations

from typing import NoReturn


def raise_unchained(error: BaseException) -> NoReturn:
    """Raise ``error`` with ``__cause__`` and ``__context__`` set to ``None``.

    Call this only after leaving the ``except`` block that produced the
    failure. If it is called while another exception is being handled, Python
    sets ``__context__`` to that exception again.
    """
    error.__cause__ = None
    error.__context__ = None
    raise error
