"""GENERIC POINTER PRIMITIVE — 2976e168.

A single, composable way to point at *a thing in a source* from a Meridian sprint
item, grounded in two established models (NOT invented fresh):

* **LSP Location / WorkspaceEdit.** A pointer's ``targets`` is an ARRAY of
  ``{uri, selector, subSelector?}`` objects — native multi-file, exactly like an
  LSP ``WorkspaceEdit`` maps one edit across many files. A ``range`` selector is
  an LSP ``Range`` (``{start_line, start_char, end_line, end_char}``).
* **W3C Web Annotation Selector composition.** A ``selector`` names *how* to point
  inside the ``uri`` (``range`` | ``symbol`` | ``node_id`` | ``zotero_key``), and
  an optional ``subSelector`` (W3C ``hasSubSelector``) nests finer granularity —
  e.g. ``symbol`` + a ``range`` subSelector = "these lines, within this function".

Shape (stored as JSON on ``sprint_item_pointers.targets`` — NOT per-domain
columns, the core requirement)::

    pointer = {
        "source_type": "code" | "docs" | "citation" | ...,
        "targets": [
            {"uri": "...", "selector": {"type": ..., ...},
             "subSelector": {"type": ..., ...}?},
            ...
        ],
        "label": "..."?,
    }

Selector variants:

* ``range``     — a line span: ``{start_line, start_char, end_line, end_char}``
                  (LSP Range; the pointer IS the location — resolved as-is).
* ``symbol``    — ``{qualified_name}`` (reuses codebase-memory-mcp's field;
                  resolved via ``db.search_graph_entities`` to a file+line).
* ``node_id``   — ``{id}`` (a ``meridian.doc_store`` element id, 9ee6d2ec).
* ``zotero_key``— ``{key}`` (resolved via ``zotero_client.resolve_citation_ref``).
* ``text_quote``— ``{exact, prefix?, suffix?, archived_url?, archived_at?}`` (W3C
                  TextQuoteSelector for source_type "web", 1d3f6e71; resolving it
                  re-fetches the URL and flags content drift).
* ``finding_id``— ``{id}`` (source_type "experiment", 1f1cd4d9; a ``save_finding``
                  artifact note resolved via ``db.get_project_note``).

This module owns:

* :func:`validate_pointer` — validates the composite shape, rejecting malformed
  input (bad selector.type, missing selector fields, malformed subSelector).
* :func:`serialize_targets` / :func:`deserialize_targets` — JSON round-trip for
  the ``targets`` column.
* :func:`resolve_pointer` — the ONE resolver, dispatching by ``selector.type``.
  Every dispatch is best-effort and guarded: an unresolvable target yields
  ``{"resolved": False, "reason": ...}``; the resolver **never raises**.

The resolver's external dependencies (code-graph search, doc_store lookup, Zotero
resolver) are all injectable so tests can stub them without touching a network or
a live Zotero.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

_log = logging.getLogger(__name__)


# The selector types the primitive dispatches on:
#   range / symbol / node_id / zotero_key — the original four.
#   text_quote  — W3C TextQuoteSelector for source_type "web" (1d3f6e71): the exact
#                 cited passage on a URL, carrying the Internet-Archive snapshot
#                 captured at citation time. Resolving it re-fetches live and flags
#                 content drift (the passage silently changed/vanished).
#   finding_id  — addresses a save_finding artifact (a kind='finding' note) for
#                 source_type "experiment" (1f1cd4d9): a zero-ceremony run log
#                 (input / output / params / timestamp), no stages, no YAML.
_SELECTOR_TYPES = frozenset(
    {"range", "symbol", "node_id", "zotero_key", "text_quote", "finding_id"}
)

# Integer fields an LSP Range carries. start_line/end_line are required; the char
# offsets are optional (a whole-line span is valid) but must be ints when present.
_RANGE_REQUIRED_INTS = ("start_line", "end_line")
_RANGE_OPTIONAL_INTS = ("start_char", "end_char")


class PointerValidationError(ValueError):
    """Raised by :func:`validate_pointer` when a pointer's shape is malformed."""


