"""MCP server over stdio for one InvenTree instance.

Read tools answer from InvenTree and never write. The write-planning tools store a plan
and write nothing. No tool here approves or runs a plan:

- Approval is a human step in a terminal: meridian_inventree.approve_cli.
- Running an approved plan is write_tools.run_plan, called by the owner.

Configuration comes only from the environment: INVENTREE_URL, INVENTREE_TOKEN and
MERIDIAN_INVENTREE_STATE. Importing this module reads no environment and makes no network
call. Startup (main) reads the InvenTree version once and reports it on stderr. Nothing is
printed to stdout, because stdout carries the MCP protocol.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, TextIO, TypeVar

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from meridian_inventree import read_tools, write_tools
from meridian_inventree.audit import AuditLog
from meridian_inventree.client import InvenTreeClient, InvenTreeError, Transport
from meridian_inventree.plans import ALLOWED_OPS, MAX_OPERATIONS, PlanError, PlanStore
from meridian_inventree.write_params import as_client_key
from meridian_inventree.write_tools import (
    check_write_version,
    reported_version as _reported_version,
    version_supported,
)

logger = logging.getLogger("meridian_inventree.server")

SERVER_NAME: Final[str] = "meridian-inventree"
ENV_URL: Final[str] = "INVENTREE_URL"
ENV_TOKEN: Final[str] = "INVENTREE_TOKEN"
ENV_STATE: Final[str] = "MERIDIAN_INVENTREE_STATE"
PLANS_SUBDIR: Final[str] = "plans"
AUDIT_FILE: Final[str] = "audit.jsonl"  # the same log approve_cli and run_plan append to
EXIT_CONFIG: Final[int] = 2
_PACKAGE_DIR: Final[Path] = Path(__file__).resolve().parent
_OPERATION_KEYS: Final[frozenset[str]] = frozenset({"op", "client_key", "params"})
_NEXT_STEP: Final[str] = (
    "Nothing has been written. The owner reviews and approves this plan in a terminal "
    "with meridian_inventree.approve_cli. An agent cannot approve or run it."
)
INSTRUCTIONS: Final[str] = (
    "Read tools query one InvenTree instance and write nothing. inventree_propose stores a "
    "plan of write operations and writes nothing. No tool here approves or runs a plan: the "
    "owner approves in a terminal, and only then can the plan run. Use inventree_plan_status "
    "to see a plan's state."
)

# Open item 2 in docs/plans/inventree-mcp-plan.md: the owner has not named the InvenTree
# version yet. Each entry is a prefix of the version string that GET /api/ reports, ending in
# a dot (for example "1.2." once the owner names it). While this is empty, every write is
# refused: that is the fail-closed default. VERIFY-LIVE against the owner's instance.
SUPPORTED_VERSION_PREFIXES: tuple[str, ...] = ()

_T = TypeVar("_T")


class ConfigError(Exception):
    """Startup configuration is missing or unsafe. The message names a variable, never its value."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class Config:
    base_url: str
    token: str = field(repr=False)
    state_dir: Path


@dataclass(frozen=True)
class Runtime:
    config: Config
    client: InvenTreeClient
    store: PlanStore


@dataclass(frozen=True)
class App:
    mcp: FastMCP
    tools: Mapping[str, Callable[..., Any]]
    runtime: Runtime


# ---------------------------------------------------------------- configuration


def default_state_dir(environ: Mapping[str, str]) -> Path:
    """The per-user state folder, used when MERIDIAN_INVENTREE_STATE is not set."""
    if sys.platform == "win32":
        base = environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "meridian" / "inventree"


def load_config(environ: Mapping[str, str]) -> Config:
    """Read and check the three settings. Raises ConfigError with a clear message."""
    base_url = environ.get(ENV_URL, "").strip()
    if not base_url:
        raise ConfigError(
            "missing_url",
            f"{ENV_URL} is not set. Set it to the base URL of your InvenTree instance, "
            "for example https://inventree.example.com",
        )
    token = environ.get(ENV_TOKEN, "").strip()
    if not token:
        raise ConfigError(
            "missing_token",
            f"{ENV_TOKEN} is not set. Create an API token for an InvenTree user and set "
            f"{ENV_TOKEN} in the environment. Do not write it to a file.",
        )
    raw_state = environ.get(ENV_STATE, "").strip()
    state_dir = (Path(raw_state).expanduser() if raw_state else default_state_dir(environ)).resolve()
    _refuse_state_in_repo(state_dir)
    return Config(base_url=base_url, token=token, state_dir=state_dir)


