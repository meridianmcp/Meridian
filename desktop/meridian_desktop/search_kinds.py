"""The Search tab's three kinds (code, outputs and research) as lists, read from the local Meridian server only.

Pure Python: no Qt import. The tab has no search box. Each kind is a list grouped by where it comes from, or it says
why there is no list:

- code: no list route exists. The code route answers only a search, so there is nothing to list by repo yet. Nothing
  is sent, and the kind says "No data source yet".
- outputs: no list route exists. The outputs route answers only a search, so there is nothing to list by project yet.
  Nothing is sent, and the kind says "No data source yet".
- research: outputs_client.list_research_runs, one GET of the research runs route. The runs are grouped by provider. A
  run with no provider reported is grouped under "provider not reported", which sorts last.

A kind whose route the server does not have is shown as "Not connected". Nothing is invented: a kind with no answer
shows its state, never a made-up row.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from meridian_desktop import outputs_client

KIND_CODE = "code"
KIND_OUTPUTS = "outputs"
KIND_RESEARCH = "research"
KINDS = (KIND_CODE, KIND_OUTPUTS, KIND_RESEARCH)
KIND_LABELS = {KIND_CODE: "Code", KIND_OUTPUTS: "Outputs", KIND_RESEARCH: "Research"}

# The kinds with a list route on the local server. The others have no list yet and never send a request.
KINDS_WITH_LIST_ROUTE = (KIND_RESEARCH,)

# Each state a kind's list can take.
STATE_RESULTS = "results"
STATE_EMPTY = "empty"
STATE_NOT_CONNECTED = "not_connected"
STATE_NO_SOURCE = "no_source"
STATE_NOT_CHECKED = "not_checked"
STATE_ERROR = "error"

NO_SOURCE_TEXT = {
    KIND_CODE: (
        "Code: no data source yet. The local Meridian server does not list the indexed code by repo, "
        "so there is nothing to show here. Nothing was sent."
    ),
    KIND_OUTPUTS: (
        "Outputs: no data source yet. The local Meridian server does not list the indexed outputs by project, "
        "so there is nothing to show here. Nothing was sent."
    ),
}
NOT_CHECKED_TEXT = "Research: not checked yet. Press Refresh to read the research runs."
NOT_CONNECTED_STOPPED = (
    "Research: not connected. The local Meridian server is not running at {}. "
    "Start Meridian on this computer, then press Refresh."
)
NOT_CONNECTED_ROUTE = "Research: not connected. The local server has no research runs route yet. Update Meridian, then press Refresh."
EMPTY_TEXT = "Research: no research runs yet."
RESULTS_TEXT = "Research: {} run(s) in {} group(s), grouped by provider."
ERROR_TEXT = "Research: the runs could not be read. {}"
NO_DETAIL = "Press Refresh to try again."
NOT_REPORTED_GROUP = "provider not reported"

# The row limit the research route is asked for. The client caps its answer too.
RESEARCH_LIMIT = 50


@dataclass(frozen=True)
class Row:
    """One run: its id, state and the times the server reported. Plain text. Empty means the server did not report it."""

    item: str
    state: str
    started: str
    updated: str
    completed: str


@dataclass(frozen=True)
class Group:
    """Rows that share one source. For research, the source is the provider."""

    title: str
    rows: tuple[Row, ...]


@dataclass(frozen=True)
class KindList:
    """One kind's list: its state, the one line that says it, and its groups (empty unless the state is results)."""

    kind: str
    state: str
    line: str
    groups: tuple[Group, ...] = ()

    @property
    def count(self) -> int:
        return sum(len(group.rows) for group in self.groups)


@dataclass(frozen=True)
class ListOutcome:
    """The lists for the chosen kinds, in the order they were asked for."""

    kinds: tuple[KindList, ...]


def _label(kind: str) -> str:
    return KIND_LABELS[kind]


def no_source_list(kind: str) -> KindList:
    """The list for a kind with no list route. It names the missing source and sends nothing."""
    return KindList(kind, STATE_NO_SOURCE, NO_SOURCE_TEXT[kind])


