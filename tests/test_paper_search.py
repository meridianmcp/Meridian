"""811881c6 — arXiv paper_search: deterministic Atom parsing + tool registration.
f65f6111 — plus OpenAlex JSON parsing + the 'source' param.

The network calls (arxiv_search / openalex_search) are only smoke-checked on the
empty-query guard so CI never depends on reaching the network; the parsing is fully
unit-tested against fixture payloads.
"""
from __future__ import annotations

from meridian.paper_search import (
    arxiv_search,
    openalex_search,
    parse_arxiv_atom,
    parse_openalex_works,
)

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


# ── f65f6111 — OpenAlex JSON parsing (offline, no network) ──────────────────────────
# OpenAlex stores abstracts as an inverted index {word: [positions]}, not plain text —
# the fixture exercises reconstruction, authorship flattening, and URL/DOI mapping.
_SAMPLE_OPENALEX = {
    "results": [
        {
            "id": "https://openalex.org/W2741809807",
            "doi": "https://doi.org/10.7717/peerj.4375",
            "title": "The state of OA",
            "publication_date": "2018-02-13",
            "updated_date": "2024-05-01T00:00:00",
            "authorships": [
                {"author": {"display_name": "Heather Piwowar"}},
                {"author": {"display_name": "Jason Priem"}},
            ],
            "abstract_inverted_index": {
                "Open": [0],
                "access": [1],
                "matters.": [2],
            },
            "primary_location": {
                "landing_page_url": "https://peerj.com/articles/4375",
                "pdf_url": "https://peerj.com/articles/4375.pdf",
            },
        },
        {
            "id": "https://openalex.org/W123",
            "display_name": "Fallback Title From display_name",
            "authorships": [],
        },
    ],
}


def test_parse_openalex_works_extracts_fields():
    papers = parse_openalex_works(_SAMPLE_OPENALEX)
    assert len(papers) == 2
    p = papers[0]
    # same normalized shape as arxiv rows, plus openalex_id + doi
    assert p["openalex_id"] == "W2741809807"
    assert p["title"] == "The state of OA"
    assert p["authors"] == ["Heather Piwowar", "Jason Priem"]
    # abstract_inverted_index reconstructed in positional order
    assert p["summary"] == "Open access matters."
    assert p["published"] == "2018-02-13"
    assert p["url"] == "https://peerj.com/articles/4375"
    assert p["pdf_url"] == "https://peerj.com/articles/4375.pdf"
    assert p["doi"] == "https://doi.org/10.7717/peerj.4375"
    # every shape key arxiv_search returns is present so the two sources are uniform
    for key in ("title", "authors", "summary", "published", "url", "pdf_url"):
        assert key in p
    # second row: title falls back to display_name, no authors, empty abstract
    q = papers[1]
    assert q["openalex_id"] == "W123"
    assert q["title"] == "Fallback Title From display_name"
    assert q["authors"] == []
    assert q["summary"] == ""


def test_parse_openalex_works_respects_limit():
    assert len(parse_openalex_works(_SAMPLE_OPENALEX, limit=1)) == 1


def test_parse_openalex_works_never_raises_on_garbage():
    assert parse_openalex_works(None) == []
    assert parse_openalex_works({}) == []
    assert parse_openalex_works({"results": None}) == []
    assert parse_openalex_works("not a dict") == []
    # malformed rows / missing sub-objects are skipped or defaulted, never crash
    assert parse_openalex_works({"results": ["nope", {}, {"authorships": [None, {}]}]}) == [
        {"openalex_id": "", "title": "", "authors": [], "summary": "",
         "published": "", "updated": "", "url": "", "pdf_url": "", "doi": ""},
        {"openalex_id": "", "title": "", "authors": [], "summary": "",
         "published": "", "updated": "", "url": "", "pdf_url": "", "doi": ""},
    ]


async def test_openalex_search_empty_query_returns_error():
    out = await openalex_search("   ")
    assert out.get("error")
    assert "results" not in out


def test_paper_search_registered_and_read_only():
    from meridian import mcp_tools

    names = {t["name"] for t in mcp_tools._MCP_TOOLS_LIST}
    assert "paper_search" in names, "paper_search must be advertised in tools/list"
    assert "paper_search" in mcp_tools._READ_ONLY_TOOLS
    entry = next(t for t in mcp_tools._MCP_TOOLS_LIST if t["name"] == "paper_search")
    props = entry["inputSchema"]["properties"]
    assert "query" in props
    assert entry["inputSchema"]["required"] == ["query"]
    # f65f6111 — the 'source' param routes between the two keyless sources
    assert "source" in props, "paper_search must expose a 'source' param"
    assert set(props["source"]["enum"]) == {"arxiv", "openalex"}


def test_research_protocol_names_the_real_tool():
    from meridian.agent_defaults import DEFAULT_AGENT_INSTRUCTIONS

    # the protocol now points at the real callable tool, not just a vague "paper-search"
    assert "paper_search" in DEFAULT_AGENT_INSTRUCTIONS
    assert "paper-search" in DEFAULT_AGENT_INSTRUCTIONS  # legacy phrasing preserved
