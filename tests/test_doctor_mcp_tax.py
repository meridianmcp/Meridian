"""be5837bc -- tests for meridian/doctor.py's MCP tool-list token-tax /
duplicate-server-registration doctor check.

Covers the sprint item's explicit test list:
  * correct duplicate detection given a synthetic multi-server manifest fixture
  * correct non-flagging when no duplicates exist
  * nothing here ever writes to a config file

Plus the supporting pure functions (``parse_tool_name``,
``load_manifest_snapshot``, ``jaccard_similarity``) since the detection
logic is only as trustworthy as its inputs.
"""
from __future__ import annotations

import io
import json

import pytest

from meridian import doctor


# ---------------------------------------------------------------------------
# Fixtures -- synthetic, multi-server manifest snapshots
# ---------------------------------------------------------------------------


def _tool(name: str, description: str = "", schema: "dict | None" = None) -> dict:
    return {"name": name, "description": description or f"Does {name}.", "inputSchema": schema or {"type": "object"}}


MERIDIAN_TOOLS = ["start_session", "log_task", "complete_sprint_item", "claim_sprint_item", "generate_handoff"]
CBM_TOOLS = ["search_code", "search_graph", "index_repository", "get_architecture"]
SERENA_TOOLS = ["find_symbol", "replace_symbol_body", "find_referencing_symbols", "get_symbols_overview", "insert_after_symbol"]
EXTRACT_TOOLS = ["find_symbol", "replace_symbol_body", "find_referencing_symbols", "rename_symbol", "safe_delete_symbol"]
GITHUB_TOOLS = ["list_issues", "create_pull_request", "get_commit"]


@pytest.fixture
def duplicate_snapshot() -> dict:
    """A synthetic multi-server snapshot reproducing the real 2026-09-29
    audit's three named cases in one fixture:

    * ``meridian`` / ``98ff5a3a-9b9d-4075-8d6e-306ff084c0eb`` -- the SAME
      server, byte-identical tool names, registered under a human name and
      a UUID-shaped connector slot -> exact duplicate, nameable fix.
    * ``codebase-memory`` / ``codebase-memory-mcp`` -- the same server under
      two human-chosen slugs, neither UUID-shaped -> exact duplicate,
      generic (non-nameable) fix.
    * ``serena`` / ``meridian-extract`` -- genuinely different servers that
      partially overlap (3 of 7 combined tool names) -> a softer
      "overlapping tool set" flag, not a hard duplicate.
    * ``github`` -- disjoint from everything else; must never be flagged.
    """
    return {
        "servers": [
            {"slot_name": "meridian", "tools": [_tool(t) for t in MERIDIAN_TOOLS]},
            {
                "slot_name": "98ff5a3a-9b9d-4075-8d6e-306ff084c0eb",
                "tools": [_tool(t) for t in MERIDIAN_TOOLS],
            },
            {"slot_name": "codebase-memory", "tools": [_tool(t) for t in CBM_TOOLS]},
            {"slot_name": "codebase-memory-mcp", "tools": [_tool(t) for t in CBM_TOOLS]},
            {"slot_name": "serena", "tools": [_tool(t) for t in SERENA_TOOLS]},
            {"slot_name": "meridian-extract", "tools": [_tool(t) for t in EXTRACT_TOOLS]},
            {"slot_name": "github", "tools": [_tool(t) for t in GITHUB_TOOLS]},
        ]
    }


@pytest.fixture
def no_duplicate_snapshot() -> dict:
    """Every server's tool set is fully disjoint from every other -- nothing
    here should ever be flagged."""
    return {
        "servers": [
            {"slot_name": "meridian", "tools": [_tool(t) for t in MERIDIAN_TOOLS]},
            {"slot_name": "github", "tools": [_tool(t) for t in GITHUB_TOOLS]},
            {"slot_name": "serena", "tools": [_tool(t) for t in SERENA_TOOLS]},
        ]
    }


