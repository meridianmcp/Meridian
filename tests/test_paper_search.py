"""811881c6 — arXiv paper_search: deterministic Atom parsing + tool registration.

The network call (arxiv_search) is only smoke-checked on the empty-query guard so CI
never depends on reaching arXiv; the parsing is fully unit-tested against a fixture feed.
"""
from __future__ import annotations

from meridian.paper_search import arxiv_search, parse_arxiv_atom

_SAMPLE_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.01234v1</id>
    <updated>2024-01-03T10:00:00Z</updated>
    <published>2024-01-02T09:00:00Z</published>
    <title>Attention Is
      All You Need Again</title>
    <summary>  We revisit transformer   attention.  </summary>
    <author><name>Ada Lovelace</name></author>
    <author><name>Alan Turing</name></author>
    <link href="http://arxiv.org/abs/2401.01234v1" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/2401.01234v1" rel="related" type="application/pdf"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/9999.99999v2</id>
    <title>Second Paper</title>
    <summary>Another abstract.</summary>
    <author><name>Grace Hopper</name></author>
  </entry>
</feed>"""


def test_parse_arxiv_atom_extracts_fields():
    papers = parse_arxiv_atom(_SAMPLE_ATOM)
    assert len(papers) == 2
    p = papers[0]
    assert p["arxiv_id"] == "2401.01234v1"
    # title + summary whitespace is normalized to single spaces
    assert p["title"] == "Attention Is All You Need Again"
    assert p["summary"] == "We revisit transformer attention."
    assert p["authors"] == ["Ada Lovelace", "Alan Turing"]
    assert p["url"] == "http://arxiv.org/abs/2401.01234v1"
    assert p["pdf_url"] == "http://arxiv.org/pdf/2401.01234v1"
    assert p["published"] == "2024-01-02T09:00:00Z"


def test_parse_arxiv_atom_respects_limit():
    assert len(parse_arxiv_atom(_SAMPLE_ATOM, limit=1)) == 1


def test_parse_arxiv_atom_never_raises_on_garbage():
    assert parse_arxiv_atom("not xml at all") == []
    assert parse_arxiv_atom("") == []
    assert parse_arxiv_atom("<feed xmlns='http://www.w3.org/2005/Atom'></feed>") == []


async def test_arxiv_search_empty_query_returns_error():
    out = await arxiv_search("   ")
    assert out.get("error")
    assert "results" not in out


def test_paper_search_registered_and_read_only():
    from meridian import mcp_tools

    names = {t["name"] for t in mcp_tools._MCP_TOOLS_LIST}
    assert "paper_search" in names, "paper_search must be advertised in tools/list"
    assert "paper_search" in mcp_tools._READ_ONLY_TOOLS
    entry = next(t for t in mcp_tools._MCP_TOOLS_LIST if t["name"] == "paper_search")
    assert "query" in entry["inputSchema"]["properties"]
    assert entry["inputSchema"]["required"] == ["query"]


def test_research_protocol_names_the_real_tool():
    from meridian.agent_defaults import DEFAULT_AGENT_INSTRUCTIONS

    # the protocol now points at the real callable tool, not just a vague "paper-search"
    assert "paper_search" in DEFAULT_AGENT_INSTRUCTIONS
    assert "paper-search" in DEFAULT_AGENT_INSTRUCTIONS  # legacy phrasing preserved
