"""Group the control log's lines by launch of the local server. Pure Python: no Qt import.

The control log (runner_controls.py) gets one line for each Start or Stop attempt, in the form
``<UTC time> <action> scope=<scope> outcome=<outcome> pid=<pid, or "not reported">``. A line that
says ``start scope=local`` is the explicit launch marker of one local server launch, whatever its
outcome. It opens a group, and each later line joins the group of the marker above it. The lines
before the first marker form a group of their own.

When the lines hold no local start marker, for example because the bounded tail starts after the
last one, group_by_launch falls back to the timestamp gap. A new group begins at a timed entry that
comes more than DEFAULT_GAP after the timed entry before it. A line with no readable timestamp
joins the group it follows. When the lines hold neither markers nor timestamps, they form one group.
GroupedLog.rule names the rule used, and GroupedLog.rule_text says it in words for the view.

Nothing here reads, writes or opens a file, and no process is started.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

RULE_MARKER = "marker"
RULE_GAP = "gap"
RULE_NONE = "none"

DEFAULT_GAP = dt.timedelta(minutes=30)
ALL_KEY = "all-lines"
BEFORE_KEY = "before-first-launch"
ALL_LABEL = "All lines"
BEFORE_LABEL = "Lines before the first local start line"
LAUNCH_LABEL = "Launch {number} of {total}: started {stamp}, outcome {outcome}, pid {pid}"
RUN_LABEL = "Run {number} of {total}: first entry {stamp}"

MARKER_RULE_TEXT = (
    "Grouped by launch. Each line that says 'start scope=local' starts one local server launch. "
    "The lines after it, up to the next such line, belong to that launch."
)
GAP_RULE_TEXT = (
    "Grouped by time. This tail has no local server start line, so a new group begins where more than "
    "{minutes} minutes pass between two timed entries."
)
NONE_RULE_TEXT = (
    "Not grouped. This tail has no local server start line and no timestamps, so its lines are shown together."
)

# The start line that runner_controls.format_log_line writes for the local scope.
_MARKER_RE = re.compile(r"^(?P<stamp>\S+) start scope=local outcome=(?P<outcome>\S+) pid=(?P<pid>.+?)\s*$")


@dataclass(frozen=True)
class LaunchGroup:
    """One group of lines. ``key`` names it for the view, and ``label`` is its heading and its picker entry."""

    key: str
    label: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class GroupedLog:
    """The groups of one log, the rule that made them, and that rule in words. ``groups`` is empty for no lines."""

    rule: str
    rule_text: str
    groups: tuple[LaunchGroup, ...]


def _timed(line: str) -> tuple[str, dt.datetime] | None:
    """The leading token and its time, when the line starts with a readable ISO 8601 timestamp.

    Both stamp forms in use parse: the control log's ``+00:00`` and the desktop log's ``+0000``. A time with
    no offset is taken as UTC, so every time can be compared with every other.
    """
    token = line.split(" ", 1)[0]
    try:
        when = dt.datetime.fromisoformat(token)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return token, when


def _group_by_marker(lines: Sequence[str]) -> tuple[LaunchGroup, ...]:
    """Split at each local start line. The start line is the first line of its own group."""
    before: list[str] = []
    launches: list[tuple[re.Match[str], list[str]]] = []
    for line in lines:
        match = _MARKER_RE.match(line)
        if match is not None:
            launches.append((match, [line]))
        elif launches:
            launches[-1][1].append(line)
        else:
            before.append(line)
    groups: list[LaunchGroup] = []
    if before:
        groups.append(LaunchGroup(BEFORE_KEY, BEFORE_LABEL, tuple(before)))
    total = len(launches)
    for number, (match, group_lines) in enumerate(launches, start=1):
        label = LAUNCH_LABEL.format(
            number=number,
            total=total,
            stamp=match.group("stamp"),
            outcome=match.group("outcome"),
            pid=match.group("pid"),
        )
        groups.append(LaunchGroup(f"launch-{number}", label, tuple(group_lines)))
    return tuple(groups)


def _group_by_gap(lines: Sequence[str], gap: dt.timedelta) -> tuple[LaunchGroup, ...]:
    """Split where more than ``gap`` passes between two timed entries. Untimed lines stay in the run they follow.

    Only called when at least one line is timed, so every run gets a first timed entry to name it by.
    """
    runs: list[tuple[str, list[str]]] = []
    current: list[str] = []
    first = ""
    last: dt.datetime | None = None
    for line in lines:
        timed = _timed(line)
        if timed is not None:
            token, when = timed
            if last is not None and when - last > gap:
                runs.append((first, current))
                current = []
                first = ""
            if not first:
                first = token
            last = when
        current.append(line)
    runs.append((first, current))
    total = len(runs)
    return tuple(
        LaunchGroup(
            f"run-{number}",
            RUN_LABEL.format(number=number, total=total, stamp=run_first),
            tuple(run_lines),
        )
        for number, (run_first, run_lines) in enumerate(runs, start=1)
    )


def group_by_launch(lines: Iterable[str], *, gap: dt.timedelta = DEFAULT_GAP) -> GroupedLog:
    """Group ``lines`` (one log's lines, in file order) by launch, using the first rule that the lines support.

    A local start marker anywhere in the lines selects the marker rule, and the timestamp gap is not used.
    With no marker, a timed line selects the gap rule. With neither, all the lines form one group.
    Raise ValueError for a gap that is not positive, because that is a bug in the caller.
    """
    if gap <= dt.timedelta(0):
        raise ValueError("gap must be a positive time span")
    items = tuple(lines)
    if not items:
        return GroupedLog(RULE_NONE, NONE_RULE_TEXT, ())
    if any(_MARKER_RE.match(line) is not None for line in items):
        return GroupedLog(RULE_MARKER, MARKER_RULE_TEXT, _group_by_marker(items))
    if any(_timed(line) is not None for line in items):
        minutes = int(gap.total_seconds() // 60)
        return GroupedLog(RULE_GAP, GAP_RULE_TEXT.format(minutes=minutes), _group_by_gap(items, gap))
    return GroupedLog(RULE_NONE, NONE_RULE_TEXT, (LaunchGroup(ALL_KEY, ALL_LABEL, items),))


NO_MATCH_TEXT = "No line in the launches shown matches the search."


def launch_view(grouped: GroupedLog, *, selected: str | None, query: str = "", newest_first: bool = False) -> str:
    """The text the view shows for ``grouped``: the chosen group, or every group, with only the lines that match ``query``.

    ``query`` is a case-insensitive substring, and blank means no filter. A group with no matching line is left out.
    A log with neither markers nor timestamps is shown with no heading, so its text is exactly its lines.
    ``newest_first`` shows the newest launch first and the newest line first inside each launch. The launch numbers
    stay in time order, so Launch 3 of 3 can come first.
    """
    needle = query.strip().casefold()
    plain = grouped.rule == RULE_NONE
    blocks: list[str] = []
    groups = reversed(grouped.groups) if newest_first else grouped.groups
    for group in groups:
        if selected is not None and group.key != selected:
            continue
        group_lines = reversed(group.lines) if newest_first else group.lines
        lines = [line for line in group_lines if not needle or needle in line.casefold()]
        if not lines:
            continue
        blocks.append("\n".join(lines) if plain else "\n".join([group.label, *lines]))
    if not blocks:
        return NO_MATCH_TEXT if needle else ""
    return "\n\n".join(blocks)


__all__ = [
    "ALL_KEY",
    "BEFORE_KEY",
    "DEFAULT_GAP",
    "GroupedLog",
    "LaunchGroup",
    "NO_MATCH_TEXT",
    "RULE_GAP",
    "RULE_MARKER",
    "RULE_NONE",
    "group_by_launch",
    "launch_view",
]
