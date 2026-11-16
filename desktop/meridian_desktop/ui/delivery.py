"""The one object that pool jobs deliver their results to. Qt code, so it sits in ui/.

A job runs on a pool worker, so it must never hold a window or a panel. A job that holds the last reference to a
window can free it on the worker thread, and a widget freed off the GUI thread crashes Qt. A job that emits to its
window's or panel's own signal can also race with that object being deleted on the GUI thread. So a job emits a
Delivered to shared_delivery() instead. That object is created on the GUI thread, has no parent, and is never
deleted, so an emit always has a live target.

Each window or panel connects its handler to ``delivered`` on the GUI thread. The handler keeps only a Delivered
whose owner is its own token, so a result that belongs to another window is never shown. Each handler also checks
its own generation, so a result from a read that a newer read has superseded is dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, Signal


class Delivery(QObject):
    """Carries Delivered objects from pool workers to the GUI thread. It has no parent and is never deleted."""

    delivered = Signal(object)


@dataclass(frozen=True)
class Delivered:
    """One finished job: the owner token of the window or panel that started it, and what the job produced."""

    owner: object
    value: Any


_SHARED: Delivery | None = None


def shared_delivery() -> Delivery:
    """The shared delivery object. The first call creates it on the GUI thread, and it lives until the process ends."""
    global _SHARED
    if _SHARED is None:
        _SHARED = Delivery()
    return _SHARED


__all__ = ["Delivered", "Delivery", "shared_delivery"]