def _repo_root(start: Path) -> Path | None:
    """The nearest folder at or above start that holds a .git entry, or None outside a checkout."""
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _refuse_state_in_repo(state_dir: Path) -> None:
    root = _repo_root(_PACKAGE_DIR)
    if root is None:
        return
    try:
        state_dir.relative_to(root)
    except ValueError:
        return
    raise ConfigError(
        "state_in_repo",
        f"{ENV_STATE} must point outside the repository. Use a folder under your user state directory.",
    )


def _build_client(config: Config, transport: Transport | None) -> InvenTreeClient:
    try:
        return InvenTreeClient(config.base_url, config.token, transport)
    except InvenTreeError as exc:
        if exc.code == "invalid_token":
            raise ConfigError("bad_token", f"{ENV_TOKEN} is not usable: {exc.message}") from None
        raise ConfigError(exc.code, f"{ENV_URL} is not usable: {exc.message}") from None
    except ValueError as exc:
        raise ConfigError("bad_token", f"{ENV_TOKEN} is not usable: {exc}") from None


# ---------------------------------------------------------------- version and write gate


def _write_gate(rt: Runtime) -> None:
    """Refuse writes unless the live InvenTree version is supported. Runs before any preview.

    The range check lives in write_tools.check_write_version, which run_plan also uses.
    """
    try:
        check_write_version(rt.client, SUPPORTED_VERSION_PREFIXES)
    except write_tools.PlanRefused as exc:
        raise ToolError(f"{exc.code}: {exc.message}") from None


# ---------------------------------------------------------------- tool bodies


def _guard(name: str, action: Callable[[], _T]) -> _T:
    """Run one tool body and turn every failure into a ToolError with a stable code."""
    try:
        return action()
    except ToolError:
        raise
    except InvenTreeError as exc:  # includes WriteValidationError
        raise ToolError(f"{exc.code}: {exc.message}") from None
    except PlanError as exc:
        raise ToolError(f"{exc.code}: {exc.message}") from None
    except (TypeError, ValueError) as exc:
        raise ToolError(f"invalid_argument: {exc}") from None
    except Exception:  # noqa: BLE001 - last resort; the exception text is never echoed
        logger.error("tool %s failed with an internal error", name)
        raise ToolError("internal_error: the server hit an unexpected error; see the server log") from None


def _shape_check(operations: object) -> list[dict[str, Any]]:
    """Cheap checks that run before any request: count, shape, allowed names, unique keys."""
    if not isinstance(operations, list):
        raise ToolError("invalid_operation: operations must be a list")
    if not operations:
        raise ToolError("empty_plan: a plan needs at least one operation")
    if len(operations) > MAX_OPERATIONS:
        raise ToolError(
            f"too_many_operations: a plan holds at most {MAX_OPERATIONS} operations; split the work into more plans"
        )
    seen: set[str] = set()
    for index, item in enumerate(operations):
        if not isinstance(item, dict) or set(item) != _OPERATION_KEYS:
            raise ToolError(f"invalid_operation: operation {index} must have exactly the keys op, client_key, params")
        name = item["op"]
        if not isinstance(name, str) or name not in ALLOWED_OPS:
            raise ToolError(f"unknown_operation: operation {index} is not an allowed write operation")
        key = as_client_key(item["client_key"])
        if key in seen:
            raise ToolError(f"duplicate_client_key: operation {index} repeats a client_key already used in this plan")
        seen.add(key)
        if not isinstance(item["params"], dict):
            raise ToolError(f"invalid_operation: operation {index} params must be an object")
    return operations