def not_checked_list(kind: str) -> KindList:
    """The list for a kind that is read on Refresh and has not been read yet."""
    if kind in KINDS_WITH_LIST_ROUTE:
        return KindList(kind, STATE_NOT_CHECKED, NOT_CHECKED_TEXT)
    return no_source_list(kind)


def _group_key(provider: str) -> tuple[int, str]:
    """Groups sort by provider name, with the group for runs that name no provider last."""
    return (1, "") if provider == NOT_REPORTED_GROUP else (0, provider.lower())


def group_runs(runs: Iterable[outputs_client.ResearchRun]) -> tuple[Group, ...]:
    """Group runs by provider. Runs keep the order the server sent them in, within each group."""
    buckets: dict[str, list[Row]] = {}
    for run in runs:
        title = run.provider or NOT_REPORTED_GROUP
        buckets.setdefault(title, []).append(
            Row(
                item=run.run_id,
                state=run.state,
                started=run.started_at,
                updated=run.updated_at,
                completed=run.completed_at,
            )
        )
    return tuple(Group(title, tuple(rows)) for title, rows in sorted(buckets.items(), key=lambda item: _group_key(item[0])))


def _research(base_url: str, transport: outputs_client.Transport | None) -> KindList:
    result = outputs_client.list_research_runs(base_url, limit=RESEARCH_LIMIT, transport=transport)
    kind = KIND_RESEARCH
    if result.state == outputs_client.RESULTS:
        groups = group_runs(result.runs)
        line = RESULTS_TEXT.format(len(result.runs), len(groups))
        return KindList(kind, STATE_RESULTS, line, groups)
    if result.state == outputs_client.EMPTY:
        return KindList(kind, STATE_EMPTY, EMPTY_TEXT)
    if result.state == outputs_client.MISSING_ROUTE:
        return KindList(kind, STATE_NOT_CONNECTED, NOT_CONNECTED_ROUTE)
    if result.state == outputs_client.NOT_RUNNING:
        return KindList(kind, STATE_NOT_CONNECTED, NOT_CONNECTED_STOPPED.format(base_url))
    return KindList(kind, STATE_ERROR, ERROR_TEXT.format(result.message or NO_DETAIL))


def read_kind(kind: str, base_url: str, *, transport: outputs_client.Transport | None = None) -> KindList:
    """One kind's list. Only research sends a request. Code and outputs return their no-source state at once.

    A kind outside KIND_LABELS is a caller error and raises ValueError. A failed research read is not raised: it becomes
    an error list. The research read goes through outputs_client, which refuses any address that is not on this computer.
    """
    if kind not in KIND_LABELS:
        raise ValueError(f"unknown kind: {kind}")
    if kind == KIND_RESEARCH:
        return _research(base_url, transport)
    return no_source_list(kind)


def read_lists(
    base_url: str,
    kinds: Sequence[str] = KINDS,
    *,
    transports: Mapping[str, outputs_client.Transport | None] | None = None,
) -> ListOutcome:
    """Read each chosen kind, in order. Every kind gets its own list, even when another kind fails. Unknown kinds are skipped."""
    lookup = dict(transports or {})
    chosen = tuple(kind for kind in kinds if kind in KIND_LABELS)
    return ListOutcome(tuple(read_kind(kind, base_url, transport=lookup.get(kind)) for kind in chosen))


__all__ = [
    "KINDS",
    "KIND_CODE",
    "KIND_LABELS",
    "KIND_OUTPUTS",
    "KIND_RESEARCH",
    "STATE_EMPTY",
    "STATE_ERROR",
    "STATE_NOT_CHECKED",
    "STATE_NOT_CONNECTED",
    "STATE_NO_SOURCE",
    "STATE_RESULTS",
    "Group",
    "KindList",
    "ListOutcome",
    "Row",
    "group_runs",
    "no_source_list",
    "not_checked_list",
    "read_kind",
    "read_lists",
]
