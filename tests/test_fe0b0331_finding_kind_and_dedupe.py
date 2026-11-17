"""Tests for sprint item fe0b0331 -- finding notes stored with kind NULL, and the
same paper saved repeatedly within one project.

Two defects, one fix set:

1. ``add_project_note``'s kind allow-list omitted ``'finding'``, so every note
   ``save_finding`` / ``capture_research_finding`` wrote had its kind coerced to
   NULL. The write is fixed and a migration (SQLite + Postgres) backfills the
   rows written before the fix, identified by their ``finding`` tag.
2. ``save_finding`` / ``capture_research_finding`` had no in-project dedupe. The
   same source (case-folded DOI, arXiv id without version, PMID, else a
   normalised URL) now returns the SOFT ``existing_note_id`` result instead of a
   copy; ``force_new=true`` escapes. Existing duplicates are never deleted.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

import meridian.server  # noqa: F401 -- must be imported before handler to avoid cycle
from meridian import db as db_module
from meridian import mcp_tools
from meridian.finding_identity import finding_identity
from meridian.mcp.handlers import notes_decisions as nd_mod
from meridian.mcp.handlers import research_watchlist as rw_mod

_DATA_DIR = "/tmp/meridian-test"


@pytest_asyncio.fixture
async def db():
    conn = await db_module.init_db(":memory:")
    yield conn
    await conn.close()


@pytest_asyncio.fixture
async def project(db):
    return await db_module.create_project(db, "fe0b0331-proj")


async def _save(db, pid, summary="Paper shows Y", **kw):
    args = {"project_id": pid, "summary": summary}
    args.update(kw)
    return await nd_mod.handle_save_finding(args, db, _DATA_DIR, None, None)


async def _capture(db, pid, url, summary="Paper shows Y", **kw):
    args = {"project_id": pid, "url": url, "summary": summary}
    args.update(kw)
    return await nd_mod.handle_capture_research_finding(args, db, _DATA_DIR, None, None)


async def _finding_notes(db, pid):
    rows = await db_module.get_project_notes(db, pid, tag="finding", bodies=True)
    return rows


async def _insert_raw_note(db, pid, nid, *, tags, kind=None, source=None,
                           created_at="2024-01-01 00:00:00",
                           updated_at="2024-01-01 00:00:00"):
    await db.execute(
        "INSERT INTO project_notes "
        "(id, project_id, title, body, tags, note_kind, source, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (nid, pid, f"note {nid}", "body", tags, kind, source, created_at, updated_at),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# 1. kind='finding' is actually written
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_saved_finding_reads_back_with_kind_finding(db, project):
    out = await _save(db, project["id"], "Found a thing", source_url="https://example.com/a")
    assert "error" not in out
    assert out["note"]["note_kind"] == "finding"
    stored = await db_module.get_project_note(db, out["note"]["id"])
    assert stored["note_kind"] == "finding"


@pytest.mark.asyncio
async def test_capture_research_finding_reads_back_with_kind_finding(db, project):
    out = await _capture(db, project["id"], "https://arxiv.org/abs/2301.12345")
    stored = await db_module.get_project_note(db, out["note"]["id"])
    assert stored["note_kind"] == "finding"


@pytest.mark.asyncio
async def test_add_project_note_accepts_finding_and_still_coerces_unknown(db, project):
    ok = await db_module.add_project_note(db, project["id"], "t", "b", kind="finding")
    assert ok["note_kind"] == "finding"
    bogus = await db_module.add_project_note(db, project["id"], "t2", "b", kind="bogus")
    assert bogus["note_kind"] is None
    # The pre-existing vocabulary is untouched.
    for kind in ("wiki", "insight", "reference", "document"):
        n = await db_module.add_project_note(db, project["id"], f"t-{kind}", "b", kind=kind)
        assert n["note_kind"] == kind


# ---------------------------------------------------------------------------
# 2. in-project dedupe on save_finding / capture_research_finding
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_doi_twice_returns_existing_note_id(db, project):
    pid = project["id"]
    first = await _save(db, pid, source_url="https://doi.org/10.1145/ABC.123")
    assert "existing_note_id" not in first
    second = await _save(db, pid, "Same paper, new phrasing", source_url="doi:10.1145/abc.123")
    assert "error" not in second  # a SOFT result, not an error
    assert second["existing_note_id"] == first["note"]["id"]
    assert second["duplicate"] is True
    assert second["identifier"] == "doi:10.1145/abc.123"
    assert second["note"]["id"] == first["note"]["id"]
    assert len(await _finding_notes(db, pid)) == 1


@pytest.mark.asyncio
async def test_publisher_url_and_doi_org_url_are_the_same_paper(db, project):
    pid = project["id"]
    first = await _capture(db, pid, "https://dl.acm.org/doi/10.1145/3292500.3330701")
    second = await _capture(db, pid, "https://doi.org/10.1145/3292500.3330701")
    assert second["existing_note_id"] == first["note"]["id"]
    assert len(await _finding_notes(db, pid)) == 1


@pytest.mark.asyncio
async def test_arxiv_versions_are_the_same_paper(db, project):
    pid = project["id"]
    first = await _capture(db, pid, "https://arxiv.org/abs/2301.12345v1")
    second = await _capture(db, pid, "http://arxiv.org/pdf/2301.12345v3.pdf")
    assert second["existing_note_id"] == first["note"]["id"]
    # A different arXiv paper is NOT a duplicate.
    third = await _capture(db, pid, "https://arxiv.org/abs/2301.99999")
    assert "existing_note_id" not in third
    assert len(await _finding_notes(db, pid)) == 2


@pytest.mark.asyncio
async def test_pmid_and_normalised_url_dedupe(db, project):
    pid = project["id"]
    p1 = await _save(db, pid, source_url="https://pubmed.ncbi.nlm.nih.gov/12345678/")
    p2 = await _save(db, pid, source_url="PMID: 12345678")
    assert p2["existing_note_id"] == p1["note"]["id"]
    u1 = await _save(db, pid, source_url="https://Blog.example.com/post/?utm_source=x")
    u2 = await _save(db, pid, source_url="http://blog.example.com/post")
    assert u2["existing_note_id"] == u1["note"]["id"]
    u3 = await _save(db, pid, source_url="https://blog.example.com/other-post")
    assert "existing_note_id" not in u3


@pytest.mark.asyncio
async def test_force_new_creates_a_second_note(db, project):
    pid = project["id"]
    first = await _save(db, pid, source_url="https://doi.org/10.1000/x1")
    forced = await _save(db, pid, source_url="https://doi.org/10.1000/x1", force_new=True)
    assert "existing_note_id" not in forced
    assert forced["note"]["id"] != first["note"]["id"]
    assert forced["note"]["note_kind"] == "finding"
    assert len(await _finding_notes(db, pid)) == 2
    # capture_research_finding honours the same escape hatch.
    forced2 = await _capture(db, pid, "https://doi.org/10.1000/x1", force_new=True)
    assert "existing_note_id" not in forced2
    assert len(await _finding_notes(db, pid)) == 3


@pytest.mark.asyncio
async def test_force_new_string_false_does_not_bypass_dedupe(db, project):
    pid = project["id"]
    first = await _save(db, pid, source_url="https://doi.org/10.1000/x2")
    again = await _save(db, pid, source_url="https://doi.org/10.1000/x2", force_new="false")
    assert again["existing_note_id"] == first["note"]["id"]
    forced = await _save(db, pid, source_url="https://doi.org/10.1000/x2", force_new="true")
    assert "existing_note_id" not in forced


@pytest.mark.asyncio
async def test_dedupe_is_scoped_to_one_project(db, project):
    other = await db_module.create_project(db, "fe0b0331-other")
    first = await _save(db, project["id"], source_url="https://doi.org/10.1000/x3")
    elsewhere = await _save(db, other["id"], source_url="https://doi.org/10.1000/x3")
    assert "existing_note_id" not in elsewhere
    assert elsewhere["note"]["id"] != first["note"]["id"]


@pytest.mark.asyncio
async def test_sources_without_an_identity_never_dedupe(db, project):
    pid = project["id"]
    a = await _save(db, pid, "one")
    b = await _save(db, pid, "two")
    assert "existing_note_id" not in b and a["note"]["id"] != b["note"]["id"]
    c = await _save(db, pid, "three", source_url="meeting notes", source_type="conversation")
    d = await _save(db, pid, "four", source_url="meeting notes", source_type="conversation")
    assert "existing_note_id" not in d and c["note"]["id"] != d["note"]["id"]
    assert len(await _finding_notes(db, pid)) == 4


@pytest.mark.asyncio
async def test_only_finding_notes_count_as_duplicates(db, project):
    pid = project["id"]
    # A wiki note and an ingested document that merely share the source string.
    await db_module.add_project_note(
        db, pid, "wiki", "b", tags="setup", source="https://doi.org/10.1000/x4",
    )
    await db_module.add_project_note(
        db, pid, "doc", "b", kind="document", source="https://doi.org/10.1000/x4",
    )
    out = await _save(db, pid, source_url="https://doi.org/10.1000/x4")
    assert "existing_note_id" not in out
    assert out["note"]["note_kind"] == "finding"


@pytest.mark.asyncio
async def test_legacy_null_kind_finding_rows_are_still_detected_and_never_deleted(db, project):
    """Pre-fix rows (kind NULL, ``finding`` tag) count as duplicates; when a
    project already holds several duplicates the OLDEST is returned and none is
    deleted."""
    pid = project["id"]
    await _insert_raw_note(db, pid, "legacy-newer", tags="finding,web",
                           source="https://arxiv.org/abs/2001.00001v2",
                           created_at="2024-03-01 00:00:00")
    await _insert_raw_note(db, pid, "legacy-older", tags="finding,arxiv",
                           source="https://arxiv.org/abs/2001.00001v1",
                           created_at="2024-02-01 00:00:00")
    out = await _capture(db, pid, "https://arxiv.org/pdf/2001.00001")
    assert out["existing_note_id"] == "legacy-older"
    remaining = {n["id"] for n in await _finding_notes(db, pid)}
    assert remaining == {"legacy-newer", "legacy-older"}  # nothing created, nothing deleted


@pytest.mark.asyncio
async def test_duplicate_with_decision_id_links_the_existing_note(db, project):
    pid = project["id"]
    first = await _save(db, pid, source_url="https://doi.org/10.1000/x5")
    dec = await db_module.pin_decision(db, pid, "T", "B")
    dup = await _save(db, pid, source_url="doi:10.1000/x5", decision_id=dec["id"])
    assert dup["existing_note_id"] == first["note"]["id"]
    assert dup["decision_id"] == dec["id"]
    assert f"decision:{dec['id']}" in (dup["note"].get("tags") or "")
    stored = await db_module.get_project_note(db, first["note"]["id"])
    assert (stored["tags"] or "").split(",").count(f"decision:{dec['id']}") == 1
    # Linking again is idempotent (tag is not duplicated).
    await _save(db, pid, source_url="doi:10.1000/x5", decision_id=dec["id"])
    stored = await db_module.get_project_note(db, first["note"]["id"])
    assert (stored["tags"] or "").split(",").count(f"decision:{dec['id']}") == 1
    assert len(await _finding_notes(db, pid)) == 1


@pytest.mark.asyncio
async def test_unknown_decision_still_errors_on_a_duplicate(db, project):
    pid = project["id"]
    await _save(db, pid, source_url="https://doi.org/10.1000/x6")
    out = await _save(db, pid, source_url="https://doi.org/10.1000/x6", decision_id="ghost")
    assert "error" in out and "not found" in out["error"]


@pytest.mark.asyncio
async def test_dedupe_and_force_new_survive_the_real_mcp_dispatch(db, project):
    """The args reach the handlers through the real dispatcher (no arg allow-list
    strips ``force_new``) and the soft result comes back intact."""
    from meridian.mcp import handler as mh

    pid = project["id"]
    args = {"project_id": pid, "summary": "via dispatch",
            "source_url": "https://doi.org/10.1000/x7"}
    first = await mh._dispatch_mcp_tool("save_finding", dict(args), db, _DATA_DIR)
    dup = await mh._dispatch_mcp_tool("save_finding", dict(args), db, _DATA_DIR)
    assert dup["existing_note_id"] == first["note"]["id"]
    # ``project_notes.created_at`` defaults to second precision in SQLite.
    # Give the original an unambiguous earlier timestamp before exercising
    # oldest-match selection; otherwise two same-second notes are ordered by
    # their random UUIDs instead of insertion time.
    await db.execute(
        "UPDATE project_notes SET created_at = ? WHERE id = ?",
        ("2000-01-01 00:00:00", first["note"]["id"]),
    )
    await db.commit()
    forced = await mh._dispatch_mcp_tool(
        "save_finding", {**args, "force_new": True}, db, _DATA_DIR,
    )
    assert "existing_note_id" not in forced
    dup2 = await mh._dispatch_mcp_tool("capture_research_finding", {
        "project_id": pid, "url": "https://doi.org/10.1000/x7", "summary": "again",
    }, db, _DATA_DIR)
    assert dup2["existing_note_id"] == first["note"]["id"]  # oldest wins


# ---------------------------------------------------------------------------
# 3. watchlist interplay: run_watchlist_query goes through save_finding
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_watchlist_run_reuses_an_existing_finding_and_stays_stable(db, project, monkeypatch):
    pid = project["id"]
    manual = await _capture(db, pid, "https://arxiv.org/abs/1111.1111v1")
    saved = await rw_mod.handle_save_watchlist_query(
        {"project_id": pid, "source_type": "arxiv", "query": "q"}, db, _DATA_DIR, None, None,
    )

    async def _fake_arxiv_search(query, limit=10, sort_by="relevance"):
        return {"query": query, "count": 1,
                "results": [{"arxiv_id": "1111.1111", "title": "Paper A", "summary": "abstract",
                             "url": "https://arxiv.org/abs/1111.1111v2"}]}

    import meridian.paper_search as ps
    monkeypatch.setattr(ps, "arxiv_search", _fake_arxiv_search)

    first = await rw_mod.handle_run_watchlist_query(
        {"project_id": pid, "watchlist_id": saved["watchlist_id"]}, db, _DATA_DIR, None, None,
    )
    assert first["new_count"] == 1  # new to THIS watchlist ...
    assert first["captured"][0]["note_id"] == manual["note"]["id"]  # ... but no copy made
    assert first["captured"][0]["existing_note_id"] == manual["note"]["id"]
    assert len(await _finding_notes(db, pid)) == 1
    tags = (await db_module.get_project_note(db, manual["note"]["id"]))["tags"]
    assert f"watchlist:{saved['watchlist_id']}" in tags

    # The tagging keeps the watchlist's next diff stable.
    second = await rw_mod.handle_run_watchlist_query(
        {"project_id": pid, "watchlist_id": saved["watchlist_id"]}, db, _DATA_DIR, None, None,
    )
    assert second["new_count"] == 0
    assert len(await _finding_notes(db, pid)) == 1


# ---------------------------------------------------------------------------
# 4. backfill migration (SQLite + Postgres mirror)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backfill_sets_kind_only_on_null_kind_finding_tagged_rows(db, project):
    pid = project["id"]
    stamp = "2020-05-05 05:05:05"
    rows = {
        "hit-plain": ("finding,web", None),
        "hit-spaced-cased": (" Finding , arxiv", None),
        "hit-with-decision": ("web,finding,decision:abc", None),
        "hit-only": ("finding", None),
        "miss-findings": ("findings,web", None),
        "miss-substring": ("refinding,web", None),
        "miss-watchlist": ("research_watchlist,arxiv", None),
        "miss-no-tags": (None, None),
        "miss-other-kind": ("finding,web", "reference"),
        "miss-code-kind": ("finding", "code"),
    }
    for nid, (tags, kind) in rows.items():
        await _insert_raw_note(db, pid, nid, tags=tags, kind=kind,
                               created_at=stamp, updated_at=stamp)

    await db_module._migrate_backfill_finding_note_kind(db)

    for nid, (tags, kind) in rows.items():
        note = await db_module.get_project_note(db, nid)
        expected = "finding" if nid.startswith("hit-") else kind
        assert note["note_kind"] == expected, nid
        assert note["updated_at"] == stamp, nid  # metadata repair, not an edit

    # Idempotent: a second run changes nothing and deletes nothing.
    await db_module._migrate_backfill_finding_note_kind(db)
    for nid, (tags, kind) in rows.items():
        note = await db_module.get_project_note(db, nid)
        assert note["note_kind"] == ("finding" if nid.startswith("hit-") else kind), nid
    assert len(await db_module.get_project_notes(db, pid)) == len(rows)


@pytest.mark.asyncio
async def test_init_db_backfills_legacy_finding_rows_on_startup(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = await db_module.init_db(path)
    try:
        proj = await db_module.create_project(conn, "legacy-proj")
        await _insert_raw_note(conn, proj["id"], "legacy-finding", tags="finding,web",
                               source="https://example.com/p")
    finally:
        await conn.close()

    conn2 = await db_module.init_db(path)
    try:
        note = await db_module.get_project_note(conn2, "legacy-finding")
        assert note["note_kind"] == "finding"
    finally:
        await conn2.close()


class _RecordingPgConn:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    async def execute(self, sql, params=None):
        self.calls.append((sql, params))


@pytest.mark.asyncio
async def test_pg_backfill_mirror_is_registered_and_percent_free():
    from meridian import pg_adapter

    assert pg_adapter._migrate_pg_backfill_finding_note_kind in pg_adapter._PG_MIGRATIONS_LATE
    conn = _RecordingPgConn()
    await pg_adapter._migrate_pg_backfill_finding_note_kind(conn)
    assert len(conn.calls) == 1
    sql, params = conn.calls[0]
    assert sql.startswith("UPDATE project_notes SET note_kind = 'finding'")
    assert "note_kind IS NULL" in sql
    assert "STRPOS" in sql and "',finding,'" in sql
    assert "%" not in sql  # nothing for the adapter's %s / LIKE escaping to trip on
    assert params is None


# ---------------------------------------------------------------------------
# 5. tool schemas advertise force_new
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool", ["save_finding", "capture_research_finding"])
def test_tool_schema_advertises_optional_boolean_force_new(tool):
    schema = next(t for t in mcp_tools._MCP_TOOLS_LIST if t["name"] == tool)
    props = schema["inputSchema"]["properties"]
    assert props["force_new"]["type"] == "boolean"
    assert "force_new" not in schema["inputSchema"].get("required", [])
    assert "existing_note_id" in schema["description"]


# ---------------------------------------------------------------------------
# 6. finding_identity unit behaviour
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "spellings,expected",
    [
        (["https://doi.org/10.1145/ABC.def", "http://dx.doi.org/10.1145/abc.DEF",
          "doi:10.1145/abc.def", "DOI: 10.1145/abc.def.", "10.1145/abc.def",
          "doi.org/10.1145/abc.def", "https://dl.acm.org/doi/10.1145/abc.def",
          "https://onlinelibrary.wiley.com/doi/full/10.1145/abc.def",
          "https://example.org/landing?doi=10.1145%2Fabc.def&x=1"],
         "doi:10.1145/abc.def"),
        (["https://arxiv.org/abs/2301.12345v2", "http://arxiv.org/pdf/2301.12345v3.pdf",
          "arXiv:2301.12345", "https://export.arxiv.org/abs/2301.12345",
          "https://doi.org/10.48550/arXiv.2301.12345"],
         "arxiv:2301.12345"),
        (["https://arxiv.org/abs/hep-th/9901001v1", "arxiv:hep-th/9901001"],
         "arxiv:hep-th/9901001"),
        (["https://pubmed.ncbi.nlm.nih.gov/12345678/", "PMID: 12345678",
          "https://www.ncbi.nlm.nih.gov/pubmed/12345678",
          "https://europepmc.org/article/MED/12345678"],
         "pmid:12345678"),
        (["https://Example.com/a/b/?utm_source=x&b=2&a=1#frag",
          "http://www.example.com/a/b?a=1&b=2", "www.example.com/a/b/?b=2&a=1"],
         "url:example.com/a/b?a=1&b=2"),
    ],
)
def test_finding_identity_equates_spellings_of_one_work(spellings, expected):
    for s in spellings:
        assert finding_identity(s) == expected, s


@pytest.mark.parametrize("source", [None, "", "   ", "meeting notes", "meridian/db/__init__.py",
                                    "ftp://example.org/x", 42])
def test_finding_identity_none_for_sources_without_identity(source):
    assert finding_identity(source) is None


def test_finding_identity_distinguishes_different_works():
    keys = {
        finding_identity("https://doi.org/10.1145/aaa"),
        finding_identity("https://doi.org/10.1145/aab"),
        finding_identity("https://arxiv.org/abs/2301.12345"),
        finding_identity("https://arxiv.org/abs/2301.12346"),
        finding_identity("https://example.com/a"),
        finding_identity("https://example.com/b"),
        finding_identity("https://example.com/a?id=1"),
        finding_identity("https://news.ycombinator.com/item?id=1"),
        finding_identity("https://news.ycombinator.com/item?id=2"),
    }
    assert len(keys) == 9


# ---------------------------------------------------------------------------
# 7. capture_research_finding takes doi / pmid (sprint item a4e43f50)
# ---------------------------------------------------------------------------

async def _capture_args(db, pid, **kw):
    args = {"project_id": pid, "summary": "Paper shows Y"}
    args.update(kw)
    return await nd_mod.handle_capture_research_finding(args, db, _DATA_DIR, None, None)


@pytest.mark.asyncio
async def test_doi_without_url_stores_the_doi_org_form(db, project):
    out = await _capture_args(db, project["id"], doi="10.1145/3292500.3330701")
    assert "error" not in out
    assert out["note"]["source"] == "https://doi.org/10.1145/3292500.3330701"
    assert out["source_type"] == "web"
    stored = await db_module.get_project_note(db, out["note"]["id"])
    assert stored["source"] == "https://doi.org/10.1145/3292500.3330701"


@pytest.mark.asyncio
async def test_doi_arg_dedupes_against_an_earlier_publisher_url(db, project):
    pid = project["id"]
    first = await _capture(db, pid, "https://dl.acm.org/doi/10.1145/3292500.3330701")
    second = await _capture_args(db, pid, doi="10.1145/3292500.3330701")
    assert "error" not in second
    assert second["existing_note_id"] == first["note"]["id"]
    assert second["duplicate"] is True
    assert len(await _finding_notes(db, pid)) == 1


@pytest.mark.asyncio
async def test_publisher_url_after_a_doi_arg_dedupes_too(db, project):
    pid = project["id"]
    first = await _capture_args(db, pid, doi="doi:10.1145/ABC.123")
    assert first["note"]["source"] == "https://doi.org/10.1145/ABC.123"
    second = await _capture(db, pid, "https://dl.acm.org/doi/10.1145/abc.123")
    assert second["existing_note_id"] == first["note"]["id"]
    assert len(await _finding_notes(db, pid)) == 1


@pytest.mark.asyncio
async def test_doi_arg_force_new_creates_a_second_note(db, project):
    pid = project["id"]
    first = await _capture_args(db, pid, doi="10.1000/x8")
    forced = await _capture_args(db, pid, doi="10.1000/x8", force_new=True)
    assert "existing_note_id" not in forced
    assert forced["note"]["id"] != first["note"]["id"]
    assert len(await _finding_notes(db, pid)) == 2


@pytest.mark.asyncio
async def test_explicit_url_wins_over_doi_and_pmid(db, project):
    out = await _capture_args(db, project["id"], url="https://example.com/post",
                              doi="10.1000/x9", pmid="12345678")
    assert out["note"]["source"] == "https://example.com/post"


@pytest.mark.asyncio
async def test_pmid_arg_stores_the_pubmed_url_and_dedupes(db, project):
    pid = project["id"]
    first = await _capture_args(db, pid, pmid="12345678")
    assert first["note"]["source"] == "https://pubmed.ncbi.nlm.nih.gov/12345678/"
    second = await _capture(db, pid, "https://pubmed.ncbi.nlm.nih.gov/12345678/")
    assert second["existing_note_id"] == first["note"]["id"]


@pytest.mark.asyncio
async def test_no_url_doi_or_pmid_keeps_the_existing_error_text(db, project):
    pid = project["id"]
    assert await _capture_args(db, pid) == {"error": "capture_research_finding requires a url"}
    blank = await _capture_args(db, pid, doi="   ", pmid="  ")
    assert blank == {"error": "capture_research_finding requires a url"}
    assert await _finding_notes(db, pid) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["not-a-doi", "10.12/x", "https://example.com/paper"])
async def test_malformed_doi_arg_is_an_error_and_stores_nothing(db, project, bad):
    pid = project["id"]
    out = await _capture_args(db, pid, doi=bad)
    assert "error" in out and "doi" in out["error"]
    assert await _finding_notes(db, pid) == []


# A DOI whose dedupe key is not the DOI itself would collide with another DOI
# (finding_identity cuts a doi.org URL at ? # & and excludes < > and quotes), so
# the doi argument refuses it and stores nothing. The refusal keeps the existing
# doi error as its prefix. A refused doi.org url keeps the url error as its prefix instead
# (section 9). Each refusal appends the reason in parentheses. The constants are imported
# from the handler, so the tests track any change to the messages.
_DOI_ERROR = nd_mod._DOI_ERROR
_URL_DOI_ERROR = nd_mod._URL_DOI_ERROR


@pytest.mark.asyncio
@pytest.mark.parametrize("bad,reason", [
    ("10.1000/x?y", "'?' is not an allowed DOI character"),
    ("10.1000/x#y", "'#' is not an allowed DOI character"),
    ("10.1000/x&y", "'&' is not an allowed DOI character"),
    ('10.1000/x"y', "'\"' is not an allowed DOI character"),
    # Legacy SICI DOIs contain < and >. The first character refused is the <.
    ("10.1002/(SICI)1097-4636(199601)30:1<40::AID-JBM4>3.0.CO;2-K",
     "'<' is not an allowed DOI character"),
    ("https://doi.org/10.1000/x?y", "'?' is not an allowed DOI character"),
])
async def test_doi_arg_stop_character_is_refused_by_allowlist(db, project, bad, reason):
    """Rule 3 (the allowlist) refuses these before rule 7 (the identity check) runs. The
    refusal names the first offending character, so the full reason is asserted."""
    pid = project["id"]
    out = await _capture_args(db, pid, doi=bad)
    assert out == {"error": f"{_DOI_ERROR} ({reason})"}
    assert await _finding_notes(db, pid) == []


@pytest.mark.asyncio
async def test_plain_doi_arg_is_still_accepted_and_stored(db, project):
    pid = project["id"]
    out = await _capture_args(db, pid, doi="10.1000/xyz")
    assert "error" not in out
    assert out["note"]["source"] == "https://doi.org/10.1000/xyz"
    stored = await db_module.get_project_note(db, out["note"]["id"])
    assert stored["source"] == "https://doi.org/10.1000/xyz"
    assert len(await _finding_notes(db, pid)) == 1


@pytest.mark.asyncio
async def test_arxiv_issued_doi_arg_is_accepted_and_dedupes_with_its_arxiv_url(db, project):
    pid = project["id"]
    first = await _capture_args(db, pid, doi="10.48550/arXiv.2301.12345")
    assert "error" not in first
    assert first["note"]["source"] == "https://doi.org/10.48550/arXiv.2301.12345"
    second = await _capture(db, pid, "https://arxiv.org/abs/2301.12345v2")
    assert second["existing_note_id"] == first["note"]["id"]
    assert len(await _finding_notes(db, pid)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["PMID12", "1234567890", "12.5"])
async def test_malformed_pmid_arg_is_an_error(db, project, bad):
    out = await _capture_args(db, project["id"], pmid=bad)
    assert "error" in out and "pmid" in out["error"]


@pytest.mark.asyncio
async def test_doi_arg_survives_the_real_mcp_dispatch(db, project):
    from meridian.mcp import handler as mh

    pid = project["id"]
    first = await mh._dispatch_mcp_tool("capture_research_finding", {
        "project_id": pid, "summary": "via doi dispatch", "doi": "10.1000/x10",
    }, db, _DATA_DIR)
    assert first["note"]["source"] == "https://doi.org/10.1000/x10"
    dup = await mh._dispatch_mcp_tool("capture_research_finding", {
        "project_id": pid, "summary": "again", "url": "https://dl.acm.org/doi/10.1000/X10",
    }, db, _DATA_DIR)
    assert dup["existing_note_id"] == first["note"]["id"]


def test_capture_tool_schema_advertises_doi_and_pmid_without_requiring_url():
    schema = next(t for t in mcp_tools._MCP_TOOLS_LIST if t["name"] == "capture_research_finding")
    props = schema["inputSchema"]["properties"]
    assert props["doi"]["type"] == "string"
    assert props["pmid"]["type"] == "string"
    assert schema["inputSchema"]["required"] == ["summary"]
    assert "arXiv URLs and arXiv-issued DOIs are tagged source_type=arxiv automatically" in schema["description"]


# ---------------------------------------------------------------------------
# 8. fe0b0331: a doi argument stores only a DOI that keys to itself, except an arXiv-issued
#    DOI, which is exempt from that check and keys to arxiv:<id>
#
# Rules (see _normalise_capture_doi). First the ASCII edge whitespace (space, tab,
# CR, LF) is trimmed and one doi: or doi.org/ prefix (including its dx. and https://
# forms) is removed. A doi.org or dx.doi.org url gets these same rules on the text
# after its host (section 9). Then, in order:
#   * a % is refused. No percent escape is decoded;
#   * the value must start 10.NNNN/ (4 to 9 digits), or it is refused by its shape;
#   * the suffix may hold only ASCII letters, digits and . _ ; ( ) / : -. The reason
#     names the first character outside that set;
#   * one trailing period is dropped as prose. A trailing ; : ( ) or .. is ambiguous;
#   * a suffix segment that is exactly . or .. is refused, because a URL parser resolves
#     it to a different URL. The raw suffix is read, so x/. is refused too;
#   * the arXiv-issued form, when it fully matches _ARXIV_DOI_RE, keys to arxiv:<id>
#     and is the only value that skips the identity check;
#   * every other DOI must key to doi: plus the DOI lowercased.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("bad,reason", [
    # Not 10.NNNN/ at the start, so the reason names the shape. A prefix lookalike is
    # never stripped, so it reaches the shape check and fails there.
    ("not-a-doi", "not of the form 10.NNNN/suffix"),
    ("10.12/x", "not of the form 10.NNNN/suffix"),
    ("10.1000", "not of the form 10.NNNN/suffix"),
    ("https://example.com/paper", "not of the form 10.NNNN/suffix"),
    ("do\u0131:10.1000/xyz", "not of the form 10.NNNN/suffix"),  # DOTLESS I in the prefix
    ("http\u017f://doi.org/10.1000/xyz", "not of the form 10.NNNN/suffix"),  # LONG S
    ("doi:\u00a010.1000/xyz", "not of the form 10.NNNN/suffix"),  # NBSP after the prefix
    ("\u00a010.1000/xyz", "not of the form 10.NNNN/suffix"),  # NBSP before the value
    ("\x0c10.1000/xyz", "not of the form 10.NNNN/suffix"),  # form feed before the value
    # The shape is right, so the reason names the first character outside the suffix set.
    ("10.1000/x\u00e9", "U+00E9 is not an allowed DOI character"),  # e with acute accent
    ("10.1000/\u00df", "U+00DF is not an allowed DOI character"),  # sharp s
    ("10.1000/\u212a", "U+212A is not an allowed DOI character"),  # KELVIN SIGN
    ("10.48550/arXiv.2301.1234\u0665", "U+0665 is not an allowed DOI character"),
    ("10.1000/x\u00a0y", "U+00A0 is not an allowed DOI character"),  # NBSP inside
    ("10.1000/xyz\u00a0", "U+00A0 is not an allowed DOI character"),  # NBSP at the end
    ("10.1000/x\ty", "U+0009 is not an allowed DOI character"),
    ("10.1000/x\x7fy", "U+007F is not an allowed DOI character"),
    ("10.1000/x y", "U+0020 is not an allowed DOI character"),
    ("10.1000/x[1]", "'[' is not an allowed DOI character"),
    ("10.1000/x]", "']' is not an allowed DOI character"),
    ("10.1000/x{y", "'{' is not an allowed DOI character"),
    ("10.1000/x}", "'}' is not an allowed DOI character"),
    ("10.1000/x\\y", "'\\\\' is not an allowed DOI character"),
    ("10.1000/x^y", "'^' is not an allowed DOI character"),
    ("10.1000/x|y", "'|' is not an allowed DOI character"),
    ("10.1000/x`y", "'`' is not an allowed DOI character"),
    ("10.1000/x,", "',' is not an allowed DOI character"),
    ("10.1000/x+y", "'+' is not an allowed DOI character"),
    # A "." or ".." segment would resolve to a different URL. The trailing "/.." of x/..
    # is caught earlier, by the trailing-character rule, and is in the test below.
    ("10.1000/../x", 'a path segment of "." or ".." would resolve to a different URL'),
    ("10.1000/x/./y", 'a path segment of "." or ".." would resolve to a different URL'),
    ("10.1000/x/../y", 'a path segment of "." or ".." would resolve to a different URL'),
    ("10.1000/x/.", 'a path segment of "." or ".." would resolve to a different URL'),  # trailing /.
    # No suffix after the prefix, once the trailing period is dropped.
    ("10.1000/.", "no suffix after the DOI prefix"),
    ("10.1000/", "no suffix after the DOI prefix"),
])
async def test_doi_refusals_name_the_reason_and_store_nothing(db, project, bad, reason):
    pid = project["id"]
    out = await _capture_args(db, pid, doi=bad)
    assert set(out) == {"error"}
    assert out["error"].startswith(_DOI_ERROR + " (")
    assert reason in out["error"]
    assert await _finding_notes(db, pid) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["10.1000/x:", "10.1000/x;", "10.1000/x;.", "10.1000/x(",
                                 "10.1000/x)", "10.1000/x..", "10.1000/x...", "10.1000/x/.."])
async def test_trailing_punctuation_other_than_a_period_is_refused(db, project, bad):
    """Decision: one trailing period is prose and is dropped. A trailing ; : ( ) is refused
    as ambiguous, because it may be part of the DOI, and so is a trailing .. The value
    10.1000/x;. is refused for its ;, once its period is dropped. Nothing is stored."""
    pid = project["id"]
    out = await _capture_args(db, pid, doi=bad)
    assert set(out) == {"error"}
    assert out["error"].startswith(_DOI_ERROR + " (")
    assert "ambiguous trailing character" in out["error"]
    assert await _finding_notes(db, pid) == []


@pytest.mark.asyncio
async def test_trailing_period_is_dropped_and_dedupes(db, project):
    """Decision: a single trailing period is prose. It is dropped, the stored DOI is the
    bare one, and it dedupes with the bare DOI. 10.1145/3292500.3330701. is one."""
    pid = project["id"]
    first = await _capture_args(db, pid, doi="10.1145/3292500.3330701.")
    assert "error" not in first
    assert first["note"]["source"] == "https://doi.org/10.1145/3292500.3330701"
    second = await _capture_args(db, pid, doi="10.1145/3292500.3330701")
    assert second["existing_note_id"] == first["note"]["id"]
    assert len(await _finding_notes(db, pid)) == 1


@pytest.mark.asyncio
async def test_every_allowlisted_punctuation_mark_is_accepted_inside_a_doi(db, project):
    """The suffix allowlist is . _ ; ( ) / : - together with letters and digits. Each mark
    is accepted between other characters, and the stored DOI is the one typed."""
    pid = project["id"]
    doi = "10.1000/a.b_c;d(e)f:g-h/i"
    out = await _capture_args(db, pid, doi=doi)
    assert "error" not in out
    assert out["note"]["source"] == f"https://doi.org/{doi}"
    assert len(await _finding_notes(db, pid)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("doi", ["10.1000/x...y", "10.1000/a..b"])
async def test_dots_inside_a_segment_are_not_dot_segments_and_are_accepted(db, project, doi):
    """Only a whole "." or ".." segment is refused. A run of dots inside a segment is
    ordinary, so the DOI is accepted and stored exactly as typed."""
    pid = project["id"]
    out = await _capture_args(db, pid, doi=doi)
    assert "error" not in out
    assert out["note"]["source"] == f"https://doi.org/{doi}"
    assert len(await _finding_notes(db, pid)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad,reason", [
    ("10.48550/arXiv.2301.12345?y", "'?'"),  # the arXiv exemption admits no stop character
    ("10.48550/arXiv.2301.12345&y", "'&'"),
    ("10.48550/arXiv.2301.12345<x", "'<'"),
    ("10.1000/x#y", "'#'"),
    ('10.1000/x"y', '\'"\''),
    ("10.1000/x'y", "\"'\""),
    # Legacy SICI DOIs contain < and >, so they are refused. This is a deliberate trade-off.
    ("10.1002/(SICI)1097-4636(199601)30:1<40::AID-JBM4>3.0.CO;2-K", "'<'"),
])
async def test_stop_characters_are_refused_and_named(db, project, bad, reason):
    pid = project["id"]
    out = await _capture_args(db, pid, doi=bad)
    assert set(out) == {"error"}
    assert out["error"].startswith(_DOI_ERROR + " (")
    assert reason in out["error"]
    assert await _finding_notes(db, pid) == []


@pytest.mark.asyncio
async def test_kelvin_sign_doi_is_refused_and_does_not_merge_with_ascii_k(db, project):
    """U+212A KELVIN SIGN lowercases to ASCII k. It is refused as a non-allowed character
    before any case folding, so it cannot merge onto the ASCII spelling of the same DOI."""
    pid = project["id"]
    ascii_k = await _capture_args(db, pid, doi="10.1000/k")
    kelvin = await _capture_args(db, pid, doi="10.1000/\u212a")
    assert set(kelvin) == {"error"}
    assert kelvin["error"] == f"{_DOI_ERROR} (U+212A is not an allowed DOI character)"
    assert len(await _finding_notes(db, pid)) == 1
    assert ascii_k["note"]["source"] == "https://doi.org/10.1000/k"


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", [
    "10.1000/xyz", "doi:10.1000/xyz", "DOI:10.1000/xyz", "doi: 10.1000/xyz",
    "doi : 10.1000/xyz", "doi:\t10.1000/xyz", "https://doi.org/10.1000/xyz",
    "http://dx.doi.org/10.1000/xyz", "dx.doi.org/10.1000/xyz", "doi.org/10.1000/xyz",
    "10.1000/xyz.",
])
async def test_accepted_doi_spellings_store_one_normalised_doi(db, project, spelling):
    """Plain DOIs are accepted in every prefix form, and a trailing period is dropped. Each
    spelling stores the same https://doi.org/ form, which dedupes with a publisher URL."""
    pid = project["id"]
    first = await _capture_args(db, pid, doi=spelling)
    assert "error" not in first
    assert first["note"]["source"] == "https://doi.org/10.1000/xyz"
    dup = await _capture(db, pid, "https://dl.acm.org/doi/10.1000/XYZ")
    assert dup["existing_note_id"] == first["note"]["id"]
    assert len(await _finding_notes(db, pid)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    "10.1000/x%2Fy",   # an encoded slash
    "10.1000%2Fxyz",   # an encoded slash in the prefix
    "10.1000/x%25y",   # an encoded percent sign, which would decode to a bare %
    "10.1000/x%20y",   # an encoded space
    "10.1000/x%2525",  # a double-encoded percent sign
    "10.1000/x%",      # a lone percent sign
    "doi:10.1000/x%2Fy",
    "https://doi.org/10.1000/x%2Fy",
])
async def test_percent_is_refused_and_no_escape_is_decoded(db, project, bad):
    """Decision: percent escapes are not decoded at all. Decoding stored malformed URLs, so
    any % is refused with a request for the plain DOI, and nothing is stored."""
    pid = project["id"]
    out = await _capture_args(db, pid, doi=bad)
    expected = f"{_DOI_ERROR} (percent escapes are not accepted; give the plain DOI)"
    assert out == {"error": expected}
    assert await _finding_notes(db, pid) == []


