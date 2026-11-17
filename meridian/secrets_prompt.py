"""Approval window for SENSITIVE secret use (secrets broker slice 2).

``SecretsBroker.run_with_secret`` asks an approver before it runs a SENSITIVE item.
This module supplies that approver as a tkinter window with two buttons, Approve
once and Deny. The decision logic (``resolve_choice``, ``dialog_lines``,
``printable``) is kept apart from the window, so it can be tested without a display.

Rules the window follows:

* Only a click on Approve once approves. Deny, closing the window, Escape, no answer
  before the timeout, and any failure to open or run the window all deny.
* The first answer wins. Later clicks change nothing.
* The window shows the item's name, purpose, group, command, argument list and the
  requesting client. It never shows the secret value: the request type has no value
  field, and the broker decrypts the value only after an approval.
* Shown text is escaped, so a stored purpose or an argument cannot add fake lines.

tkinter is imported inside ``ask_approval``, never at module import time, so the rest
of the broker works where tkinter is missing. Only a mocked tkinter is exercised by
the test suite. The real window has not been run on a Windows desktop in this slice.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from typing import Any

from .secrets_broker import ApprovalRequest

APPROVE_LABEL = "Approve once"
DENY_LABEL = "Deny"
WINDOW_TITLE = "Meridian: approve secret use"
DEFAULT_TIMEOUT_SECONDS = 120.0
CHOICE_APPROVE = "approve_once"
CHOICE_DENY = "deny"
_FIELD_LIMIT = 300
_ARGV_LIMIT = 600


def resolve_choice(choice: object) -> bool:
    """Decision logic: True only for an explicit Approve once. Anything else denies."""
    return isinstance(choice, str) and choice == CHOICE_APPROVE


def printable(text: object, limit: int = _FIELD_LIMIT) -> str:
    """Escape non-printable characters and cap the length. Every shown field passes here."""
    escaped = "".join(ch if ch.isprintable() else repr(ch)[1:-1] for ch in str(text))
    if len(escaped) > limit:
        return escaped[: limit - 3] + "..."
    return escaped


def dialog_lines(
    name: object,
    purpose: object,
    group: object,
    command: object,
    argv: Sequence[object],
    client: object,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[str]:
    """The text the window shows. Metadata only: no parameter here can carry a value."""
    arguments = printable(", ".join(repr(str(arg)) for arg in argv), _ARGV_LIMIT)
    seconds = int(round(timeout_seconds))
    return [
        "Meridian wants to run a command that uses a SENSITIVE secret.",
        f"Secret name: {printable(name)}",
        f"Purpose: {printable(purpose)}",
        f"Group: {printable(group)}",
        f"Command: {printable(command)}",
        f"Argument list: {arguments}",
        f"Requested by: {printable(client)}",
        "The secret value is not shown here. It reaches the command's environment "
        "only, and any output that contains it is redacted.",
        f"{APPROVE_LABEL} runs this command once. {DENY_LABEL}, closing this window, "
        f"or no answer within {seconds} seconds denies it.",
    ]


def _run_window(tk: Any, lines: list[str], timeout_seconds: float) -> str | None:
    """Build the window, run its loop, and return the first choice made (or None)."""
    answers: list[str] = []
    root = tk.Tk()
    try:

        def answer(choice: str) -> None:
            if not answers:
                answers.append(choice)
                root.quit()

        root.title(WINDOW_TITLE)
        root.resizable(False, False)
        root.attributes("-topmost", True)
        body = tk.Frame(root, padx=16, pady=14)
        body.pack(fill="both", expand=True)
        for line in lines:
            label = tk.Label(body, text=line, anchor="w", justify="left", wraplength=560)
            label.pack(fill="x", pady=2)
        row = tk.Frame(body)
        row.pack(fill="x", pady=(14, 0))
        deny = tk.Button(row, text=DENY_LABEL, width=14, command=lambda: answer(CHOICE_DENY))
        approve = tk.Button(row, text=APPROVE_LABEL, width=14, command=lambda: answer(CHOICE_APPROVE))
        deny.pack(side="right", padx=(8, 0))
        approve.pack(side="right")
        root.protocol("WM_DELETE_WINDOW", lambda: answer(CHOICE_DENY))
        root.bind("<Escape>", lambda _event: answer(CHOICE_DENY))
        root.bind("<Return>", lambda _event: answer(CHOICE_DENY))  # Enter denies, the safe choice
        root.after(max(1, int(timeout_seconds * 1000)), lambda: answer(CHOICE_DENY))
        deny.focus_set()
        root.mainloop()
    finally:
        root.destroy()
    return answers[0] if answers else None


def ask_approval(
    name: object,
    purpose: object,
    group: object,
    command: object,
    argv: Sequence[object],
    client: object,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    tkinter_module: Any | None = None,
) -> bool:
    """Show the approval window. Returns True only if the person clicks Approve once.

    ``tkinter_module`` lets tests pass a mock. When it is None, the real tkinter is
    imported here. Any failure, including no tkinter and no display, returns False.
    """
    lines = dialog_lines(
        name, purpose, group, command, argv, client, timeout_seconds=timeout_seconds
    )
    try:
        tk = tkinter_module if tkinter_module is not None else importlib.import_module("tkinter")
        choice = _run_window(tk, lines, timeout_seconds)
    except Exception:  # fail closed: the command does not run
        return False
    return resolve_choice(choice)


def approver_from_dialog(request: ApprovalRequest) -> bool:
    """Approver for ``SecretsBroker.run_with_secret``: asks the window about one request."""
    return ask_approval(
        request.name,
        request.purpose,
        request.group,
        request.command,
        request.argv,
        request.client,
    )
