"""Keys kept in a file the owner chooses, read as a list of names and as one value at a time. Pure Python: no Qt import.

The owner picks one of two kinds of file. The kind comes from the file name only, never from the content:

* a .json file: one JSON object of name to value text, for example {"OpenAI": "..."};
* a dotenv file: ``.env``, ``.env.*`` (for example ``.env.local``), ``*.env``, and secrets files (``*.secrets``,
  ``*.secret``, ``secrets.*``). Each line is ``NAME=value``. A line may start with ``export``, a value may be in single or
  double quotes, and a line that starts with ``#`` is a comment. An unquoted value runs to the end of its line or to a comment, which is a whitespace character and then ``#``. A ``#`` right after ``=`` is part of the value.
  Backslash escapes are not read. A value that spans lines is not read.

Any other name is refused. Only the file's path is kept in settings (``keys_file_path``). The values stay in the file:
they are never copied to the keychain or to settings, never sent anywhere except one Check request, and the app never
writes the file.

Listing reads the file to check every entry, and a bad entry is skipped, not the whole file. Listing keeps the names only.
A Check reads the one value it asks for, for that one call. A file that is missing, that is larger than 64 KB, that is
not UTF-8 text, that does not read as its kind, is refused whole, with a plain reason and
a count. Each refused entry is also listed with its place in the file (its entry number in a JSON file, its line number in
a dotenv file) and its plain reason. No value from the file is ever shown, and a refused entry's name is not shown either. An accepted
name is listed, and the names export writes it down, so keep secrets out of the name position. ``log_listing`` writes the same lines to the control log, one line for each refused entry, so the
owner can read them in the Logs page.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from meridian_desktop import api_keys
from meridian_desktop.api_keys import BUILT_IN_NAMES, KeyNameError, clean_name, slug
from meridian_desktop.runner_controls import append_control_line, default_log_path
from meridian_desktop.secret_store import MAX_SECRET_LENGTH

MAX_FILE_BYTES = 64 * 1024

JSON_FORMAT = "json"
DOTENV_FORMAT = "dotenv"

MISSING_TEXT = "The keys file is missing. Choose the file again."
UNREADABLE_TEXT = "The keys file could not be read. Choose the file again."
TOO_LARGE_TEXT = "Not loaded. The keys file is larger than 64 KB, so 0 entries were read."
NOT_SUPPORTED_TEXT = "Not loaded. Choose a .json, .env or secrets file, so 0 entries were read."
NOT_TEXT_TEXT = "Not loaded. The keys file is not UTF-8 text, so 0 entries were read."
NOT_JSON_TEXT = "Not loaded. The keys file is not valid JSON, so 0 entries were read."
NOT_OBJECT_TEXT = "Not loaded. The keys file must hold one JSON object of name to text, so 0 entries were read."
# A refusal: the count line, then one line for each refused entry, up to LINES_LISTED of them, then the rest as a count,
# then the line that says nothing was loaded. The control log gets every refused entry, not only the first few.
REFUSED_COUNT_TEXT = "Not loaded. {refused} of {total} entries were refused."
REFUSED_LINE_TEXT = "{where}: {reason}"
REFUSED_MORE_TEXT = "and {more} more"
NOTHING_LOADED_TEXT = "Nothing was loaded."
# A file that loads with some entries skipped: the count, then the skipped entries, as the Keys block and the log show them.
SKIPPED_COUNT_TEXT = "Skipped {skipped} of {total} entries. The rest are loaded."
ALL_SKIPPED_TEXT = "Not loaded. All {total} entries were skipped."
ENTRY_SKIPPED_TEXT = (
    "That entry was skipped when the keys file was listed, so it cannot be checked. Fix its line, then choose the file again."
)
LINES_LISTED = 10
# The fixed reasons an entry is refused for. None of them holds a name or a value from the file.
REASON_NAME_NOT_ALLOWED = "name is not allowed"
REASON_NAME_BUILT_IN = "name is a built-in name, which has its own entry"
REASON_NAME_REPEATS = "name repeats the one on {place}"
REASON_VALUE_EMPTY = "value is empty"
REASON_VALUE_NOT_TEXT = "value is not text"
REASON_VALUE_LONG = "value is longer than {limit} characters"
REASON_LINE_NOT_NAME_VALUE = "line is not NAME=value"
REASON_LINE_NO_CLOSING_QUOTE = "value has no closing quote"
REASON_LINE_TEXT_AFTER_QUOTE = "value has text after its closing quote"
_BUILT_IN_SLUGS = frozenset(slug(name) for name in BUILT_IN_NAMES)
# A line may start with ``export`` followed by spaces or a tab. The name is what follows.
_EXPORT = re.compile(r"export\s+")
# Only a CR LF, CR or LF ends a line here. str.splitlines also splits on form feeds and other characters, which would
# shift the line numbers reported to the owner.
_LINE_BREAK = re.compile(r"\r\n|\r|\n")
# An unquoted value ends at a comment: a whitespace character, then "#". A "#" right after "=" is part of the value.
_COMMENT = re.compile(r"\s#")


class KeysFileError(Exception):
    """The keys file cannot be used. The message is plain words. ``refused`` lists each refused entry, when there are any."""

    def __init__(
        self, message: str, refused: tuple[Refusal, ...] = (), total: int = 0, *, entry_skipped: bool = False,
    ) -> None:
        super().__init__(message)
        self.refused = refused
        self.total = total
        # True only when read_value refuses one name because its entry was skipped. Its message is then plain words of ours.
        self.entry_skipped = entry_skipped


@dataclass(frozen=True)
class Refusal:
    """One refused entry: its place in the file and its plain reason. Its name and value are never kept or shown."""

    where: str
    reason: str


@dataclass(frozen=True)
class FileNames:
    """What the Keys block lists from the file: its names, or the plain reason it is refused or missing.

    ``refused`` and ``total`` are set only when entries were refused: the refused entries, and how many entries the file
    holds in all.
    """

    names: tuple[str, ...] = ()
    problem: str | None = None
    refused: tuple[Refusal, ...] = ()
    total: int = 0


class _Object:
    """The pairs of one JSON object, in file order. A repeated name stays in the pairs, so it is seen and refused."""

    __slots__ = ("pairs",)

    def __init__(self, pairs: list[tuple[str, object]]) -> None:
        self.pairs = pairs


# One entry, or one line, of a file: its place (an entry number, or a line number), its name, and its value. A line that
# is not a usable NAME=value line has no name (None), and its value is then the fixed reason it is refused for.
_Item = tuple[str, str | None, object]


def _local_now() -> datetime:
    return datetime.now().astimezone()


def file_format(path: str | Path) -> str | None:
    """The kind of keys file a name is: JSON_FORMAT for .json, DOTENV_FORMAT for .env, .env.*, *.env and the secrets
    files (*.secrets, *.secret, secrets.*), and None for any other name. Only the name is looked at, never the content."""
    name = Path(path).name.lower()
    if name.endswith(".json"):
        return JSON_FORMAT
    if name == ".env" or name.startswith(".env.") or name.endswith(".env"):
        return DOTENV_FORMAT
    if name.endswith((".secrets", ".secret")) or name == "secrets" or name.startswith("secrets."):
        return DOTENV_FORMAT
    return None


def is_classified(name: str) -> bool:
    """True when ``name`` is a known service (``key_services.SERVICES``) or a built-in name (``BUILT_IN_NAMES``).

    Every other name is unclassified. Both lists are read on each call, so a name either gains is classified at once.
    key_services is imported here, not at the top of the module: key_services imports read_value from this module, so a
    top-level import would be circular.
    """
    from meridian_desktop import key_services

    known = {slug(service.name) for service in key_services.SERVICES}
    known |= {slug(built_in) for built_in in api_keys.BUILT_IN_NAMES}
    return slug(name) in known


def split_by_classification(names: Iterable[str]) -> tuple[list[str], list[str]]:
    """The names split into (classified, unclassified). Each group keeps the order the names came in."""
    listed = list(names)
    return [name for name in listed if is_classified(name)], [name for name in listed if not is_classified(name)]


def _read_raw(path: Path) -> bytes:
    """The bytes of the file. Raise KeysFileError with plain words when it is missing, unreadable or too large."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        raise KeysFileError(MISSING_TEXT) from None
    except (OSError, ValueError):
        raise KeysFileError(UNREADABLE_TEXT) from None
    if size > MAX_FILE_BYTES:
        raise KeysFileError(TOO_LARGE_TEXT)
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except (OSError, ValueError):
        raise KeysFileError(UNREADABLE_TEXT) from None
    if len(raw) > MAX_FILE_BYTES:
        raise KeysFileError(TOO_LARGE_TEXT)
    return raw


