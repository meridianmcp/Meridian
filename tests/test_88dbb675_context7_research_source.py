"""88dbb675 — Context7 as a registered research source in Meridian's plugin catalog.

Context7 (by Upstash, package @upstash/context7-mcp) is a general-purpose
library/framework documentation MCP: it indexes React, Tailwind, Next.js, and
thousands of other libraries so AI agents get up-to-date API docs without web
search. It complements paper_search (academic papers) and GitHub search
(code/issues) on the research surface.

Context7 is NOT a tunnel built-in (no dedicated server route / fixed slot) because
it is a remote-first MCP with no local binary that fits the existing proxy model.
Instead it is:
  1. Listed in KNOWN_PLUGIN_TOOLS as a known-but-not-yet-bundled catalog entry,
     so the dashboard and docs enumerate it correctly as a user-wirable source.
  2. Documented in AGENTS.md with a ready-to-paste connection snippet.
  3. Added to the RESEARCH ROUTING PROTOCOL in DEFAULT_AGENT_INSTRUCTIONS so
     sessions that have Context7 in their tool list use it first for
     framework/library questions rather than falling back to web search.

The registration is verified here; connection details (confirmed by live web
research during implementation):
  - npm package:  @upstash/context7-mcp  (runtime: npx)
  - remote MCP:   https://mcp.context7.com/mcp  (Streamable HTTP)
  - API key:      optional; free tier works without one; get a key at
                  context7.com/dashboard for higher rate limits.
"""
from __future__ import annotations

from meridian import tunnel_plugins as tp


# ---------------------------------------------------------------------------
# Catalog registration
# ---------------------------------------------------------------------------

def test_context7_appears_in_known_plugin_tools():
    names = [t["name"] for t in tp.known_plugin_tools()]
    assert "context7" in names, "context7 must appear in known_plugin_tools()"


def test_context7_catalog_entry_shape():
    by_name = {t["name"]: t for t in tp.known_plugin_tools()}
    entry = by_name["context7"]

    # Required fields from the catalog schema
    required = {"name", "package", "runtime", "slot", "bundled", "owner_item", "description"}
    assert required <= set(entry), f"context7 entry missing fields: {required - set(entry)}"

    assert entry["package"] == "@upstash/context7-mcp"
    assert entry["runtime"] == "npx"
    assert entry["slot"] is None, "context7 has no dedicated tunnel slot"
    assert entry["bundled"] is False
    assert entry["owner_item"] == "88dbb675"
    assert isinstance(entry["description"], str) and entry["description"]


def test_context7_is_unbundled_not_bundled():
    bundled_names = {t["name"] for t in tp.bundled_plugin_tools()}
    unbundled_names = {t["name"] for t in tp.unbundled_plugin_tools()}
    assert "context7" not in bundled_names
    assert "context7" in unbundled_names


def test_context7_entry_mentions_remote_endpoint():
    by_name = {t["name"]: t for t in tp.known_plugin_tools()}
    desc = by_name["context7"]["description"]
    assert "mcp.context7.com" in desc, (
        "description must reference the official remote endpoint mcp.context7.com"
    )


def test_context7_entry_mentions_api_key_optional():
    by_name = {t["name"]: t for t in tp.known_plugin_tools()}
    desc = by_name["context7"]["description"].lower()
    # The description must convey that the API key is optional / free tier exists
    assert "free" in desc or "optional" in desc or "api key" in desc.replace("_", " "), (
        "description must mention free tier / optional API key"
    )


# ---------------------------------------------------------------------------
# Catalog invariants still hold after adding context7
# ---------------------------------------------------------------------------

def test_catalog_names_still_unique():
    names = [t["name"] for t in tp.known_plugin_tools()]
    assert len(names) == len(set(names)), "catalog must have unique names"


def test_context7_does_not_perturb_resolve_plugins():
    # Adding context7 to the catalog must not change what the tunnel actually
    # spawns: the resolved built-in set is determined by BUILTIN_PLUGINS, not by
    # the catalog.
    resolved_names = [p["name"] for p in tp.resolve_plugins(None)]
    assert "context7" not in resolved_names, (
        "context7 must not appear in resolve_plugins — it has no tunnel slot"
    )
    assert resolved_names == list(tp.builtin_names())


def test_unbundled_invariant_slot_and_owner_item():
    # The catalog invariant: unbundled tools have slot=None and a non-null owner_item.
    for tool in tp.unbundled_plugin_tools():
        assert tool["slot"] is None, f"{tool['name']}: unbundled tool must have slot=None"
        assert tool["owner_item"], f"{tool['name']}: unbundled tool must carry owner_item"


# ---------------------------------------------------------------------------
# Research routing protocol (agent_defaults)
# ---------------------------------------------------------------------------

def test_research_routing_mentions_context7():
    from meridian.agent_defaults import DEFAULT_AGENT_INSTRUCTIONS

    lowered = DEFAULT_AGENT_INSTRUCTIONS.lower()
    assert "context7" in lowered, (
        "DEFAULT_AGENT_INSTRUCTIONS must mention Context7 in the research-routing protocol"
    )


def test_research_routing_mentions_context7_tools():
    from meridian.agent_defaults import DEFAULT_AGENT_INSTRUCTIONS

    # The executor rules should tell agents which Context7 tools to call
    assert "resolve-library-id" in DEFAULT_AGENT_INSTRUCTIONS or \
           "get-library-docs" in DEFAULT_AGENT_INSTRUCTIONS, (
        "Research-routing protocol should name the Context7 tool(s) to call "
        "(resolve-library-id / get-library-docs)"
    )


def test_agent_instructions_standard_version_bumped_for_context7():
    from meridian.agent_defaults import (
        AGENT_INSTRUCTIONS_STANDARD_VERSION,
        DEFAULT_AGENT_INSTRUCTIONS,
    )
    # Context7 is a meaningful research-routing addition — standard version must be >= 6.
    assert AGENT_INSTRUCTIONS_STANDARD_VERSION >= 6, (
        "Standard version must be bumped to >= 6 for the Context7 routing addition"
    )
    assert (
        f"meridian-executor-standard: v{AGENT_INSTRUCTIONS_STANDARD_VERSION}"
        in DEFAULT_AGENT_INSTRUCTIONS
    )


# ---------------------------------------------------------------------------
# AGENTS.md connection example
# ---------------------------------------------------------------------------

def test_agents_md_documents_context7_connection():
    """AGENTS.md must contain a ready-to-use Context7 connection snippet so
    any developer cloning the repo knows how to wire it without consulting
    external docs."""
    import pathlib
    agents_md = pathlib.Path(__file__).parent.parent / "AGENTS.md"
    text = agents_md.read_text(encoding="utf-8")

    assert "context7" in text.lower(), "AGENTS.md must mention Context7"
    assert "@upstash/context7-mcp" in text, (
        "AGENTS.md must include the npm package @upstash/context7-mcp"
    )
    assert "mcp.context7.com" in text, (
        "AGENTS.md must reference the remote endpoint mcp.context7.com"
    )