# ---------------------------------------------------------------------------
# Validation (pure — no DB, unit-testable)
# ---------------------------------------------------------------------------

def _validate_selector(selector: Any, *, is_sub: bool = False) -> dict[str, Any]:
    """Validate one selector (or subSelector) dict; return a normalized copy.

    A selector is ``{"type": <one of _SELECTOR_TYPES>, ...type-specific fields}``.
    ``is_sub`` only affects error wording (a subSelector is itself a full
    selector — W3C hasSubSelector — validated by the SAME rules, recursively, so
    a subSelector may itself carry a subSelector).
    """
    what = "subSelector" if is_sub else "selector"
    if not isinstance(selector, dict):
        raise PointerValidationError(f"{what} must be an object")
    stype = selector.get("type")
    if stype not in _SELECTOR_TYPES:
        raise PointerValidationError(
            f"{what}.type must be one of {sorted(_SELECTOR_TYPES)}, got {stype!r}"
        )
    out: dict[str, Any] = {"type": stype}

    if stype == "range":
        for field in _RANGE_REQUIRED_INTS:
            val = selector.get(field)
            if not isinstance(val, int) or isinstance(val, bool):
                raise PointerValidationError(
                    f"{what} range requires integer {field!r}"
                )
            out[field] = val
        for field in _RANGE_OPTIONAL_INTS:
            if field in selector and selector[field] is not None:
                val = selector[field]
                if not isinstance(val, int) or isinstance(val, bool):
                    raise PointerValidationError(
                        f"{what} range {field!r} must be an integer"
                    )
                out[field] = val
    elif stype == "symbol":
        qn = selector.get("qualified_name")
        if not isinstance(qn, str) or not qn.strip():
            raise PointerValidationError(
                f"{what} symbol requires a non-empty qualified_name"
            )
        out["qualified_name"] = qn.strip()
    elif stype == "node_id":
        nid = selector.get("id")
        if not isinstance(nid, str) or not nid.strip():
            raise PointerValidationError(
                f"{what} node_id requires a non-empty id"
            )
        out["id"] = nid.strip()
    elif stype == "zotero_key":
        key = selector.get("key")
        if not isinstance(key, str) or not key.strip():
            raise PointerValidationError(
                f"{what} zotero_key requires a non-empty key"
            )
        out["key"] = key.strip()
    elif stype == "text_quote":
        # 1d3f6e71 — W3C TextQuoteSelector: the exact cited passage, optionally
        # bracketed by prefix/suffix for disambiguation. `exact` is stored VERBATIM
        # (not stripped) — leading/trailing whitespace is part of an exact match.
        # archived_url / archived_at carry the Internet-Archive snapshot captured at
        # citation time (set by add_sprint_item_pointer for source_type "web").
        exact = selector.get("exact")
        if not isinstance(exact, str) or not exact.strip():
            raise PointerValidationError(
                f"{what} text_quote requires a non-empty exact"
            )
        out["exact"] = exact
        for opt in ("prefix", "suffix", "archived_url", "archived_at"):
            if opt in selector and selector[opt] is not None:
                val = selector[opt]
                if not isinstance(val, str):
                    raise PointerValidationError(
                        f"{what} text_quote {opt!r} must be a string"
                    )
                out[opt] = val
    elif stype == "finding_id":
        # 1f1cd4d9 — addresses a save_finding artifact (a kind='finding' note) by id.
        fid = selector.get("id")
        if not isinstance(fid, str) or not fid.strip():
            raise PointerValidationError(
                f"{what} finding_id requires a non-empty id"
            )
        out["id"] = fid.strip()

    # W3C hasSubSelector — optional, recursive, validated by the same rules.
    sub = selector.get("subSelector")
    if sub is not None:
        out["subSelector"] = _validate_selector(sub, is_sub=True)
    return out