@pytest.mark.asyncio
async def test_ascii_case_variants_are_accepted_and_dedupe(db, project):
    pid = project["id"]
    first = await _capture_args(db, pid, doi="10.1000/XYZ")
    assert "error" not in first
    assert first["note"]["source"] == "https://doi.org/10.1000/XYZ"
    second = await _capture_args(db, pid, doi="10.1000/xyz")
    assert second["existing_note_id"] == first["note"]["id"]
    assert len(await _finding_notes(db, pid)) == 1


@pytest.mark.asyncio
async def test_casefold_collision_is_refused_and_does_not_merge(db, project):
    """The sharp s (U+00DF) casefolds to ss. It is refused as a non-allowed character, so it
    cannot merge onto an existing 10.1000/ss note, while ASCII case variants of ss still
    dedupe."""
    pid = project["id"]
    ss = await _capture_args(db, pid, doi="10.1000/ss")
    refused = await _capture_args(db, pid, doi="10.1000/\u00df")
    assert set(refused) == {"error"}
    assert refused["error"] == f"{_DOI_ERROR} (U+00DF is not an allowed DOI character)"
    assert len(await _finding_notes(db, pid)) == 1
    upper = await _capture_args(db, pid, doi="10.1000/SS")
    assert upper["existing_note_id"] == ss["note"]["id"]


