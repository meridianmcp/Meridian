"""Tests for meridian.memory_import -- the dry-run-by-default Claude Code
auto-memory importer (sprint item 55d48d69). Every test builds its own fake
``projects`` tree under tmp_path; the real ~/.claude is never read."""
from __future__ import annotations

import datetime as dt
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from meridian import memory_import as mi
from meridian.__main__ import main as meridian_main

NOW = dt.datetime(2026, 9, 26, 12, 0, tzinfo=dt.timezone.utc)
PID = "5787cc92-ba7d-4788-b17c-28ab7938b839"
# AWS's own documented example key id, assembled at runtime.
FAKE_AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"


def _md(fm: dict | None, body: str, *, nested: bool = False) -> str:
    if fm is None:
        return body
    lines = ["---"]
    for k, v in fm.items():
        if k == "type" and nested:
            continue
        lines.append(f"{k}: {json.dumps(v)}")
    if nested and "type" in fm:
        lines += ["metadata: ", "  node_type: memory", f"  type: {fm['type']}", "  modified: 2026-09-20T01:02:03.000Z"]
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "work" / "My_Repo"
    (r / "meridian").mkdir(parents=True)
    (r / "meridian" / "server.py").write_text("# real file\n", encoding="utf-8")
    (r / "CLAUDE.local.md").write_text(f"# local\nProject ID: {PID}\n", encoding="utf-8")
    return r


@pytest.fixture()
def projects(tmp_path: Path, repo: Path) -> Path:
    root = tmp_path / "claude" / "projects"
    mem = root / mi.claude_slug(repo) / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text(
        "# Memory Index\n\n"
        "- [Never commit to main](feedback_never_main.md) \u2014 push to dev only\n"
        "- [Code search tooling](feedback_code_search.md) \u2014 how to search\n"
        "- [Gone file](feedback_gone.md) \u2014 dangling\n",
        encoding="utf-8",
    )
    (mem / "feedback_never_main.md").write_text(
        _md({"name": "feedback-never-main", "description": "Never push to main directly", "type": "feedback"},
            "Never push to main.\n\n**Why:** prod auto-deploys.\n"),
        encoding="utf-8",
    )
    (mem / "feedback_code_search.md").write_text(
        _md({"name": "feedback-code-search", "description": "Search code with the graph", "type": "feedback"},
            "Prefer the graph tools over grep for code.\n"
            'Pass project="meridian-repo" to search_graph.\n'
            "Try mcp__meridian__extractor__find_symbol first.\n"
            "Edit routes/tunnel.py carefully; it is a collision hub.\n"
            "The meridian-build project is the product.\n"
            'Call index_repository(name="meridian-build") first.\n'
            f"creds: {FAKE_AWS_KEY}\n"
            "Keep reading meridian/server.py and meridian/gone_module.py before edits.\n",
            nested=True),
        encoding="utf-8",
    )
    (mem / "reference_build.md").write_text(
        _md({"name": "reference-build", "description": "How to build the dashboard", "type": "reference"},
            "Run node build.mjs.\n"),
        encoding="utf-8",
    )
    (mem / "project_state.md").write_text(
        _md({"name": "project-state", "description": "Meridian project state", "type": "project"},
            "- Prod is at **origin/main@c34723c** (live).\n"
            "- ~1773 tests passing.\n"
            "- Status as of 2026-06-24: tunnel sprint in progress.\n"
            "- The dashboard is TypeScript + Preact.\n"),
        encoding="utf-8",
    )
    (mem / "user_adam.md").write_text(
        _md({"name": "User profile", "description": "Solo developer", "type": "user"}, "Adam builds Meridian.\n"),
        encoding="utf-8",
    )
    (mem / "notes-without-frontmatter.md").write_text("# Loose note\n\nSome durable fact.\n", encoding="utf-8")
    (mem / "feedback_mismatch.md").write_text(
        _md({"name": "x", "description": "FIXED: a bug that is gone", "type": "reference"}, "Old bug details.\n"),
        encoding="utf-8",
    )
    # A second memory dir for a path that no longer exists, with a duplicate.
    other = root / "Z--Nowhere-at-all" / "memory"
    other.mkdir(parents=True)
    (other / "feedback_dup.md").write_text(
        _md({"name": "Never commit to main", "description": "same rule elsewhere", "type": "feedback"},
            "Never push to main.\n\n**Why:** prod auto-deploys.\n"),
        encoding="utf-8",
    )
    (other / "reference_pid.md").write_text(
        _md({"name": "reference-pid", "description": "Where state lives", "type": "reference"},
            "State lives in project id `193b0c19-986d-4d08-b48e-45cf00980776`.\n"),
        encoding="utf-8",
    )
    # An empty memory dir is reported but not decoded.
    (root / "D--empty-run" / "memory").mkdir(parents=True)
    # A renamed archive is never scanned.
    (root / "C--archived" / "memory.imported-2026-09-01").mkdir(parents=True)
    return root