# ---------------------------------------------------------------------------
# parse_tool_name
# ---------------------------------------------------------------------------


class TestParseToolName:
    def test_strips_slot_prefix(self):
        assert doctor.parse_tool_name("mcp__meridian__start_session") == ("meridian", "start_session")

    def test_uuid_slot_prefix(self):
        assert doctor.parse_tool_name(
            "mcp__98ff5a3a-9b9d-4075-8d6e-306ff084c0eb__start_session"
        ) == ("98ff5a3a-9b9d-4075-8d6e-306ff084c0eb", "start_session")

    def test_tool_name_with_double_underscore_is_preserved_whole(self):
        # maxsplit=2 keeps everything after the second "__" as the tool name,
        # even if that name itself contains "__".
        assert doctor.parse_tool_name("mcp__slot__weird__tool__name") == ("slot", "weird__tool__name")

    def test_non_mcp_tool_has_no_slot(self):
        assert doctor.parse_tool_name("Read") == (None, "Read")
        assert doctor.parse_tool_name("Bash") == (None, "Bash")

    def test_malformed_mcp_prefix_does_not_raise(self):
        slot, bare = doctor.parse_tool_name("mcp__onlyslot")
        assert slot == "onlyslot"
        assert bare == ""


# ---------------------------------------------------------------------------
# load_manifest_snapshot -- accepted shapes
# ---------------------------------------------------------------------------


class TestLoadManifestSnapshot:
    def test_empty_snapshot(self):
        assert doctor.load_manifest_snapshot({}) == ()
        assert doctor.load_manifest_snapshot([]) == ()
        assert doctor.load_manifest_snapshot(None) == ()

    def test_servers_key_present_but_empty(self):
        # A non-empty wrapper dict whose "servers" list is itself empty --
        # distinct from an empty snapshot entirely (covers the post-"servers"
        # empty-entries path, not just the top-level empty check).
        assert doctor.load_manifest_snapshot({"servers": []}) == ()

    def test_pregrouped_servers_key(self, no_duplicate_snapshot):
        servers = doctor.load_manifest_snapshot(no_duplicate_snapshot)
        assert {s.slot_name for s in servers} == {"meridian", "github", "serena"}
        by_name = {s.slot_name: s for s in servers}
        assert by_name["meridian"].bare_names == set(MERIDIAN_TOOLS)
        assert len(by_name["meridian"].tools) == len(MERIDIAN_TOOLS)

    def test_bare_mapping_of_slot_to_tools(self):
        snapshot = {"meridian": MERIDIAN_TOOLS, "github": GITHUB_TOOLS}
        servers = doctor.load_manifest_snapshot(snapshot)
        assert {s.slot_name for s in servers} == {"meridian", "github"}
        by_name = {s.slot_name: s for s in servers}
        assert by_name["meridian"].bare_names == set(MERIDIAN_TOOLS)

    def test_flat_tools_key_groups_by_mcp_prefix(self):
        flat = {
            "tools": [
                f"mcp__meridian__{t}" for t in MERIDIAN_TOOLS
            ] + [
                f"mcp__github__{t}" for t in GITHUB_TOOLS
            ]
        }
        servers = doctor.load_manifest_snapshot(flat)
        assert {s.slot_name for s in servers} == {"meridian", "github"}
        by_name = {s.slot_name: s for s in servers}
        assert by_name["meridian"].bare_names == set(MERIDIAN_TOOLS)
        assert by_name["github"].bare_names == set(GITHUB_TOOLS)

    def test_bare_flat_list_with_no_wrapper_key(self):
        flat = [f"mcp__meridian__{t}" for t in MERIDIAN_TOOLS]
        servers = doctor.load_manifest_snapshot(flat)
        assert len(servers) == 1
        assert servers[0].slot_name == "meridian"
        assert servers[0].bare_names == set(MERIDIAN_TOOLS)

    def test_non_mcp_tools_land_in_synthetic_group_not_dropped(self):
        flat = ["Read", "Bash", "mcp__meridian__start_session"]
        servers = doctor.load_manifest_snapshot(flat)
        by_name = {s.slot_name: s for s in servers}
        assert by_name["(no server prefix)"].bare_names == {"Read", "Bash"}
        assert by_name["meridian"].bare_names == {"start_session"}

    def test_flat_and_pregrouped_agree_on_duplicate_detection(self, duplicate_snapshot):
        """The exact same logical manifest, expressed in either accepted
        shape, must produce the same duplicate findings -- the caller
        shouldn't have to know which shape they captured to get a
        trustworthy report."""
        pregrouped = doctor.load_manifest_snapshot(duplicate_snapshot)

        flat_tools = []
        for entry in duplicate_snapshot["servers"]:
            for t in entry["tools"]:
                flat_tools.append({**t, "name": f"mcp__{entry['slot_name']}__{t['name']}"})
        flat = doctor.load_manifest_snapshot({"tools": flat_tools})

        pregrouped_sets = {s.slot_name: s.bare_names for s in pregrouped}
        flat_sets = {s.slot_name: s.bare_names for s in flat}
        assert pregrouped_sets == flat_sets

        assert doctor.find_duplicate_registrations(pregrouped) == doctor.find_duplicate_registrations(flat)


