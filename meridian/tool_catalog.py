"""Lean MCP tool surface: a tool catalog plus search, describe and execute helpers.

Meridian exposes roughly 250 native MCP tools. Every client that lists them pays
for each name and JSON schema on every turn. This module holds the pure logic
for a smaller surface:

* ``build_catalog`` derives one ``ToolEntry`` per native tool (name, one-line
  summary, category, tier, JSON schema). Categories and tiers are rules over
  the tool name, so the catalog is deterministic and needs no hand-kept table.
* ``search_tools``, ``describe_tools`` and ``execute_tool`` are the three
  meta-tools. Every function takes an explicit ``allowed`` set and fails
  closed: a name outside that set is reported as unavailable, never as
  forbidden, so hidden tools cannot be enumerated. An empty set exposes
  nothing, and ``None`` is rejected rather than read as "all tools".
* ``LEAN_PROFILE_TOOL_NAMES`` and ``ROLE_PROFILE_TOOL_NAMES`` define the
  profiles that ``meridian.mcp_profiles.get_tool_allowlist`` resolves.

There is no code sandbox in v1. ``execute_tool`` only dispatches to a tool that
the profile already exposes, through a caller-supplied dispatcher.
"""
from __future__ import annotations

import copy
import math
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

LEAN_PROFILE = "lean"

SEARCH_TOOLS = "search_tools"
DESCRIBE_TOOLS = "describe_tools"
EXECUTE_TOOL = "execute_tool"
META_TOOL_NAMES: frozenset[str] = frozenset({SEARCH_TOOLS, DESCRIBE_TOOLS, EXECUTE_TOOL})

LEAN_CORE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "start_session",
        "claim_sprint_item",
        "complete_sprint_item",
        "log_task",
        "generate_handoff",
    }
)
LEAN_PROFILE_TOOL_NAMES: frozenset[str] = LEAN_CORE_TOOL_NAMES | META_TOOL_NAMES

_ROLE_EXTRA_TOOL_NAMES: dict[str, frozenset[str]] = {
    "executor": frozenset(
        {
            "get_sprint_progress",
            "get_sprint_items",
            "claim_file",
            "release_file",
            "get_file_claims",
            "prospect_symbol",
            "add_note",
            "request_hitl",
            "get_hitl_request",
            "get_session_brief",
            "load_handoff",
            "get_pinned_decisions",
        }
    ),
    "planner": frozenset(
        {
            "get_planning_brief",
            "get_parallelizable_groups",
            "get_sprint_items",
            "add_sprint_item",
            "update_sprint_item",
            "set_sprint",
            "pin_decision",
            "paper_search",
            "capture_research_finding",
            "search_all",
            "get_context_block",
            "checkpoint",
            "get_session_brief",
            "request_hitl",
        }
    ),
    "verifier": frozenset(
        {
            "get_sprint_items",
            "get_sprint_progress",
            "get_sprint_item_pointers",
            "resolve_sprint_item_pointers",
            "verify_handoff_token",
            "load_handoff",
            "get_capability_manifest",
            "run_verification",
            "get_tasks",
            "search_tasks",
            "get_findings",
            "get_session_log",
            "request_hitl",
            "get_hitl_request",
        }
    ),
}
ROLE_PROFILE_TOOL_NAMES: dict[str, frozenset[str]] = {
    role: LEAN_PROFILE_TOOL_NAMES | extra for role, extra in _ROLE_EXTRA_TOOL_NAMES.items()
}