def _records(mapping: dict) -> dict[str, dict]:
    return {Path(r["source_file"]).name: r for r in mapping["records"]}


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}


# ---------------------------------------------------------------------------
# Slugs and resolution
# ---------------------------------------------------------------------------


def test_slug_variants():
    assert mi.claude_slug("C:\\Users\\me\\Masters_Thesis\\CODE") == "C--Users-me-Masters-Thesis-CODE"
    assert mi.claude_slug_legacy("C:\\Users\\me\\Masters_Thesis\\CODE") == "C--Users-me-Masters_Thesis-CODE"
    assert mi.claude_slug("/home/u/.claude") == "-home-u--claude"


def test_decode_slug_round_trips_real_directories(repo):
    assert mi.decode_slug(mi.claude_slug(repo)) == repo
    assert mi.decode_slug(mi.claude_slug_legacy(repo)) == repo
    assert mi.decode_slug(mi.claude_slug(repo) + "-does-not-exist") is None
    assert mi.decode_slug("no-drive-prefix") is None


def test_project_resolved_from_claude_local_md(projects, repo):
    mapping = mi.build_mapping(projects, now=NOW)
    dirs = {d["slug"]: d for d in mapping["memory_dirs"]}
    main = dirs[mi.claude_slug(repo)]
    assert main["project_id"] == PID
    assert main["resolution"] == "CLAUDE.local.md"
    assert Path(main["decoded_root"]) == repo
    assert main["dangling_index_entries"] == ["feedback_gone.md"]
    other = dirs["Z--Nowhere-at-all"]
    assert other["project_id"] is None and other["resolution"].startswith("unresolved")
    assert other["candidate_project_ids"] == ["193b0c19-986d-4d08-b48e-45cf00980776"]
    assert dirs["D--empty-run"]["resolution"] == "skipped (empty memory dir)"
    assert "C--archived" not in dirs


def test_explicit_project_override(projects):
    mapping = mi.build_mapping(projects, now=NOW, overrides={"Z--Nowhere-at-all": "11111111-2222-3333-4444-555555555555"})
    dirs = {d["slug"]: d for d in mapping["memory_dirs"]}
    assert dirs["Z--Nowhere-at-all"]["resolution"] == "explicit --project"
    assert _records(mapping)["reference_pid.md"]["target"]["project_id"] == "11111111-2222-3333-4444-555555555555"


def test_parse_overrides_accepts_paths_and_rejects_garbage():
    assert mi._parse_overrides(["C:\\Users\\me\\repo=abc"]) == {"C--Users-me-repo": "abc"}
    assert mi._parse_overrides(["slug-x=pid"]) == {"slug-x": "pid"}
    with pytest.raises(ValueError):
        mi._parse_overrides(["no-equals"])


# ---------------------------------------------------------------------------
# Parsing + classification + mapping
# ---------------------------------------------------------------------------