# ---------------------------------------------------------------------------
# jaccard_similarity
# ---------------------------------------------------------------------------


class TestJaccardSimilarity:
    def test_identical_sets(self):
        s = frozenset({"a", "b", "c"})
        assert doctor.jaccard_similarity(s, s) == 1.0

    def test_disjoint_sets(self):
        assert doctor.jaccard_similarity(frozenset({"a"}), frozenset({"b"})) == 0.0

    def test_both_empty_is_zero_not_identical(self):
        # Two servers with NO tools at all are not a meaningful "duplicate".
        assert doctor.jaccard_similarity(frozenset(), frozenset()) == 0.0

    def test_partial_overlap(self):
        a = frozenset({"x", "y", "z"})
        b = frozenset({"x", "y", "w"})
        # shared=2, union=4
        assert doctor.jaccard_similarity(a, b) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# find_duplicate_registrations -- the core detection logic
# ---------------------------------------------------------------------------


class TestFindDuplicateRegistrations:
    def test_exact_duplicate_with_uuid_slot_names_the_uuid_as_the_duplicate(self, duplicate_snapshot):
        servers = doctor.load_manifest_snapshot(duplicate_snapshot)
        findings = doctor.find_duplicate_registrations(servers)
        by_pair = {frozenset({f.slot_a, f.slot_b}): f for f in findings}

        pair = frozenset({"meridian", "98ff5a3a-9b9d-4075-8d6e-306ff084c0eb"})
        assert pair in by_pair
        finding = by_pair[pair]
        assert finding.kind == "exact_duplicate_registration"
        assert finding.similarity == pytest.approx(1.0)
        assert finding.shared_tool_count == len(MERIDIAN_TOOLS)
        # The UUID-shaped slot must be the one plain-language-named as "the duplicate".
        assert "98ff5a3a-9b9d-4075-8d6e-306ff084c0eb" in finding.suggested_fix
        assert "duplicate" in finding.suggested_fix.lower()

    def test_exact_duplicate_with_two_human_names_gives_generic_fix(self, duplicate_snapshot):
        servers = doctor.load_manifest_snapshot(duplicate_snapshot)
        findings = doctor.find_duplicate_registrations(servers)
        by_pair = {frozenset({f.slot_a, f.slot_b}): f for f in findings}

        pair = frozenset({"codebase-memory", "codebase-memory-mcp"})
        assert pair in by_pair
        finding = by_pair[pair]
        assert finding.kind == "exact_duplicate_registration"
        assert finding.similarity == pytest.approx(1.0)
        # Neither slot name is UUID-shaped, so the fix must not pretend to
        # single one out -- both names should appear, ownership left to the human.
        assert "codebase-memory" in finding.suggested_fix
        assert "codebase-memory-mcp" in finding.suggested_fix

    def test_partial_overlap_flagged_as_overlapping_not_exact(self, duplicate_snapshot):
        servers = doctor.load_manifest_snapshot(duplicate_snapshot)
        findings = doctor.find_duplicate_registrations(servers)
        by_pair = {frozenset({f.slot_a, f.slot_b}): f for f in findings}

        pair = frozenset({"serena", "meridian-extract"})
        assert pair in by_pair
        finding = by_pair[pair]
        assert finding.kind == "overlapping_tool_set"
        assert finding.shared_tool_count == 3  # find_symbol, replace_symbol_body, find_referencing_symbols
        assert 0.25 <= finding.similarity < 0.98

    def test_disjoint_server_never_flagged(self, duplicate_snapshot):
        servers = doctor.load_manifest_snapshot(duplicate_snapshot)
        findings = doctor.find_duplicate_registrations(servers)
        involved_slots = {f.slot_a for f in findings} | {f.slot_b for f in findings}
        assert "github" not in involved_slots

    def test_finding_as_dict_shape(self, duplicate_snapshot):
        servers = doctor.load_manifest_snapshot(duplicate_snapshot)
        findings = doctor.find_duplicate_registrations(servers)
        payload = findings[0].as_dict()
        assert set(payload) == {"slot_a", "slot_b", "shared_tool_count", "similarity", "kind", "suggested_fix"}
        # Must be JSON-serializable (this is what a caller renders/ships).
        json.dumps(payload)

    def test_exact_count_of_findings(self, duplicate_snapshot):
        """Exactly the three intended pairs are flagged -- no false positives
        from the remaining (disjoint) cross-pairs in this 7-server fixture."""
        servers = doctor.load_manifest_snapshot(duplicate_snapshot)
        findings = doctor.find_duplicate_registrations(servers)
        assert len(findings) == 3
        kinds = sorted(f.kind for f in findings)
        assert kinds == ["exact_duplicate_registration", "exact_duplicate_registration", "overlapping_tool_set"]

    def test_no_duplicates_returns_empty_tuple(self, no_duplicate_snapshot):
        servers = doctor.load_manifest_snapshot(no_duplicate_snapshot)
        findings = doctor.find_duplicate_registrations(servers)
        assert findings == ()

    def test_single_server_never_flagged_against_itself(self):
        servers = doctor.load_manifest_snapshot({"servers": [{"slot_name": "solo", "tools": MERIDIAN_TOOLS}]})
        assert doctor.find_duplicate_registrations(servers) == ()

    def test_server_with_no_tools_is_ignored_not_a_false_duplicate(self):
        snapshot = {
            "servers": [
                {"slot_name": "empty-a", "tools": []},
                {"slot_name": "empty-b", "tools": []},
            ]
        }
        servers = doctor.load_manifest_snapshot(snapshot)
        assert doctor.find_duplicate_registrations(servers) == ()

    def test_second_server_with_no_tools_is_skipped_not_a_false_duplicate(self):
        # First server is non-empty (enters the inner loop); the SECOND one
        # it is paired against has no tools at all.
        snapshot = {
            "servers": [
                {"slot_name": "meridian", "tools": [_tool(t) for t in MERIDIAN_TOOLS]},
                {"slot_name": "empty-server", "tools": []},
            ]
        }
        servers = doctor.load_manifest_snapshot(snapshot)
        assert doctor.find_duplicate_registrations(servers) == ()

    def test_uuid_slot_listed_first_is_still_named_as_the_duplicate(self):
        # Same exact-duplicate pair as the main fixture, but with the
        # UUID-shaped slot appearing FIRST in the snapshot -- the "which side
        # is the human name" decision must not depend on list order.
        snapshot = {
            "servers": [
                {"slot_name": "98ff5a3a-9b9d-4075-8d6e-306ff084c0eb", "tools": [_tool(t) for t in MERIDIAN_TOOLS]},
                {"slot_name": "meridian", "tools": [_tool(t) for t in MERIDIAN_TOOLS]},
            ]
        }
        servers = doctor.load_manifest_snapshot(snapshot)
        findings = doctor.find_duplicate_registrations(servers)
        assert len(findings) == 1
        finding = findings[0]
        assert finding.kind == "exact_duplicate_registration"
        assert "98ff5a3a-9b9d-4075-8d6e-306ff084c0eb" in finding.suggested_fix
        assert "'meridian'" in finding.suggested_fix


