"""Command line for the secrets broker (secrets broker slice 2).

``python -m meridian secrets init``
    Creates the vault. Asks for a new passphrase twice (getpass, no echo), checks it
    against the passphrase rules first, then prints the recovery code once. The code
    is not stored anywhere, so write it down.

``python -m meridian secrets list``
    Prints name, group, class and purpose for every item. Never a value. Needs no
    passphrase.

``python -m meridian secrets run NAME -- ARGV...``
    Asks for the passphrase, then runs ARGV with NAME's value in the child's
    environment. A SENSITIVE item first asks in the Approve once / Deny window.

Exit codes: 0 success; after a completed run, the command's own exit code; 1 error;
2 usage; 3 approval not given (the command did not run); 124 the command timed out.

Nothing here prints or logs a value or a passphrase. The credential store comes from
``secrets_broker.open_default_broker``. Tests inject a broker instead.
"""

from __future__ import annotations

import getpass
import os
import sys
from collections.abc import Callable
from typing import TextIO

from . import secrets_broker as sb
from . import secrets_prompt
from .secrets_prompt import printable

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_DENIED = 3
EXIT_TIMED_OUT = 124

_USAGE = """\
usage: python -m meridian secrets <command>

  init                   create the vault; prints a recovery code once
  list                   names, groups, classes and purposes (never values)
  run NAME -- ARGV...    run ARGV with NAME's value in its environment
"""
_RUN_USAGE = "usage: python -m meridian secrets run NAME -- ARGV...\n"
_INIT_USAGE = "usage: python -m meridian secrets init\n"
_LIST_USAGE = "usage: python -m meridian secrets list\n"


def cli_main(
    argv: list[str] | None = None,
    *,
    broker_factory: Callable[[], sb.SecretsBroker] | None = None,
    passphrase_reader: Callable[[str], str] | None = None,
    approver: sb.Approver | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Dispatch one ``secrets`` subcommand. Returns the process exit code.

    The keyword arguments exist for tests. In normal use the defaults are the Windows
    credential store, getpass, and the Approve once / Deny window.
    """
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    args = list(argv) if argv is not None else list(sys.argv[1:])
    if not args:
        err.write(_USAGE)
        return EXIT_USAGE
    if args[0] in ("-h", "--help", "help"):
        out.write(_USAGE)
        return EXIT_OK
    make_broker = broker_factory if broker_factory is not None else sb.open_default_broker
    read = passphrase_reader if passphrase_reader is not None else getpass.getpass
    command, rest = args[0], args[1:]
    try:
        if command == "init":
            return _cmd_init(rest, make_broker, read, out, err)
        if command == "list":
            return _cmd_list(rest, make_broker, out, err)
        if command == "run":
            ask = approver if approver is not None else secrets_prompt.approver_from_dialog
            return _cmd_run(rest, make_broker, read, ask, out, err)
    except (sb.BrokerError, NotImplementedError) as exc:
        err.write(f"error: {exc}\n")
        return EXIT_ERROR
    err.write(f"error: unknown command '{printable(command, 40)}'\n")
    err.write(_USAGE)
    return EXIT_USAGE


def _cmd_init(
    args: list[str],
    make_broker: Callable[[], sb.SecretsBroker],
    read: Callable[[str], str],
    out: TextIO,
    err: TextIO,
) -> int:
    if args:
        err.write(_INIT_USAGE)
        return EXIT_USAGE
    broker = make_broker()
    try:
        if broker.is_initialized():
            err.write("error: the secrets broker is already initialized\n")
            return EXIT_ERROR
        passphrase = read("New passphrase (at least 12 characters): ")
        try:
            sb.check_passphrase(passphrase)
        except sb.BrokerError as exc:
            err.write(f"error: {exc}; nothing was stored\n")
            return EXIT_ERROR
        if read("Repeat the passphrase: ") != passphrase:
            err.write("error: the two passphrases do not match; nothing was stored\n")
            return EXIT_ERROR
        recovery_code = broker.initialize(passphrase)
    finally:
        broker.close()
    out.write(_recovery_banner(recovery_code))
    return EXIT_OK


def _recovery_banner(code: str) -> str:
    return (
        "\nRecovery code. WRITE THIS DOWN NOW. It is shown once and is not stored anywhere.\n"
        f"  {code}\n"
        "It opens SECRET and SENSITIVE items if the passphrase is lost. "
        "PUBLIC and INTERNAL items do not need it.\n"
    )


def _cmd_list(
    args: list[str],
    make_broker: Callable[[], sb.SecretsBroker],
    out: TextIO,
    err: TextIO,
) -> int:
    if args:
        err.write(_LIST_USAGE)
        return EXIT_USAGE
    broker = make_broker()
    try:
        rows = broker.list_names()
    finally:
        broker.close()
    if not rows:
        out.write("No secrets are stored.\n")
        return EXIT_OK
    out.write("NAME\tGROUP\tCLASS\tPURPOSE\n")
    for row in rows:
        cells = [
            printable(row["name"], 128),
            printable(row["group"], 64),
            printable(row["class"], 20),
            printable(row["purpose"], sb.MAX_PURPOSE_CHARS),
        ]
        out.write("\t".join(cells) + "\n")
    return EXIT_OK


def _cmd_run(
    args: list[str],
    make_broker: Callable[[], sb.SecretsBroker],
    read: Callable[[str], str],
    approver: sb.Approver,
    out: TextIO,
    err: TextIO,
) -> int:
    if "--" not in args:
        err.write(_RUN_USAGE)
        return EXIT_USAGE
    split = args.index("--")
    names, command_argv = args[:split], args[split + 1 :]
    if len(names) != 1 or not command_argv:
        err.write(_RUN_USAGE)
        return EXIT_USAGE
    broker = make_broker()
    try:
        broker.unlock(read("Passphrase: "))
        result = broker.run_with_secret(
            names[0], command_argv, approver=approver, client=_client_label()
        )
    finally:
        broker.close()
    return _report(result, out, err)


def _report(result: sb.RunResult, out: TextIO, err: TextIO) -> int:
    if result.status == "completed":
        out.write(result.stdout)
        err.write(result.stderr)
        return result.exit_code if result.exit_code is not None else EXIT_ERROR
    if result.status == "timed_out":
        out.write(result.stdout)
        err.write(result.stderr)
        err.write("error: the command timed out\n")
        return EXIT_TIMED_OUT
    if result.status == "denied":
        err.write("denied: approval was not given, so the command did not run\n")
        return EXIT_DENIED
    err.write("error: the command did not run\n")
    return EXIT_ERROR


def _client_label() -> str:
    return f"meridian secrets run (pid {os.getpid()})"