@pytest.mark.asyncio
async def test_arxiv_doi_forms_are_exempt_and_dedupe_with_the_arxiv_url(db, project):
    pid = project["id"]
    new_style = await _capture_args(db, pid, doi="10.48550/ARXIV.2301.12345v2")
    assert "error" not in new_style
    old_style = await _capture_args(db, pid, doi="10.48550/arXiv.hep-th/9901001.")
    assert "error" not in old_style
    assert old_style["note"]["source"] == "https://doi.org/10.48550/arXiv.hep-th/9901001"
    new_dup = await _capture(db, pid, "https://arxiv.org/abs/2301.12345")
    assert new_dup["existing_note_id"] == new_style["note"]["id"]
    old_dup = await _capture(db, pid, "https://arxiv.org/abs/hep-th/9901001v1")
    assert old_dup["existing_note_id"] == old_style["note"]["id"]
    assert len(await _finding_notes(db, pid)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("padded", ["  10.1000/xyz  ", "\t10.1000/xyz\r\n", "  doi: 10.1000/xyz  "])
async def test_padded_doi_arg_is_accepted_and_dedupes_with_the_bare_doi(db, project, padded):
    """Leading and trailing ASCII space, tab, CR and LF are trimmed from the argument before
    the allowlist check, so a padded value stores and dedupes."""
    pid = project["id"]
    first = await _capture_args(db, pid, doi=padded)
    assert "error" not in first
    assert first["note"]["source"] == "https://doi.org/10.1000/xyz"
    second = await _capture_args(db, pid, doi="10.1000/xyz")
    assert second["existing_note_id"] == first["note"]["id"]
    assert len(await _finding_notes(db, pid)) == 1


@pytest.mark.asyncio
async def test_nbsp_only_doi_arg_is_refused_not_treated_as_absent(db, project):
    """Only ASCII blank means no doi. An NBSP-only value is a value: it gets the doi error
    with its reason, not the 'requires a url' error that a blank doi gets."""
    pid = project["id"]
    out = await _capture_args(db, pid, doi="\u00a0")
    assert set(out) == {"error"}
    assert out["error"] == f"{_DOI_ERROR} (it is not of the form 10.NNNN/suffix)"
    assert await _finding_notes(db, pid) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    "do\u0131:10.1000/xyz",              # DOTLESS I (U+0131): Unicode IGNORECASE matches "i"
    "http\u017f://doi.org/10.1000/xyz",  # LONG S (U+017F): Unicode IGNORECASE matches "s"
])
async def test_lookalike_letters_do_not_match_the_doi_prefix(db, project, bad):
    """re.ASCII keeps the prefix regex from matching these lookalikes, so no prefix is
    stripped. The value then fails the shape check and nothing is stored."""
    pid = project["id"]
    out = await _capture_args(db, pid, doi=bad)
    assert set(out) == {"error"}
    assert out["error"] == f"{_DOI_ERROR} (it is not of the form 10.NNNN/suffix)"
    assert await _finding_notes(db, pid) == []