def _read_items(path: Path) -> list[_Item]:
    """Every entry (JSON) or line (dotenv) of the file, in file order. Raise KeysFileError with plain words when the name is
    of no kind the app reads, or the file is missing, unreadable, too large, or does not read as its kind."""
    kind = file_format(path)
    if kind is None:
        raise KeysFileError(NOT_SUPPORTED_TEXT)
    raw = _read_raw(path)
    if kind == JSON_FORMAT:
        try:
            data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_Object)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise KeysFileError(NOT_JSON_TEXT) from None
        if not isinstance(data, _Object):
            raise KeysFileError(NOT_OBJECT_TEXT)
        return [(f"entry {place}", key, value) for place, (key, value) in enumerate(data.pairs, start=1)]
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise KeysFileError(NOT_TEXT_TEXT) from None
    return _dotenv_items(text)


def _dotenv_items(text: str) -> list[_Item]:
    """The NAME=value lines of a dotenv-style file, in file order. A blank line and a ``#`` comment are not entries.

    A line may start with ``export``. The name is everything before the first ``=``, so a value may contain ``=``. A line
    with no ``=`` is refused by its line number, and so is a value whose quote is not closed on its line.
    """
    items: list[_Item] = []
    for number, line in enumerate(_LINE_BREAK.split(text), start=1):
        body = line.strip()
        if not body or body.startswith("#"):
            continue
        place = f"line {number}"
        exported = _EXPORT.match(body)
        if exported:
            body = body[exported.end():]
        name, separator, rest = body.partition("=")
        if not separator:
            items.append((place, None, REASON_LINE_NOT_NAME_VALUE))
            continue
        value, reason = _dotenv_value(rest)
        if reason is not None:
            items.append((place, None, reason))
            continue
        items.append((place, name.strip(), value))
    return items