def test_frontmatter_styles_and_fallback_parser():
    fm, body, start = mi.split_frontmatter('---\nname: a\ndescription: "say \\"hi\\""\ntype: feedback\n---\n\nBody\n')
    parsed, note = mi.parse_frontmatter(fm)
    assert parsed == {"name": "a", "description": 'say "hi"', "type": "feedback"} and note is None
    assert body.strip() == "Body" and start == 6
    minimal = mi._parse_frontmatter_minimal(
        "name: n\ndescription: >-\n  folded line one\n  line two\nmetadata:\n  type: project\n  node_type: memory\nq: 'it''s'\n"
    )
    assert minimal["description"] == "folded line one line two"
    assert minimal["metadata"] == {"type": "project", "node_type": "memory"}
    assert minimal["q"] == "it's"
    assert mi.split_frontmatter("no frontmatter")[0] is None
    assert mi.split_frontmatter("---\nunterminated")[0] is None
    bad, bad_note = mi.parse_frontmatter("name: [unclosed\n")
    assert bad_note == "parsed with fallback parser"
    assert mi.parse_frontmatter("- just\n- a list\n") == ({}, "frontmatter is not a mapping")


def test_classify_kind_precedence():
    assert mi.classify_kind({"type": "project"}, "feedback_x.md") == (
        "project", "frontmatter", "frontmatter type 'project' but filename prefix 'feedback_'")
    assert mi.classify_kind({"metadata": {"type": "reference"}}, "anything.md")[:2] == ("reference", "frontmatter")
    assert mi.classify_kind({}, "user_profile.md")[:2] == ("user", "filename")
    assert mi.classify_kind({"name": "feedback-x"}, "x.md")[:2] == ("feedback", "name")
    assert mi.classify_kind({}, "x.md")[:2] == ("unclassified", "none")


def test_kind_to_target_mapping(projects):
    recs = _records(mi.build_mapping(projects, now=NOW))
    never = recs["feedback_never_main.md"]
    assert never["decision_candidate"] is True
    assert never["target"]["tool"] == "pin_decision" and never["target"]["category"] == "PROCESS"
    assert never["alternatives"][0]["tool"] == "add_note"
    search = recs["feedback_code_search.md"]
    assert search["target"]["tool"] == "add_note" and search["target"]["category"] == "rule"
    assert search["alternatives"][0] == {"tool": "pin_decision", "category": "PROCESS"}
    ref = recs["reference_build.md"]
    assert ref["target"]["tool"] == "add_note" and ref["target"]["kind"] == "reference"
    assert ref["target"]["project_id"] == PID
    assert recs["project_state.md"]["target"]["project_id"] == PID
    user = recs["user_adam.md"]
    assert user["target"]["tool"] == "add_workspace_note" and "project_id" not in user["target"]
    loose = recs["notes-without-frontmatter.md"]
    assert loose["memory_kind"] == "unclassified"
    assert any(w["kind"] == "no-frontmatter" for w in loose["warnings"])


def test_titles_tags_and_provenance(projects):
    recs = _records(mi.build_mapping(projects, now=NOW))
    never = recs["feedback_never_main.md"]
    assert never["title"] == "Never commit to main" and never["title_source"] == "MEMORY.md link text"
    assert recs["user_adam.md"]["title"] == "User profile"
    assert recs["reference_build.md"]["title"] == "How to build the dashboard"
    for r in recs.values():
        tags = r["target"]["tags"].split(",")
        assert mi.IMPORT_TAG in tags
        assert f"amimport-{r['title_hash']}" in tags
        assert r["approved"] is False
    assert "Imported from Claude Code auto-memory" in never["target"]["body"]


def test_dedupe_by_title_hash(projects):
    recs = _records(mi.build_mapping(projects, now=NOW))
    dup = recs["feedback_dup.md"]
    assert dup["action"] == "skip_duplicate"
    assert dup["duplicate_of"] == recs["feedback_never_main.md"]["record_id"]
    assert mi.title_hash("Never commit to  MAIN!") == mi.title_hash("never commit to main")


def test_unresolved_project_marks_needs_project(projects):
    recs = _records(mi.build_mapping(projects, now=NOW))
    assert recs["reference_pid.md"]["action"] == "needs_project"
    assert recs["reference_pid.md"]["target"]["project_id"] is None