@pytest.mark.asyncio
async def test_identity_guard_refuses_a_key_that_differs_and_only_arxiv_skips_it(
    db, project, monkeypatch,
):
    """The identity check (rule 7) is a backstop: no DOI that rules 1 to 6 admit is expected
    to fail it. So finding_identity is patched to return a different key, which checks that
    the guard refuses. The arXiv form is still stored, because it is the only case that
    skips the check."""
    monkeypatch.setattr(nd_mod, "finding_identity", lambda source: "doi:10.9999/other")
    pid = project["id"]
    out = await _capture_args(db, pid, doi="10.1000/xyz")
    assert set(out) == {"error"}
    assert out["error"] == f"{_DOI_ERROR} (its dedupe key would not be the DOI itself)"
    assert await _finding_notes(db, pid) == []
    arxiv = await _capture_args(db, pid, doi="10.48550/arXiv.2301.12345")
    assert arxiv["note"]["source"] == "https://doi.org/10.48550/arXiv.2301.12345"


# ---------------------------------------------------------------------------
# 9. fe0b0331 follow-up: a doi.org url is read as a DOI, and arXiv-issued DOIs are tagged
#
# A url whose host is doi.org or dx.doi.org (any case, with or without https://) takes the
# text after its host as the DOI and is held to the rules in section 8. It is stored as
# https://doi.org/<normalised DOI>. Any other url is stored exactly as given. An arXiv-issued
# DOI is tagged source_type "arxiv", as its arxiv.org URL is.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("bad,reason", [
    ("https://doi.org/10.1000/x?y", "'?' is not an allowed DOI character"),
    ("https://doi.org/10.1000/x#y", "'#' is not an allowed DOI character"),
    ("https://doi.org/10.1000/../x",
     'a path segment of "." or ".." would resolve to a different URL'),
    ("https://doi.org/10.1000/x%2Fy", "percent escapes are not accepted"),
    ("https://doi.org/10.1000/x y", "U+0020 is not an allowed DOI character"),
    ("https://doi.org/10.1000/x\ty", "U+0009 is not an allowed DOI character"),
    ("https://doi.org/", "it is not of the form 10.NNNN/suffix"),
    ("https://doi.org", "it is not of the form 10.NNNN/suffix"),
])
async def test_doi_org_url_is_held_to_the_doi_rules_and_stores_nothing(db, project, bad, reason):
    pid = project["id"]
    out = await _capture(db, pid, bad)
    assert set(out) == {"error"}
    assert out["error"].startswith(_URL_DOI_ERROR + " (")
    assert reason in out["error"]
    assert await _finding_notes(db, pid) == []