def _dotenv_value(rest: str) -> tuple[str, str | None]:
    """The value of one dotenv line, or the reason the line is refused. ``rest`` is the text after the first ``=``, unstripped.

    A quoted value (single or double quotes) must close on the same line, and only a ``#`` comment may follow the close. An
    unquoted value runs to a comment, which is a whitespace character and then ``#``. A ``#`` right after ``=`` is part of
    the value. Backslash escapes are not read.
    """
    text = rest.strip()
    if text[:1] in ("'", '"'):
        close = text.find(text[0], 1)
        if close == -1:
            return "", REASON_LINE_NO_CLOSING_QUOTE
        tail = text[close + 1:].strip()
        if tail and not tail.startswith("#"):
            return "", REASON_LINE_TEXT_AFTER_QUOTE
        return text[1:close], None
    if rest[:1] in (" ", "\t") and text.startswith("#"):
        return "", None
    cut = _COMMENT.search(text)
    if cut is not None:
        text = text[:cut.start()]
    return text.strip(), None


def _name_of(key: str) -> str | None:
    """The key name as it is listed, or None when the name is not a valid key name."""
    try:
        return clean_name(key)
    except KeyNameError:
        return None


def _refusal_text(refusal: Refusal) -> str:
    """The one line that says where an entry is and why it is refused. It never holds the entry's name or value."""
    return REFUSED_LINE_TEXT.format(where=refusal.where, reason=refusal.reason)