def _describe(index: int, item: Mapping[str, Any], preview: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "index": index,
        "op": item["op"],
        "client_key": item["client_key"],
        "method": preview.get("method"),
        "path": preview.get("path"),
        "before": preview.get("before"),
        "after": preview.get("after"),
    }


def _propose(rt: Runtime, operations: object) -> dict[str, Any]:
    ops = _shape_check(operations)
    _write_gate(rt)
    previews = [write_tools.preview(item["op"], item["params"], rt.client) for item in ops]
    plan = rt.store.propose(ops, previews)
    return {
        "plan_id": plan["plan_id"],
        "status": plan["status"],
        "nothing_written": True,
        "operation_count": len(plan["operations"]),
        "operations": [_describe(i, item, preview) for i, (item, preview) in enumerate(zip(ops, previews))],
        "next_step": _NEXT_STEP,
    }


def _plan_status(rt: Runtime, plan_id: str) -> dict[str, Any]:
    plan = rt.store.get(plan_id)
    approval = plan.get("approval")
    # The plan hash and the approved hash are withheld. The owner reads the hash in the
    # approval terminal, so an agent never holds the value that approval needs.
    approved_at = approval.get("approved_at") if isinstance(approval, dict) else None
    return {
        "plan_id": plan["plan_id"],
        "status": plan["status"],
        "created_at": plan.get("created_at"),
        "operation_count": len(plan["operations"]),
        "approved_at": approved_at,
        "executed_at": plan.get("executed_at"),
        "results": plan.get("results"),
    }


def _check_version(rt: Runtime) -> dict[str, Any]:
    data = rt.client.check_version()
    reported = _reported_version(data)
    return {
        "reported_version": reported,
        "api_version": data.get("apiVersion"),  # VERIFY-LIVE: key name in GET /api/
        "writes_allowed": version_supported(reported, SUPPORTED_VERSION_PREFIXES),
        "supported_prefixes": list(SUPPORTED_VERSION_PREFIXES),
    }


# ---------------------------------------------------------------- registration


def _register(rt: Runtime) -> tuple[FastMCP, dict[str, Callable[..., Any]]]:
    mcp = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS)
    tools: dict[str, Callable[..., Any]] = {}

    def tool(name: str, *, read_only: bool) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            mcp.tool(name=name, annotations=ToolAnnotations(readOnlyHint=read_only))(fn)
            tools[name] = fn
            return fn

        return register

    @tool("inventree_search_parts", read_only=True)
    def inventree_search_parts(
        query: str = "",
        category: int | str | None = None,
        limit: int = read_tools.SEARCH_LIMIT_DEFAULT,
    ) -> dict[str, Any]:
        """Search parts by text, optionally inside one category. Read only. Results are capped at limit (1 to 200); a capped result says so in notes."""
        return _guard(
            "inventree_search_parts",
            lambda: read_tools.search_parts(rt.client, query, category, limit),
        )

    @tool("inventree_get_part", read_only=True)
    def inventree_get_part(pk: int | str) -> dict[str, Any]:
        """Return one part by primary key. Read only."""
        return _guard("inventree_get_part", lambda: read_tools.get_part(rt.client, pk))

    @tool("inventree_list_categories", read_only=True)
    def inventree_list_categories(parent: int | str | None = None) -> dict[str, Any]:
        """List part categories, or only the children of parent when it is given. Read only."""
        return _guard("inventree_list_categories", lambda: read_tools.list_categories(rt.client, parent))

    @tool("inventree_search_stock", read_only=True)
    def inventree_search_stock(
        query: str = "",
        location: int | str | None = None,
        cascade: bool = True,
    ) -> dict[str, Any]:
        """Search stock items by text, optionally inside one location. With cascade true, child locations are included. Read only."""
        return _guard(
            "inventree_search_stock",
            lambda: read_tools.search_stock(rt.client, query, location, cascade),
        )

    @tool("inventree_get_stock_item", read_only=True)
    def inventree_get_stock_item(pk: int | str) -> dict[str, Any]:
        """Return one stock item by primary key. Read only."""
        return _guard("inventree_get_stock_item", lambda: read_tools.get_stock_item(rt.client, pk))

    @tool("inventree_list_locations", read_only=True)
    def inventree_list_locations(parent: int | str | None = None) -> dict[str, Any]:
        """List stock locations, or only the children of parent when it is given. Read only."""
        return _guard("inventree_list_locations", lambda: read_tools.list_locations(rt.client, parent))

    @tool("inventree_roles", read_only=True)
    def inventree_roles() -> dict[str, Any]:
        """Return the roles of the token's user, so you can see what this token may do. Read only."""
        return _guard("inventree_roles", lambda: read_tools.roles(rt.client))

    @tool("inventree_check_version", read_only=True)
    def inventree_check_version() -> dict[str, Any]:
        """Read the InvenTree version and report whether writes are allowed. Read only."""
        return _guard("inventree_check_version", lambda: _check_version(rt))

    @tool("inventree_propose", read_only=False)
    def inventree_propose(operations: list[dict[str, Any]]) -> dict[str, Any]:
        """Validate write operations, read the live state each one would change, and store a plan. Nothing is written to InvenTree. Each operation is {"op", "client_key", "params"}; op is create_part, update_part, create_location, create_stock_item, transfer_stock or adjust_stock_quantity. A plan holds at most 50 operations. The owner must approve the plan in a terminal before it can run."""
        return _guard("inventree_propose", lambda: _propose(rt, operations))

    @tool("inventree_plan_status", read_only=True)
    def inventree_plan_status(plan_id: str) -> dict[str, Any]:
        """Show a plan's status, approval time and results. The approval hash is not shown. Read only."""
        return _guard("inventree_plan_status", lambda: _plan_status(rt, plan_id))

    return mcp, tools