@pytest.mark.asyncio
async def test_refused_doi_org_url_gets_the_url_prefix_and_not_the_doi_prefix(db, project):
    pid = project["id"]
    out = await _capture(db, pid, "https://doi.org/10.1000/x?y")
    assert out == {"error": f"{_URL_DOI_ERROR} ('?' is not an allowed DOI character)"}
    assert not out["error"].startswith(_DOI_ERROR)
    assert await _finding_notes(db, pid) == []


@pytest.mark.asyncio
async def test_refused_doi_argument_keeps_the_doi_prefix_and_not_the_url_prefix(db, project):
    pid = project["id"]
    out = await _capture_args(db, pid, doi="10.1000/x?y")
    assert out == {"error": f"{_DOI_ERROR} ('?' is not an allowed DOI character)"}
    assert not out["error"].startswith(_URL_DOI_ERROR)
    assert await _finding_notes(db, pid) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://doi.org/10.1000/xyz",
    "HTTPS://DOI.ORG/10.1000/xyz",
    "http://dx.doi.org/10.1000/xyz",
    "doi.org/10.1000/xyz",
    "https://doi.org/10.1000/xyz.",
])
async def test_doi_org_url_is_accepted_and_stored_as_the_normalised_doi_form(db, project, url):
    pid = project["id"]
    out = await _capture(db, pid, url)
    assert "error" not in out
    assert out["note"]["source"] == "https://doi.org/10.1000/xyz"
    assert out["source_type"] == "web"
    stored = await db_module.get_project_note(db, out["note"]["id"])
    assert stored["source"] == "https://doi.org/10.1000/xyz"
    dup = await _capture(db, pid, "https://dl.acm.org/doi/10.1000/XYZ")
    assert dup["existing_note_id"] == out["note"]["id"]
    assert len(await _finding_notes(db, pid)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://example.com/paper?y=1#top",
    "https://dl.acm.org/doi/10.1000/x?y",
    "https://doi.org.example.com/10.1000/x?y",
    "https://arxiv.org/abs/2301.12345v2",
    "ftp://doi.org/10.1000/xyz",  # another scheme is not read as a DOI link
])
async def test_non_doi_url_is_stored_exactly_as_given(db, project, url):
    pid = project["id"]
    out = await _capture(db, pid, url)
    assert "error" not in out
    assert out["note"]["source"] == url