# ---------------------------------------------------------------------------
# Stale facts are flagged, not copied
# ---------------------------------------------------------------------------


def test_stale_lines_are_excluded_and_listed(projects):
    rec = _records(mi.build_mapping(projects, now=NOW))["feedback_code_search.md"]
    body = rec["target"]["body"]
    kinds = {f["kind"] for f in rec["excluded_lines"]}
    assert kinds == {"stale-index-name", "tunnel-dependent", "secret-shaped"}
    assert "meridian-repo" not in body
    assert "mcp__meridian__extractor__" not in body
    assert 'index_repository(name="meridian-build")' not in body
    # Not stale: a file path containing 'tunnel', and the project named in prose.
    assert "routes/tunnel.py" in body
    assert "The meridian-build project is the product." in body
    assert "Prefer the graph tools over grep for code." in body
    assert rec["action"] == "import_with_exclusions"
    flagged_text = " ".join(f.get("text", "") for f in rec["excluded_lines"])
    assert 'project="meridian-repo"' in flagged_text


def test_secret_shaped_lines_are_never_reproduced(projects, tmp_path):
    mapping = mi.build_mapping(projects, now=NOW)
    rec = _records(mapping)["feedback_code_search.md"]
    secret = [f for f in rec["excluded_lines"] if f["kind"] == "secret-shaped"]
    assert len(secret) == 1 and "text" not in secret[0]
    assert "aws-access-key-id" in secret[0]["reason"]
    blob = json.dumps(mapping) + mi.render_summary_markdown(mapping)
    assert FAKE_AWS_KEY not in blob and FAKE_AWS_KEY[:12] not in blob


def test_dated_state_lines_are_excluded(projects):
    rec = _records(mi.build_mapping(projects, now=NOW))["project_state.md"]
    assert [f["kind"] for f in rec["excluded_lines"]] == ["dated-state"] * 3
    assert rec["target"]["body"].startswith("- The dashboard is TypeScript + Preact.")
    assert rec["action"] == "review_only"  # most of the snapshot was stale


@pytest.mark.parametrize(
    "line,kind",
    [
        ('search_graph(project="meridian-canonical")', "stale-index-name"),
        ("indexed as meridian-core in the graph", "stale-index-name"),
        ("`dnabert-error-correction` is now indexed", "stale-index-name"),
        ("use the meridian-code slot", "tunnel-dependent"),
        ("route calls via the tunnel first", "tunnel-dependent"),
        ("Prod is at main@abcdef1 (live)", "dated-state"),
        ("Target 617+ passing", "dated-state"),
        ("Currently blocked as of 2026-09-01", "dated-state"),
    ],
)
def test_flag_line_positive(line, kind):
    verdict = mi.flag_line(line, mi._index_name_patterns(mi.DEFAULT_STALE_INDEX_NAMES))
    assert verdict is not None and verdict[0] == kind


@pytest.mark.parametrize(
    "line",
    [
        "the dnabert-error-correction repo has a README",
        "meridian-build is the Meridian project name",
        "a tunnel restart reopens the browser",
        "hosted-tunnel variants 503 while local stdio works",
        "Fixed on 2026-09-15 in a cherry-pick",
        "meridian-extract (Serena) is the local server",
        "search_graph works on the slug index",
    ],
)
def test_flag_line_negative(line):
    assert mi.flag_line(line, mi._index_name_patterns(mi.DEFAULT_STALE_INDEX_NAMES)) is None


