"""Thin MCP stdio server wrapping langchain-ai/open_deep_research (item da8fbeec).

Run with ``uvx --from <path> meridian-deep-research-mcp`` (console entry point)
or ``python -m meridian_deep_research.server``.

UNLIKE every sibling extension in this repo (meridian-outputs, meridian-docs,
meridian-codeindex, meridian-file-inspection -- all of which are explicit
"no hosted call is made by any tool here" local-only tools), this server DOES
make a real hosted call on every successful ``deep_research`` invocation: it
drives open_deep_research's compiled LangGraph agent
(``open_deep_research.deep_researcher.deep_researcher``), which calls the
Anthropic Messages API (Claude) for the research/summarization/compression/
final-report steps AND Anthropic's native ``web_search`` server tool for live
retrieval. Nothing here is a local simulation -- a successful call spends real
tokens against the caller's own Anthropic account and reaches the public
internet through Anthropic's web search.

open_deep_research (MIT, langchain-ai) is not itself packaged as an MCP
server -- verified via GitHub code search before writing this module: zero
``FastMCP``/``server.py`` references anywhere in that repository. Its own MCP
usage (``langchain-mcp-adapters``, ``MultiServerMCPClient``) is entirely
client-side (it connects OUT to other MCP servers as research tools), which is
not a server this wrapper can reuse. This module therefore imports the
compiled graph directly and drives it with ``ainvoke`` -- real integration
work, not a config flag.

=== CRITICAL: Anthropic-only Configuration (read before changing defaults) ===

``open_deep_research.configuration.Configuration`` DEFAULTS to
``search_api="tavily"`` AND all four model fields (``research_model``,
``summarization_model``, ``compression_model``, ``final_report_model``)
default to ``openai:gpt-4.1*`` models (verified directly against the pinned
``open-deep-research==0.0.16`` wheel's own ``configuration.py``). Invoking
``deep_researcher`` with a bare/default ``Configuration`` therefore SILENTLY
requires ``OPENAI_API_KEY`` and ``TAVILY_API_KEY`` too -- directly
contradicting this wrapper's one job, which is to need only the caller's own
``ANTHROPIC_API_KEY``. Every call path in this module that constructs a
``Configuration`` MUST go through :func:`_build_configuration`, which
explicitly sets ``search_api="anthropic"`` plus all four model fields to
``anthropic:claude-*`` model strings -- never the bare/default constructor.
:func:`_build_configuration` is also the acceptance-criterion seam: a test can
monkeypatch ``deep_researcher.ainvoke`` and assert on the ``Configuration``
this function produced without needing OPENAI_API_KEY/TAVILY_API_KEY, or any
real network access, to be present.

Anthropic-native web search (``search_api="anthropic"``) is real, working
code in upstream but is NOT exercised by either of upstream's own runnable
eval scripts (both hardcode Tavily "to stay consistent" even when using
Claude as the model) -- it is less field-proven than the Tavily path. Treat a
first real end-to-end run as the actual verification of that path, not an
assumption.

=== API key handling ===

``ANTHROPIC_API_KEY`` is read exactly like ``meridian/dashboard.py``'s direct
env-var fallback (graceful-optional: ``os.environ.get("ANTHROPIC_API_KEY",
"").strip()``), and ONLY inside the tool function -- never at import/startup
time. This is deliberately NOT ``meridian/hosted.py``'s fail-closed
``_require_cfg`` pattern (that one is reserved for hosted-tier billing/infra
secrets); this server must still start and list its tools with no key set at
all, and only degrade (return a structured, non-raising error) when
``deep_research`` is actually invoked without one -- matching the "optional"
``availability_policy`` this capability is declared under
(``capability_manifest``, id ``deep_research``).

Similarly, ``open_deep_research`` itself is imported defensively at module
scope: if the (large, optional-by-default -- it lives behind the
``deep_research`` pixi feature, see root ``pixi.toml``) dependency isn't
installed, the import failure is caught and ``deep_researcher``/
``Configuration``/``SearchAPI`` are left ``None`` rather than crashing the
whole server at startup. ``deep_research`` reports this as a structured
``available: False`` response instead of raising, and
``get_deep_research_status`` (the cheap, no-network diagnostic tool) surfaces
it directly.
"""
from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

try:  # pragma: no cover - exercised only when the optional heavy dependency is absent
    from open_deep_research.configuration import Configuration, SearchAPI
    from open_deep_research.deep_researcher import deep_researcher
except Exception:  # noqa: BLE001 - genuinely any import-time failure should degrade, not crash
    Configuration = None  # type: ignore[assignment,misc]
    SearchAPI = None  # type: ignore[assignment,misc]
    deep_researcher = None  # type: ignore[assignment]

mcp = FastMCP("meridian-deep-research")