def _refused_text(refused: tuple[Refusal, ...], total: int) -> str:
    """The refusal of a file: its count, its refused entries, any rest as a count, then the line that says nothing loaded."""
    lines = [REFUSED_COUNT_TEXT.format(refused=len(refused), total=total)]
    lines += [_refusal_text(item) for item in refused[:LINES_LISTED]]
    if len(refused) > LINES_LISTED:
        lines.append(REFUSED_MORE_TEXT.format(more=len(refused) - LINES_LISTED))
    lines.append(NOTHING_LOADED_TEXT)
    return "\n".join(lines)


def _value_reason(value: object) -> str | None:
    """Why a value is refused, or None when it is usable text. A value from a dotenv line is always text."""
    if not isinstance(value, str):
        return REASON_VALUE_NOT_TEXT
    text = value.strip()
    if not text:
        return REASON_VALUE_EMPTY
    if len(text) > MAX_SECRET_LENGTH:
        return REASON_VALUE_LONG.format(limit=MAX_SECRET_LENGTH)
    return None


@dataclass(frozen=True)
class _Checked:
    """The usable entries of a file, and the entries that were skipped, each with its place and plain reason.

    ``skipped_slugs`` holds the slug of each skipped entry whose name is known, so one name can be refused on its own.
    """

    entries: tuple[tuple[str, str], ...]
    skipped: tuple[Refusal, ...]
    skipped_slugs: frozenset[str]
    total: int


def _check_items(items: list[_Item]) -> _Checked:
    """Split the entries into the usable ones and the skipped ones. A skipped entry is left out, and the rest still load.

    An entry is skipped when its line is not a usable NAME=value line, its name is not a key name, its name is a built-in
    name such as RunPod, which keeps its own entry, its name repeats an earlier name by its slug (the earlier one stays), or
    its value is not usable text. Places are entry numbers in a JSON file and line numbers in a dotenv file.
    """
    first_place: dict[str, str] = {}
    entries: list[tuple[str, str]] = []
    skipped: list[Refusal] = []
    skipped_slugs: set[str] = set()
    for place, key, value in items:
        if key is None:
            # The line is not a usable NAME=value line. Its value is the fixed reason, and no name is shown.
            skipped.append(Refusal(where=place, reason=str(value)))
            continue
        name = _name_of(key)
        if name is None:
            skipped.append(Refusal(where=place, reason=REASON_NAME_NOT_ALLOWED))
            continue
        found = slug(name)
        if found in _BUILT_IN_SLUGS:
            skipped.append(Refusal(where=place, reason=REASON_NAME_BUILT_IN))
            skipped_slugs.add(found)
            continue
        if found in first_place:
            skipped.append(Refusal(where=place, reason=REASON_NAME_REPEATS.format(place=first_place[found])))
            continue
        first_place[found] = place
        reason = _value_reason(value)
        if reason is not None:
            skipped.append(Refusal(where=place, reason=reason))
            skipped_slugs.add(found)
            continue
        entries.append((name, str(value).strip()))
    return _Checked(
        entries=tuple(entries),
        skipped=tuple(skipped),
        skipped_slugs=frozenset(skipped_slugs),
        total=len(items),
    )


def _all_skipped_text(refused: tuple[Refusal, ...], total: int) -> str:
    """The problem when every entry was skipped: the count, each skipped entry, any rest as a count, then nothing loaded."""
    lines = [ALL_SKIPPED_TEXT.format(total=total)]
    lines += [_refusal_text(item) for item in refused[:LINES_LISTED]]
    if len(refused) > LINES_LISTED:
        lines.append(REFUSED_MORE_TEXT.format(more=len(refused) - LINES_LISTED))
    lines.append(NOTHING_LOADED_TEXT)
    return "\n".join(lines)


