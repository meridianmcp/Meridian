"""Test doubles. Nothing here touches the OS credential store or the network."""

from __future__ import annotations

import sys
import urllib.parse
from http.server import ThreadingHTTPServer

from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError


class QuietServer(ThreadingHTTPServer):
    """A loopback stub server that drops the traceback for a client that closed early.

    Size-cap and timeout tests close the connection on purpose, so the write fails.
    Every other error still reaches the default handler.
    """

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


class MemoryKeyring(KeyringBackend):
    """An in-memory keyring backend. It holds values in a dict and nothing else.

    conftest.py installs one for the whole session. Its priority is low, so keyring
    would never pick it on its own. It raises PasswordDeleteError for a missing entry,
    as the real backends do.
    """

    priority = 0.5

    def __init__(self) -> None:
        super().__init__()
        self._values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self._values:
            raise PasswordDeleteError("not found")
        del self._values[(service, username)]

    def snapshot(self) -> dict[tuple[str, str], str]:
        return dict(self._values)


class FailingKeyring:
    """A backend whose every call fails. Each error message contains a secret on purpose."""

    def __init__(self, secret: str) -> None:
        self._secret = secret

    def get_password(self, service: str, username: str) -> str | None:
        raise RuntimeError(f"read failed while holding {self._secret}")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise ValueError(f"write refused for value {password}")

    def delete_password(self, service: str, username: str) -> None:
        raise OSError(f"delete failed for {self._secret}")


class FakeUserRegistry:
    """A fake of the Windows user environment writer. It records each write and each broadcast, and writes nothing real.

    ``fail_write`` is raised by the write, and ``announced`` is what the broadcast reports.
    """

    def __init__(self, *, announced: bool = True, fail_write: BaseException | None = None) -> None:
        self.writes: list[tuple[str, str]] = []
        self.announcements = 0
        self._announced = announced
        self._fail_write = fail_write

    def write_user_value(self, name: str, value: str) -> None:
        if self._fail_write is not None:
            raise self._fail_write
        self.writes.append((name, value))

    def announce_change(self) -> bool:
        self.announcements += 1
        return self._announced


class ScriptedTransport:
    """A transport that answers by path from a queue per path. The last item of a queue repeats. An exception is raised."""

    def __init__(self, replies: dict[str, list]) -> None:
        self._queues = {path: list(items) for path, items in replies.items()}
        self.requests: list[dict] = []

    def __call__(self, request, timeout):
        path = urllib.parse.urlsplit(request.full_url).path
        self.requests.append(
            {
                "method": request.get_method(),
                "path": path,
                "headers": dict(request.header_items()),
                "body": request.data,
            }
        )
        queue = self._queues.get(path)
        if not queue:
            raise AssertionError(f"unscripted request to {path}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        return item

    def paths(self) -> list[str]:
        return [request["path"] for request in self.requests]