def test_record_warnings(projects):
    # feedback_code_search.md is a durable rule modified 2026-09-20 (nested
    # frontmatter): no age warning at +45 days, one at +120 days.
    soon = _records(mi.build_mapping(projects, now=NOW + dt.timedelta(days=45)))
    assert "age" not in {w["kind"] for w in soon["feedback_code_search.md"]["warnings"]}
    recs = _records(mi.build_mapping(projects, now=NOW + dt.timedelta(days=120)))
    search = recs["feedback_code_search.md"]
    kinds = {w["kind"] for w in search["warnings"]}
    assert "age" in kinds
    missing = [w for w in search["warnings"] if w["kind"] == "missing-paths"]
    assert missing and "meridian/gone_module.py" in missing[0]["reason"]
    assert "meridian/server.py" not in missing[0]["reason"]
    mismatch = recs["feedback_mismatch.md"]
    assert {w["kind"] for w in mismatch["warnings"]} >= {"kind-mismatch", "resolved-or-superseded"}


def test_project_state_ages_faster_than_rules(tmp_path):
    root = tmp_path / "projects"
    mem = root / "Z--gone" / "memory"
    mem.mkdir(parents=True)
    for name, kind in (("project_s.md", "project"), ("feedback_r.md", "feedback")):
        (mem / name).write_text(
            "---\nname: n-" + kind + "\ndescription: d " + kind + "\nmetadata:\n  type: " + kind
            + "\n  modified: 2026-08-01T00:00:00Z\n---\n\nbody " + kind + "\n",
            encoding="utf-8",
        )
    recs = _records(mi.build_mapping(root, now=NOW))  # 56 days later
    assert "age" in {w["kind"] for w in recs["project_s.md"]["warnings"]}
    assert "age" not in {w["kind"] for w in recs["feedback_r.md"]["warnings"]}


def test_missing_paths_only_checked_for_this_repos_top_level_dirs(tmp_path):
    repo = tmp_path / "thesis"
    (repo / "helpers").mkdir(parents=True)
    root = tmp_path / "projects"
    mem = root / mi.claude_slug(repo) / "memory"
    mem.mkdir(parents=True)
    (mem / "reference_x.md").write_text(
        "---\nname: r\ndescription: d\ntype: reference\n---\n\n"
        "See meridian/server.py (another repo) and helpers/missing.py (this repo).\n",
        encoding="utf-8",
    )
    rec = _records(mi.build_mapping(root, now=NOW))["reference_x.md"]
    (warning,) = [w for w in rec["warnings"] if w["kind"] == "missing-paths"]
    assert "helpers/missing.py" in warning["reason"]
    assert "meridian/server.py" not in warning["reason"]


# ---------------------------------------------------------------------------
# Read-only + outputs
# ---------------------------------------------------------------------------


def test_dry_run_is_read_only_and_writes_only_outputs(projects, tmp_path):
    before = _snapshot(projects)
    out = tmp_path / "out" / "mapping.json"
    summary = tmp_path / "out" / "summary.md"
    stdout = io.StringIO()
    rc = mi.cli_main(["import", "--projects-root", str(projects), "--out", str(out), "--summary", str(summary)], stdout=stdout)
    assert rc == 0
    assert _snapshot(projects) == before
    mapping = json.loads(out.read_text(encoding="utf-8"))
    assert mapping["schema"] == mi.SCHEMA and mapping["mode"] == "dry-run"
    assert mapping["approval"]["approved"] is False
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["mapping.json", "summary.md"]
    text = summary.read_text(encoding="utf-8")
    for needle in (
        "## Per kind",
        "## Per memory dir / project",
        "## Flagged stale facts",
        "## Record-level warnings",
        "## Not importable as proposed",
        "stale-index-name",
        'project="meridian-repo"',
        PID,
    ):
        assert needle in text, needle
    assert "[dry-run]" in stdout.getvalue()


def test_summary_counts(projects):
    s = mi.build_mapping(projects, now=NOW)["summary"]
    assert s["records"] == 9
    assert s["by_kind"] == {"feedback": 3, "project": 1, "reference": 3, "unclassified": 1, "user": 1}
    assert s["by_action"]["skip_duplicate"] == 1
    assert s["excluded_lines_by_flag"]["dated-state"] == 3


def test_cli_rejects_bad_project_override(projects, tmp_path):
    err = io.StringIO()
    rc = mi.cli_main(["import", "--projects-root", str(projects), "--out", str(tmp_path / "m.json"),
                      "--project", "oops"], stderr=err)
    assert rc == 2 and "--project" in err.getvalue()