def _validate_target(target: Any) -> dict[str, Any]:
    """Validate one ``{uri, selector, subSelector?}`` target; return a normalized copy.

    A ``subSelector`` at the TARGET level (a peer of ``selector``) is accepted as
    an alias for nesting it under the selector — some callers put it there per the
    W3C shape. Either placement is normalized into ``selector.subSelector``.
    """
    if not isinstance(target, dict):
        raise PointerValidationError("each target must be an object")
    uri = target.get("uri")
    if not isinstance(uri, str) or not uri.strip():
        raise PointerValidationError("each target requires a non-empty uri")
    if "selector" not in target:
        raise PointerValidationError("each target requires a selector")
    selector_src = dict(target["selector"]) if isinstance(target["selector"], dict) else target["selector"]
    # A target-level subSelector is folded into the selector (unless the selector
    # already carries its own, which wins).
    if isinstance(selector_src, dict) and target.get("subSelector") is not None:
        selector_src.setdefault("subSelector", target["subSelector"])
    selector = _validate_selector(selector_src)
    return {"uri": uri.strip(), "selector": selector}


def validate_pointer(pointer: Any) -> dict[str, Any]:
    """Validate a full pointer and return a normalized copy.

    Enforces ``{source_type: non-empty str, targets: non-empty list of
    {uri, selector, subSelector?}, label?: str}``. Raises
    :class:`PointerValidationError` on any malformed field. The returned dict is a
    fresh, normalized structure safe to serialize.
    """
    if not isinstance(pointer, dict):
        raise PointerValidationError("pointer must be an object")
    source_type = pointer.get("source_type")
    if not isinstance(source_type, str) or not source_type.strip():
        raise PointerValidationError("pointer requires a non-empty source_type")
    targets = pointer.get("targets")
    if not isinstance(targets, list) or not targets:
        raise PointerValidationError(
            "pointer requires a non-empty targets array"
        )
    normalized_targets = [_validate_target(t) for t in targets]
    out: dict[str, Any] = {
        "source_type": source_type.strip(),
        "targets": normalized_targets,
    }
    label = pointer.get("label")
    if label is not None:
        if not isinstance(label, str):
            raise PointerValidationError("label must be a string when provided")
        out["label"] = label
    return out


# ---------------------------------------------------------------------------
# Serialize / deserialize the JSON ``targets`` column
# ---------------------------------------------------------------------------

def serialize_targets(targets: list[dict[str, Any]]) -> str:
    """Serialize a validated ``targets`` list to the JSON stored in the column."""
    return json.dumps(targets, ensure_ascii=False, sort_keys=True)