# ---------------------------------------------------------------- entry points


def build_app(environ: Mapping[str, str] | None = None, *, transport: Transport | None = None) -> App:
    """Build the server. Makes no network call. Raises ConfigError for missing or unsafe settings.

    environ defaults to the process environment. transport is injectable so tests never
    reach the network.
    """
    config = load_config(os.environ if environ is None else environ)
    rt = Runtime(
        config=config,
        client=_build_client(config, transport),
        store=PlanStore(config.state_dir / PLANS_SUBDIR, audit=AuditLog(config.state_dir / AUDIT_FILE)),
    )
    mcp, tools = _register(rt)
    return App(mcp=mcp, tools=tools, runtime=rt)


def create_server(environ: Mapping[str, str] | None = None, *, transport: Transport | None = None) -> FastMCP:
    """Return the configured FastMCP server. Makes no network call."""
    return build_app(environ, transport=transport).mcp


def _startup_line(rt: Runtime) -> str:
    prefix = "meridian-inventree:"
    try:
        data = rt.client.check_version()
    except InvenTreeError as exc:
        return f"{prefix} InvenTree version check failed ({exc.code}); writes are refused until it passes"
    reported = _reported_version(data)
    allowed = version_supported(reported, SUPPORTED_VERSION_PREFIXES)
    return f"{prefix} InvenTree reports version {reported or 'unknown'}; writes {'allowed' if allowed else 'refused'}"


def _serve_stdio(mcp: FastMCP) -> None:
    mcp.run(transport="stdio")


def main(
    environ: Mapping[str, str] | None = None,
    *,
    transport: Transport | None = None,
    stderr: TextIO | None = None,
    serve: Callable[[FastMCP], None] | None = None,
) -> int:
    """Start the stdio server. Returns an exit code; 2 means a configuration problem.

    A configuration problem prints one line to stderr and exits before anything is served.
    serve replaces the stdio loop in tests.
    """
    err = sys.stderr if stderr is None else stderr
    try:
        app = build_app(environ, transport=transport)
    except ConfigError as exc:
        print(f"meridian-inventree: {exc.message}", file=err)
        return EXIT_CONFIG
    except OSError:
        print(f"meridian-inventree: cannot use the state folder; check {ENV_STATE} and its permissions", file=err)
        return EXIT_CONFIG
    print(_startup_line(app.runtime), file=err)
    (serve or _serve_stdio)(app.mcp)
    return 0