# ---------------------------------------------------------------------------
# Anthropic-only defaults (see module docstring's CRITICAL section above).
#
# research_model / compression_model / final_report_model default to the
# Sonnet-tier model (heavier reasoning: driving the tool-calling research
# loop, compressing sub-agent findings, and writing the final report all
# benefit from the stronger model) -- mirroring upstream's own pattern of
# giving those three fields the bigger default model. summarization_model
# defaults to the Haiku-tier model (upstream's own summarization_model
# default is likewise its smallest/cheapest model: high-volume, low-judgment
# work -- summarizing individual raw search results before they reach the
# research loop). Every default below is independently overridable via an
# env var (for a deployment-wide change) or a per-call tool argument (for a
# one-off), so a caller is never stuck with these choices.
# ---------------------------------------------------------------------------
_DEFAULT_RESEARCH_MODEL = os.environ.get(
    "MERIDIAN_DEEP_RESEARCH_MODEL", "anthropic:claude-sonnet-5"
)
_DEFAULT_SUMMARIZATION_MODEL = os.environ.get(
    "MERIDIAN_DEEP_RESEARCH_SUMMARIZATION_MODEL", "anthropic:claude-haiku-4-5"
)
_DEFAULT_COMPRESSION_MODEL = os.environ.get(
    "MERIDIAN_DEEP_RESEARCH_COMPRESSION_MODEL", "anthropic:claude-sonnet-5"
)
_DEFAULT_FINAL_REPORT_MODEL = os.environ.get(
    "MERIDIAN_DEEP_RESEARCH_FINAL_REPORT_MODEL", "anthropic:claude-sonnet-5"
)


def _get_anthropic_api_key() -> str:
    """Graceful-optional read of ANTHROPIC_API_KEY.

    Mirrors ``meridian/dashboard.py``'s direct environment-variable fallback
    exactly (``os.environ.get("ANTHROPIC_API_KEY", "").strip()``) -- never
    raises, never reads any other credential source (no OAuth-token fallback
    here: open_deep_research's ``ChatAnthropic`` model client needs a real API
    key, not a Claude-Code OAuth access token). Returns ``""`` when unset.
    """
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


def _build_configuration(
    *,
    max_concurrent_research_units: int | None = None,
    max_researcher_iterations: int | None = None,
    research_model: str | None = None,
    summarization_model: str | None = None,
    compression_model: str | None = None,
    final_report_model: str | None = None,
) -> Any:
    """Construct a real, validated ``Configuration`` that is Anthropic-only.

    This is the ONE place allowed to construct ``open_deep_research``'s
    ``Configuration`` -- see the module docstring's CRITICAL section. It
    always sets ``search_api="anthropic"`` and all four model fields to
    ``anthropic:claude-*`` strings (the module-level defaults above, or an
    explicit per-call override), regardless of what upstream's own
    ``Configuration`` defaults to. Every other field (iteration/concurrency
    limits) is optional and only set when explicitly given, so upstream's own
    (non-provider-specific) defaults apply otherwise.

    Returns a real ``open_deep_research.configuration.Configuration``
    instance. Raises if ``open_deep_research`` isn't importable -- callers
    must check :data:`Configuration is not None` (or call
    ``get_deep_research_status`` first) before invoking this.
    """
    if Configuration is None:  # pragma: no cover - guarded by callers
        raise RuntimeError(
            "open_deep_research is not installed in this environment; "
            "install the 'deep_research' pixi feature (or `pip install "
            "meridian-deep-research-mcp`) first."
        )
    kwargs: dict[str, Any] = {
        "search_api": "anthropic",
        "research_model": research_model or _DEFAULT_RESEARCH_MODEL,
        "summarization_model": summarization_model or _DEFAULT_SUMMARIZATION_MODEL,
        "compression_model": compression_model or _DEFAULT_COMPRESSION_MODEL,
        "final_report_model": final_report_model or _DEFAULT_FINAL_REPORT_MODEL,
    }
    if max_concurrent_research_units is not None:
        kwargs["max_concurrent_research_units"] = max_concurrent_research_units
    if max_researcher_iterations is not None:
        kwargs["max_researcher_iterations"] = max_researcher_iterations
    return Configuration(**kwargs)


@mcp.tool()
def get_deep_research_status() -> dict[str, Any]:
    """Cheap, no-network diagnostic: is this tool actually usable right now?

    Never makes a network call and never requires an API key -- safe to call
    unconditionally (e.g. from a capability-manifest ``verification_command``,
    or before a UI enables the "deep research" action) to distinguish
    "package not installed" from "package installed but no API key yet" from
    "ready".

    Returns:
      {available, api_key_configured, ready, defaults} where:
        - ``available``: whether ``open_deep_research`` imported successfully.
        - ``api_key_configured``: whether ``ANTHROPIC_API_KEY`` is currently
          set (non-empty) in this process's environment. Never returns the
          key itself.
        - ``ready``: ``available and api_key_configured`` -- whether
          ``deep_research`` would actually attempt a real call right now.
        - ``defaults``: the anthropic-only ``search_api``/model defaults
          :func:`_build_configuration` uses when a call doesn't override them.
    """
    available = Configuration is not None and deep_researcher is not None
    api_key_configured = bool(_get_anthropic_api_key())
    return {
        "available": available,
        "api_key_configured": api_key_configured,
        "ready": available and api_key_configured,
        "defaults": {
            "search_api": "anthropic",
            "research_model": _DEFAULT_RESEARCH_MODEL,
            "summarization_model": _DEFAULT_SUMMARIZATION_MODEL,
            "compression_model": _DEFAULT_COMPRESSION_MODEL,
            "final_report_model": _DEFAULT_FINAL_REPORT_MODEL,
        },
    }