def test_custom_stale_index_list(projects, tmp_path):
    out = tmp_path / "m.json"
    mi.cli_main(["import", "--projects-root", str(projects), "--out", str(out), "--stale-index", "some-other"],
                stdout=io.StringIO())
    mapping = json.loads(out.read_text(encoding="utf-8"))
    assert mapping["stale_index_names"] == ["some-other"]
    rec = _records(mapping)["feedback_code_search.md"]
    assert "stale-index-name" not in {f["kind"] for f in rec["excluded_lines"]}


def test_main_dispatches_memory_subcommand(projects, tmp_path, capsys):
    out = tmp_path / "via_main.json"
    assert meridian_main(["memory", "import", "--projects-root", str(projects), "--out", str(out)]) == 0
    assert out.is_file() and "[dry-run]" in capsys.readouterr().out


def test_default_projects_root(tmp_path):
    assert mi.default_projects_root({"CLAUDE_CONFIG_DIR": str(tmp_path)}) == tmp_path / "projects"
    assert mi.default_projects_root({}).parts[-2:] == (".claude", "projects")
    assert mi.scan_memory_dirs(tmp_path / "missing") == []


# ---------------------------------------------------------------------------
# --apply: owner approval required; idempotent; fake writer only
# ---------------------------------------------------------------------------


class FakeWriter:
    def __init__(self, existing: set[str] | None = None, fail: set[str] | None = None) -> None:
        self.existing = existing or set()
        self.fail = fail or set()
        self.created: list[dict] = []

    def exists(self, record):
        return record["record_id"] in self.existing

    def create(self, record):
        if record["record_id"] in self.fail:
            raise OSError("boom")
        self.created.append(dict(record["target"]))
        return {"id": f"new-{record['record_id']}"}


def _approved_copy(mapping: dict, tmp_path: Path, approve: set[str], *, approver: str | None = "adam") -> Path:
    m = json.loads(json.dumps(mapping))
    m["approval"]["approved"] = True
    m["approval"]["approved_by"] = approver
    for r in m["records"]:
        r["approved"] = Path(r["source_file"]).name in approve
    path = tmp_path / "approved.json"
    path.write_text(json.dumps(m), encoding="utf-8")
    return path


def test_apply_refuses_the_unapproved_dry_run_output(projects, tmp_path):
    out = tmp_path / "m.json"
    mi.cli_main(["import", "--projects-root", str(projects), "--out", str(out)], stdout=io.StringIO())
    writer = FakeWriter()
    err = io.StringIO()
    assert mi.cli_main(["import", "--apply", str(out)], stderr=err, writer=writer) == 1
    assert "not approved" in err.getvalue()
    assert writer.created == []


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda m: m.update(schema="other"), "is not a"),
        (lambda m: m["approval"].update(approved_by=""), "approved_by"),
        (lambda m: [r.update(approved=False) for r in m["records"]], "no record"),
        (lambda m: m.update(records="nope"), "no records"),
    ],
)
def test_load_approved_mapping_validation(projects, tmp_path, mutate, match):
    mapping = mi.build_mapping(projects, now=NOW)
    path = _approved_copy(mapping, tmp_path, {"reference_build.md"})
    m = json.loads(path.read_text(encoding="utf-8"))
    mutate(m)
    path.write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(mi.ApplyError, match=match):
        mi.load_approved_mapping(path)
    with pytest.raises(mi.ApplyError):
        mi.load_approved_mapping(tmp_path / "missing.json")


