"""Owner approval for one proposed InvenTree plan, from an interactive terminal.

Usage:
    python -m meridian_inventree.approve_cli --plans-dir DIR --audit-file FILE PLAN_ID

The command shows the plan and asks the owner to type its plan_hash exactly.
It needs stdin to be a terminal. Without one it prints the refusal message,
approves nothing, and exits with code 2. Approval is a human step only: the
MCP server must not expose or call the approval path in plans.PlanStore.

Before anything is printed, each plain plan field is checked: plan_id and
plan_hash are hex, status and operation names are identifiers, and created_at
is ISO 8601. A field that fails refuses the approval with unsafe_display, and
only the field's name is shown. The JSON values are printed with control
characters escaped.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Final, TextIO

from .audit import AuditLog
from .plans import OWNER, PROPOSED, PlanError, PlanStore

EXIT_APPROVED: Final[int] = 0
EXIT_REFUSED: Final[int] = 1
EXIT_NOT_INTERACTIVE: Final[int] = 2
NOT_INTERACTIVE_MESSAGE: Final[str] = "approval needs an interactive terminal"
UNSAFE_DISPLAY: Final[str] = "unsafe_display"
_WRAP_WIDTH: Final[int] = 100
_CONTINUATION: Final[str] = "        "  # continuation lines sit under the value, indented eight spaces
# Fields printed as plain text, not as JSON. Each must match its pattern in full, so a control
# character, a newline or an escape sequence can never reach the terminal through them.
_PLAIN_ID: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_]{1,64}")
_PLAN_ID: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}")
_PLAN_HASH: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_ISO_8601: Final[re.Pattern[str]] = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)


def _windows_console(fd: int) -> bool:
    """True only for a real console handle. GetConsoleMode fails for NUL and for pipes."""
    import ctypes
    import msvcrt

    try:
        handle = msvcrt.get_osfhandle(fd)
        mode = ctypes.c_uint32()
        return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        return False


def _is_interactive(stream: TextIO) -> bool:
    """True when stream is a terminal.

    On Windows the NUL device (what subprocess.DEVNULL and /dev/null map to) also
    reports isatty() as True, so a console check runs after isatty().
    """
    try:
        if not stream.isatty():
            return False
    except (AttributeError, ValueError, OSError):
        return False
    if sys.platform != "win32":
        return True
    try:
        fd = stream.fileno()
    except (AttributeError, ValueError, OSError):
        # No real file descriptor (an in-memory stream). isatty() is the only evidence there is.
        return True
    return _windows_console(fd)


def _compact(value: object) -> str:
    # json.dumps escapes control characters, so a plan cannot write escape codes to the terminal.
    return json.dumps(value, sort_keys=True, ensure_ascii=True)


def _field(label: str, value: object) -> list[str]:
    """One labelled value in full. Long text wraps at fixed widths. No character is dropped.

    The owner approves what is printed, so nothing is cut. Joining the pieces, with the
    continuation indent removed, gives back exactly the JSON text.
    """
    text = _compact(value)
    first = max(1, _WRAP_WIDTH - len(label))
    rest = max(1, _WRAP_WIDTH - len(_CONTINUATION))
    lines = [label + text[:first]]
    lines.extend(_CONTINUATION + text[start : start + rest] for start in range(first, len(text), rest))
    return lines


def _is_iso_8601(value: object) -> bool:
    if not isinstance(value, str) or _ISO_8601.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _unsafe_field(plan: dict[str, Any]) -> str | None:
    """Name the first printed plan field that is not a plain identifier or ISO 8601 time, or None.

    The plan hash covers operations and previews only, so created_at and status can be edited on disk
    without a hash mismatch. Each plain field is checked here before anything is printed. The JSON
    values (params, request, before, after, client_key) are printed escaped, so they need no check.
    """
    plain_checks: tuple[tuple[str, Any, re.Pattern[str]], ...] = (
        ("plan_id", plan.get("plan_id"), _PLAN_ID),
        ("status", plan.get("status"), _PLAIN_ID),
        ("plan_hash", plan.get("plan_hash"), _PLAN_HASH),
    )
    for name, value, pattern in plain_checks:
        if not isinstance(value, str) or pattern.fullmatch(value) is None:
            return name
    if not _is_iso_8601(plan.get("created_at")):
        return "created_at"
    for index, op in enumerate(plan.get("operations", []), start=1):
        name = op.get("op") if isinstance(op, dict) else None
        if not isinstance(name, str) or _PLAIN_ID.fullmatch(name) is None:
            return f"operation {index} name"
    return None


def _summary(plan: dict[str, Any]) -> str:
    lines = [
        f"Plan {plan['plan_id']}",
        f"  created:    {plan['created_at']}",
        f"  status:     {plan['status']}",
        f"  operations: {len(plan['operations'])}",
    ]
    for index, (op, preview) in enumerate(zip(plan["operations"], plan["previews"]), start=1):
        lines.extend(_field(f"  {index}. {op['op']} client_key=", op["client_key"]))
        lines.extend(_field("     params: ", op["params"]))
        # The request is what the run will send, as stored at proposal. The owner approves this.
        lines.extend(_field("     request: ", [preview.get("method"), preview.get("path"), preview.get("body")]))
        lines.extend(_field("     before: ", preview["before"]))
        lines.extend(_field("     after:  ", preview["after"]))
    lines.append(f"plan_hash: {plan['plan_hash']}")
    return "\n".join(lines)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m meridian_inventree.approve_cli",
        description="Approve one proposed InvenTree plan. Needs an interactive terminal.",
    )
    parser.add_argument("--plans-dir", required=True, help="folder that holds the plan files")
    parser.add_argument("--audit-file", required=True, help="append-only audit log to record the decision")
    parser.add_argument("plan_id", help="32-character plan id")
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    read_line: Callable[[str], str] | None = None,
) -> int:
    """Run the approval prompt. Returns the process exit code.

    The streams and read_line can be replaced in tests. By default they are the
    process streams and input().
    """
    source = sys.stdin if stdin is None else stdin
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    if not _is_interactive(source):
        print(NOT_INTERACTIVE_MESSAGE, file=err)
        return EXIT_NOT_INTERACTIVE

    args = _parse_args(argv)
    audit = AuditLog(args.audit_file)
    store = PlanStore(args.plans_dir, audit=audit)
    ask: Callable[[str], str] = input if read_line is None else read_line

    try:
        plan = store.get(args.plan_id)
    except PlanError as exc:
        print(f"refused: {exc.code}: {exc.message}", file=err)
        return EXIT_REFUSED

    # Checked before anything is printed. A field that fails is never shown, only named.
    unsafe = _unsafe_field(plan)
    if unsafe is not None:
        audit.append({"event": "plan_approval_refused", "plan_id": plan["plan_id"], "reason": UNSAFE_DISPLAY})
        print(
            f"refused: {UNSAFE_DISPLAY}: the plan's {unsafe} is not a plain identifier or ISO 8601 time, "
            "so the plan is not shown and nothing was approved",
            file=err,
        )
        return EXIT_REFUSED

    if plan["status"] != PROPOSED:
        audit.append({"event": "plan_approval_refused", "plan_id": plan["plan_id"], "reason": "not_proposed"})
        print(f"refused: plan is {plan['status']}; only a proposed plan can be approved", file=err)
        return EXIT_REFUSED

    print(_summary(plan), file=out)
    print("Type the plan_hash shown above, exactly, to approve. Anything else refuses.", file=out)
    try:
        typed = ask("plan_hash: ")
    except EOFError:
        typed = ""

    try:
        approved = store.approve(plan["plan_id"], typed, approved_by=OWNER)
    except PlanError as exc:
        audit.append({"event": "plan_approval_refused", "plan_id": plan["plan_id"], "reason": exc.code})
        print(f"refused: {exc.code}: {exc.message}", file=err)
        return EXIT_REFUSED

    audit.append(
        {
            "event": "plan_approved",
            "plan_id": approved["plan_id"],
            "plan_hash": approved["plan_hash"],
            "approved_by": OWNER,
            "operation_count": len(approved["operations"]),
        }
    )
    print(f"approved plan {approved['plan_id']}", file=out)
    return EXIT_APPROVED


if __name__ == "__main__":
    raise SystemExit(main())