@pytest.mark.asyncio
async def test_arxiv_issued_doi_is_tagged_arxiv_by_arg_and_by_url(db, project):
    pid = project["id"]
    by_arg = await _capture_args(db, pid, doi="10.48550/arXiv.2301.12345")
    assert by_arg["source_type"] == "arxiv"
    by_url = await _capture(db, pid, "https://doi.org/10.48550/ARXIV.2301.12346")
    assert by_url["source_type"] == "arxiv"
    assert by_url["note"]["source"] == "https://doi.org/10.48550/ARXIV.2301.12346"
    # The same arXiv paper as its arxiv.org URL: the dedupe key is arxiv:2301.12345 for both.
    arxiv_url = await _capture(db, pid, "https://arxiv.org/abs/2301.12345v2")
    assert arxiv_url["existing_note_id"] == by_arg["note"]["id"]
    assert len(await _finding_notes(db, pid)) == 2


@pytest.mark.asyncio
async def test_doi_containing_the_text_arxiv_org_is_tagged_web(db, project):
    """Only the arXiv-issued DOI form is tagged arxiv. A publisher DOI that merely contains
    the text arxiv.org is still a DOI, so it stays web."""
    out = await _capture_args(db, project["id"], doi="10.1000/arxiv.org.x")
    assert out["source_type"] == "web"
    assert out["note"]["source"] == "https://doi.org/10.1000/arxiv.org.x"