MAX_QUERY_CHARS = 200
MAX_LIMIT = 50
DEFAULT_LIMIT = 10
MAX_DESCRIBE_NAMES = 25
MAX_NAME_CHARS = 128
DETAIL_LEVELS: tuple[str, ...] = ("names", "summary", "full")
_SUMMARY_MAX_CHARS = 160
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,%d}$" % MAX_NAME_CHARS)
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Ordered rules: the first category whose keyword set intersects the tool
# name's tokens wins. Order resolves overlaps (e.g. "claim_docx_region" is a
# document tool, "claim_sprint_item" is a sprint tool).
_CATEGORY_RULES: tuple[tuple[str, frozenset[str]], ...] = (
    ("workspace", frozenset({"workspace", "workspaces"})),
    (
        "sprint",
        frozenset(
            {"sprint", "sprints", "wave", "waves", "item", "items", "parallelizable", "subtask", "subtasks"}
        ),
    ),
    ("hitl", frozenset({"hitl"})),
    ("messaging", frozenset({"message", "messages", "send", "receive", "idle"})),
    ("tasks", frozenset({"task", "tasks"})),
    ("logs", frozenset({"log", "logs"})),
    ("proposals", frozenset({"proposal", "proposals", "lineage", "successor"})),
    ("decisions", frozenset({"decision", "decisions", "pin", "pinned", "assumption", "assumptions"})),
    ("notes", frozenset({"note", "notes"})),
    (
        "documents",
        frozenset(
            {
                "docx", "document", "documents", "paragraph", "paragraphs", "equation", "equations",
                "figure", "figures", "table", "tables", "citation", "citations", "bibliography",
                "latex", "contract", "contracts", "caption", "captions", "ingest", "flag", "flags",
                "section", "sections", "md", "anchor", "anchors", "tex",
            }
        ),
    ),
    (
        "file-locking",
        frozenset({"claim", "claims", "release", "lock", "locks", "worktree", "worktrees", "lease", "leases"}),
    ),
    (
        "outputs",
        frozenset({"output", "outputs", "artifact", "artifacts", "provenance", "derivative", "derivatives", "annotate"}),
    ),
    ("experiments", frozenset({"experiment", "experiments"})),
    (
        "research",
        frozenset(
            {
                "research", "paper", "papers", "search", "searches", "github", "social", "zotero",
                "finding", "findings", "insight", "insights", "synthesis", "watchlist", "watchlists",
                "capture", "blog", "posts",
            }
        ),
    ),
    (
        "code",
        frozenset(
            {
                "symbol", "symbols", "usages", "prospect", "code", "graph", "semantic", "hotspots",
                "index", "indexed", "snapshot",
            }
        ),
    ),
    ("plugins", frozenset({"plugin", "plugins", "hook", "hooks", "tunnel", "tunnels"})),
    (
        "config",
        frozenset(
            {
                "config", "settings", "setting", "executor", "profile", "profiles", "layer", "layers",
                "capability", "capabilities", "manifest", "manifests", "instructions", "repo", "repos", "agent",
            }
        ),
    ),
    (
        "session",
        frozenset(
            {"session", "sessions", "handoff", "handoffs", "checkpoint", "recovery", "resumable", "brief", "context"}
        ),
    ),
    ("project", frozenset({"project", "projects", "goal", "goals", "north", "star", "milestones", "parent", "rename"})),
    ("external", frozenset({"external", "job", "jobs", "remote", "issue", "issues", "webhook"})),
    (
        "analysis",
        frozenset({"analyze", "analysis", "efficiency", "metrics", "model", "verify", "verification"}),
    ),
)
CATEGORIES: tuple[str, ...] = tuple(sorted({c for c, _ in _CATEGORY_RULES} | {"meta", "misc"}))

# Built-in schemas for the meta-tools. They are not native Meridian tools, so
# they are added to the catalog here rather than read from the tool list.
_META_SPECS: dict[str, tuple[str, dict[str, Any]]] = {
    SEARCH_TOOLS: (
        "Search the tools this profile exposes by keyword, category or name.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": MAX_QUERY_CHARS},
                "detail": {"type": "string", "enum": list(DETAIL_LEVELS)},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
                "category": {"type": "string", "enum": list(CATEGORIES)},
            },
        },
    ),
    DESCRIBE_TOOLS: (
        "Return the full JSON schema for the named tools this profile exposes.",
        {
            "type": "object",
            "properties": {
                "names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": MAX_DESCRIBE_NAMES,
                },
            },
            "required": ["names"],
        },
    ),
    EXECUTE_TOOL: (
        "Run one tool this profile exposes, by name, with an arguments object.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "arguments": {"type": "object"},
            },
            "required": ["name"],
        },
    ),
}


class ToolCatalogError(ValueError):
    """Invalid input to a catalog function. The message is safe to return to a client."""


class ToolUnavailableError(ToolCatalogError):
    """The tool is not exposed by the active profile (or is a meta-tool)."""


@dataclass(frozen=True)
class ToolEntry:
    name: str
    summary: str
    category: str
    tier: str
    tags: tuple[str, ...]
    input_schema: Mapping[str, Any]


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _category_for(name: str) -> str:
    tokens = set(_tokens(name))
    for category, keywords in _CATEGORY_RULES:
        if tokens & keywords:
            return category
    return "misc"


