# meridian-deep-research

Thin MCP stdio server wrapping [langchain-ai/open_deep_research](https://github.com/langchain-ai/open_deep_research)
(MIT, exact-pinned at `0.0.16`) as Meridian's deep-research capability.

**Unlike every other `extensions/*` package in this repo, this one makes a
real hosted call on every successful `deep_research` invocation.** It drives
open_deep_research's compiled LangGraph agent, which calls the Anthropic
Messages API (Claude) for research/summarization/compression/final-report
generation, plus Anthropic's native `web_search` server tool for live
retrieval. There is no local/offline mode.

## Why a wrapper, not just a config flag

open_deep_research is not itself packaged as an MCP server -- its own MCP
usage (`langchain-mcp-adapters`) is entirely client-side (it connects OUT to
other MCP servers as research tools). This package imports the compiled graph
directly (`open_deep_research.deep_researcher.deep_researcher`) and drives it
with `ainvoke` inside a real MCP tool.

## Anthropic-only, by construction

open_deep_research's `Configuration` defaults to `search_api="tavily"` and all
four model fields default to `openai:gpt-4.1*` models. A bare/default
`Configuration` therefore silently requires `OPENAI_API_KEY` and
`TAVILY_API_KEY` too. This wrapper's `_build_configuration()` (see
`meridian_deep_research/server.py`) always sets `search_api="anthropic"` plus
all four model fields to `anthropic:claude-*` models -- the `deep_research`
tool never falls back to upstream's OpenAI/Tavily defaults, no matter what.
Defaults are overridable per-call (tool arguments) or deployment-wide
(`MERIDIAN_DEEP_RESEARCH_*_MODEL` env vars); `search_api` itself is not
overridable through this tool -- it is always `"anthropic"`.

Anthropic-native web search is real, working code in upstream but is **not**
exercised by either of upstream's own runnable eval scripts (both hardcode
Tavily even when using Claude as the model) -- less field-proven than the
Tavily path.

## Install

This package is intentionally NOT installed by a plain `pixi run` in the
parent Meridian repo -- it pulls in open_deep_research's ~30 transitive
dependencies (LangGraph, `langchain-openai`/`-anthropic`/`-community`, the
optional Azure/AWS/GCP provider SDKs, pandas, ipykernel, ...), which nobody
running the default environment should pay for. Opt in explicitly:

```bash
pixi run -e deep_research python -m meridian_deep_research.server
```

Or standalone:

```bash
uvx --from /path/to/extensions/meridian-deep-research meridian-deep-research-mcp
```

Or add to your MCP client config:

```json
{
  "mcpServers": {
    "meridian-deep-research": {
      "command": "uvx",
      "args": ["--from", "/path/to/extensions/meridian-deep-research", "meridian-deep-research-mcp"],
      "env": { "ANTHROPIC_API_KEY": "sk-ant-..." }
    }
  }
}
```

## API key

`ANTHROPIC_API_KEY` is read exactly like `meridian/dashboard.py`'s direct
environment-variable fallback -- graceful-optional
(`os.environ.get("ANTHROPIC_API_KEY", "").strip()`), checked **inside** the
`deep_research` tool function, never at server startup. The server always
starts and lists its tools with no key set; `deep_research` degrades to a
structured `{"ok": False, ...}` response (never raises) when invoked without
one, and `get_deep_research_status` reports readiness without making any
network call at all.

## Tools

| Tool | Makes a network call? | Description |
|------|:---:|-------------|
| `get_deep_research_status` | No | Cheap diagnostic: is `open_deep_research` installed, is `ANTHROPIC_API_KEY` set, and what are the current Anthropic-only model defaults? |
| `deep_research` | **Yes, always** | Runs the full clarify -> research-brief -> supervised concurrent research -> compression -> final-report pipeline over a query and returns the final report. |

## Testing without live API access

`tests/test_server.py` monkeypatches `deep_researcher.ainvoke` (an
`AsyncMock`) so the full test suite never makes a real network call or spends
real tokens, while still asserting on the exact `Configuration` this wrapper
constructed -- proving the Anthropic-only contract holds without requiring
`OPENAI_API_KEY`/`TAVILY_API_KEY` to be absent from a *real* run (the
strongest test that doesn't require live external APIs). Run with:

```bash
pixi run -e deep_research pytest extensions/meridian-deep-research/tests -q
```