def deserialize_targets(raw: Any) -> list[dict[str, Any]]:
    """Deserialize the JSON ``targets`` column back into a list.

    Tolerant: a None/blank column, or an already-decoded list, or malformed JSON
    all degrade to ``[]`` rather than raising — a stored pointer must always be
    readable even if its column was somehow corrupted.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str):
        return []
    try:
        decoded = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return decoded if isinstance(decoded, list) else []


def row_to_pointer(row: dict[str, Any]) -> dict[str, Any]:
    """Materialize a ``sprint_item_pointers`` DB row into a pointer dict.

    Deserializes the JSON ``targets`` column and echoes the id / sprint_item_id /
    source_type / label / created_at, so callers get the full pointer shape plus
    its persistence metadata.
    """
    return {
        "id": row.get("id"),
        "project_id": row.get("project_id"),
        "sprint_item_id": row.get("sprint_item_id"),
        "source_type": row.get("source_type"),
        "targets": deserialize_targets(row.get("targets")),
        "label": row.get("label"),
        "created_at": row.get("created_at"),
    }


# ---------------------------------------------------------------------------
# The ONE resolver — dispatch by selector.type. Best-effort; NEVER raises.
# ---------------------------------------------------------------------------

# Injectable resolver seams (tests stub these; production wires the real ones).
SymbolResolver = Callable[..., Awaitable[list[dict[str, Any]]]]
NodeResolver = Callable[..., Awaitable[dict[str, Any] | None]]
CitationResolver = Callable[..., Awaitable[dict[str, Any] | None]]
WebFetcher = Callable[..., Awaitable[str | None]]
FindingResolver = Callable[..., Awaitable[dict[str, Any] | None]]


def _unresolved(reason: str, **extra: Any) -> dict[str, Any]:
    """Build a guarded unresolved result: ``{resolved: False, reason, ...}``."""
    return {"resolved": False, "reason": reason, **extra}


async def _resolve_range(selector: dict[str, Any], uri: str) -> dict[str, Any]:
    """``range`` — the pointer IS the location. Return ``{uri, range}`` as-is."""
    rng = {k: selector[k] for k in ("start_line", "start_char", "end_line", "end_char")
           if k in selector}
    return {"resolved": True, "selector_type": "range", "uri": uri, "range": rng}


async def _resolve_symbol(
    db: Any,
    project_id: str,
    selector: dict[str, Any],
    uri: str,
    symbol_resolver: SymbolResolver,
) -> dict[str, Any]:
    """``symbol`` — best-match the qualified_name in the cached code graph."""
    qn = selector.get("qualified_name")
    try:
        matches = await symbol_resolver(db, project_id, qn, 5)
    except Exception:  # noqa: BLE001 — a search failure is just "unresolvable"
        _log.debug("symbol resolve failed for %r", qn, exc_info=True)
        return _unresolved("symbol lookup failed", selector_type="symbol",
                           uri=uri, qualified_name=qn)
    if not matches:
        return _unresolved("no matching symbol in graph snapshot",
                           selector_type="symbol", uri=uri, qualified_name=qn)
    # Prefer an exact qualified_name match; else the first (best-ranked) row.
    best = None
    for m in matches:
        if isinstance(m, dict) and str(m.get("qualified_name") or "") == str(qn):
            best = m
            break
    best = best or matches[0]
    return {
        "resolved": True,
        "selector_type": "symbol",
        "uri": uri,
        "qualified_name": qn,
        "file": (best.get("file") if isinstance(best, dict) else None),
        "match": best,
    }


async def _resolve_node_id(
    selector: dict[str, Any],
    uri: str,
    node_resolver: NodeResolver | None,
) -> dict[str, Any]:
    """``node_id`` — look an element up in the doc_store by its id (9ee6d2ec)."""
    nid = selector.get("id")
    if node_resolver is None:
        return _unresolved("no doc_store available", selector_type="node_id",
                           uri=uri, id=nid)
    try:
        found = await node_resolver(nid)
    except Exception:  # noqa: BLE001 — store lookup failure → unresolvable
        _log.debug("node_id resolve failed for %r", nid, exc_info=True)
        return _unresolved("doc_store lookup failed", selector_type="node_id",
                           uri=uri, id=nid)
    if not found:
        return _unresolved("no element with that id", selector_type="node_id",
                           uri=uri, id=nid)
    return {
        "resolved": True,
        "selector_type": "node_id",
        "uri": uri,
        "id": nid,
        "element": (found.get("element") if isinstance(found, dict) else found),
        "document": (found.get("document") if isinstance(found, dict) else None),
    }


async def _resolve_zotero_key(
    selector: dict[str, Any],
    uri: str,
    citation_resolver: CitationResolver,
) -> dict[str, Any]:
    """``zotero_key`` — resolve via ``resolve_citation_ref('zotero:'+key)``."""
    key = selector.get("key")
    try:
        item = await citation_resolver(f"zotero:{key}")
    except Exception:  # noqa: BLE001 — the citation resolver never raises, but be safe
        _log.debug("zotero resolve failed for %r", key, exc_info=True)
        return _unresolved("zotero resolve failed", selector_type="zotero_key",
                           uri=uri, key=key)
    if not isinstance(item, dict) or not item.get("zotero_key"):
        return _unresolved("zotero item not found (or Zotero unavailable)",
                           selector_type="zotero_key", uri=uri, key=key)
    return {
        "resolved": True,
        "selector_type": "zotero_key",
        "uri": uri,
        "key": key,
        "item": item,
    }


async def _resolve_text_quote(
    selector: dict[str, Any],
    uri: str,
    web_fetcher: WebFetcher | None,
) -> dict[str, Any]:
    """``text_quote`` — is the exact cited passage still present at ``uri``? (1d3f6e71)

    Fetches the live page (injectable ``web_fetcher``) and checks whether ``exact``
    (bracketed by ``prefix``/``suffix`` when given) is still present. A missing
    passage is a RESOLVED result with ``drift: True`` — the URL's content changed
    since it was cited — not an error. Echoes the ``archived_url`` snapshot captured
    at citation time so a caller can fall back to the archived copy.
    """
    exact = selector.get("exact") or ""
    base: dict[str, Any] = {"selector_type": "text_quote", "uri": uri, "exact": exact}
    if selector.get("archived_url"):
        base["archived_url"] = selector["archived_url"]
    if web_fetcher is None:
        return _unresolved("no web fetcher available", **base)
    try:
        text = await web_fetcher(uri)
    except Exception:  # noqa: BLE001 — a fetch failure is just "unresolvable"
        _log.debug("web fetch failed for %r", uri, exc_info=True)
        return _unresolved("web fetch failed", **base)
    if text is None:
        return _unresolved("web fetch returned nothing", **base)
    prefix = selector.get("prefix") or ""
    suffix = selector.get("suffix") or ""
    needle = f"{prefix}{exact}{suffix}" if (prefix or suffix) else exact
    present = needle in text
    return {"resolved": True, **base, "found": present, "drift": not present}


async def _resolve_finding_id(
    selector: dict[str, Any],
    uri: str,
    finding_resolver: FindingResolver | None,
) -> dict[str, Any]:
    """``finding_id`` — resolve a save_finding artifact (a note) by id (1f1cd4d9)."""
    fid = selector.get("id")
    if finding_resolver is None:
        return _unresolved("no finding resolver available",
                           selector_type="finding_id", uri=uri, id=fid)
    try:
        found = await finding_resolver(fid)
    except Exception:  # noqa: BLE001 — lookup failure → unresolvable
        _log.debug("finding_id resolve failed for %r", fid, exc_info=True)
        return _unresolved("finding lookup failed",
                           selector_type="finding_id", uri=uri, id=fid)
    if not found:
        return _unresolved("no finding artifact with that id",
                           selector_type="finding_id", uri=uri, id=fid)
    return {
        "resolved": True, "selector_type": "finding_id", "uri": uri,
        "id": fid, "artifact": found,
    }


async def _resolve_selector(
    db: Any,
    project_id: str,
    selector: dict[str, Any],
    uri: str,
    *,
    symbol_resolver: SymbolResolver,
    node_resolver: NodeResolver | None,
    citation_resolver: CitationResolver,
    web_fetcher: WebFetcher | None = None,
    finding_resolver: FindingResolver | None = None,
) -> dict[str, Any]:
    """Dispatch ONE selector to its type-specific resolver (guarded)."""
    stype = selector.get("type")
    if stype == "range":
        return await _resolve_range(selector, uri)
    if stype == "symbol":
        return await _resolve_symbol(db, project_id, selector, uri, symbol_resolver)
    if stype == "node_id":
        return await _resolve_node_id(selector, uri, node_resolver)
    if stype == "zotero_key":
        return await _resolve_zotero_key(selector, uri, citation_resolver)
    if stype == "text_quote":
        return await _resolve_text_quote(selector, uri, web_fetcher)
    if stype == "finding_id":
        return await _resolve_finding_id(selector, uri, finding_resolver)
    return _unresolved(f"unknown selector.type {stype!r}", uri=uri)


async def resolve_pointer(
    db: Any,
    pointer: dict[str, Any],
    *,
    project_id: str | None = None,
    symbol_resolver: SymbolResolver | None = None,
    node_resolver: NodeResolver | None = None,
    citation_resolver: CitationResolver | None = None,
    web_fetcher: WebFetcher | None = None,
    finding_resolver: FindingResolver | None = None,
) -> dict[str, Any]:
    """Resolve every target of a pointer, dispatching by ``selector.type``.

    Returns ``{source_type, label?, targets: [<resolved-target>, ...]}`` where each
    resolved-target carries ``{uri, selector_type, resolved, ...}``. A ``range``
    target returns its location as-is; ``symbol`` / ``node_id`` / ``zotero_key``
    each call their (best-effort, injectable) resolver. An unresolvable target
    yields ``{resolved: False, reason}``. A ``subSelector`` is handled by resolving
    the OUTER selector first, then narrowing: the subSelector's resolution is
    attached under ``subResolved`` (and, when the subSelector is a ``range``, its
    ``range`` is echoed as ``narrowed_range`` — "these lines, within this
    function"). **NEVER raises** — malformed targets degrade to unresolved.

    Resolver seams default to the real implementations
    (``db.search_graph_entities`` / doc_store / ``zotero_client``); tests inject
    stubs so no network / live Zotero is touched.
    """
    # Default resolver seams (lazy imports to avoid import cycles / optional deps).
    if symbol_resolver is None:
        from .db import search_graph_entities as _sg  # noqa: PLC0415

        async def symbol_resolver(_db: Any, _pid: str, _q: str, _lim: int):  # type: ignore[misc]
            return await _sg(_db, _pid, _q, _lim)

    if citation_resolver is None:
        from .zotero_client import resolve_citation_ref as _rc  # noqa: PLC0415

        async def citation_resolver(_ref: str):  # type: ignore[misc]
            return await _rc(_ref)

    if web_fetcher is None:
        # 1d3f6e71 — default live fetch for text_quote drift checks (httpx, guarded).
        from .web_archive import default_web_fetcher as _wf  # noqa: PLC0415

        async def web_fetcher(_uri: str):  # type: ignore[misc]
            return await _wf(_uri)

    if finding_resolver is None:
        # 1f1cd4d9 — a save_finding artifact IS a kind='finding' project note.
        from .db import get_project_note as _gn  # noqa: PLC0415

        async def finding_resolver(_id: str):  # type: ignore[misc]
            return await _gn(db, _id)

    pid = project_id or pointer.get("project_id") or ""
    source_type = pointer.get("source_type")
    targets = pointer.get("targets") or []

    resolved_targets: list[dict[str, Any]] = []
    for target in targets:
        if not isinstance(target, dict):
            resolved_targets.append(_unresolved("malformed target"))
            continue
        uri = target.get("uri") or ""
        selector = target.get("selector")
        if not isinstance(selector, dict):
            resolved_targets.append(
                _unresolved("malformed selector", uri=uri)
            )
            continue
        try:
            outer = await _resolve_selector(
                db, pid, selector, uri,
                symbol_resolver=symbol_resolver,
                node_resolver=node_resolver,
                citation_resolver=citation_resolver,
                web_fetcher=web_fetcher,
                finding_resolver=finding_resolver,
            )
        except Exception:  # noqa: BLE001 — belt-and-suspenders: a target never crashes the pass
            _log.debug("resolve_pointer target failed", exc_info=True)
            resolved_targets.append(_unresolved("resolve error", uri=uri))
            continue

        # subSelector — resolve the outer, then narrow. The subSelector is itself a
        # full selector (W3C hasSubSelector); resolve it against the SAME uri.
        sub = selector.get("subSelector")
        if isinstance(sub, dict):
            try:
                sub_resolved = await _resolve_selector(
                    db, pid, sub, uri,
                    symbol_resolver=symbol_resolver,
                    node_resolver=node_resolver,
                    citation_resolver=citation_resolver,
                    web_fetcher=web_fetcher,
                    finding_resolver=finding_resolver,
                )
            except Exception:  # noqa: BLE001
                sub_resolved = _unresolved("subSelector resolve error", uri=uri)
            outer["subResolved"] = sub_resolved
            # "these lines, within this function": echo a range subSelector's span.
            if sub_resolved.get("resolved") and sub_resolved.get("selector_type") == "range":
                outer["narrowed_range"] = sub_resolved.get("range")
        resolved_targets.append(outer)

    result: dict[str, Any] = {
        "source_type": source_type,
        "targets": resolved_targets,
    }
    if pointer.get("label") is not None:
        result["label"] = pointer.get("label")
    if pointer.get("id") is not None:
        result["id"] = pointer.get("id")
    return result