# ---------------------------------------------------------------------------
# check_mcp_tool_manifest_tax / run_doctor_checks -- the DoctorReport surface
# ---------------------------------------------------------------------------


class TestCheckMcpToolManifestTax:
    def test_reports_total_tool_count_and_per_server_tokens(self, no_duplicate_snapshot):
        report = doctor.check_mcp_tool_manifest_tax(no_duplicate_snapshot, clock=lambda: 1234.5)
        assert isinstance(report, doctor.DoctorReport)
        assert report.scope == "mcp_tool_manifest_tax"
        assert report.generated_at == 1234.5

        names = {c.name: c for c in report.checks}
        assert "tool_count" in names
        total = len(MERIDIAN_TOOLS) + len(GITHUB_TOOLS) + len(SERENA_TOOLS)
        assert str(total) in names["tool_count"].detail
        assert "estimated_prefix_token_cost" in names
        assert names["estimated_prefix_token_cost"].severity == "ok"

    def test_no_duplicates_gives_ok_severity_and_healthy_report(self, no_duplicate_snapshot):
        report = doctor.check_mcp_tool_manifest_tax(no_duplicate_snapshot)
        dup_check = next(c for c in report.checks if c.name == "duplicate_server_registrations")
        assert dup_check.severity == "ok"
        assert "no duplicate" in dup_check.detail.lower()
        assert report.healthy is True

    def test_duplicates_surface_as_named_warn_checks_never_fail(self, duplicate_snapshot):
        report = doctor.check_mcp_tool_manifest_tax(duplicate_snapshot)
        dup_checks = [c for c in report.checks if c.name.startswith("duplicate_registration:")]
        assert len(dup_checks) == 3
        for c in dup_checks:
            # Advisory only: a duplicate registration wastes tokens, it does
            # not mean anything is actually broken.
            assert c.severity == "warn"
        # A purely advisory finding must never flip the report unhealthy.
        assert report.healthy is True

    def test_empty_snapshot_warns_but_stays_healthy(self):
        report = doctor.check_mcp_tool_manifest_tax({})
        assert len(report.checks) == 1
        assert report.checks[0].severity == "warn"
        assert report.healthy is True

    def test_never_returns_fail_severity(self, duplicate_snapshot):
        """The item is explicit: advisory only. Nothing this check finds
        should ever be rendered as a hard failure."""
        report = doctor.check_mcp_tool_manifest_tax(duplicate_snapshot)
        assert all(c.severity in ("ok", "warn") for c in report.checks)