@mcp.tool()
async def deep_research(
    query: str,
    max_concurrent_research_units: int | None = None,
    max_researcher_iterations: int | None = None,
    research_model: str | None = None,
    summarization_model: str | None = None,
    compression_model: str | None = None,
    final_report_model: str | None = None,
) -> dict[str, Any]:
    """Run a real, multi-step deep-research agent over ``query`` and return
    its final report.

    THIS TOOL MAKES A REAL HOSTED CALL -- see the module docstring. Every
    successful invocation drives open_deep_research's compiled LangGraph
    agent end to end (clarify -> research brief -> supervised, concurrent
    sub-agent research -> compression -> final report), spending real
    Anthropic API tokens and making real Anthropic-native ``web_search``
    calls against the live internet. This is not a local simulation and
    has no cached/offline mode.

    Configured Anthropic-only on every call -- see :func:`_build_configuration`
    and the module docstring's CRITICAL section: ``search_api`` is always
    ``"anthropic"``, and all four model fields default to ``anthropic:claude-*``
    models (overridable per-call via the ``*_model`` arguments below, or
    deployment-wide via the ``MERIDIAN_DEEP_RESEARCH_*_MODEL`` env vars). This
    tool never falls back to open_deep_research's own OpenAI/Tavily defaults.

    Args:
      query:                          The research question or task.
      max_concurrent_research_units:  Optional override for how many
                                      sub-agent research units the supervisor
                                      may run concurrently (upstream default
                                      applies when omitted).
      max_researcher_iterations:      Optional override for how many
                                      reflection/follow-up iterations the
                                      Research Supervisor may run (upstream
                                      default applies when omitted).
      research_model:                 Optional override, e.g.
                                      "anthropic:claude-opus-5" for harder
                                      queries. Must be an "anthropic:..."
                                      model string -- this tool does not
                                      support switching ``search_api`` away
                                      from "anthropic".
      summarization_model:            Optional override (see research_model).
      compression_model:              Optional override (see research_model).
      final_report_model:             Optional override (see research_model).

    Returns:
      On success: {"ok": True, "final_report": <str>, "notes": [...],
      "raw_notes_count": <int>, "configuration": {...}} -- ``configuration``
      echoes back the exact Anthropic-only settings this call used (search_api
      + all four model fields), so a caller can confirm no OpenAI/Tavily
      config was ever in play.

      On a graceful degrade (package not installed, or no API key set):
      {"ok": False, "available": <bool>, "api_key_configured": <bool>,
      "error": <str>} -- never raises for either of these two expected,
      "optional capability not ready yet" cases.

      On a real failure during the actual research call (network error,
      Anthropic API error, an upstream graph error, ...): {"ok": False,
      "error": <str>, "error_type": <exception class name>} -- caught and
      returned structured, never left to propagate and crash the MCP
      session, since this tool depends on a live external API on every call.
    """
    if Configuration is None or deep_researcher is None:
        return {
            "ok": False,
            "available": False,
            "api_key_configured": bool(_get_anthropic_api_key()),
            "error": (
                "open_deep_research is not installed in this environment; "
                "install the 'deep_research' pixi feature (or `pip install "
                "meridian-deep-research-mcp`) first."
            ),
        }

    api_key = _get_anthropic_api_key()
    if not api_key:
        return {
            "ok": False,
            "available": True,
            "api_key_configured": False,
            "error": (
                "ANTHROPIC_API_KEY is not set. This tool degrades gracefully "
                "rather than requiring the key at server startup -- set "
                "ANTHROPIC_API_KEY and retry."
            ),
        }

    configuration = _build_configuration(
        max_concurrent_research_units=max_concurrent_research_units,
        max_researcher_iterations=max_researcher_iterations,
        research_model=research_model,
        summarization_model=summarization_model,
        compression_model=compression_model,
        final_report_model=final_report_model,
    )
    configurable = configuration.model_dump(mode="json")

    try:
        result = await deep_researcher.ainvoke(
            {"messages": [{"role": "user", "content": query}]},
            config={"configurable": configurable},
        )
    except Exception as exc:  # noqa: BLE001 - a live external-API call; never crash the session
        return {
            "ok": False,
            "available": True,
            "api_key_configured": True,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "configuration": configurable,
        }

    final_report = result.get("final_report", "") if isinstance(result, dict) else ""
    notes = result.get("notes", []) if isinstance(result, dict) else []
    raw_notes = result.get("raw_notes", []) if isinstance(result, dict) else []
    return {
        "ok": True,
        "final_report": final_report,
        "notes": notes,
        "raw_notes_count": len(raw_notes),
        "configuration": configurable,
    }


def main() -> None:
    """Entry point for the ``meridian-deep-research-mcp`` console script."""
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