def _entries(path: Path) -> _Checked:
    """The usable entries of the file and the skipped ones. Raise KeysFileError only when the whole file is refused."""
    return _check_items(_read_items(path))


def read_names(path: str | Path) -> FileNames:
    """The key names in the keys file, or the plain reason the whole file is refused or missing. Skipped entries are listed
    with their place and reason, and the rest still load. No value is returned or kept."""
    try:
        checked = _entries(Path(path))
    except KeysFileError as exc:
        return FileNames(problem=str(exc))
    names = tuple(name for name, _text in checked.entries)
    if not checked.skipped:
        return FileNames(names=names)
    if names:
        return FileNames(names=names, refused=checked.skipped, total=checked.total)
    return FileNames(
        problem=_all_skipped_text(checked.skipped, checked.total),
        refused=checked.skipped,
        total=checked.total,
    )


def skipped_summary(listing: FileNames) -> str | None:
    """The note for a file that loaded with some entries skipped, or None when nothing was skipped or the file is refused.

    The note holds the count and each skipped entry's place and plain reason. It never holds a name or a value.
    """
    if listing.problem is not None or not listing.refused:
        return None
    lines = [SKIPPED_COUNT_TEXT.format(skipped=len(listing.refused), total=listing.total)]
    lines += [_refusal_text(item) for item in listing.refused[:LINES_LISTED]]
    if len(listing.refused) > LINES_LISTED:
        lines.append(REFUSED_MORE_TEXT.format(more=len(listing.refused) - LINES_LISTED))
    return "\n".join(lines)


def read_value(path: str | Path, name: str) -> str | None:
    """The value of ``name`` in the keys file, read for one Check. None when the file holds no such name.

    Raise KeysFileError when the file is missing or refused. The value goes only to the caller, which sends one request
    with it. It is not kept here after the call.
    """
    key = slug(name)
    checked = _entries(Path(path))
    for listed, text in checked.entries:
        if slug(listed) == key:
            return text
    if key in checked.skipped_slugs:
        raise KeysFileError(ENTRY_SKIPPED_TEXT, entry_skipped=True)
    return None


def listing_log_lines(listing: FileNames, stamp: str) -> list[str]:
    """The control log lines for one listing: one for each refused entry, or one for a refused or missing file, or one for
    a file that lists its names. A line never holds a name or a value from the file."""
    if listing.problem is not None:
        return [f"{stamp} keys file: {line}" for line in listing.problem.splitlines()]
    lines = [f"{stamp} keys file: names listed, {len(listing.names)} in all"]
    if listing.refused:
        lines.append(f"{stamp} keys file: skipped {len(listing.refused)} of {listing.total} entries")
        lines += [f"{stamp} keys file {_refusal_text(item)}" for item in listing.refused]
    return lines


def log_listing(settings_path: Path, listing: FileNames, *, clock: Callable[[], datetime] = _local_now) -> None:
    """Append the listing's lines to the control log beside the settings file. It never raises: a log that cannot be
    written must not change the Keys block."""
    try:
        stamp = clock().isoformat(timespec="seconds")
        lines = listing_log_lines(listing, stamp)
    except Exception:  # noqa: BLE001 - a logging failure is not the owner's concern and must not change the block
        return
    log = default_log_path(settings_path)
    for line in lines:
        append_control_line(log, line)


__all__ = [
    "DOTENV_FORMAT",
    "JSON_FORMAT",
    "MAX_FILE_BYTES",
    "FileNames",
    "KeysFileError",
    "Refusal",
    "file_format",
    "is_classified",
    "listing_log_lines",
    "log_listing",
    "read_names",
    "read_value",
    "skipped_summary",
    "split_by_classification",
]
