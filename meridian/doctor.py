"""be5837bc -- Doctor check: MCP tool-list token tax and duplicate server
registrations.

A 2026-09-29 usage audit found every workflow agent in this repo starting at
roughly 75K tokens of context (the main session ~125K) largely from the
MCP tool-list, which gets re-sent on every single request -- and that a
large chunk of it is pure waste: one observed session's deferred-tool
listing carried 1,030 tool names, of which 285 of 724 *base* tool names were
duplicated across more than one MCP server registration (e.g. the same
Meridian server registered twice -- once as ``mcp__meridian`` and once under
a UUID-prefixed connector slot name -- codebase-memory registered twice, and
``serena``/``meridian-extract`` exposing overlapping tool sets). This module
is the diagnostic that finds and reports that, so a human can trim their own
MCP config/connector list.

**Where this fits.** This repo already has one "doctor" concept:
:meth:`meridian.local_runner.LocalRunner.doctor` -- several small, named,
bounded checks (``state_dir_writable``, ``lease_broker``, ...) collapsed into
one :class:`~meridian.local_runner.DoctorReport` (``{scope, generated_at,
healthy, checks: [{name, severity, detail}]}``). This module is a *second*
home for doctor-style checks that are not scoped to one running local-runner
instance -- it reuses that exact schema (imported, not re-defined, so the
two never drift apart) and follows the same "advisory, bounded, dependency-
free" posture.

**Why this takes a manifest *snapshot* as input, not live introspection.**
There is no code path anywhere in this repository that can enumerate an
external MCP client's (Claude Code's, Claude Desktop's, Cursor's...)
*currently configured* tool list from the inside -- that list lives in the
client process, not in anything this server can query. ``meridian/
tool_manifest.py`` (``build_tool_manifest``) is the closest existing
precedent, but it only describes *this server's own* built-in tools, passed
in by the MCP handler that already has them in hand -- it says nothing about
sibling servers (Serena, codebase-memory, GitHub, ...) sharing the same
client session. So, matching that same "pure function over data the caller
already has" shape, :func:`check_mcp_tool_manifest_tax` takes a *snapshot* --
whatever the caller captured from their own client's tool list (a dumped
``tools/list`` response, a copy of a session's own deferred-tool listing,
the ``mcpServers`` block of an MCP config file cross-referenced with each
server's advertised tools, ...) -- and reports on THAT, rather than
guessing at a data source that does not exist in this codebase.

**Token estimate.** This codebase has no tokenizer dependency anywhere (see
``meridian/db/__init__.py``'s ``WORKER_CONTEXT_XML_BUDGET_CHARS`` comment for
the same reasoning) -- adding ``tiktoken`` for one diagnostic would be a new
dependency for a number that is only ever used *comparatively* ("server A is
costing you 3x server B"), never billed or enforced. ``CHARS_PER_TOKEN_
ESTIMATE`` is the same "~4 chars/token for English prose" rule of thumb
already used there: a deterministic, dependency-free approximation, not an
exact conversion.

**Advisory only.** Nothing in this module ever writes to a file, an MCP
config, or Meridian project state. Every function here is a pure
transformation from an in-memory snapshot to an in-memory
:class:`~meridian.local_runner.DoctorReport` / list of findings; printing a
report (the small CLI at the bottom) or handing it back over MCP is the
caller's job.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# Re-exported, not re-defined: this module's reports use the EXACT same
# {name, severity, detail} / {scope, generated_at, healthy, checks} shape
# LocalRunner.doctor() already established, so the two schemas can never
# drift apart. See module docstring "Where this fits."
from .local_runner import DoctorCheck, DoctorReport

__all__ = [
    "DoctorCheck",
    "DoctorReport",
    "ToolInfo",
    "ServerToolSet",
    "DuplicateFinding",
    "CHARS_PER_TOKEN_ESTIMATE",
    "EXACT_DUPLICATE_JACCARD_THRESHOLD",
    "PARTIAL_OVERLAP_JACCARD_THRESHOLD",
    "parse_tool_name",
    "load_manifest_snapshot",
    "jaccard_similarity",
    "find_duplicate_registrations",
    "check_mcp_tool_manifest_tax",
    "run_doctor_checks",
]

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Rough, dependency-free token estimate -- see module docstring. Not exact;
# good enough for a *relative* "this server costs more than that one" signal.
CHARS_PER_TOKEN_ESTIMATE = 4

# Two server slots are flagged as the SAME underlying server registered
# under two different connector/slot names when their bare tool-name sets
# overlap at or above this Jaccard similarity -- e.g. "mcp__meridian__*" and
# "mcp__<uuid>__*" exposing byte-identical tool names, the audit's headline
# finding. 0.98 rather than 1.0 tolerates one or two tools present on one
# slot but not yet visible on the other (a mid-rollout deploy, a stale
# discovery cache -- see AGENTS.md's "Tool not found" != "tool doesn't
# exist" note) without missing an otherwise-obvious duplicate.
EXACT_DUPLICATE_JACCARD_THRESHOLD = 0.98

# Below the exact-duplicate threshold but still meaningfully overlapping
# (e.g. Serena vs. meridian-extract both exposing find_symbol/replace_
# symbol_body/find_referencing_symbols-shaped tools) gets a softer
# "overlapping tool set" flag instead of "same server twice" -- these are
# usually genuinely different servers/implementations that just cover
# similar ground, not a config mistake to blindly delete one side of.
PARTIAL_OVERLAP_JACCARD_THRESHOLD = 0.25

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _looks_uuid_like(slot_name: str) -> bool:
    """True for a bare-UUID connector/slot name (e.g. a claude.ai connector's
    auto-generated slot), the exact shape the audit calls out as the
    "duplicate" half of a same-server-twice registration -- as opposed to a
    human-chosen name like ``meridian`` or ``codebase-memory``."""
    return bool(_UUID_RE.match(slot_name.strip()))


# ---------------------------------------------------------------------------
# Manifest data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolInfo:
    """One tool's schema, as reported by its server -- BARE name (no
    ``mcp__<slot>__`` prefix; see :func:`parse_tool_name`)."""

    name: str
    description: str = ""
    input_schema: "Mapping[str, Any] | None" = None

    def estimated_tokens(self) -> int:
        payload = json.dumps(
            {
                "name": self.name,
                "description": self.description,
                "inputSchema": self.input_schema or {},
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        # Never report 0 for a real tool -- even a bare name costs a few
        # tokens of client-side schema/dispatch overhead.
        return max(1, len(payload) // CHARS_PER_TOKEN_ESTIMATE)


@dataclass(frozen=True)
class ServerToolSet:
    """Every tool one configured MCP server (one connector/config slot)
    exposes."""

    slot_name: str
    tools: "tuple[ToolInfo, ...]" = ()

    @property
    def bare_names(self) -> "frozenset[str]":
        return frozenset(t.name for t in self.tools if t.name)

    @property
    def estimated_tokens(self) -> int:
        return sum(t.estimated_tokens() for t in self.tools)


def parse_tool_name(full_name: str) -> "tuple[str | None, str]":
    """Split a flat, possibly slot-prefixed tool name into
    ``(slot_name, bare_name)``.

    Convention observed across every MCP-serving client this repo talks to
    (Claude Code's own deferred-tool listing, this repo's own AGENTS.md
    worked examples): a tool coming from a configured MCP server is
    namespaced as ``mcp__<slot>__<tool>`` -- the server's config/connector
    slot name, double-underscore-joined, ahead of its own tool name (e.g.
    ``mcp__meridian__start_session``). ``maxsplit=2`` keeps this correct even
    when the tool's own name legitimately contains ``__`` (unusual, but not
    disallowed). A name with no ``mcp__`` prefix is treated as a built-in /
    native (non-MCP) tool -- ``slot_name`` is ``None``.
    """
    if not full_name.startswith("mcp__"):
        return None, full_name
    parts = full_name.split("__", 2)
    if len(parts) < 3:
        # "mcp__something" with no third segment: malformed/truncated input
        # -- surface it rather than raising on real-world data.
        return (parts[1] if len(parts) > 1 else None), ""
    return parts[1], parts[2]


def _coerce_tool_info(raw: "Mapping[str, Any] | str") -> ToolInfo:
    if isinstance(raw, str):
        return ToolInfo(name=raw)
    return ToolInfo(
        name=str(raw.get("name") or raw.get("bare_name") or ""),
        description=str(raw.get("description") or ""),
        input_schema=raw.get("inputSchema") or raw.get("input_schema") or None,
    )


def _group_flat_tools(entries: "Sequence[Any]") -> "tuple[ServerToolSet, ...]":
    groups: "dict[str, list[ToolInfo]]" = {}
    order: "list[str]" = []
    for raw in entries:
        full_name = raw if isinstance(raw, str) else str((raw or {}).get("name") or "")
        slot, bare = parse_tool_name(full_name)
        slot = slot or "(no server prefix)"
        info = dataclasses.replace(_coerce_tool_info(raw), name=bare)
        if slot not in groups:
            groups[slot] = []
            order.append(slot)
        groups[slot].append(info)
    return tuple(ServerToolSet(slot_name=s, tools=tuple(groups[s])) for s in order)


def load_manifest_snapshot(
    snapshot: "Mapping[str, Any] | Sequence[Any]",
) -> "tuple[ServerToolSet, ...]":
    """Normalize a captured MCP tool-list snapshot into per-server tool sets.

    This repo has no existing on-disk format for "a client's configured MCP
    tool list" (see module docstring), so this accepts whichever of two
    natural shapes the caller has on hand:

    * **Pre-grouped**, already split by server -- ``{"servers": [{"slot_name"
      (or "server"/"name"): str, "tools": [...]}]}``, a bare mapping of
      ``{slot_name: [tools]}``, or just the bare list of server dicts. Each
      tool in ``tools`` is either ``{"name", "description"?, "inputSchema"?}``
      or a plain string (bare tool name).
    * **Flat**, exactly the shape of a live session's own deferred-tool
      listing or a raw ``tools/list`` dump -- ``{"tools": [...]}`` or the
      bare list -- grouped here by the ``mcp__<slot>__`` naming convention
      (:func:`parse_tool_name`). A tool with no ``mcp__`` prefix lands in a
      synthetic ``"(no server prefix)"`` group instead of being silently
      dropped, so nothing captured in a snapshot ever vanishes from the
      report.

    Returns ``()`` for an empty/falsy snapshot.
    """
    if not snapshot:
        return ()

    if isinstance(snapshot, Mapping):
        if "servers" in snapshot:
            entries: "Sequence[Any]" = snapshot["servers"] or []
        elif "tools" in snapshot:
            return _group_flat_tools(snapshot["tools"] or [])
        else:
            # Bare mapping of slot_name -> tools list.
            entries = [{"slot_name": k, "tools": v} for k, v in snapshot.items()]
    else:
        entries = list(snapshot)

    if not entries:
        return ()

    first = entries[0]
    if isinstance(first, Mapping) and "tools" in first:
        servers = []
        for entry in entries:
            slot = str(entry.get("slot_name") or entry.get("server") or entry.get("name") or "")
            tools = tuple(_coerce_tool_info(t) for t in (entry.get("tools") or []))
            servers.append(ServerToolSet(slot_name=slot, tools=tools))
        return tuple(servers)

    # Flat list of tool descriptors/strings with no "tools" wrapper key.
    return _group_flat_tools(entries)


# ---------------------------------------------------------------------------
# Duplicate-registration detection
# ---------------------------------------------------------------------------


def jaccard_similarity(a: "frozenset[str]", b: "frozenset[str]") -> float:
    """``|a n b| / |a u b|``; ``0.0`` when both are empty (no basis for
    similarity, never treated as "identical")."""
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


@dataclass(frozen=True)
class DuplicateFinding:
    slot_a: str
    slot_b: str
    shared_tool_count: int
    similarity: float
    kind: str  # "exact_duplicate_registration" | "overlapping_tool_set"
    suggested_fix: str

    def as_dict(self) -> "dict[str, Any]":
        return dataclasses.asdict(self)


def _suggest_exact_duplicate_fix(a: ServerToolSet, b: ServerToolSet, shared: int, sim: float) -> str:
    a_uuid, b_uuid = _looks_uuid_like(a.slot_name), _looks_uuid_like(b.slot_name)
    if a_uuid and not b_uuid:
        dup, keep = a.slot_name, b.slot_name
    elif b_uuid and not a_uuid:
        dup, keep = b.slot_name, a.slot_name
    else:
        dup, keep = None, None
    if dup is not None:
        return (
            f"Remove the {dup!r} connector entry (or its local MCP config entry) -- "
            f"it looks like an auto-generated/UUID-prefixed duplicate of {keep!r}, "
            f"which already exposes the same {shared} tool(s) ({sim:.0%} overlap)."
        )
    return (
        f"{a.slot_name!r} and {b.slot_name!r} expose essentially identical tool "
        f"sets ({shared} shared tool name(s), {sim:.0%} overlap) -- this looks "
        f"like the SAME underlying MCP server registered twice under two "
        f"connector/slot names. Remove one entry from your MCP client config "
        f"or connector settings and keep the other; name whichever one you "
        f"did not intentionally add twice."
    )


def _suggest_overlap_fix(a: ServerToolSet, b: ServerToolSet, shared: int, sim: float) -> str:
    return (
        f"{a.slot_name!r} and {b.slot_name!r} are probably different servers/"
        f"implementations but overlap on {shared} tool name(s) ({sim:.0%} of "
        f"their combined tool set) -- not necessarily a true duplicate. Review "
        f"whether both connectors are actually needed; keeping only the one you "
        f"use for this purpose would cut the repeated token tax."
    )


def find_duplicate_registrations(
    servers: "Sequence[ServerToolSet]",
) -> "tuple[DuplicateFinding, ...]":
    """Pairwise-compare every server's bare tool-name set and flag the ones
    that overlap enough to matter. Deterministic order: highest similarity
    first, then alphabetically by slot names (so output is stable for a
    given snapshot, independent of dict/set iteration order)."""
    findings: "list[DuplicateFinding]" = []
    for i in range(len(servers)):
        a = servers[i]
        if not a.tools:
            continue
        for j in range(i + 1, len(servers)):
            b = servers[j]
            if not b.tools:
                continue
            sim = jaccard_similarity(a.bare_names, b.bare_names)
            if sim < PARTIAL_OVERLAP_JACCARD_THRESHOLD:
                continue
            shared = len(a.bare_names & b.bare_names)
            if sim >= EXACT_DUPLICATE_JACCARD_THRESHOLD:
                kind = "exact_duplicate_registration"
                fix = _suggest_exact_duplicate_fix(a, b, shared, sim)
            else:
                kind = "overlapping_tool_set"
                fix = _suggest_overlap_fix(a, b, shared, sim)
            findings.append(
                DuplicateFinding(
                    slot_a=a.slot_name,
                    slot_b=b.slot_name,
                    shared_tool_count=shared,
                    similarity=sim,
                    kind=kind,
                    suggested_fix=fix,
                )
            )
    findings.sort(key=lambda f: (-f.similarity, f.slot_a, f.slot_b))
    return tuple(findings)


# ---------------------------------------------------------------------------
# The doctor check itself
# ---------------------------------------------------------------------------


def check_mcp_tool_manifest_tax(
    snapshot: "Mapping[str, Any] | Sequence[Any]",
    *,
    clock: "Callable[[], float]" = time.time,
) -> DoctorReport:
    """The be5837bc doctor check: total tool count, an estimated prefix-
    token cost per server, and any duplicate/overlapping server
    registrations found in *snapshot* (see :func:`load_manifest_snapshot`
    for accepted shapes). Advisory only -- never touches any file, MCP
    config, or Meridian project state; purely a report.
    """
    servers = load_manifest_snapshot(snapshot)
    checks: "list[DoctorCheck]" = []

    if not servers:
        checks.append(
            DoctorCheck(
                "mcp_tool_manifest_tax",
                "warn",
                "empty/no MCP tool-manifest snapshot supplied -- nothing to check "
                "(this repo cannot introspect a live client's tool list on its "
                "own; see meridian/doctor.py module docstring)",
            )
        )
        return DoctorReport(scope="mcp_tool_manifest_tax", generated_at=clock(), checks=tuple(checks))

    total_tools = sum(len(s.tools) for s in servers)
    total_estimated_tokens = sum(s.estimated_tokens for s in servers)
    per_server = ", ".join(
        f"{s.slot_name!r}: {len(s.tools)} tool(s) / ~{s.estimated_tokens} tok"
        for s in sorted(servers, key=lambda s: (-s.estimated_tokens, s.slot_name))
    )
    checks.append(
        DoctorCheck(
            "tool_count",
            "ok",
            f"{total_tools} tool(s) across {len(servers)} server slot(s). {per_server}",
        )
    )
    checks.append(
        DoctorCheck(
            "estimated_prefix_token_cost",
            "ok",
            f"~{total_estimated_tokens} tokens total, re-sent on every request "
            f"(rough chars/{CHARS_PER_TOKEN_ESTIMATE} estimate -- see module "
            f"docstring; this repo has no tokenizer dependency).",
        )
    )

    duplicates = find_duplicate_registrations(servers)
    if not duplicates:
        checks.append(
            DoctorCheck(
                "duplicate_server_registrations",
                "ok",
                f"no duplicate or overlapping server registrations detected "
                f"across {len(servers)} server slot(s).",
            )
        )
    else:
        for d in duplicates:
            checks.append(
                DoctorCheck(
                    name=f"duplicate_registration:{d.slot_a}~{d.slot_b}",
                    # Advisory only, never "fail": nothing is actually broken,
                    # it is just wasting token budget.
                    severity="warn",
                    detail=(
                        f"[{d.kind}] {d.slot_a!r} <-> {d.slot_b!r}: "
                        f"{d.shared_tool_count} shared tool name(s), "
                        f"{d.similarity:.0%} similarity. {d.suggested_fix}"
                    ),
                )
            )

    return DoctorReport(scope="mcp_tool_manifest_tax", generated_at=clock(), checks=tuple(checks))


def run_doctor_checks(
    *,
    mcp_manifest_snapshot: "Mapping[str, Any] | Sequence[Any] | None" = None,
    clock: "Callable[[], float]" = time.time,
) -> DoctorReport:
    """Aggregate every doctor check this module owns into one
    :class:`DoctorReport`, mirroring :meth:`LocalRunner.doctor`'s own
    aggregation pattern (several named sub-checks -> one report, ``healthy``
    iff none of them failed). Today this module owns exactly one check
    (MCP tool-list token tax / duplicate registrations); pass its required
    input via ``mcp_manifest_snapshot`` to run it. Omitting it yields a
    single ``warn``-severity "skipped" check instead of guessing at a data
    source this repo cannot actually reach (see module docstring) -- never a
    silent no-op with an empty, misleadingly-``healthy`` report. Structured
    so a future second doctor check slots in here the same way without
    disturbing this one's call sites.
    """
    if mcp_manifest_snapshot is None:
        checks: "tuple[DoctorCheck, ...]" = (
            DoctorCheck(
                "mcp_tool_manifest_tax",
                "warn",
                "skipped: no MCP tool-manifest snapshot supplied (pass "
                "mcp_manifest_snapshot=... -- this repo cannot introspect a "
                "live MCP client's tool list on its own).",
            ),
        )
    else:
        checks = check_mcp_tool_manifest_tax(mcp_manifest_snapshot, clock=clock).checks
    return DoctorReport(scope="meridian-doctor", generated_at=clock(), checks=checks)


# ---------------------------------------------------------------------------
# Standalone CLI -- ``python -m meridian.doctor <snapshot.json>``
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m meridian.doctor",
        description=(
            "Advisory-only MCP tool-list token-tax / duplicate-registration "
            "doctor check. Reads a captured tool-manifest snapshot (JSON) and "
            "prints a report; never writes to any file."
        ),
    )
    parser.add_argument(
        "snapshot",
        help=(
            "Path to a JSON file holding a captured MCP tool-list snapshot "
            "(see load_manifest_snapshot's docstring for accepted shapes), "
            "or '-' to read the JSON from stdin."
        ),
    )
    return parser


def main(argv: "Sequence[str] | None" = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    raw_text = sys.stdin.read() if args.snapshot == "-" else Path(args.snapshot).read_text(encoding="utf-8")
    snapshot = json.loads(raw_text)
    report = run_doctor_checks(mcp_manifest_snapshot=snapshot)
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0 if report.healthy else 1


if __name__ == "__main__":  # pragma: no cover -- exercised via main() in tests
    raise SystemExit(main())
