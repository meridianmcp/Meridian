"""Declarative deterministic tool-routing rules (e5a7ce7f, item_group:
scoped-discovery-and-fallback, v0.2.6).

Ships a validated ``.meridian/tool-routing.toml`` contract (explicit rules,
priorities, required tools, fallbacks, operation mode, confidence
thresholds) plus a pure, DB-free ``route()`` function that applies a fixed
5-layer priority chain:

    1. capability manifest  (deterministic — declared required_tools/
       fallback_chain/availability_policy; fail-closed for "required")
    2. explicit rules        (deterministic — this module's own .toml)
    3. exact category match  (deterministic — keyword/category affinity)
    4. BM25 candidate discovery (lexical — via an injected callable)
    5. optional Model2Vec reranking (semantic — ANNOTATE/REORDER ONLY)

An unknown or low-confidence request returns CANDIDATES, never a guess
(layer 6, implicit — see ``route()``'s final fallback).

Architecture note (pinned decision 2a3a3882, finding 5569beca, item
78127d55 "INVESTIGATE: specify a layered semantic tool auto-router..."):
this module is a THIN GENERALIZATION over four primitives that already
exist and are already tested/hardened elsewhere in this codebase — it does
NOT reimplement matching/ranking logic:

    - Layer 1 reuses ``meridian.capability_manifest``'s existing schema
      (required_tools/fallback_chain/availability_policy) unchanged.
    - Layer 3 reuses ``meridian.mcp_tools.match_categories_by_keywords``
      (the a749f87c ``_select_active_tool_set`` keyword-affinity pattern,
      generalized to accept an arbitrary caller-supplied affinity mapping —
      see that function's own docstring for the "why a new function instead
      of refactoring ``_select_active_tool_set`` itself" rationale).
    - Layer 4 is caller-injected (``bm25_fn``) — this module never imports
      ``meridian.db`` directly, keeping it DB-free/pure like
      ``meridian.semantic_search`` (a caller wires it to
      ``db.hybrid_candidate_retrieval`` / ``db.planning_search``'s lexical
      path; tests wire a fake).
    - Layer 5 reuses ``meridian.semantic_search.rank_confident`` /
      ``score_confidence`` as-is (safe-by-default, RSS-guarded, dual-gate
      abstention) when importable, or an injected ``semantic_fn``.

Hard invariants (from this item's own notes — enforced in code, not just
documented):

    * A semantic score may REORDER or ANNOTATE the candidate list produced
      by layer 3/4. It may never REMOVE a candidate, never change
      ``blocked``/``required_tools``/``fallback_chain``, and never sets
      ``tool`` — see ``_reorder_candidates_by_semantic`` (raises
      :class:`ToolRoutingInvariantError` if candidate membership would
      change) and the ``dataclasses.replace`` call in
      ``_maybe_semantic_rerank`` (only ``stage``/``candidates``/
      ``confidence``/``reason`` are ever touched).
    * Semantic/BM25/category-match/unknown decisions never authorize a
      mutation — only ``authorize_mutation()`` may say yes, and it only
      does so for the two deterministic stages (``capability_manifest``,
      ``explicit_rule``), independently re-checked, never trusting
      ``route()``'s own opinion.
    * A request whose category-match stage resolves to ``code-intel`` or
      ``docx`` is answered AT THAT LAYER (priority 3) — layers 4/5 (BM25,
      semantic) are never reached for it, so semantic reranking structurally
      cannot replace Serena/codebase-memory (code) or Meridian-docs (OOXML)
      for those requests; it is not merely told not to.

Explicitly OUT OF SCOPE for this item (per the paired investigation's own
rollout-gate design, see the INVESTIGATE f30bbd89 comment block at the end
of ``meridian/mcp_tools.py``): this module never filters the MCP
``tools/list`` surface itself ("stage 2 enforcing" in that design) — it is
a standalone, importable, fully-tested decision function. Wiring it into
``start_session``/``_select_active_tool_set`` or any other live call site is
a deliberate, separate follow-up requiring its own HITL-reviewed item, exactly
as that investigation concluded. Nothing in this module is imported by, or
changes the behavior of, any existing runtime path.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any, Callable

from meridian.capability_manifest import CapabilityManifestError, _check_no_secrets_or_local_paths
from meridian.mcp_tools import _extract_kws, match_categories_by_keywords

try:  # pragma: no cover - exercised implicitly by Python >=3.11 requirement
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - defensive only
    tomllib = None  # type: ignore[assignment]

_CONFIG_DIRNAME = ".meridian"
_CONFIG_FILENAME = "tool-routing.toml"

ROUTING_MODES = frozenset({"shadow", "advisory", "enforcing"})
_DEFAULT_MODE = "shadow"
_DEFAULT_CONFIDENCE_THRESHOLD = 0.6

_RULE_REQUIRED_FIELDS = ("id", "priority")
_RULE_ALLOWED_FIELDS = frozenset({
    "id", "priority", "match_keywords", "match_pattern", "category", "tool",
    "required_tools", "fallback_chain", "capability", "notes",
})
_ROUTING_TABLE_ALLOWED_FIELDS = frozenset({"mode", "confidence_threshold"})
_TOP_LEVEL_ALLOWED_FIELDS = frozenset({"routing", "rules"})

# code-intel / docx are answered at layer 3 (category match) before layers
# 4/5 ever run — see module docstring. This set exists only so tests can
# assert the structural guarantee directly against the same vocabulary
# AGENTS.md's "Code intelligence" / "Meridian-docs for OOXML" sections use.
PINNED_CATEGORIES = frozenset({"code-intel", "docx"})


class ToolRoutingConfigError(ValueError):
    """Raised when ``.meridian/tool-routing.toml`` fails schema/safety validation."""


class ToolRoutingInvariantError(RuntimeError):
    """Raised if a routing-layer invariant (e.g. semantic reorder-only) would be violated.

    This should never be reachable via any public code path — it exists as
    a defense-in-depth assertion that fails loudly (never silently) if a
    future change to this module breaks the "semantic reranking never
    removes/adds a candidate" contract.
    """


# ---------------------------------------------------------------------------
# Schema validation — mirrors meridian.capability_manifest's strict,
# fail-closed, deterministic-ordering style exactly (same rejection shape:
# unknown fields reject, required fields reject when missing/empty, output
# is sorted so identical input always normalizes identically).
# ---------------------------------------------------------------------------


def normalize_rule(raw: Any) -> dict[str, Any]:
    """Validate and normalize a single ``[[rules]]`` entry.

    Raises :class:`ToolRoutingConfigError` on any schema/safety violation.
    """
    if not isinstance(raw, dict):
        raise ToolRoutingConfigError("rule entry must be an object")
    unknown = set(raw) - _RULE_ALLOWED_FIELDS
    if unknown:
        raise ToolRoutingConfigError(f"unknown rule field(s): {sorted(unknown)}")
    for field in _RULE_REQUIRED_FIELDS:
        if raw.get(field) in (None, ""):
            raise ToolRoutingConfigError(f"rule missing required field: {field}")

    rule_id = raw["id"]
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise ToolRoutingConfigError("rule id must be a non-empty string")
    rule_id = rule_id.strip()

    priority = raw["priority"]
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise ToolRoutingConfigError(f"rule[{rule_id}]: priority must be an integer")

    match_keywords = raw.get("match_keywords") or []
    if not isinstance(match_keywords, list) or not all(
        isinstance(k, str) and k.strip() for k in match_keywords
    ):
        raise ToolRoutingConfigError(
            f"rule[{rule_id}]: match_keywords must be a list of non-empty strings"
        )
    match_keywords = [k.strip() for k in match_keywords]

    match_pattern = raw.get("match_pattern")
    if match_pattern is not None:
        if not isinstance(match_pattern, str) or not match_pattern.strip():
            raise ToolRoutingConfigError(
                f"rule[{rule_id}]: match_pattern must be a non-empty string or null"
            )
        match_pattern = match_pattern.strip()
        try:
            re.compile(match_pattern)
        except re.error as exc:
            raise ToolRoutingConfigError(
                f"rule[{rule_id}]: match_pattern is not a valid regex: {exc}"
            ) from exc

    if not match_keywords and not match_pattern:
        raise ToolRoutingConfigError(
            f"rule[{rule_id}]: must declare at least one of match_keywords/match_pattern"
        )

    category = raw.get("category")
    if category is not None and (not isinstance(category, str) or not category.strip()):
        raise ToolRoutingConfigError(f"rule[{rule_id}]: category must be a non-empty string or null")
    category = category.strip() if category else None

    tool = raw.get("tool")
    if tool is not None and (not isinstance(tool, str) or not tool.strip()):
        raise ToolRoutingConfigError(f"rule[{rule_id}]: tool must be a non-empty string or null")
    tool = tool.strip() if tool else None

    if not category and not tool:
        raise ToolRoutingConfigError(
            f"rule[{rule_id}]: must declare at least one of category/tool"
        )

    required_tools = raw.get("required_tools") or []
    if not isinstance(required_tools, list) or not all(isinstance(t, str) for t in required_tools):
        raise ToolRoutingConfigError(f"rule[{rule_id}]: required_tools must be a list of strings")
    required_tools = [t.strip() for t in required_tools if t.strip()]

    fallback_chain = raw.get("fallback_chain") or []
    if not isinstance(fallback_chain, list) or not all(isinstance(t, str) for t in fallback_chain):
        raise ToolRoutingConfigError(f"rule[{rule_id}]: fallback_chain must be a list of strings")
    fallback_chain = [t.strip() for t in fallback_chain if t.strip()]

    capability = raw.get("capability")
    if capability is not None and (not isinstance(capability, str) or not capability.strip()):
        raise ToolRoutingConfigError(f"rule[{rule_id}]: capability must be a non-empty string or null")
    capability = capability.strip() if capability else None

    notes = raw.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise ToolRoutingConfigError(f"rule[{rule_id}]: notes must be a string or null")

    normalized = {
        "id": rule_id,
        "priority": priority,
        "match_keywords": match_keywords,
        "match_pattern": match_pattern,
        "category": category,
        "tool": tool,
        "required_tools": required_tools,
        "fallback_chain": fallback_chain,
        "capability": capability,
        "notes": notes,
    }
    try:
        _check_no_secrets_or_local_paths(normalized, path=f"rule[{rule_id}]")
    except CapabilityManifestError as exc:
        # Reuse capability_manifest's exact secret/path validation (per
        # AGENTS.md's provenance rules — never re-implement it), but this
        # module's public API only ever raises its own exception type so
        # callers need one except clause, not two.
        raise ToolRoutingConfigError(str(exc)) from exc
    return normalized


def normalize_routing_config(raw: Any) -> dict[str, Any]:
    """Validate and canonicalize a full ``tool-routing.toml`` document.

    Deterministic: rules are always returned sorted by
    ``(-priority, id)`` — highest priority first, ties broken by id — so
    evaluation order is identical regardless of the .toml's own array
    order, and identical input always normalizes identically (mirrors
    ``capability_manifest.normalize_manifest``'s id-sort determinism).

    Raises :class:`ToolRoutingConfigError` on any schema/safety violation —
    this contract fails CLOSED on a malformed config: an operator error in
    an explicit routing rule must never be silently ignored, unlike
    ``meridian.toml_config``'s best-effort connection-profile reader (a
    malformed routing rule could silently misroute or silently fail to gate
    a required tool, which is exactly the class of "no guessing" mistake
    this whole item exists to prevent).
    """
    if not isinstance(raw, dict):
        raise ToolRoutingConfigError("tool-routing.toml must be a table")
    unknown = set(raw) - _TOP_LEVEL_ALLOWED_FIELDS
    if unknown:
        raise ToolRoutingConfigError(f"unknown top-level table(s): {sorted(unknown)}")

    routing_tbl = raw.get("routing") or {}
    if not isinstance(routing_tbl, dict):
        raise ToolRoutingConfigError("[routing] must be a table")
    unknown_routing = set(routing_tbl) - _ROUTING_TABLE_ALLOWED_FIELDS
    if unknown_routing:
        raise ToolRoutingConfigError(f"unknown [routing] field(s): {sorted(unknown_routing)}")

    mode = routing_tbl.get("mode", _DEFAULT_MODE)
    if not isinstance(mode, str) or mode.strip().lower() not in ROUTING_MODES:
        raise ToolRoutingConfigError(f"[routing].mode must be one of {sorted(ROUTING_MODES)}")
    mode = mode.strip().lower()

    confidence_threshold = routing_tbl.get("confidence_threshold", _DEFAULT_CONFIDENCE_THRESHOLD)
    if isinstance(confidence_threshold, bool) or not isinstance(confidence_threshold, (int, float)):
        raise ToolRoutingConfigError("[routing].confidence_threshold must be a number")
    confidence_threshold = float(confidence_threshold)
    if not (0.0 <= confidence_threshold <= 1.0):
        raise ToolRoutingConfigError("[routing].confidence_threshold must be within [0.0, 1.0]")

    raw_rules = raw.get("rules") or []
    if not isinstance(raw_rules, list):
        raise ToolRoutingConfigError("[[rules]] must be an array of tables")
    rules = [normalize_rule(r) for r in raw_rules]
    ids = [r["id"] for r in rules]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ToolRoutingConfigError(f"duplicate rule id(s): {dupes}")
    rules.sort(key=lambda r: (-r["priority"], r["id"]))

    return {"mode": mode, "confidence_threshold": confidence_threshold, "rules": rules}


def _config_path(base_dir: "str | Path | None" = None) -> "Path | None":
    """Return the path to ``.meridian/tool-routing.toml`` if it exists, else None."""
    root = Path(base_dir) if base_dir is not None else Path.cwd()
    candidate = root / _CONFIG_DIRNAME / _CONFIG_FILENAME
    return candidate if candidate.exists() else None


def load_routing_config(path: "str | Path | None" = None) -> "dict[str, Any] | None":
    """Load + validate ``.meridian/tool-routing.toml``.

    Returns ``None`` when the file does not exist — absence is not an
    error; ``route()`` simply skips the explicit-rules layer. Raises
    :class:`ToolRoutingConfigError` when the file exists but is malformed
    TOML or fails schema validation — a present-but-broken config fails
    closed rather than being silently treated as absent.
    """
    resolved = Path(path) if path is not None else _config_path()
    if resolved is None:
        return None
    if not resolved.exists():
        return None
    if tomllib is None:  # pragma: no cover - Python >=3.11 is a hard requirement
        raise ToolRoutingConfigError("tomllib is unavailable (Python 3.11+ required)")
    try:
        raw = tomllib.loads(resolved.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ToolRoutingConfigError(f"invalid TOML in {resolved}: {exc}") from exc
    return normalize_routing_config(raw)


# ---------------------------------------------------------------------------
# Routing decision
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RoutingDecision:
    """One ``route()`` call's outcome.

    ``stage`` is one of ``"capability_manifest"``, ``"explicit_rule"``,
    ``"category_match"``, ``"bm25"``, ``"semantic_rerank"``, or
    ``"unknown"`` — always reflects which layer of the priority chain
    actually produced the decision.

    ``blocked`` is ``True`` only for a ``"capability_manifest"`` decision
    where a ``required`` capability has no available tool anywhere in its
    ``required_tools``/``fallback_chain`` — the fail-closed mutation gate
    (see :func:`authorize_mutation`) treats a blocked decision as an
    automatic ``False``.

    ``candidates`` is never authoritative on its own — see
    :func:`authorize_mutation` for the one function permitted to turn a
    decision into "safe to use for a write."
    """

    stage: str
    tool: "str | None"
    category: "str | list[str] | None"
    candidates: "list[str]"
    confidence: "float | None"
    reason: str
    blocked: bool
    mode: str
    required_tools: "list[str]"
    fallback_chain: "list[str]"


# ---------------------------------------------------------------------------
# Layer 1 — capability manifest (deterministic, fail-closed for "required").
# ---------------------------------------------------------------------------


def _first_available(names: "list[str]", tool_inventory: "set[str] | None") -> "str | None":
    if tool_inventory is None:
        return None
    for name in names:
        if name in tool_inventory:
            return name
    return None


def _match_capability(
    text: str,
    manifest: "list[dict[str, Any]]",
    tool_inventory: "set[str] | None",
    mode: str,
) -> "RoutingDecision | None":
    """Layer 1. Returns ``None`` to fall through to layer 2 (no match, or
    availability cannot be determined at all — never guessed)."""
    if tool_inventory is None:
        # Cannot determine availability -- never guess "available" or
        # "unavailable"; fall through rather than fabricate a verdict.
        return None
    tokens = _extract_kws(text)
    for cap in manifest:
        cap_id = cap.get("id") or ""
        if not cap_id:
            continue
        purpose = cap.get("purpose") or ""
        haystack = f"{cap_id} {purpose}"
        matched = cap_id.lower() in text.lower() or bool(tokens & _extract_kws(haystack))
        if not matched:
            continue
        required = list(cap.get("required_tools") or [])
        fallback = list(cap.get("fallback_chain") or [])
        policy = (cap.get("availability_policy") or "required").lower()
        available_tool = _first_available(required + fallback, tool_inventory)
        if available_tool is not None:
            return RoutingDecision(
                stage="capability_manifest",
                tool=available_tool,
                category=None,
                candidates=[available_tool],
                confidence=1.0,
                reason=f"capability_manifest_match:{cap_id}",
                blocked=False,
                mode=mode,
                required_tools=required,
                fallback_chain=fallback,
            )
        if policy == "required":
            return RoutingDecision(
                stage="capability_manifest",
                tool=None,
                category=None,
                candidates=[],
                confidence=None,
                reason=f"capability_required_unavailable:{cap_id}",
                blocked=True,
                mode=mode,
                required_tools=required,
                fallback_chain=fallback,
            )
        # optional/degraded_ok with nothing available: this capability
        # doesn't get to claim the request — fall through to the next
        # matching capability (if any) or the next layer.
    return None


# ---------------------------------------------------------------------------
# Layer 2 — explicit rules (deterministic, from the loaded .toml).
# ---------------------------------------------------------------------------


def _rule_matches(rule: "dict[str, Any]", text: str) -> bool:
    lowered = text.lower()
    for kw in rule.get("match_keywords") or []:
        if kw.lower() in lowered:
            return True
    pattern = rule.get("match_pattern")
    if pattern:
        try:
            if re.search(pattern, text):
                return True
        except re.error:  # pragma: no cover - normalize_rule already validated compile
            return False
    return False


def _match_explicit_rule(
    text: str, rules: "list[dict[str, Any]]", mode: str
) -> "RoutingDecision | None":
    for rule in rules:  # already sorted (-priority, id) by normalize_routing_config
        if _rule_matches(rule, text):
            tool = rule.get("tool")
            return RoutingDecision(
                stage="explicit_rule",
                tool=tool,
                category=rule.get("category"),
                candidates=[tool] if tool else [],
                confidence=1.0,
                reason=f"explicit_rule_match:{rule['id']}",
                blocked=False,
                mode=mode,
                required_tools=list(rule.get("required_tools") or []),
                fallback_chain=list(rule.get("fallback_chain") or []),
            )
    return None


# ---------------------------------------------------------------------------
# Layer 5 — optional Model2Vec reranking. ANNOTATE/REORDER ONLY.
# ---------------------------------------------------------------------------


def _reorder_candidates_by_semantic(original: "list[str]", scored_ids: "list[str]") -> "list[str]":
    """Reorder ``original`` by ``scored_ids``' rank — membership-preserving.

    Never drops or adds a candidate. Raises :class:`ToolRoutingInvariantError`
    (should be unreachable) if the reordered set would ever differ from the
    original set.
    """
    rank = {cid: i for i, cid in enumerate(scored_ids)}
    reordered = sorted(
        original,
        key=lambda cid: (0, rank[cid]) if cid in rank else (1, original.index(cid)),
    )
    if set(reordered) != set(original):
        raise ToolRoutingInvariantError(
            "semantic rerank must never change candidate membership"
        )
    return reordered


def _default_semantic_fn() -> "Callable[[str, list[tuple[str, str]]], list[Any]] | None":
    try:
        from meridian.semantic_search import rank_confident
    except Exception:  # noqa: BLE001 - semantic_search itself never raises on import,
        return None  # but guard defensively anyway; layer 5 is always optional.
    return rank_confident


def _maybe_semantic_rerank(
    decision: RoutingDecision,
    text: str,
    candidates: "list[tuple[str, str]]",
    semantic_fn: "Callable[..., list[Any]] | None",
    confidence_threshold: float,
) -> RoutingDecision:
    """Layer 5. Only ever touches ``stage``/``candidates``/``confidence``/
    ``reason`` on the returned decision (via ``dataclasses.replace``) —
    ``tool``/``blocked``/``required_tools``/``fallback_chain`` are
    structurally unchanged, satisfying "may not alter availability or
    authorize writes" by construction, not by convention.
    """
    fn = semantic_fn if semantic_fn is not None else _default_semantic_fn()
    if fn is None or not candidates:
        return decision
    try:
        matches = fn(text, candidates)
    except Exception:  # noqa: BLE001 - semantic layer is best-effort, never fatal
        return decision
    if not matches:
        return decision
    scored_ids = [m.id for m in matches]
    reordered = _reorder_candidates_by_semantic(decision.candidates, scored_ids)
    top = matches[0]
    top_confident = bool(getattr(top, "confident", False))
    new_confidence = (
        float(getattr(top, "fused_score", decision.confidence))
        if top_confident
        else decision.confidence
    )
    return dataclasses.replace(
        decision,
        stage="semantic_rerank",
        candidates=reordered,
        confidence=new_confidence,
        reason=decision.reason + (
            "+semantic_confident" if top_confident else "+semantic_annotated_low_confidence"
        ),
    )


# ---------------------------------------------------------------------------
# route() — the priority chain.
# ---------------------------------------------------------------------------


def route(
    query_text: "str | None",
    *,
    manifest: "list[dict[str, Any]] | None" = None,
    tool_inventory: "set[str] | None" = None,
    routing_config: "dict[str, Any] | None" = None,
    category_affinity: "dict[str, str] | None" = None,
    bm25_fn: "Callable[[str], list[tuple[str, str]]] | None" = None,
    semantic_fn: "Callable[..., list[Any]] | None" = None,
) -> RoutingDecision:
    """Route ``query_text`` through the 5-layer deterministic priority chain.

    Every argument is optional and independently omittable — a caller with
    no capability manifest, no ``.toml`` config, and no BM25/semantic
    wiring still gets a well-formed decision (falling through every layer
    to ``"unknown"``, never raising for missing wiring).

    ``manifest`` — an already-normalized list of capability dicts (see
    ``capability_manifest.normalize_manifest``). ``tool_inventory`` — the
    set of currently-available tool/server names; ``None`` means
    availability cannot be determined, which SKIPS layer 1 entirely rather
    than guessing (never reports a capability as available OR unavailable
    without real inventory data).

    ``routing_config`` — the dict returned by :func:`load_routing_config`
    (or :func:`normalize_routing_config`). ``None`` skips layer 2.

    ``category_affinity`` — defaults to
    ``meridian.mcp_tools._KEYWORD_CATEGORY_AFFINITY`` when omitted, so
    layer 3 matches Meridian's own tool categories out of the box; pass a
    caller-supplied mapping to route among a different tool taxonomy
    entirely (the "beyond Meridian's own tools" generalization).

    ``bm25_fn`` / ``semantic_fn`` — layers 4/5; omitted means those layers
    are skipped (degrade gracefully, never error).
    """
    text = query_text or ""
    mode = (routing_config or {}).get("mode", _DEFAULT_MODE)
    confidence_threshold = (routing_config or {}).get(
        "confidence_threshold", _DEFAULT_CONFIDENCE_THRESHOLD
    )

    # Layer 1 — capability manifest.
    if manifest:
        cap_decision = _match_capability(text, manifest, tool_inventory, mode)
        if cap_decision is not None:
            return cap_decision

    # Layer 2 — explicit rules.
    if routing_config and routing_config.get("rules"):
        rule_decision = _match_explicit_rule(text, routing_config["rules"], mode)
        if rule_decision is not None:
            return rule_decision

    # Layer 3 — exact category matching.
    if category_affinity is None:
        from meridian.mcp_tools import _KEYWORD_CATEGORY_AFFINITY as _default_affinity
        category_affinity = _default_affinity
    matched_categories, kw_signals = match_categories_by_keywords(text, category_affinity)
    if matched_categories:
        return RoutingDecision(
            stage="category_match",
            tool=None,
            category=sorted(matched_categories),
            candidates=sorted(set(kw_signals)),
            confidence=1.0,
            reason="category_match",
            blocked=False,
            mode=mode,
            required_tools=[],
            fallback_chain=[],
        )

    # Layer 4 — BM25 candidate discovery (caller-injected; this module never
    # imports meridian.db directly — see module docstring).
    bm25_candidates: "list[tuple[str, str]]" = []
    if bm25_fn is not None:
        try:
            bm25_candidates = list(bm25_fn(text) or [])
        except Exception:  # noqa: BLE001 - BM25 layer is best-effort, never fatal
            bm25_candidates = []
    if bm25_candidates:
        cand_ids = [cid for cid, _ in bm25_candidates]
        decision = RoutingDecision(
            stage="bm25",
            tool=None,
            category=None,
            candidates=cand_ids,
            confidence=None,
            reason="bm25_candidates",
            blocked=False,
            mode=mode,
            required_tools=[],
            fallback_chain=[],
        )
        # Layer 5 — optional semantic rerank (annotate/reorder only).
        return _maybe_semantic_rerank(
            decision, text, bm25_candidates, semantic_fn, confidence_threshold
        )

    # No layer matched — unknown/low-confidence: return candidates (empty
    # here, since nothing surfaced any), never a guess.
    return RoutingDecision(
        stage="unknown",
        tool=None,
        category=None,
        candidates=[],
        confidence=None,
        reason="no_match_return_candidates_not_guess",
        blocked=False,
        mode=mode,
        required_tools=[],
        fallback_chain=[],
    )


# ---------------------------------------------------------------------------
# Fail-closed mutation gate. A route() decision is ADVISORY ONLY for writes
# until independently re-checked here — never trust route()'s own opinion.
# ---------------------------------------------------------------------------


def authorize_mutation(
    decision: RoutingDecision,
    *,
    tool_inventory: "set[str] | None" = None,
) -> "tuple[bool, str]":
    """Decide whether ``decision`` may be used to authorize a MUTATING action.

    Returns ``(authorized, reason)``. Only the two fully-deterministic
    stages — ``"capability_manifest"`` and ``"explicit_rule"`` — can ever
    authorize a write; ``"category_match"`` (keyword presence, not a tool
    pick), ``"bm25"``, ``"semantic_rerank"``, and ``"unknown"`` NEVER do,
    regardless of confidence — this is the "semantic scores... may not
    authorize writes" contract enforced structurally rather than by
    convention.

    A blocked decision (``decision.blocked``) is always refused. When
    ``tool_inventory`` is supplied, the resolved ``decision.tool`` must
    also actually be present in it — a stale/cached decision naming a tool
    that is no longer connected can never authorize a write either.
    """
    if decision.blocked:
        return False, "decision_is_blocked"
    if decision.stage not in ("capability_manifest", "explicit_rule"):
        return False, f"stage_{decision.stage}_cannot_authorize_write"
    if not decision.tool:
        return False, "no_tool_resolved"
    if tool_inventory is not None and decision.tool not in tool_inventory:
        return False, "tool_not_in_inventory"
    return True, "authorized"