class TestRunDoctorChecks:
    def test_without_snapshot_skips_rather_than_guessing(self):
        report = doctor.run_doctor_checks()
        assert report.scope == "meridian-doctor"
        assert len(report.checks) == 1
        assert report.checks[0].severity == "warn"
        assert "skipped" in report.checks[0].detail.lower()
        assert report.healthy is True

    def test_with_snapshot_delegates_to_the_mcp_check(self, duplicate_snapshot):
        report = doctor.run_doctor_checks(mcp_manifest_snapshot=duplicate_snapshot)
        assert report.scope == "meridian-doctor"
        names = {c.name for c in report.checks}
        assert "tool_count" in names
        assert any(n.startswith("duplicate_registration:") for n in names)


# ---------------------------------------------------------------------------
# Advisory-only guarantee -- nothing here ever writes to a config file
# ---------------------------------------------------------------------------


class TestNeverWrites:
    """Patches every filesystem-write entry point Python offers and asserts
    calling every public function in this module (including the CLI's
    ``main()``, reading a real snapshot file from disk) never touches one.
    A write slipping in anywhere would raise inside the patched call and
    fail the test."""

    @pytest.fixture(autouse=True)
    def _forbid_writes(self, monkeypatch):
        import builtins
        from pathlib import Path

        real_open = builtins.open

        def guarded_open(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                raise AssertionError(f"unexpected write-mode open() call: file={file!r} mode={mode!r}")
            return real_open(file, mode, *args, **kwargs)

        def guarded_write_text(self, *a, **kw):
            raise AssertionError(f"unexpected Path.write_text() call on {self!r}")

        def guarded_write_bytes(self, *a, **kw):
            raise AssertionError(f"unexpected Path.write_bytes() call on {self!r}")

        monkeypatch.setattr(builtins, "open", guarded_open)
        monkeypatch.setattr(Path, "write_text", guarded_write_text, raising=True)
        monkeypatch.setattr(Path, "write_bytes", guarded_write_bytes, raising=True)

    def test_check_and_aggregate_never_write(self, duplicate_snapshot, no_duplicate_snapshot):
        doctor.check_mcp_tool_manifest_tax(duplicate_snapshot)
        doctor.check_mcp_tool_manifest_tax(no_duplicate_snapshot)
        doctor.check_mcp_tool_manifest_tax({})
        doctor.run_doctor_checks(mcp_manifest_snapshot=duplicate_snapshot)
        doctor.run_doctor_checks()

    def test_cli_reads_and_prints_but_never_writes(self, tmp_path, capsys, duplicate_snapshot):
        snapshot_path = tmp_path / "snapshot.json"
        # Written OUTSIDE the guarded block (this is test setup, not code
        # under test) via the raw os-level write, so the guard above is only
        # ever exercised by meridian.doctor's own code.
        import os

        fd = os.open(
            str(snapshot_path),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0),
        )
        os.write(fd, json.dumps(duplicate_snapshot).encode("utf-8"))
        os.close(fd)
        before = snapshot_path.read_bytes()

        exit_code = doctor.main([str(snapshot_path)])

        assert exit_code == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["scope"] == "meridian-doctor"
        assert "checks" in payload
        # The input file itself must be byte-for-byte untouched.
        assert snapshot_path.read_bytes() == before

    def test_cli_reads_snapshot_from_stdin_without_writing(self, monkeypatch, duplicate_snapshot):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(duplicate_snapshot)))
        exit_code = doctor.main(["-"])
        assert exit_code == 0

    def test_no_new_files_created_in_a_scratch_directory(self, tmp_path, duplicate_snapshot):
        import os

        marker = tmp_path / "meridian.toml"
        marker_contents = b'[project]\nproject_id = "do-not-touch"\n'
        # Written via the raw os-level call (test scaffolding, not code under
        # test) so the write-guard above -- active for this whole test body --
        # is only ever exercised by meridian.doctor's own code below.
        fd = os.open(
            str(marker),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0),
        )
        os.write(fd, marker_contents)
        os.close(fd)
        before_listing = sorted(p.name for p in tmp_path.iterdir())

        doctor.check_mcp_tool_manifest_tax(duplicate_snapshot)
        doctor.run_doctor_checks(mcp_manifest_snapshot=duplicate_snapshot)
        doctor.find_duplicate_registrations(doctor.load_manifest_snapshot(duplicate_snapshot))

        after_listing = sorted(p.name for p in tmp_path.iterdir())
        assert before_listing == after_listing
        assert marker.read_bytes() == marker_contents
