"""Executor tool-discovery compiler + pre-edit receipt gate (86b36617).

The gap this closes: an item's typed ``tool_requirements``
(:mod:`meridian.tool_requirements`) already reaches the executor as JSON
prose (embedded verbatim in ``handoff.build_item_briefing``'s
``<tool_requirements>`` clause and in ``executor_contract.build_executor_contract``'s
``allowed_tools``/``forbidden_tools``), and static per-tool availability is
already classified there too (:func:`meridian.executor_contract.default_tool_availability`).
But nothing actually COMPILES that typed list into the concrete discovery
request an executor should issue against a ``ToolSearch``-style tool-loading
mechanism, and nothing PROVES -- as opposed to advises -- that a required
codebase-memory/Serena tool was actually CALLED before the executor started
editing files. This module is that compiler + that gate.

Four pieces, matching the sprint item's four asks:

1. :func:`compile_discovery_request` -- the COMPILER. Turns a claimed item's
   ``tool_requirements`` into an actual discovery request: a
   ``select:<name>[,<name>...]`` query per tool (this environment's own
   ToolSearch convention -- see the ``claude-in-chrome`` MCP server's
   documented usage pattern of batching ``select:`` queries per server), a
   secondary free-text keyword query as a fallback discovery strategy, and a
   ``batched_queries`` list that groups every requirement by
   ``server_or_namespace`` into ONE ``select:`` query per server (avoiding
   the "load tools one at a time" anti-pattern this environment explicitly
   warns against).

2. :func:`build_tool_discovery_state` -- required/preferred AVAILABILITY
   tracking plus FALLBACK telemetry, composed with the compiled request and
   the receipt gate below into one object with stable, always-present field
   names: ``requested`` / ``selected`` / ``first_call`` / ``availability`` /
   ``fallback`` / ``receipt`` (the exact vocabulary 86b36617's acceptance
   criteria names). Availability classification is reused --
   never reimplemented -- from :mod:`meridian.capability_availability` via
   :mod:`meridian.executor_contract`'s existing bridge (lazy-imported here
   to avoid a circular import, mirroring ``capability_contract.py``'s own
   documented lazy-import-of-``executor_contract`` pattern for the identical
   reason).

3. :func:`verify_pre_edit_receipt` -- the RECEIPT gate. Deliberately
   NOT the same mechanism as :mod:`meridian.code_intel_receipt`'s
   ``verify_code_intel_prospecting``: that one gates ``complete_sprint_item``
   at COMPLETION time, only for a project that opted in via a capability
   manifest, scoped to ``touches_resources``. This gate applies at
   DISCOVERY/selection time, unconditionally, to any item whose OWN typed
   ``tool_requirements`` names a REQUIRED codebase-memory/Serena tool --
   independent of any capability-manifest opt-in. It reuses the exact same
   underlying receipt storage
   (:func:`meridian.code_intel_receipt.find_recent_prospect_receipt`, backed
   by the structural, tool-dispatch-written ``action_audit_log`` rows --
   never a self-report) so the two gates can never disagree about what
   counts as "the tool was actually called," but they answer different
   questions at different points in the lifecycle and neither replaces the
   other.

   Per this item's own explicit non-goals, NONE of the following are ever
   treated as proof a required codebase-memory/Serena tool was actually
   called: an advisory hook, ``touches_resources`` alone, ``index_status``
   alone, or a MERIDIAN-SIDE ``prospect_symbol`` call recorded on behalf of
   someone other than the executor. Only a genuine
   ``search_graph``/``get_code_snippet``/``find_symbol``/
   ``find_referencing_symbols`` (or the other
   :data:`meridian.code_intel_receipt.CODE_INTEL_RECEIPT_TOOLS`) receipt
   counts -- see :func:`_requires_code_intel_prospecting` and
   :func:`verify_pre_edit_receipt`. A tool that was successfully compiled
   into the discovery request AND resolves as ``available`` is EXPOSED, not
   USED -- see the ``exposed`` vs. ``actually_called`` fields on the gate's
   result. This is what makes the "exposed-but-unused" failure mode
   (9c8336c4) a rejection rather than a silent pass.

4. :func:`run_targeted_tests` -- exit-code-SAFE targeted-test orchestration.
   Mirrors the exact root-cause fix ``tunnel_client._handle_run_cmd`` /
   ``_shell_subprocess_env`` (525d86bb) already applies to the full-suite
   ``run_verification`` path: a wrapping layer's own exit code (a shell
   pipe's last stage, e.g. ``pytest ... | tail -n 50``, which reports
   ``tail``'s exit code -- almost always 0 -- never pytest's) must never
   stand in for the wrapped command's. This is the analogous PRIMITIVE for
   an item's TARGETED tests (a caller-supplied file/expression list, no
   stored project-wide ``test_cmd``): spawn via argument-list
   ``create_subprocess_exec`` (no shell, no pipe) whenever the caller
   supplies a token list, so ``proc.returncode`` IS the real target-process
   exit status, never masked by an intermediate pipeline stage. e24f2daa
   additionally routes that real exit code (plus signal/timeout/captured
   output) through :func:`meridian.test_run_receipt.classify_subprocess_result`
   before returning, so a thin/empty result (``exit_code=0``, no output, no
   parsed counts) is never indistinguishable from a genuine pass, and an
   xdist/pytest infrastructure crash (exit 3/4, a recognized crash-text
   marker) is never misreported as an ordinary test failure.

Every public function here is fully guarded against unexpected errors from
its DB-backed dependencies (mirrors the rest of this codebase's
"orientation/handoff must never break" convention) EXCEPT where an invalid
caller-supplied argument is a programming error worth surfacing immediately
(e.g. :func:`compile_discovery_request` on a non-dict ``item``).
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from . import code_intel_receipt as _code_intel_receipt
from . import test_run_receipt as _test_run_receipt
from . import tool_requirements as _tool_requirements

TOOL_DISCOVERY_SCHEMA_VERSION = 1
DISCOVERY_SCOPE_SCHEMA_VERSION = 1

#: Reused verbatim from code_intel_receipt.py -- the canonical set of bare
#: tool names that count as genuine codebase-memory/Serena prospecting.
#: Never redefined independently: a name added there is automatically
#: recognized here too.
CODE_INTEL_TOOL_NAMES = _code_intel_receipt.CODE_INTEL_RECEIPT_TOOLS


# ---------------------------------------------------------------------------
# 1. The compiler.
# ---------------------------------------------------------------------------

def _split_fallback_ref(raw: str) -> tuple[str, str]:
    """Best-effort ``(server_or_namespace, name)`` split of a free-form
    ``fallback`` string. ``tool_requirements`` fallbacks use the
    ``"<server>: <name>"`` convention (mirrors
    ``executor_contract._bridge_fallback_ref``'s own documented convention)
    when a colon is present; a bare string is treated as a namespace-less
    tool name."""
    text = (raw or "").strip()
    if not text:
        return "", ""
    if ":" in text:
        head, _, tail = text.partition(":")
        return head.strip(), (tail.strip() or text)
    return "", text


def _toolsearch_queries_for(name: str, server_or_namespace: str) -> dict[str, str]:
    """The two discovery strategies for one bare tool *name*.

    ``select`` -- this environment's own direct-selection ToolSearch
    convention (``"select:<tool_name>[,<tool_name>...]"``). ``keyword`` --
    a free-text fallback strategy (server + name + any surrounding words a
    keyword-mode ToolSearch could match against) for a caller whose
    ToolSearch implementation doesn't support (or whose exact runtime tool
    id doesn't match) direct selection by bare name.
    """
    name = (name or "").strip()
    server = (server_or_namespace or "").strip()
    if not name:
        return {"select": "", "keyword": ""}
    return {
        "select": f"select:{name}",
        "keyword": " ".join(p for p in (server, name) if p),
    }


def compile_discovery_request(item: dict[str, Any]) -> dict[str, Any]:
    """THE COMPILER (86b36617 acceptance #1): turn ``item['tool_requirements']``
    into an actual ToolSearch-style discovery request.

    Reads the SAME canonical, precedence-resolved requirements
    :func:`meridian.tool_requirements.effective_tool_requirements` returns
    (structured field wins; legacy ``required_tool`` pin is the read-time
    compatibility bridge) -- never re-derives or duplicates that precedence
    rule.

    Returns::

        {
          "schema_version": 1,
          "item_id": str | None,
          "requested": [
            {
              "name", "server_or_namespace", "required_or_preferred",
              "purpose", "call_template",
              "query": "select:<name>",              # direct-selection strategy
              "keyword_query": "<server> <name>",     # keyword-search strategy
              "fallback": [str, ...],                 # declared fallback chain, verbatim
            }, ...
          ],
          "batched_queries": [
            {"server_or_namespace": str, "query": "select:name1,name2", "names": [...]}, ...
          ],
        }

    ``requested`` is deterministically ordered exactly as
    ``effective_tool_requirements``/``normalize_tool_requirements`` already
    sort it: ``(server_or_namespace, name)``. ``batched_queries`` groups the
    SAME entries by ``server_or_namespace`` into one comma-joined ``select:``
    query per server -- the batching pattern this environment's own
    ``claude-in-chrome`` MCP server explicitly documents ("batch every tool
    you expect to need into ONE ToolSearch call ... do NOT load tools one at
    a time").

    Never raises for a well-formed item; an item with no resolvable
    requirements compiles to an empty (but present) ``requested``/
    ``batched_queries``, never ``None`` -- a caller can always iterate the
    result.
    """
    if not isinstance(item, dict):
        raise TypeError("compile_discovery_request(item) requires a dict")

    requirements = _tool_requirements.effective_tool_requirements(item)
    requested: list[dict[str, Any]] = []
    by_server: "dict[str, list[str]]" = {}

    for req in requirements:
        name = req.get("name") or ""
        server = req.get("server_or_namespace") or ""
        queries = _toolsearch_queries_for(name, server)
        requested.append({
            "name": name,
            "server_or_namespace": server,
            "required_or_preferred": req.get("required_or_preferred"),
            "purpose": req.get("purpose"),
            "call_template": req.get("call_template"),
            "query": queries["select"],
            "keyword_query": queries["keyword"],
            "fallback": list(req.get("fallback") or []),
        })
        if name:
            by_server.setdefault(server, []).append(name)

    batched_queries = [
        {
            "server_or_namespace": server,
            "query": "select:" + ",".join(names),
            "names": list(names),
        }
        for server, names in sorted(by_server.items())
    ]

    return {
        "schema_version": TOOL_DISCOVERY_SCHEMA_VERSION,
        "item_id": item.get("id"),
        "requested": requested,
        "batched_queries": batched_queries,
    }


# ---------------------------------------------------------------------------
# 2. Availability / fallback telemetry + explicit degraded/fail-closed state.
# ---------------------------------------------------------------------------

def _default_availability_by_key(
    requirements: list[dict[str, Any]], *, inventory: "dict[str, Any] | None" = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Lazy-imports :mod:`meridian.executor_contract` for its
    ``default_tool_availability`` bridge -- mirrors
    ``capability_contract.py``'s own documented lazy-import of
    ``executor_contract`` for the identical reason (``executor_contract``
    imports THIS module to embed a ``tool_discovery`` field on its own
    contract, so a top-level import here would be circular). Guarded: any
    import/classification failure degrades to an empty mapping (every
    requirement then classifies as unclassified/"unknown" downstream) rather
    than breaking discovery-state building.
    """
    try:
        from . import executor_contract as _executor_contract  # noqa: PLC0415

        return _executor_contract.default_tool_availability(requirements, inventory=inventory)
    except Exception:  # noqa: BLE001 — availability classification is best-effort
        return {}


def _requirement_key(requirement: dict[str, Any]) -> tuple[str, str]:
    return (requirement.get("server_or_namespace") or "", requirement.get("name") or "")


def classify_requirement_state(
    requirement: dict[str, Any], availability_entry: "dict[str, Any] | None",
) -> dict[str, Any]:
    """Explicit degraded/fail-closed classification (86b36617 acceptance #4)
    -- distinct from the raw ``capability_availability`` status string, which
    a caller could otherwise silently treat as "fine, proceed" for any
    non-``missing`` value.

    Returns one of four EXPLICIT states, never left implicit:

    * ``"ok"`` -- the primary tool itself is confirmed available.
    * ``"degraded_fallback"`` -- the primary tool is not directly available,
      but a declared fallback rescued it (``fallback_used`` is set). Visible,
      not implied: the caller sees exactly which fallback fired.
    * ``"fail_closed"`` -- a REQUIRED tool with no rescue: either confirmed
      ``missing`` with the fallback chain exhausted/absent, or ``unknown``
      with no fallback declared at all (``requirement_risk_class ==
      "hard_block"``) — the item cannot honestly proceed on this tool.
    * ``"soft_unavailable"`` -- a ``preferred`` (not required) tool that is
      unavailable/unknown: never blocking, but still explicitly named rather
      than silently dropped.
    """
    avail = availability_entry or {}
    status = avail.get("status") or "unknown"
    fallback_used = avail.get("fallback_used")
    required = requirement.get("required_or_preferred") == "required"
    risk = _tool_requirements.requirement_risk_class(requirement)

    if status == "available":
        state = "ok"
    elif status == "degraded":
        state = "degraded_fallback" if fallback_used else ("fail_closed" if required else "soft_unavailable")
    elif status == "missing":
        state = "fail_closed" if required else "soft_unavailable"
    else:  # "unknown" or anything unclassified
        state = "fail_closed" if (required and risk == "hard_block") else "soft_unavailable"

    return {
        "state": state,
        "status": status,
        "required_or_preferred": requirement.get("required_or_preferred"),
        "risk_class": risk,
        "fallback_used": (fallback_used or {}).get("fallback_tool") if isinstance(fallback_used, dict) else fallback_used,
    }


# ---------------------------------------------------------------------------
# 3. The receipt gate.
# ---------------------------------------------------------------------------

def _requires_code_intel_prospecting(requirements: list[dict[str, Any]]) -> bool:
    """True when at least one REQUIRED ``tool_requirements`` entry names a
    genuine codebase-memory/Serena prospecting tool (reuses
    :data:`CODE_INTEL_TOOL_NAMES` verbatim -- never a second, independently
    maintained list that could drift from ``code_intel_receipt.py``'s own).
    A ``preferred`` entry naming the same tool does NOT make the gate
    applicable -- mirrors this item's own instruction that the gate targets
    items that genuinely REQUIRE prospecting, not ones that merely mention
    it as optional.
    """
    for req in requirements or []:
        if req.get("required_or_preferred") != "required":
            continue
        if _code_intel_receipt.is_code_intel_receipt_tool(req.get("name") or ""):
            return True
    return False


async def verify_pre_edit_receipt(
    db: Any,
    project_id: str,
    item: dict[str, Any],
    requirements: "list[dict[str, Any]] | None" = None,
    *,
    session_id: "str | None" = None,
    tenant: "dict[str, Any] | None" = None,
    since: "str | None" = None,
) -> dict[str, Any]:
    """THE pre-edit semantic-search RECEIPT gate (86b36617 acceptance #3).

    Distinct from :func:`meridian.code_intel_receipt.verify_code_intel_prospecting`
    (see this module's docstring) -- applies unconditionally, driven by the
    item's OWN typed ``tool_requirements`` rather than a project-level
    capability-manifest opt-in scoped to ``touches_resources``.

    Never raises for an expected condition: every result is a structured
    dict, same discipline as ``code_intel_receipt.verify_code_intel_prospecting``.
    Returns::

        {
          "applicable": bool,       # False = this gate has nothing to check
          "ok": bool,               # False = BLOCKED (a required prospecting
                                     #   tool was never actually called)
          "code": "TOOL_DISCOVERY_RECEIPT_MISSING" | None,
          "exposed": bool,          # a ToolSearch discovery request COULD be
                                     #   (or was) compiled for this tool
          "actually_called": bool,  # a genuine action_audit_log receipt exists
          "receipt": dict | None,   # the matched receipt row, when found
          "message": str | None,
        }

    ``exposed=True, actually_called=False, ok=False`` is the EXACT
    "exposed-but-unused" rejection (9c8336c4) this gate exists to produce —
    a tool being compiled into the discovery request (and even resolving as
    available) is never, by itself, treated as proof it was used.
    """
    reqs = (
        requirements if requirements is not None
        else _tool_requirements.effective_tool_requirements(item)
    )
    if not _requires_code_intel_prospecting(reqs):
        return {
            "applicable": False, "ok": True, "code": None,
            "exposed": False, "actually_called": False, "receipt": None,
            "message": None,
        }

    since_ts = since
    if since_ts is None:
        try:
            since_ts = _code_intel_receipt._claimed_at_since(item)
        except Exception:  # noqa: BLE001
            since_ts = None

    tenant_id = (tenant or {}).get("id") if tenant else None
    receipt = await _code_intel_receipt.find_recent_prospect_receipt(
        db, project_id=project_id, tenant_id=tenant_id, since=since_ts,
    )
    if receipt is not None:
        return {
            "applicable": True, "ok": True, "code": None,
            "exposed": True, "actually_called": True, "receipt": receipt,
            "message": None,
        }
    return {
        "applicable": True, "ok": False, "code": "TOOL_DISCOVERY_RECEIPT_MISSING",
        "exposed": True, "actually_called": False, "receipt": None,
        "message": (
            "This item's tool_requirements declare a REQUIRED codebase-memory/"
            "Serena prospecting tool (search_graph/get_code_snippet/find_symbol/"
            "find_referencing_symbols/...). A ToolSearch discovery request was "
            "compiled for it, but no durable receipt of an ACTUAL call was found "
            "since this item was claimed. Being exposed via ToolSearch (or "
            "resolving as 'available') is not proof of use -- an advisory hook, "
            "touches_resources alone, index_status alone, or a Meridian-side "
            "prospect_symbol call made on the server's own behalf do not satisfy "
            "this gate either. Call the tool yourself, then retry."
        ),
    }


# ---------------------------------------------------------------------------
# Composition: the single stable-shaped discovery-state object.
# ---------------------------------------------------------------------------

def _fallback_tool_name(fallback_used: "dict[str, Any] | str | None") -> "str | None":
    if isinstance(fallback_used, dict):
        return fallback_used.get("fallback_tool")
    if isinstance(fallback_used, str) and fallback_used:
        return fallback_used
    return None


async def build_tool_discovery_state(
    db: Any,
    project_id: str,
    item: dict[str, Any],
    *,
    availability_by_key: "dict[tuple[str, str], dict[str, Any]] | None" = None,
    tool_inventory: "dict[str, Any] | None" = None,
    session_id: "str | None" = None,
    tenant: "dict[str, Any] | None" = None,
    since: "str | None" = None,
) -> dict[str, Any]:
    """Compose the compiler + availability/fallback telemetry + receipt gate
    into ONE object with stable, always-present field names (86b36617
    acceptance: "requested / selected / first-call / availability / fallback
    / receipt fields are present and stable").

    ``availability_by_key`` -- pass a pre-computed mapping (e.g. a caller
    (like :func:`meridian.executor_contract.build_executor_contract`) that
    already ran ``default_tool_availability`` once for the SAME
    requirements) to avoid recomputing it. When omitted, this function
    computes its own via a lazy import of ``executor_contract`` (see
    :func:`_default_availability_by_key`).

    Never raises: every DB-backed or classification sub-step is guarded so a
    failure degrades that ONE section rather than breaking the whole
    composition -- this feeds mandatory handoff/contract paths.
    """
    requirements = _tool_requirements.effective_tool_requirements(item)
    request = compile_discovery_request(item)

    if availability_by_key is None:
        availability_by_key = _default_availability_by_key(requirements, inventory=tool_inventory)

    selected: list[dict[str, Any]] = []
    fallback_entries: list[dict[str, Any]] = []
    availability_rollup: dict[str, list[str]] = {
        "available": [], "degraded": [], "missing": [], "unknown": [],
    }
    degraded_or_fail_closed: list[dict[str, Any]] = []

    for req in requirements:
        key = _requirement_key(req)
        avail_entry = availability_by_key.get(key)
        classification = classify_requirement_state(req, avail_entry)
        label = f"{req.get('server_or_namespace')}: {req.get('name')}"
        status = classification["status"]
        availability_rollup.setdefault(status, []).append(label)

        fallback_used_tool = classification["fallback_used"]
        selected_tool = (
            fallback_used_tool if fallback_used_tool
            else (req.get("name") if status in ("available",) else None)
        )
        selected.append({
            "name": req.get("name"),
            "server_or_namespace": req.get("server_or_namespace"),
            "required_or_preferred": req.get("required_or_preferred"),
            "selected_tool": selected_tool,
            "source": "fallback" if fallback_used_tool else ("primary" if selected_tool else None),
            "state": classification["state"],
        })

        declared_fallback = list(req.get("fallback") or [])
        if declared_fallback or fallback_used_tool:
            fallback_entries.append({
                "name": req.get("name"),
                "server_or_namespace": req.get("server_or_namespace"),
                "declared": declared_fallback,
                "used": fallback_used_tool,
                "rescued": bool(fallback_used_tool),
            })

        if classification["state"] in ("fail_closed", "degraded_fallback", "soft_unavailable"):
            degraded_or_fail_closed.append({
                "name": req.get("name"),
                "server_or_namespace": req.get("server_or_namespace"),
                "state": classification["state"],
                "status": status,
            })

    receipt_check = await verify_pre_edit_receipt(
        db, project_id, item, requirements,
        session_id=session_id, tenant=tenant, since=since,
    )

    first_call: "dict[str, Any] | None" = None
    if receipt_check.get("receipt"):
        _receipt = receipt_check["receipt"]
        _tool_name = None
        try:
            import json as _json  # noqa: PLC0415

            _tool_name = _json.loads(_receipt.get("detail") or "{}").get("tool")
        except Exception:  # noqa: BLE001
            _tool_name = None
        first_call = {"tool": _tool_name, "at": _receipt.get("created_at")}

    executable = True
    executable_reasons: list[str] = []
    fail_closed_names = [
        f"{e['server_or_namespace']}: {e['name']}"
        for e in degraded_or_fail_closed if e["state"] == "fail_closed"
    ]
    if fail_closed_names:
        executable = False
        executable_reasons.append("fail_closed_tools:" + ",".join(fail_closed_names))
    if receipt_check.get("applicable") and not receipt_check.get("ok"):
        executable = False
        executable_reasons.append(receipt_check.get("code") or "TOOL_DISCOVERY_RECEIPT_MISSING")

    return {
        "schema_version": TOOL_DISCOVERY_SCHEMA_VERSION,
        "item_id": item.get("id"),
        "requested": request["requested"],
        "batched_queries": request["batched_queries"],
        "selected": selected,
        "first_call": first_call,
        "availability": availability_rollup,
        "fallback": fallback_entries,
        "receipt": receipt_check,
        "degraded_or_fail_closed": degraded_or_fail_closed,
        "executable": executable,
        "executable_reasons": executable_reasons,
    }


# ---------------------------------------------------------------------------
# 4. Exit-code-safe targeted-test orchestration.
# ---------------------------------------------------------------------------

_TAIL_BYTES = 16_384

# Mirrors tunnel_client.py's own pytest short-summary parser contract: looks
# for "N passed" / "N failed" anywhere in the combined output tail. Kept as
# an independent, self-contained implementation (not imported from
# tunnel_client.py) -- that module is the CLIENT-side tunnel process, a huge
# file well outside this item's claimed symbols/touches_resources, and this
# utility is meant to be usable standalone (e.g. from a future server-side
# orchestration point) without pulling in the tunnel client's full
# dependency surface.
_PASSED_RE = re.compile(r"(\d+)\s+passed")
_FAILED_RE = re.compile(r"(\d+)\s+failed")


def _parse_test_counts(output: str) -> tuple["int | None", "int | None"]:
    passed = None
    failed = None
    m = _PASSED_RE.search(output)
    if m:
        passed = int(m.group(1))
    m = _FAILED_RE.search(output)
    if m:
        failed = int(m.group(1))
    return passed, failed


def _shell_subprocess_env() -> "dict[str, str] | None":
    """Same Windows cmd.exe PATH fix as ``tunnel_client._shell_subprocess_env``
    (525d86bb) -- reimplemented locally (not imported) for the same
    standalone-utility reason as :func:`_parse_test_counts`. See that
    module's docstring for the full root-cause writeup: a shell-string
    ``create_subprocess_shell`` child on Windows resolves a bare program name
    via cmd.exe's OWN PATH search, which does not get the "directory this
    interpreter loaded from" freebie the exec form gets -- so a bare
    ``python``/``pixi`` can 'not be recognized' and produce a wrapper's own
    exit code (1) that is easily mistaken for the wrapped command's real
    exit status. Returns ``None`` on non-Windows (no such indirection).
    """
    import os
    import sys

    if sys.platform != "win32":
        return None
    py_dir = os.path.dirname(sys.executable)
    if not py_dir:
        return None
    env = dict(os.environ)
    existing_path = env.get("PATH", "")
    parts = existing_path.split(os.pathsep) if existing_path else []
    if py_dir not in parts:
        env["PATH"] = py_dir + (os.pathsep + existing_path if existing_path else "")
    return env


async def run_targeted_tests(
    cmd: "list[str] | str",
    *,
    cwd: "str | None" = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """Exit-code-SAFE targeted-test runner (86b36617 acceptance #5).

    ROOT CAUSE this guards against: piping a test runner's output through a
    second process (e.g. ``pytest tests/test_x.py | tail -n 50``) makes the
    calling shell's own reported status reflect the LAST stage of the pipe
    (``tail``, which exits 0 almost unconditionally), silently discarding
    the test runner's real exit code -- a run that actually FAILED can look
    like ``exit_code=0``. This function never constructs or executes such a
    pipeline itself.

    Preferred form: *cmd* as a list of tokens
    (e.g. ``["pixi", "run", "python", "-m", "pytest", "tests/test_x.py",
    "-q"]``) -- spawned via ``asyncio.create_subprocess_exec`` with NO shell
    and NO pipe in between, so ``proc.returncode`` IS the real target
    process's exit status, unconditionally. A shell-string *cmd* is also
    accepted (spawned via ``create_subprocess_shell``, with the same
    Windows PATH fix ``tunnel_client._shell_subprocess_env`` applies) for a
    caller that genuinely needs shell features (e.g. env-var expansion) --
    but a shell string that itself contains a ``| tail``/``| head`` (or any
    other pipeline) reintroduces exactly the masking this function exists to
    avoid; that is the CALLER's own choice, not something this function can
    rescue after the fact. The list form is how a caller gets the safety
    guarantee unconditionally.

    Returns a dict with keys: ``status`` (``"ok"``/``"timeout"``/``"error"``),
    ``exit_code`` (the real, unmasked code, or ``None`` on timeout/error),
    ``passed``/``failed`` (best-effort parsed counts), ``stdout_tail``/
    ``stderr_tail`` (last :data:`_TAIL_BYTES` bytes each), ``cmd`` (echoed
    back for the caller's own logging).

    e24f2daa — also carries ``classification`` (one of
    :data:`meridian.test_run_receipt.CLASSIFICATIONS`) and
    ``classification_reason``: this function's own exit-code/signal safety
    guarantee (above) only protects against a WRAPPING shell masking the
    real exit code — it says nothing about whether ``exit_code=0`` with an
    EMPTY captured log is genuine evidence of a pass (it is not), or whether
    a nonzero code came from a real assertion failure versus pytest's own
    INTERNAL_ERROR/USAGE_ERROR exit codes (3/4, an infrastructure crash, not
    a code regression). ``classify_subprocess_result`` applies that same
    fail-closed discipline the durable ``TestRunRecord`` consumer applies to
    a full-suite run — never silently treating a thin/empty result as
    ``passed``. ``status``/``exit_code``/``passed``/``failed`` above are
    UNCHANGED (this is a pure addition) for every existing caller.
    """
    if not cmd:
        _empty_classified = _test_run_receipt.classify_subprocess_result(exit_code=None)
        return {
            "status": "error", "message": "cmd is empty", "exit_code": None,
            "passed": None, "failed": None, "stdout_tail": "", "stderr_tail": "",
            "cmd": cmd,
            "classification": _empty_classified["classification"],
            "classification_reason": "cmd is empty -- no command was ever executed",
        }

    try:
        if isinstance(cmd, (list, tuple)):
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd or None,
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                str(cmd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd or None,
                env=_shell_subprocess_env(),
            )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
            return {
                "status": "timeout",
                "message": f"command timed out after {timeout:.0f}s",
                "exit_code": None, "passed": None, "failed": None,
                "stdout_tail": "", "stderr_tail": "", "cmd": cmd,
                "classification": _test_run_receipt.CLASS_TIMEOUT,
                "classification_reason": f"command timed out after {timeout:.0f}s",
            }

        exit_code: int = proc.returncode if proc.returncode is not None else -1
        stdout_str = stdout_b[-_TAIL_BYTES:].decode("utf-8", errors="replace") if stdout_b else ""
        stderr_str = stderr_b[-_TAIL_BYTES:].decode("utf-8", errors="replace") if stderr_b else ""
        passed, failed = _parse_test_counts(stdout_str + "\n" + stderr_str)
        _classified = _test_run_receipt.classify_subprocess_result(
            exit_code=exit_code, stdout=stdout_str, stderr=stderr_str,
            passed=passed, failed=failed,
        )

        return {
            "status": "ok",
            "exit_code": exit_code,
            "passed": passed,
            "failed": failed,
            "stdout_tail": stdout_str,
            "stderr_tail": stderr_str,
            "cmd": cmd,
            "classification": _classified["classification"],
            "classification_reason": _classified["reason"],
        }
    except Exception as exc:  # noqa: BLE001 — a broken runner must report, never crash the caller
        return {
            "status": "error", "message": str(exc), "exit_code": None,
            "passed": None, "failed": None, "stdout_tail": "", "stderr_tail": "",
            "cmd": cmd,
            "classification": _test_run_receipt.CLASS_INFRA_CRASH,
            "classification_reason": f"failed to spawn/communicate with the target process: {exc}",
        }


# ---------------------------------------------------------------------------
# 5. Domain-neutral discovery-scope contract (7f23cd62).
#
# Everything above this section (pieces 1-4, 86b36617) answers "is a
# REQUIRED TOOL available for this item" — scoped to codebase-memory/Serena
# tool_requirements specifically. This section answers a related but
# distinct question that applies to ANY domain (code, DOCX structure,
# Outputs/provenance, filesystem, tunnel, external MCP): "is this
# RESOURCE/SCOPE even in bounds for discovery right now, and on what
# evidence." A tool can be perfectly available while the specific resource
# it would operate on is explicitly out of scope (an ignored directory, an
# unfetched remote snapshot, a path outside its declared artifact subtree) —
# that is QUARANTINE, a distinct outcome from the tool-availability states
# above, never collapsed into "unavailable" (which would hide the real
# reason: policy exclusion, not absence).
#
# Deliberately generalizes rather than duplicates: this reuses the same
# fail-closed-for-writes philosophy as capability_availability.py's
# availability_policy handling and the same non-empty-reason + audited
# override pattern as code_intel_receipt.record_prospect_receipt_override —
# extended here with a real, checked EXPIRY, which neither existing override
# path has (both are one-shot, per-call acknowledgements; this one is
# usable across multiple calls until it lapses, so it needs a bound).
# ---------------------------------------------------------------------------

#: The four domain-neutral scope kinds an item/session may declare for a
#: resource this contract governs. Free text elsewhere in this codebase
#: (capability manifests, tool_requirements) stays free text; this one small
#: vocabulary is closed because "resolution" below is defined in terms of it.
DISCOVERY_SCOPE_TRACKED = "tracked"
DISCOVERY_SCOPE_ALLOWLISTED_IGNORED = "allowlisted_ignored"
DISCOVERY_SCOPE_REMOTE_SNAPSHOT = "remote_snapshot"
DISCOVERY_SCOPE_ARTIFACT_SUBTREE = "artifact_subtree"

DISCOVERY_SCOPES = frozenset({
    DISCOVERY_SCOPE_TRACKED, DISCOVERY_SCOPE_ALLOWLISTED_IGNORED,
    DISCOVERY_SCOPE_REMOTE_SNAPSHOT, DISCOVERY_SCOPE_ARTIFACT_SUBTREE,
})

#: The four resolution outcomes the item's own acceptance criteria name
#: verbatim ("must return ready, degraded, unavailable, or quarantined").
RESOLUTION_READY = "ready"
RESOLUTION_DEGRADED = "degraded"
RESOLUTION_UNAVAILABLE = "unavailable"
RESOLUTION_QUARANTINED = "quarantined"

RESOLUTIONS = frozenset({
    RESOLUTION_READY, RESOLUTION_DEGRADED, RESOLUTION_UNAVAILABLE, RESOLUTION_QUARANTINED,
})

#: Mutations gated by this contract must fail closed on any resolution other
#: than READY — DEGRADED is explicitly a read-only "proceed on approved
#: fallback" state (mirrors tool_discovery's own "degraded_fallback" state
#: never being treated as unconditionally safe), matching this item's own
#: "read-only discovery may continue on an approved fallback; writes, claims,
#: promotion, and completion must fail closed" requirement verbatim.
_MUTATION_SAFE_RESOLUTIONS = frozenset({RESOLUTION_READY})

DISCOVERY_SCOPE_OVERRIDE_EVENT_TYPE = "discovery_scope_override"


def classify_discovery_scope(
    scope: str, *, evidence: "dict[str, Any] | None" = None,
) -> dict[str, Any]:
    """Pure, deterministic resolver: (scope, evidence) -> one of the four
    named resolutions, with a human-readable reason. No I/O, no model call —
    mirrors capability_availability.classify_tool's purity discipline.

    ``evidence`` is a plain dict of booleans the CALLER already gathered for
    this specific resource (this function never fetches anything itself):

    * ``tracked``: ``indexed`` (bool), ``index_stale`` (bool).
    * ``allowlisted_ignored``: no evidence needed — quarantined by policy,
      unconditionally, unless a validated override is applied afterward via
      :func:`apply_discovery_override`.
    * ``remote_snapshot``: ``snapshot_fetched`` (bool), ``snapshot_stale`` (bool).
    * ``artifact_subtree``: ``within_declared_subtree`` (bool).

    An unrecognized ``scope`` resolves UNAVAILABLE with an explicit reason
    (never silently treated as "not applicable" — this item's own explicit
    non-goal) rather than raising, so a caller iterating heterogeneous
    resources never has one malformed entry crash the whole pass.
    """
    ev = evidence or {}
    if scope not in DISCOVERY_SCOPES:
        return {
            "discovery_scope": scope,
            "resolution": RESOLUTION_UNAVAILABLE,
            "reason": f"unrecognized discovery_scope '{scope}' — not one of {sorted(DISCOVERY_SCOPES)}.",
        }

    if scope == DISCOVERY_SCOPE_ALLOWLISTED_IGNORED:
        return {
            "discovery_scope": scope,
            "resolution": RESOLUTION_QUARANTINED,
            "reason": (
                "resource is in an explicitly allowlisted-ignored location — "
                "excluded by policy, not by absence. Requires an audited "
                "override (see apply_discovery_override) to proceed."
            ),
        }

    if scope == DISCOVERY_SCOPE_TRACKED:
        if not ev.get("indexed"):
            return {
                "discovery_scope": scope,
                "resolution": RESOLUTION_UNAVAILABLE,
                "reason": "tracked resource has no index evidence — never seen by discovery.",
            }
        if ev.get("index_stale"):
            return {
                "discovery_scope": scope,
                "resolution": RESOLUTION_DEGRADED,
                "reason": "tracked resource is indexed, but the index is known-stale — read-only use only.",
            }
        return {
            "discovery_scope": scope,
            "resolution": RESOLUTION_READY,
            "reason": "tracked resource has a fresh index entry.",
        }

    if scope == DISCOVERY_SCOPE_REMOTE_SNAPSHOT:
        if not ev.get("snapshot_fetched"):
            return {
                "discovery_scope": scope,
                "resolution": RESOLUTION_UNAVAILABLE,
                "reason": "remote_snapshot resource has never been fetched locally.",
            }
        if ev.get("snapshot_stale"):
            return {
                "discovery_scope": scope,
                "resolution": RESOLUTION_DEGRADED,
                "reason": "remote_snapshot was fetched but is known-stale — read-only use only.",
            }
        return {
            "discovery_scope": scope,
            "resolution": RESOLUTION_READY,
            "reason": "remote_snapshot is fetched and fresh.",
        }

    # DISCOVERY_SCOPE_ARTIFACT_SUBTREE
    if not ev.get("within_declared_subtree"):
        return {
            "discovery_scope": scope,
            "resolution": RESOLUTION_QUARANTINED,
            "reason": (
                "resource falls outside its item's declared artifact subtree — "
                "excluded by scope boundary, not by absence."
            ),
        }
    return {
        "discovery_scope": scope,
        "resolution": RESOLUTION_READY,
        "reason": "resource confirmed within its declared artifact subtree.",
    }


def is_mutation_safe(resolution: str) -> bool:
    """True only for RESOLUTION_READY — the single fail-closed gate every
    write/claim/promotion/completion path governed by this contract must
    consult. DEGRADED is deliberately excluded: read-only discovery may
    proceed on it, but this function is specifically the mutation gate."""
    return resolution in _MUTATION_SAFE_RESOLUTIONS


def validate_discovery_override(
    *, actor: "str | None", reason: "str | None", expires_at: "str | None",
    now: "datetime | None" = None,
) -> dict[str, Any]:
    """Pure validation for an audited discovery-scope override — NOT an
    unbounded bypass (this item's own explicit requirement). Three
    independent checks, all must pass:

    1. ``reason`` non-empty (mirrors code_intel_receipt.
       record_prospect_receipt_override's identical requirement).
    2. ``actor`` non-empty — an override with no attributed human/session is
       not auditable either.
    3. ``expires_at`` parses as an ISO-8601 timestamp AND is strictly in the
       future relative to ``now`` (defaults to real UTC now; injectable for
       deterministic tests) — an override with no expiry, or one already
       lapsed, is refused. This is the piece neither existing override path
       (code_intel_receipt's, sprint_evidence_guard's) has: those are
       one-shot acknowledgements consumed in the same call, so they don't
       need a bound. A discovery-scope override is meant to cover repeated
       resolution calls over some window, so it needs one.

    Returns ``{"valid": bool, "errors": [str, ...]}`` — never raises, so a
    caller can always inspect ``errors`` for the exact reason(s) rather than
    catching an exception.
    """
    from datetime import datetime as _datetime, timezone as _timezone  # noqa: PLC0415

    errors: list[str] = []
    if not (reason or "").strip():
        errors.append("reason is required and must be non-empty.")
    if not (actor or "").strip():
        errors.append("actor is required and must be non-empty.")

    if not (expires_at or "").strip():
        errors.append("expires_at is required — an unbounded override is refused.")
    else:
        try:
            parsed = _datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_timezone.utc)
        except (TypeError, ValueError):
            parsed = None
            errors.append(f"expires_at '{expires_at}' is not a valid ISO-8601 timestamp.")
        if parsed is not None:
            _now = now if now is not None else _datetime.now(_timezone.utc)
            if _now.tzinfo is None:
                _now = _now.replace(tzinfo=_timezone.utc)
            if parsed <= _now:
                errors.append(
                    f"expires_at '{expires_at}' has already lapsed (now={_now.isoformat()}) — "
                    "a lapsed override is refused, not silently extended."
                )

    return {"valid": not errors, "errors": errors}


def apply_discovery_override(
    classification: dict[str, Any], *, actor: str, reason: str, expires_at: str,
    now: "datetime | None" = None,
) -> dict[str, Any]:
    """Apply a validated override to a :func:`classify_discovery_scope` result.

    Refuses (returns the ORIGINAL classification, unmodified, with
    ``override_rejected`` set) unless :func:`validate_discovery_override`
    passes — never silently swaps state on a malformed override. On success,
    returns a NEW dict (the input is never mutated) with ``resolution``
    upgraded to READY and an explicit ``override`` block recording exactly
    who/why/until — auditable, bounded, and visible on the result itself,
    not just in a side-channel log. Callers that persist overrides durably
    should ALSO call :func:`record_discovery_scope_override` (this function
    is pure and does not touch the database).
    """
    validation = validate_discovery_override(actor=actor, reason=reason, expires_at=expires_at, now=now)
    if not validation["valid"]:
        return {**classification, "override_rejected": validation["errors"]}
    return {
        **classification,
        "resolution": RESOLUTION_READY,
        "override": {
            "actor": actor,
            "reason": reason,
            "expires_at": expires_at,
            "prior_resolution": classification.get("resolution"),
        },
    }


async def record_discovery_scope_override(
    db: Any, project_id: str, *, actor: "str | None", reason: "str | None",
    discovery_scope: str, expires_at: "str | None", tenant_id: "str | None" = None,
) -> dict[str, Any]:
    """Audit-log an applied discovery-scope override — mirrors
    code_intel_receipt.record_prospect_receipt_override's exact shape
    (same underlying action_audit_log table, same non-empty-reason refusal),
    plus the ``expires_at`` bound this contract adds. Raises ``ValueError``
    on an empty reason, same as the code-intel precedent — a caller that
    reaches this point should already have validated via
    :func:`apply_discovery_override`, so this is a defense-in-depth check,
    not the primary gate.
    """
    _reason = (reason or "").strip()
    if not _reason:
        raise ValueError(
            "reason is required and must be non-empty to record a discovery-scope "
            "override — an override with no stated reason is not auditable and is refused."
        )
    import json as _json  # noqa: PLC0415 — mirrors this module's existing local-import style
    from . import db as db_module  # noqa: PLC0415 — avoid a top-level cycle with meridian.db

    detail = _json.dumps({
        "discovery_scope": discovery_scope,
        "reason": _reason,
        "expires_at": expires_at,
    })
    return await db_module.record_action_audit_event(
        db, DISCOVERY_SCOPE_OVERRIDE_EVENT_TYPE,
        tenant_id=tenant_id, project_id=project_id,
        actor=actor, detail=detail,
    )