def _summary_for(description: Any) -> str:
    text = " ".join(str(description or "").split())
    if not text:
        return ""
    for i in range(len(text) - 1):
        if text[i] == "." and text[i + 1] == " ":
            return text[: i + 1]
    if len(text) <= _SUMMARY_MAX_CHARS:
        return text
    return text[: _SUMMARY_MAX_CHARS - 3].rstrip() + "..."


def _entry(name: str, summary: str, schema: Mapping[str, Any], category: str) -> ToolEntry:
    tier = "core" if name in LEAN_CORE_TOOL_NAMES or name in META_TOOL_NAMES else "deferred"
    tags = tuple(sorted(set(_tokens(name)) | {category}))
    return ToolEntry(
        name=name,
        summary=summary,
        category=category,
        tier=tier,
        tags=tags,
        input_schema=schema,
    )


def build_catalog(tools: Iterable[Mapping[str, Any]] | None) -> dict[str, ToolEntry]:
    """Return ``{name: ToolEntry}`` for the native tools plus the three meta-tools.

    Malformed entries, duplicate names and names outside a conservative
    character set are skipped. A native tool cannot shadow a meta-tool name.
    """
    catalog: dict[str, ToolEntry] = {}
    for name, (summary, schema) in _META_SPECS.items():
        catalog[name] = _entry(name, summary, schema, "meta")
    for tool in tools or ():
        if not isinstance(tool, Mapping):
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name) or name in catalog:
            continue
        schema = tool.get("inputSchema")
        if not isinstance(schema, Mapping):
            schema = {"type": "object"}
        catalog[name] = _entry(name, _summary_for(tool.get("description")), schema, _category_for(name))
    return catalog


def _require_allowed(allowed: Any) -> frozenset[str]:
    if not isinstance(allowed, (set, frozenset)):
        raise ToolCatalogError(
            "allowed must be an explicit set of tool names; None and other types are "
            "rejected rather than treated as all tools"
        )
    if not all(isinstance(name, str) for name in allowed):
        raise ToolCatalogError("allowed must contain only tool names")
    return frozenset(allowed)


def _visible(catalog: Mapping[str, ToolEntry], allowed: Any) -> dict[str, ToolEntry]:
    """Entries exposed by *allowed*. Anything not in both sets is invisible."""
    names = _require_allowed(allowed)
    return {name: catalog[name] for name in names if name in catalog}


def _check_detail(detail: Any) -> str:
    if detail not in DETAIL_LEVELS:
        raise ToolCatalogError(f"detail must be one of {', '.join(DETAIL_LEVELS)}")
    return detail


def _check_limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ToolCatalogError(f"limit must be an integer from 1 to {MAX_LIMIT}")
    return limit


def _check_category(category: Any) -> str | None:
    if category is None:
        return None
    if category not in CATEGORIES:
        raise ToolCatalogError(f"unknown category; expected one of {', '.join(CATEGORIES)}")
    return category


def _check_query(query: Any) -> str:
    if query is None:
        return ""
    if not isinstance(query, str):
        raise ToolCatalogError("query must be a string")
    if len(query) > MAX_QUERY_CHARS:
        raise ToolCatalogError(f"query must be at most {MAX_QUERY_CHARS} characters")
    return query.strip()


def _render(entry: ToolEntry, detail: str) -> dict[str, Any]:
    if detail == "names":
        return {"name": entry.name}
    rendered: dict[str, Any] = {
        "name": entry.name,
        "summary": entry.summary,
        "category": entry.category,
        "tier": entry.tier,
    }
    if detail == "full":
        rendered["inputSchema"] = copy.deepcopy(dict(entry.input_schema))
    return rendered


class _Bm25:
    """Small in-memory BM25 ranker over the catalog. Scores use catalog-wide IDF."""

    _K1 = 1.5
    _B = 0.75

    def __init__(self, docs: Mapping[str, list[str]]) -> None:
        self._tf = {name: Counter(tokens) for name, tokens in docs.items()}
        self._len = {name: len(tokens) for name, tokens in docs.items()}
        n = len(docs)
        self._avgdl = (sum(self._len.values()) / n) if n else 0.0
        df: Counter[str] = Counter()
        for tokens in docs.values():
            df.update(set(tokens))
        self._idf = {term: math.log(1.0 + (n - d + 0.5) / (d + 0.5)) for term, d in df.items()}

    def score(self, query_tokens: Iterable[str], name: str) -> float:
        tf = self._tf[name]
        dl = self._len[name]
        norm = self._K1 * (1.0 - self._B + self._B * dl / self._avgdl) if self._avgdl else self._K1
        total = 0.0
        for term in set(query_tokens):
            freq = tf.get(term, 0)
            if freq:
                total += self._idf.get(term, 0.0) * freq * (self._K1 + 1.0) / (freq + norm)
        return total


