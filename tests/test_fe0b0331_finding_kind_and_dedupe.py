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