def test_apply_writes_only_approved_valid_records(projects, tmp_path):
    mapping = mi.build_mapping(projects, now=NOW)
    recs = _records(mapping)
    path = _approved_copy(
        mapping, tmp_path,
        {"reference_build.md", "user_adam.md", "feedback_dup.md", "reference_pid.md", "feedback_never_main.md"},
    )
    writer = FakeWriter(existing={recs["feedback_never_main.md"]["record_id"]})
    out = io.StringIO()
    rc = mi.cli_main(["import", "--apply", str(path)], stdout=out, writer=writer)
    assert rc == 0
    titles = sorted(t["title"] for t in writer.created)
    assert titles == ["How to build the dashboard", "User profile"]
    counts = json.loads(out.getvalue().splitlines()[0])
    assert counts == {"applied": 2, "skipped_existing": 1, "rejected": 2, "errors": 0}
    text = out.getvalue()
    assert "duplicate record" in text and "project_id missing" in text


def test_apply_rejects_secret_shaped_and_reports_errors(projects, tmp_path):
    mapping = mi.build_mapping(projects, now=NOW)
    recs = _records(mapping)
    ref = json.loads(json.dumps(recs["reference_build.md"]))
    ref["approved"] = True
    ref["target"]["body"] += f"\nkey {FAKE_AWS_KEY}\n"
    report = mi.apply_mapping([ref], FakeWriter())
    assert report["rejected"][0]["reason"] == "target contains secret-shaped text"
    good = json.loads(json.dumps(recs["user_adam.md"]))
    report = mi.apply_mapping([good], FakeWriter(fail={good["record_id"]}))
    assert report["errors"] and "boom" in report["errors"][0]["error"]
    bad_tool = {"record_id": "x", "target": {"tool": "delete_note", "title": "t", "body": "b"}}
    assert "unsupported" in mi.apply_mapping([bad_tool], FakeWriter())["rejected"][0]["reason"]
    assert mi.apply_mapping([{"record_id": "y"}], FakeWriter())["rejected"][0]["reason"] == "record has no target"


class _StubMeridian(BaseHTTPRequestHandler):
    notes: list[dict] = []
    decisions: list[dict] = []
    workspace: list[dict] = []
    seen_auth: list[str | None] = []

    def log_message(self, *args):  # silence
        pass

    def _send(self, code: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        type(self).seen_auth.append(self.headers.get("Authorization"))
        path, _, query = self.path.partition("?")
        tag = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("tag", "")
        if path.endswith("/decisions-pinned"):
            return self._send(200, type(self).decisions)
        rows = type(self).workspace if path == "/workspace/notes" else type(self).notes
        return self._send(200, [r for r in rows if tag and tag in r.get("tags", "")])

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.endswith("/decisions-pinned"):
            type(self).decisions.append(body)
        elif self.path == "/workspace/notes":
            type(self).workspace.append(body)
        else:
            type(self).notes.append(body)
        return self._send(201, {"id": f"id{len(type(self).notes) + len(type(self).decisions) + len(type(self).workspace)}"})


def test_http_writer_against_stub_server_is_idempotent(projects):
    _StubMeridian.notes, _StubMeridian.decisions, _StubMeridian.workspace, _StubMeridian.seen_auth = [], [], [], []
    server = HTTPServer(("127.0.0.1", 0), _StubMeridian)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        recs = _records(mi.build_mapping(projects, now=NOW))
        chosen = [recs[n] for n in ("reference_build.md", "user_adam.md", "feedback_never_main.md")]
        writer = mi.HttpMeridianWriter(f"http://127.0.0.1:{server.server_port}", token="test-token")
        first = mi.apply_mapping(chosen, writer)
        assert len(first["applied"]) == 3, first
        second = mi.apply_mapping(chosen, writer)
        assert len(second["skipped_existing"]) == 3 and not second["applied"]
        assert _StubMeridian.notes[0]["kind"] == "reference"
        assert _StubMeridian.decisions[0]["category"] == "PROCESS"
        assert set(_StubMeridian.seen_auth) == {"Bearer test-token"}
    finally:
        server.shutdown()
        server.server_close()


def test_http_writer_unreachable_server_is_an_error_not_a_crash(projects):
    recs = _records(mi.build_mapping(projects, now=NOW))
    writer = mi.HttpMeridianWriter("http://127.0.0.1:9", timeout=2)
    report = mi.apply_mapping([recs["reference_build.md"]], writer)
    assert report["errors"] and not report["applied"]