def _index(catalog: Mapping[str, ToolEntry]) -> _Bm25:
    docs: dict[str, list[str]] = {}
    for name, entry in catalog.items():
        # The name is weighted twice so a query for "claim sprint item" ranks
        # claim_sprint_item above tools that only mention one of the words.
        docs[name] = _tokens(name) * 2 + _tokens(entry.category) + _tokens(entry.summary)
    return _Bm25(docs)


def search_tools(
    catalog: Mapping[str, ToolEntry],
    allowed: Any,
    *,
    query: Any = "",
    detail: Any = "names",
    limit: Any = DEFAULT_LIMIT,
    category: Any = None,
) -> dict[str, Any]:
    """Rank the tools *allowed* exposes against *query*, or list them when no query is given."""
    visible = _visible(catalog, allowed)
    text = _check_query(query)
    detail = _check_detail(detail)
    limit = _check_limit(limit)
    category = _check_category(category)

    candidates = [e for e in visible.values() if category is None or e.category == category]
    if text:
        query_tokens = _tokens(text)
        bm25 = _index(catalog)
        scored = []
        for entry in candidates:
            score = bm25.score(query_tokens, entry.name)
            if score > 0:
                scored.append((-score, entry.name))
        scored.sort()
        names = [name for _, name in scored]
    else:
        names = sorted(entry.name for entry in candidates)

    return {
        "detail": detail,
        "total_matches": len(names),
        "results": [_render(catalog[name], detail) for name in names[:limit]],
    }


def describe_tools(
    catalog: Mapping[str, ToolEntry],
    allowed: Any,
    names: Any,
) -> dict[str, Any]:
    """Return full schemas for *names* that are exposed. Others are reported as unavailable.

    Hidden and nonexistent names share the ``unavailable`` bucket, so a caller
    cannot tell which hidden tools exist.
    """
    visible = _visible(catalog, allowed)
    if not isinstance(names, (list, tuple)) or not names or len(names) > MAX_DESCRIBE_NAMES:
        raise ToolCatalogError(f"names must be a list of 1 to {MAX_DESCRIBE_NAMES} tool names")
    found: list[dict[str, Any]] = []
    unavailable: list[str] = []
    seen: set[str] = set()
    for name in names:
        if not isinstance(name, str) or len(name) > MAX_NAME_CHARS:
            raise ToolCatalogError("names must contain only tool-name strings")
        if name in seen:
            continue
        seen.add(name)
        entry = visible.get(name)
        if entry is None:
            unavailable.append(name)
        else:
            found.append(_render(entry, "full"))
    return {"tools": found, "unavailable": unavailable}


def execute_tool(
    catalog: Mapping[str, ToolEntry],
    allowed: Any,
    name: Any,
    arguments: Any,
    dispatch: Callable[[str, dict[str, Any]], Any],
) -> Any:
    """Dispatch *name* with *arguments* when the profile exposes it.

    Meta-tools are refused so execute_tool cannot nest. Required arguments from
    the tool's schema are checked before dispatch. Errors raised by *dispatch*
    propagate unchanged.
    """
    visible = _visible(catalog, allowed)
    if not isinstance(name, str) or name in META_TOOL_NAMES or name not in visible:
        raise ToolUnavailableError("tool is not available in this profile")
    if not callable(dispatch):
        raise ToolCatalogError("dispatch must be callable")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, Mapping):
        raise ToolCatalogError("arguments must be an object")
    required = visible[name].input_schema.get("required") or []
    if not isinstance(required, (list, tuple)):
        raise ToolCatalogError("tool schema has a malformed required list")
    missing = sorted(str(key) for key in required if key not in arguments)
    if missing:
        raise ToolCatalogError("missing required argument(s): " + ", ".join(missing))
    return dispatch(name, dict(arguments))


def category_index(catalog: Mapping[str, ToolEntry], allowed: Any) -> dict[str, int]:
    """Count the tools *allowed* exposes per category, for a compact orientation index."""
    counts = Counter(entry.category for entry in _visible(catalog, allowed).values())
    return {category: counts[category] for category in sorted(counts)}
