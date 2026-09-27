"""55d48d69 -- Meridian guard decision core (meridian/guard_core.py).

Two layers:

1. The parity fixture ``tests/fixtures/guard_cases.json`` drives every case
   through :func:`guard_core.evaluate`. The ps1/sh shims are tested against the
   SAME file, so a failure here means the spec itself changed.
2. Focused unit tests for behaviour a single-call fixture cannot show:
   multi-call sequences (deny -> receipt -> retry, degraded, breaker, rate
   limits), state immutability, message/brief bounds, the reference runner's
   state + audit files, and structural invariants (Read is never matched).

Pure Python, no subprocesses, no network, never touches the real cache.
"""

from __future__ import annotations

import copy
import io
import json
import re
from pathlib import Path

import pytest

from meridian import guard_core as gc
from meridian.cbm_registry import DictFS

FIXTURE = Path(__file__).parent / "fixtures" / "guard_cases.json"
DOC = json.loads(FIXTURE.read_text(encoding="utf-8"))
CASES = DOC["cases"]
NOW = DOC["now"]
REPO = "C:/Users/13144/Documents/Meridian/repository"
SLUG_M = "C-Users-13144-Documents-Meridian-repository"
MEM = "C:/Users/13144/.claude/projects/C--Users-13144-Documents-Meridian-repository/memory"


def _env(overrides: dict | None = None) -> dict:
    env = dict(DOC["base_env"])
    for k, v in (overrides or {}).items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return env


def _fs(overlay: dict | None = None) -> DictFS:
    return DictFS(DOC["filesystems"]["machine_2026_09_26"], overlay=overlay)


def _run_case(case: dict) -> dict:
    return gc.evaluate(
        case["event"],
        case["payload"],
        DOC["snapshots"].get(case["snapshot_key"]),
        case.get("state"),
        _env(case.get("env")),
        fs=DictFS(DOC["filesystems"][case["fs_key"]], overlay=case.get("fs_overlay")),
        now=case.get("now", NOW),
    )


def _case(name: str) -> dict:
    return next(c for c in CASES if c["name"] == name)


def _eval(payload: dict, *, event: str = "PreToolUse", state=None, env=None, snapshot="indexed", now=NOW, overlay=None) -> dict:
    return gc.evaluate(event, payload, DOC["snapshots"][snapshot], state, _env(env), fs=_fs(overlay), now=now)


def _pre(tool: str, ti: dict, cwd: str = REPO, sid: str = "seq") -> dict:
    return {"session_id": sid, "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": ti, "cwd": cwd}


def _post(tool: str, ti: dict, resp, event: str = "PostToolUse") -> dict:
    return {"session_id": "seq", "hook_event_name": event, "tool_name": tool, "tool_input": ti,
            "tool_response": resp, "cwd": REPO}


D1 = _case("D1_grep_repo_root")["payload"]


# ---------------------------------------------------------------------------
# 1. Parity fixture
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_fixture_case(case):
    res = _run_case(case)
    assert res["decision"] == case["expected_decision"], res["reason"]
    assert res["rule_id"] == case["expected_rule"], res["reason"]
    if "expected_project" in case:
        assert res.get("project") == case["expected_project"], res["reason"]
    for s in case.get("expected_reason_contains", []):
        assert s in res["reason"], f"{s!r} not in {res['reason']!r}"
    for s in case.get("expected_reason_excludes", []):
        assert s not in res["reason"], f"{s!r} unexpectedly in {res['reason']!r}"


def test_fixture_matches_its_generator():
    """The committed JSON is exactly what tests/fixtures/gen_guard_cases.py produces (no drift)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_gen_guard_cases", FIXTURE.parent / "gen_guard_cases.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    assert json.loads(json.dumps(mod.DOC)) == DOC, (
        "guard_cases.json is stale: run python tests/fixtures/gen_guard_cases.py tests/fixtures/guard_cases.json")
    assert mod.main([]) == 1, "the generator refuses to run without an explicit output path"


def test_fixture_is_ascii_and_self_describing():
    raw = FIXTURE.read_bytes()
    assert all(b < 0x80 for b in raw), "fixture must stay ASCII for the ps1 shim tests"
    assert DOC["version"] == 1
    for key in ("purpose", "env", "snapshot_key", "fs_key", "expected_decision", "expected_rule"):
        assert key in DOC["_doc"]
    names = [c["name"] for c in CASES]
    assert len(names) == len(set(names)), "case names must be unique"
    for c in CASES:
        assert c["expected_decision"] in gc.DECISIONS
        assert c["expected_rule"] is None or c["expected_rule"] in gc.RULES
        assert c["snapshot_key"] in DOC["snapshots"]
        assert c["fs_key"] in DOC["filesystems"]
        for k in ("name", "event", "payload", "snapshot_key", "env", "expected_decision", "expected_rule"):
            assert k in c, (c["name"], k)


def test_fixture_covers_every_replay_call():
    by_name = {c["name"].split("_", 1)[0]: c for c in CASES if c.get("source") == "replay-2026-09-26"}
    for i in range(1, 9):
        assert by_name[f"D{i}"]["expected_decision"] == "deny", f"D{i}"
    for i in range(1, 13):
        assert by_name[f"A{i}"]["expected_decision"] == "allow", f"A{i}"


def test_fixture_covers_every_rule():
    fired = {c["expected_rule"] for c in CASES if c["expected_rule"]}
    assert fired >= set(gc.RULES), sorted(set(gc.RULES) - fired)
    # every blocking rule has both a firing and a non-firing neighbour case
    groups = {c["group"] for c in CASES}
    for g in ("G0", "G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9", "G10", "G11", "G12", "G13", "G14", "G15",
              "G16", "escape", "breaker", "malformed", "replay"):
        assert g in groups, g
    # the kill-switch, breaker, receipt-escape and malformed families are all present
    assert any(c["expected_rule"] == "G0" for c in CASES)
    assert any("breaker" in c["name"] for c in CASES)
    assert any(c["name"].startswith("escape_") and c["expected_decision"] == "allow" for c in CASES)
    assert sum(1 for c in CASES if c["group"] == "malformed") >= 10


def test_every_g3_shape_is_exercised_through_every_shell_tool():
    tools = {c["payload"]["tool_name"] for c in CASES if c["name"].startswith("G3_deny_") and isinstance(c["payload"], dict)}
    assert {"Bash", "PowerShell", "Monitor", "mcp__dc__start_process", "mcp__dc__interact_with_process"} <= tools


# ---------------------------------------------------------------------------
# 2. Sequences (state carried between calls, exactly as a shim would)
# ---------------------------------------------------------------------------


def test_deny_then_identical_retry_then_receipt_then_allowed():
    r1 = _eval(D1)
    assert r1["decision"] == "deny" and r1["rule_id"] == "G1"
    st = r1["state"]
    assert st["denies"] == 1
    r2 = _eval(D1, state=st, now=NOW + 5)
    assert r2["decision"] == "deny", "an identical retry without consulting the index stays denied"
    st = r2["state"]
    rec = _eval(_post("mcp__codebase-memory__search_code", {"project": SLUG_M, "pattern": "arxiv"}, "{\"results\": []}"),
                event="PostToolUse", state=st, now=NOW + 20)
    assert rec["decision"] == "allow" and rec["rule_id"] == "G13"
    r3 = _eval(D1, state=rec["state"], now=NOW + 30)
    assert r3["decision"] == "allow" and r3["rule_id"] == "G1" and r3["reason"].startswith("escape")
    # the consult window is 10 minutes
    r4 = _eval(D1, state=rec["state"], now=NOW + 20 + gc.CONSULT_WINDOW_S + 1)
    assert r4["decision"] in ("deny", "inject")


def test_two_code_intel_errors_set_degraded_then_grep_allowed():
    st = None
    err = _post("mcp__codebase-memory-mcp__search_graph", {"project": SLUG_M}, "Error: 503 Service Unavailable")
    r1 = _eval(err, event="PostToolUse", state=st, now=NOW)
    assert r1["decision"] == "allow" and r1["state"]["degraded_until"] == 0.0
    r2 = _eval(err, event="PostToolUse", state=r1["state"], now=NOW + 60)
    assert r2["decision"] == "inject" and r2["rule_id"] == "G13"
    assert r2["state"]["degraded_until"] == pytest.approx(NOW + 60 + gc.DEGRADED_FOR_S)
    grep = _eval(D1, state=r2["state"], now=NOW + 120)
    assert grep["decision"] == "allow" and "degraded" in grep["reason"]
    later = _eval(D1, state=r2["state"], now=NOW + 60 + gc.DEGRADED_FOR_S + gc.CONSULT_WINDOW_S + 5)
    assert later["decision"] == "deny"


def test_breaker_after_three_denies_turns_code_rules_into_inject():
    st = None
    for i in range(3):
        r = _eval(D1, state=st, now=NOW + i)
        assert r["decision"] == "deny"
        st = r["state"]
    assert st["denies"] == 3
    r4 = _eval(D1, state=st, now=NOW + 10)
    assert r4["decision"] == "inject" and r4["rule_id"] == "G1" and "breaker" in r4["reason"]
    assert "state" not in r4, "an inject does not count as a deny"
    # hard rules keep denying
    d4 = _eval(_case("D4_write_automem")["payload"], state=st, now=NOW + 11)
    assert d4["decision"] == "deny" and d4["rule_id"] == "G6"


def test_hard_rule_denies_do_not_feed_the_breaker():
    r = _eval(_case("D4_write_automem")["payload"])
    assert r["decision"] == "deny" and "state" not in r


def test_advisory_mode_never_counts_denies():
    r = _eval(D1, env={"MERIDIAN_GUARD": "advisory"})
    assert r["decision"] == "inject"
    assert "state" not in r


def test_g2_advisory_once_per_root_per_10_minutes():
    r1 = _eval(D1, snapshot="pre_prerequisite")
    assert r1["decision"] == "inject" and r1["rule_id"] == "G2"
    r2 = _eval(D1, snapshot="pre_prerequisite", state=r1["state"], now=NOW + 60)
    assert r2["decision"] == "allow" and r2["rule_id"] == "G2"
    r3 = _eval(D1, snapshot="pre_prerequisite", state=r1["state"], now=NOW + gc.ADVISORY_EVERY_S + 1)
    assert r3["decision"] == "inject"


def test_g2_disabled_does_not_touch_rate_limit_state():
    r = _eval(D1, snapshot="pre_prerequisite", env={"MERIDIAN_GUARD_DISABLE": "G2"})
    assert r["decision"] == "allow" and "state" not in r


def test_g12_reminder_at_most_once_per_15_minutes_and_capture_silences_it():
    web = _post("WebSearch", {"query": "x"}, "results")
    r1 = _eval(web, event="PostToolUse")
    assert r1["decision"] == "inject" and r1["rule_id"] == "G12"
    r2 = _eval(web, event="PostToolUse", state=r1["state"], now=NOW + 60)
    assert r2["decision"] == "allow"
    cap = _eval(_post("mcp__meridian__capture_research_finding", {"project_id": "p"}, "{\"ok\": true}"),
                event="PostToolUse", now=NOW)
    r3 = _eval(web, event="PostToolUse", state=cap["state"], now=NOW + 60)
    assert r3["decision"] == "allow"


def test_g11_research_receipt_lifecycle():
    ps = _eval(_post("mcp__meridian__paper_search", {"query": "sae"}, "{\"results\": [1]}"), event="PostToolUse")
    q = _pre("WebSearch", {"query": "papers on sparse autoencoders"})
    r = _eval(q, state=ps["state"], now=NOW + 60)
    assert r["decision"] == "deny" and r["rule_id"] == "G11"
    failed = _eval(_post("mcp__meridian__paper_search", {"query": "sae"}, "Error: tunnel 503"), event="PostToolUse",
                   state=r["state"], now=NOW + 90)
    r2 = _eval(q, state=failed["state"], now=NOW + 120)
    assert r2["decision"] == "allow" and r2["rule_id"] == "G11"
    stale = _eval(q, state=ps["state"], now=NOW + gc.RESEARCH_WINDOW_S + 5)
    assert stale["decision"] == "allow" and stale["rule_id"] is None


def test_failed_capture_is_not_a_capture_receipt():
    r = _eval(_post("mcp__meridian__add_note", {"project_id": "p"}, "Error: 503"), event="PostToolUse")
    assert not r.get("state", {}).get("capture_receipts")
    assert r["rule_id"] is None


def test_receipts_are_pruned_and_bounded():
    st = {"code_receipts": [[NOW - 9000, True, None]] + [[NOW - i, True, None] for i in range(40)]}
    r = _eval(_post("mcp__codebase-memory-mcp__search_code", {"project": SLUG_M}, "ok"), event="PostToolUse", state=st)
    rec = r["state"]["code_receipts"]
    assert len(rec) == 20 and all(NOW - t <= 7200 for t, _o, _p in rec)


def test_g13_and_g14_on_one_call_join_texts():
    r = _eval(_post("mcp__meridian__start_session", {"project_id": "p"}, "no_confirmation " + "x" * 70000), event="PostToolUse")
    assert r["rule_id"] == "G14"
    assert "no_confirmation" in r["reason"] and "chars" in r["reason"]
    assert r["state"]["research_receipts"], "start_session is also a research health receipt"


def test_g14_scan_is_bounded_to_256k():
    big = "x" * (gc.QUARANTINE_SCAN_CHARS + 10) + " no_confirmation"
    r = _eval(_post("mcp__meridian__get_sprint_items", {}, big), event="PostToolUse")
    assert r["rule_id"] == "G14"
    assert "no_confirmation" not in r["reason"], "directives past the scan bound are not reported"
    assert "chars" in r["reason"], "but the oversize notice still fires"


def test_g13_degraded_plus_g14_both_reported():
    st = {"code_receipts": [[NOW - 30, False, None]]}
    r = _eval(_post("mcp__meridian__search_code", {"query": "x"}, "Error: boom"), event="PostToolUse", state=st)
    assert r["rule_id"] == "G13" and "degraded" in r["reason"]


# ---------------------------------------------------------------------------
# 3. Purity, fail-open, output contract
# ---------------------------------------------------------------------------


def test_evaluate_never_mutates_its_inputs():
    payload = copy.deepcopy(D1)
    state = {"denies": 1, "code_receipts": [[NOW - 5000, True, None]], "advisory_seen": {}}
    snap = DOC["snapshots"]["indexed"]
    before = (json.dumps(payload, sort_keys=True), json.dumps(state, sort_keys=True), json.dumps(snap, sort_keys=True))
    r = gc.evaluate("PreToolUse", payload, snap, state, _env(), fs=_fs(), now=NOW)
    assert r["state"]["denies"] == 2
    after = (json.dumps(payload, sort_keys=True), json.dumps(state, sort_keys=True), json.dumps(snap, sort_keys=True))
    assert before == after


@pytest.mark.parametrize("payload", [None, 0, 1.5, True, [], "x", {"tool_name": 5}, {"tool_name": "Grep", "tool_input": []},
                                     {"tool_name": "Bash", "tool_input": {"command": "\x00\"'`$("}},
                                     {"tool_name": "Bash", "tool_input": {"command": "grep -r " + "(" * 5000}},
                                     {"tool_name": "Write", "tool_input": {"file_path": ["x"]}},
                                     {"tool_name": "Grep", "tool_input": {"path": {"a": 1}}, "cwd": 7}])
@pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse", "SessionStart", "SubagentStart", None, 42])
def test_evaluate_never_raises(payload, event):
    for snap in (DOC["snapshots"]["indexed"], None, "junk", {"schema": gc.reg.SNAPSHOT_SCHEMA, "rows": "x"}):
        r = gc.evaluate(event, payload, snap, "junk-state", {"MERIDIAN_GUARD": "enforce"}, fs=_fs(), now=NOW)
        assert r["decision"] in gc.DECISIONS


def test_internal_error_fails_open(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(gc, "_evaluate", boom)
    r = gc.evaluate("PreToolUse", D1, DOC["snapshots"]["indexed"], None, _env(), fs=_fs(), now=NOW)
    assert r == {"decision": "allow", "rule_id": None, "reason": "fail-open: RuntimeError"}


def test_check_error_inside_rule_fails_open(monkeypatch):
    monkeypatch.setattr(gc, "_g1", lambda ctx: 1 / 0)
    r = gc.evaluate("PreToolUse", D1, DOC["snapshots"]["indexed"], None, _env(), fs=_fs(), now=NOW)
    assert r["decision"] == "allow"


def test_render_output_contract():
    deny = gc.render_output("PreToolUse", {"decision": "deny", "rule_id": "G1", "reason": "r"})
    assert deny == {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": "r"}}
    ask = gc.render_output("PreToolUse", {"decision": "ask", "rule_id": "G10", "reason": "q"})
    assert ask["hookSpecificOutput"]["permissionDecision"] == "ask"
    inj = gc.render_output("SessionStart", {"decision": "inject", "rule_id": "G15", "reason": "b"})
    assert inj == {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "b"}}
    assert gc.render_output("PreToolUse", {"decision": "allow", "rule_id": "G1", "reason": "escape"}) is None
    assert gc.render_output("PreToolUse", {"decision": "inject", "rule_id": "G2", "reason": ""}) is None


def test_every_fixture_output_is_ascii_json():
    for c in CASES:
        res = _run_case(c)
        ev = c["event"] if isinstance(c["event"], str) else "PreToolUse"
        out = gc.render_output(ev, res)
        if out is not None:
            json.dumps(out, ensure_ascii=True)
            if res["decision"] in ("deny", "ask"):
                assert out["hookSpecificOutput"]["permissionDecision"] == res["decision"]


def test_static_messages_are_ascii():
    texts = [gc._G6_MSG, gc._G7_MSG, gc._G8_MSG, gc._G9_MSG, gc._G11_MSG, gc._KILL_SWITCH_NOTE]
    for c in CASES:
        texts.append(_run_case(c)["reason"])
    for t in texts:
        assert t.isascii(), t


# ---------------------------------------------------------------------------
# 4. Structural invariants
# ---------------------------------------------------------------------------


def test_read_is_never_matched_by_any_rule():
    for name, rx in gc._TOOL_RE.items():
        assert not rx.fullmatch("Read"), name
    for rx in (gc._CODE_INTEL_RE, gc._RESEARCH_RE, gc._CAPTURE_RE, gc._QUARANTINE_RE):
        assert not rx.fullmatch("Read")
    for path in (MEM + "/MEMORY.md", "C:/Users/13144/AppData/Local/meridian/guard/guard.off", REPO + "/.claude/settings.json"):
        r = _eval(_pre("Read", {"file_path": path}), state={"denies": 99})
        assert r == {"decision": "allow", "rule_id": None, "reason": ""}


def test_rule_ids_are_exactly_g0_to_g16():
    assert list(gc.RULES) == [f"G{i}" for i in range(17)]
    assert gc.ESCAPABLE == {"G1", "G3", "G4", "G5", "G11"}


def test_owner_default_is_enforce_everywhere():
    assert gc.guard_mode({}, _fs()) == "enforce"
    for name in ("D1_grep_repo_root", "D6_bash_grep_rn", "G11_papers_on_with_receipt"):
        c = _case(name)
        assert _run_case(dict(c, env={}))["decision"] == "deny", name


# ---------------------------------------------------------------------------
# 5. Kill switch helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [("", "enforce"), ("enforce", "enforce"), ("ENFORCE", "enforce"),
                                            ("off", "off"), (" Off ", "off"), ("advisory", "advisory"),
                                            ("0", "advisory"), ("disabled", "advisory")])
def test_guard_mode_env(value, expected):
    assert gc.guard_mode({"MERIDIAN_GUARD": value, "LOCALAPPDATA": "C:\\x"}, DictFS({})) == expected


def test_guard_mode_sentinels_and_missing_guard_dir():
    fs = DictFS({"files": {"C:/x/meridian/guard/guard.advisory": ""}})
    assert gc.guard_mode({"LOCALAPPDATA": "C:\\x"}, fs) == "advisory"
    fs2 = DictFS({"files": {"C:/x/meridian/guard/guard.off": ""}})
    assert gc.guard_mode({"LOCALAPPDATA": "C:\\x", "MERIDIAN_GUARD": "advisory"}, fs2) == "off"
    assert gc.guard_mode({}, fs2) == "enforce", "no LOCALAPPDATA/home: no sentinel lookup"


def test_disabled_rules_parsing():
    assert gc.disabled_rules({"MERIDIAN_GUARD_DISABLE": "G3,G11"}) == {"G3", "G11"}
    assert gc.disabled_rules({"MERIDIAN_GUARD_DISABLE": " g1 ; G06-x  G10-settings-weakening"}) == {"G1", "G6", "G10"}
    assert gc.disabled_rules({"MERIDIAN_GUARD_DISABLE": "G1x, ALL, *"}) == set()
    assert gc.disabled_rules({}) == set()


# ---------------------------------------------------------------------------
# 6. Shell classifier + helpers
# ---------------------------------------------------------------------------


def test_tokenize_splits_pipelines_and_stages():
    p = gc.tokenize("cd x && grep -rn 'a | b' . | head -5; echo \"q;z\" || true", "bash")
    assert [[s["w"] for s in pipe] for pipe in p] == [
        [["cd", "x"]], [["grep", "-rn", "a | b", "."], ["head", "-5"]], [["echo", "q;z"]], [["true"]]]


def test_tokenize_redirects_and_fd_dups():
    p = gc.tokenize("cmd 2>&1 > out.txt >> log 2>/dev/null", "bash")
    assert p[0][0]["w"] == ["cmd"]
    assert p[0][0]["r"] == [(">", "out.txt"), (">>", "log"), (">", "/dev/null")]
    ps = gc.tokenize("Get-Thing *> all.txt", "ps")
    assert ps[0][0]["r"] == [(">", "all.txt")]


def test_tokenize_dialect_escapes():
    assert gc.tokenize("cat C:\\Users\\x", "bash")[0][0]["w"] == ["cat", "C:\\Users\\x"], "backslash before a letter survives"
    assert gc.tokenize("echo a\\ b", "bash")[0][0]["w"] == ["echo", "a b"]
    assert gc.tokenize("Write-Host `\"x`\"", "ps")[0][0]["w"] == ["Write-Host", "\"x\""]
    assert gc.tokenize("echo it''s", "ps")[0][0]["w"] == ["echo", "its"]
    assert gc.tokenize("echo 'it''s'", "ps")[0][0]["w"] == ["echo", "it's"]
    assert gc.tokenize("findstr ^& x", "cmd")[0][0]["w"] == ["findstr", "&", "x"]
    assert gc.tokenize("echo 'a", "bash") is None
    assert gc.tokenize('echo "a', "ps") is None


def test_tokenize_heredoc_and_here_string():
    p = gc.tokenize("cat <<-EOF > f\n\tgrep -r x .\n\tEOF\nls", "bash")
    assert [pipe[0]["w"] for pipe in p] == [["cat"], ["ls"]]
    h = gc.tokenize("$s = @'\ngrep -r x .\n'@\nls", "ps")
    assert ["ls"] in [pipe[0]["w"] for pipe in h]
    assert not any(pipe[0]["w"][:1] == ["grep"] for pipe in h)
    assert gc.tokenize("$s = @'\nnever closed", "ps") is None


def test_tokenize_braces_parens_and_comments():
    p = gc.tokenize("{ grep -r x .; } && find . -exec grep y {} \\;", "bash")
    words = [s["w"] for pipe in p for s in pipe]
    assert ["grep", "-r", "x", "."] in words
    assert ["find", ".", "-exec", "grep", "y", "{}", ";"] in words
    assert gc.tokenize("gci | % { sls x $_ }", "ps")[1][0]["w"][0] == "sls"
    assert gc.tokenize("# only a comment", "bash") == []


def test_fast_path_skip():
    assert gc.fast_path_skip("pixi run pytest -q")
    assert gc.fast_path_skip("git log --grep=foo") is True, "--grep is not a search verb"
    assert not gc.fast_path_skip("git grep foo")
    assert not gc.fast_path_skip("cat ~/x/memory/a.md")
    assert not gc.fast_path_skip("setx MERIDIAN_GUARD off")
    assert not gc.fast_path_skip("C:/Git/usr/bin/grep.exe -r x .")
    assert not gc.fast_path_skip("Get-ChildItem -Recurse")


@pytest.mark.parametrize("pattern,expected", [
    ("*.md", {"md"}), ("**/*.{md,json}", {"md", "json"}), ("!**/.claude/**", None), ("src/**", None),
    ("py", {"py"}), ("\\.py$", {"py"}), ("", None), (None, None), ("*.{ }", None),
])
def test_filter_exts(pattern, expected):
    assert gc.filter_exts(pattern) == expected


def test_analyze_shell_tracks_cwd_and_unwraps_once():
    rp = lambda s, cwd: gc.reg.norm_path(s, cwd, msys=True)  # noqa: E731
    a = gc.analyze_shell("cd meridian && pushd db && rg foo", "bash", REPO, rp, "C:/Users/13144")
    assert a["searches"][0]["cwd"] == REPO + "/meridian/db"
    b = gc.analyze_shell("cd && rg foo", "bash", REPO, rp, "C:/Users/13144")
    assert b["searches"][0]["cwd"] == "C:/Users/13144"
    c = gc.analyze_shell("Set-Location -Path meridian; sls -Path *.py -Pattern x", "ps", REPO, rp, None)
    assert c["searches"][0]["cwd"] == REPO + "/meridian"
    d = gc.analyze_shell("sh -lc 'git grep x'", "bash", REPO, rp, None)
    assert d["searches"][0]["verb"] == "git grep"
    e = gc.analyze_shell("pwsh -c 'Select-String -Path *.py -Pattern x'", "bash", REPO, rp, None)
    assert e["searches"][0]["verb"] == "Select-String"
    f = gc.analyze_shell("bash -c \"echo 'unclosed\"", "bash", REPO, rp, None)
    assert f["parsed"] is False


def test_search_shapes_parse_options():
    assert gc._parse_grep(["-n", "x", "."]) is None
    g = gc._parse_grep(["-rnA", "3", "-e", "pat", "--include=*.py", "src"])
    assert g["pattern"] == "pat" and g["paths"] == ["src"] and g["filters"] == [("glob", "*.py")]
    assert gc._parse_grep(["--directories", "recurse", "x"])["paths"] == []
    assert gc._parse_grep(["-d", "recurse", "x", "a"])["paths"] == ["a"]
    assert gc._parse_grep(["-r", "-f", "pats.txt", "a"])["paths"] == ["a"]
    r = gc._parse_rg(["-g", "!*.md", "-t", "py", "--", "-weird", "dir"])
    assert r["pattern"] == "-weird" and r["paths"] == ["dir"] and r["filters"] == [("type", "py")]
    assert gc._parse_rg(["--files"]) is None
    assert gc._parse_ag("ag", ["-g", "x"]) is None
    assert gc._parse_ag("ack", ["--type", "md", "x", "d"])["filters"] == [("type", "md")]
    assert gc._parse_git(["log", "--grep", "x"]) is None
    assert gc._parse_git(["grep", "--no-index", "x"]) is None
    gg = gc._parse_git(["-c", "a=b", "--git-dir=x", "--no-pager", "grep", "-e", "p", "HEAD", "--", ":!*.md", ":(glob)**/*.py"])
    assert gg["pattern"] == "p" and gg["revs"] == ["HEAD"] and gg["paths"] == ["**/*.py"]
    fs = gc._parse_findstr(["/s", "/c:two words", "/d:a;b", "*.py"])
    assert fs["pattern"] is None and fs["paths"] == ["*.py", "a", "b"]
    assert gc._parse_findstr(["/i", "x", "*.py"]) is None
    assert gc._parse_sls(["x"]) is None
    assert gc._parse_sls(["-Pattern", "x", "-Path", "a.py,b.py"]) is None
    assert gc._parse_sls(["-Pattern:x", "-LiteralPath:src", "-Recurse"])["paths"] == ["src"]


def test_lister_shapes():
    assert gc._lister_shape("find", ["x"], "ps") is None
    f = gc._lister_shape("find", ["-L", "src", "!", "-name", "*.md", "-o", "-iname", "*.py"], "bash")
    assert f["paths"] == ["src"] and f["filters"] == [("glob", "*.py")]
    assert gc._lister_shape("ls", ["-la"], "bash") is None
    assert gc._lister_shape("ls", ["-laR", "src"], "bash")["paths"] == ["src"]
    assert gc._lister_shape("dir", ["/s", "/b", "src"], "cmd")["paths"] == ["src"]
    assert gc._lister_shape("dir", ["/b"], "cmd") is None
    assert gc._lister_shape("gci", ["-Depth", "2", "src", "*.py"], "ps")["filters"] == [("glob", "*.py")]
    assert gc._lister_shape("get-childitem", ["-Path", "src", "*.py", "-Recurse"], "ps")["filters"] == [("glob", "*.py")]
    assert gc._lister_shape("gci", ["src"], "ps") is None


def test_xargs_and_searcher_pattern():
    assert gc._xargs_searcher(["-0", "-n", "5", "grep", "x"])
    assert not gc._xargs_searcher(["-I", "{}", "cat", "{}"])
    assert not gc._xargs_searcher(["-0"])
    assert gc._searcher_pattern(["xargs", "-0", "rg", "-e", "pat"]) == "pat"
    assert gc._searcher_pattern(["xargs", "cat"]) is None
    assert gc._searcher_pattern(["sls", "-Pattern", "p"]) == "p"
    assert gc._searcher_pattern(["grep", "-n"]) is None


def test_unwrap_variants():
    assert gc._unwrap("bash", ["-c", "ls"]) == ("ls", "bash")
    assert gc._unwrap("bash", ["script.sh"]) is None
    assert gc._unwrap("bash", ["-c"]) is None
    assert gc._unwrap("powershell", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "gci", "-r"]) == ("gci -r", "ps")
    assert gc._unwrap("pwsh", ["-File", "x.ps1"]) is None
    assert gc._unwrap("pwsh", ["-enc", "AAAA"]) is None
    assert gc._unwrap("powershell", ["Get-Date"]) == ("Get-Date", "ps")
    assert gc._unwrap("powershell", ["-Command"]) is None
    assert gc._unwrap("cmd", ["/d", "/s", "/c", "dir", "/s"]) == ("dir /s", "cmd")
    assert gc._unwrap("cmd", ["x"]) is None
    assert gc._unwrap("node", ["-e", "x"]) is None


def test_stage_cmd_skips_wrappers():
    assert gc._stage_cmd(["FOO=1", "BAR=2", "nice", "-n", "timeout", "5", "grep", "-r"]) == ("grep", ["-r"])
    assert gc._stage_cmd(["&", "C:\\Git\\grep.exe", "x"]) == ("grep", ["x"])
    assert gc._stage_cmd(["FOO=1"]) == (None, [])


def test_ps_param_resolution():
    names = ("path", "pattern", "literalpath")
    assert gc._ps_param("Path", names) == "path"
    assert gc._ps_param("patt", names) == "pattern"
    assert gc._ps_param("pat", names) is None, "ambiguous prefix"
    assert gc._ps_param("lp", names, {"lp": "literalpath"}) == "literalpath"


# ---------------------------------------------------------------------------
# 7. Misc behaviour not shown by a single fixture row
# ---------------------------------------------------------------------------


def test_winner_db_missing_means_unknown_allow():
    snap = copy.deepcopy(DOC["snapshots"]["indexed"])
    for r in snap["rows"]:
        if r["name"] == SLUG_M:
            r["db"] = "C:/nowhere/gone.db"
            r["wal"] = "C:/nowhere/gone.db-wal"
    r = gc.evaluate("PreToolUse", D1, snap, None, _env(), fs=_fs(), now=NOW)
    assert r["decision"] == "allow" and r["rule_id"] is None


def test_coverage_unknown_is_only_advisory():
    snap = copy.deepcopy(DOC["snapshots"]["indexed"])
    for r in snap["rows"]:
        if r["name"] == SLUG_M:
            r["covered_dirs"] = None
    r = gc.evaluate("PreToolUse", D1, snap, None, _env(), fs=_fs(), now=NOW)
    assert r["decision"] == "inject" and r["rule_id"] == "G2"
    assert "does not report" in r["reason"]


def test_wal_mtime_keeps_an_old_index_fresh():
    snap = copy.deepcopy(DOC["snapshots"]["indexed"])
    for r in snap["rows"]:
        if r["name"] == SLUG_M:
            r["indexed_epoch"] = NOW - 30 * 86400
    stale = gc.evaluate("PreToolUse", D1, snap, None, _env(), fs=_fs(
        {"mtimes": {f"C:/Users/13144/.cache/codebase-memory-mcp/{SLUG_M}.db": NOW - 30 * 86400}}), now=NOW)
    assert stale["rule_id"] == "G2"
    fresh = gc.evaluate("PreToolUse", D1, snap, None, _env(), fs=_fs(
        {"mtimes": {f"C:/Users/13144/.cache/codebase-memory-mcp/{SLUG_M}.db": NOW - 30 * 86400,
                    f"C:/Users/13144/.cache/codebase-memory-mcp/{SLUG_M}.db-wal": NOW - 3600}}), now=NOW)
    assert fresh["decision"] == "deny", "the watcher's WAL activity is a lower bound on freshness"


def test_scratchpad_from_payload_is_excluded():
    p = _pre("Grep", {"pattern": "x", "path": REPO + "/meridian"})
    p["scratchpad_dir"] = REPO + "/meridian"
    assert _eval(p)["decision"] == "allow"


def test_temp_env_dir_is_excluded():
    assert _eval(_pre("Grep", {"pattern": "x", "path": REPO + "/meridian"}), env={"TMPDIR": REPO})["decision"] == "allow"


def test_brief_bounds_with_huge_inputs():
    snap = copy.deepcopy(DOC["snapshots"]["indexed"])
    base = next(r for r in snap["rows"] if r["name"] == SLUG_M)
    for i in range(400):
        extra = dict(base, name=f"dup-{i:03d}-" + "x" * 40, slug_match=False)
        snap["rows"].append(extra)
    sess = gc.evaluate("SessionStart", {"hook_event_name": "SessionStart", "source": "startup", "cwd": REPO}, snap, None,
                       _env(), fs=_fs(), now=NOW)
    assert sess["decision"] == "inject" and len(sess["reason"].encode()) <= gc.BRIEF_MAX_BYTES
    assert sess["reason"].startswith("[Meridian guard brief]")
    assert SLUG_M in sess["reason"], "the computed code-intel fact is never dropped"
    sub = gc.evaluate("SubagentStart", {"hook_event_name": "SubagentStart", "cwd": REPO}, snap, None, _env(), fs=_fs(), now=NOW)
    assert len(sub["reason"].encode()) <= gc.SUBAGENT_BRIEF_MAX_BYTES
    assert SLUG_M in sub["reason"]


def test_brief_stale_and_ancestor_lines():
    stale = gc.evaluate("SessionStart", {"hook_event_name": "SessionStart", "cwd": REPO}, DOC["snapshots"]["pre_prerequisite"],
                        None, _env(), fs=_fs(), now=NOW)
    assert "STALE" in stale["reason"] and "index_repository" in stale["reason"]
    anc = gc.evaluate("SessionStart", {"hook_event_name": "SessionStart", "cwd": "C:/Users/13144/Documents/round3_interview/sub"},
                      DOC["snapshots"]["indexed"], None, _env(), fs=_fs(), now=NOW)
    assert "ancestor index" in anc["reason"]
    sub = gc.evaluate("SubagentStart", {"hook_event_name": "SubagentStart", "cwd": REPO}, DOC["snapshots"]["pre_prerequisite"],
                      None, _env(), fs=_fs(), now=NOW)
    assert "stale" in sub["reason"]


def test_project_id_from_claude_local_md():
    overlay = {"files": {REPO + "/meridian.toml": "[default]\nx = 1\n", REPO + "/CLAUDE.local.md":
                         "Project ID: 99999999-8888-7777-6666-555555555555\n"}}
    r = gc.evaluate("SessionStart", {"hook_event_name": "SessionStart", "cwd": REPO}, DOC["snapshots"]["indexed"], None,
                    _env(), fs=_fs(overlay), now=NOW)
    assert "99999999-8888-7777-6666-555555555555" in r["reason"]


def test_meridian_toml_only_project_key_is_read():
    toml = '[default]\ntoken = "sk_meridian_SECRET"\n[project]\nproject_id = "' + "a" * 8 + '-bbbb-cccc-dddd-' + "e" * 12 + '"\n'
    r = gc.evaluate("SessionStart", {"hook_event_name": "SessionStart", "cwd": REPO}, DOC["snapshots"]["indexed"], None,
                    _env(), fs=_fs({"files": {REPO + "/meridian.toml": toml}}), now=NOW)
    assert "sk_meridian_SECRET" not in r["reason"]
    assert "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in r["reason"]


def test_shell_cwd_msys_and_windows_payload_cwd():
    r = _eval(_pre("Bash", {"command": "cd /c/Users/13144/Documents/Meridian/repository/meridian && rg x"}, cwd="/c/Users/13144"))
    assert r["decision"] == "deny" and r["rule_id"] == "G3"


def test_g3_multiple_targets_first_deny_wins():
    r = _eval(_pre("Bash", {"command": f"rg x docs {REPO}/meridian"}))
    assert r["decision"] == "deny"
    r2 = _eval(_pre("Bash", {"command": "rg x scripts meridian"}), state={"advisory_seen": {}})
    assert r2["decision"] == "deny", "a deny target outranks an advisory target"


# ---------------------------------------------------------------------------
# 8. Reference runner (state file + audit log), in a temp guard dir
# ---------------------------------------------------------------------------


def _runner_env(tmp_path) -> dict:
    env = _env()
    env["LOCALAPPDATA"] = str(tmp_path / "lad")
    return env


def test_run_hook_persists_state_and_audits(tmp_path):
    env = _runner_env(tmp_path)
    gdir = tmp_path / "lad" / "meridian" / "guard"
    gdir.mkdir(parents=True)
    (gdir / "snapshot.json").write_text(json.dumps(DOC["snapshots"]["indexed"]), encoding="utf-8")
    payload = dict(D1, session_id="sess/../evil id")
    out1 = gc.run_hook("PreToolUse", json.dumps(payload), env=env, fs=_fs(), now=NOW)
    assert json.loads(out1)["hookSpecificOutput"]["permissionDecision"] == "deny"
    state_file = gdir / "state" / "sessevilid.json"
    assert json.loads(state_file.read_text(encoding="utf-8"))["denies"] == 1
    gc.run_hook(None, json.dumps(payload), env=env, fs=_fs(), now=NOW + 1)
    assert json.loads(state_file.read_text(encoding="utf-8"))["denies"] == 2
    lines = [json.loads(x) for x in (gdir / "audit.log").read_text(encoding="utf-8").splitlines()]
    assert [ln["rule"] for ln in lines] == ["G1", "G1"]
    assert all("command" not in ln and "pattern" not in json.dumps(ln) for ln in lines), "no command text in audit"
    # receipts and kill-switch allows are not audited; escape-allows are
    receipt = _post("mcp__codebase-memory__search_code", {"project": SLUG_M}, "ok")
    receipt["session_id"] = payload["session_id"]
    assert gc.run_hook("PostToolUse", json.dumps(receipt), env=env, fs=_fs(), now=NOW + 2) == ""
    assert gc.run_hook("PreToolUse", json.dumps(payload), env=env, fs=_fs(), now=NOW + 3) == ""
    off = dict(env, MERIDIAN_GUARD="off")
    assert gc.run_hook("PreToolUse", json.dumps(payload), env=off, fs=_fs(), now=NOW + 4) == ""
    lines = [json.loads(x) for x in (gdir / "audit.log").read_text(encoding="utf-8").splitlines()]
    assert [(ln["rule"], ln["decision"]) for ln in lines] == [("G1", "deny"), ("G1", "deny"), ("G1", "allow")]


def test_run_hook_fail_open_inputs(tmp_path):
    env = _runner_env(tmp_path)
    for raw in ("", "   ", "not json", "[1,2]", "null", json.dumps({"hook_event_name": "PreToolUse"})):
        assert gc.run_hook(None, raw, env=env, fs=_fs(), now=NOW) == ""
    # no snapshot on disk: code rules allow, memory rule still denies
    assert gc.run_hook("PreToolUse", json.dumps(D1), env=env, fs=_fs(), now=NOW) == ""
    out = gc.run_hook("PreToolUse", json.dumps(_case("D4_write_automem")["payload"]), env=env, fs=_fs(), now=NOW)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_run_hook_session_start_prunes_old_state(tmp_path):
    import os

    env = _runner_env(tmp_path)
    sdir = tmp_path / "lad" / "meridian" / "guard" / "state"
    sdir.mkdir(parents=True)
    old, fresh = sdir / "old.json", sdir / "fresh.json"
    old.write_text("{}", encoding="utf-8")
    fresh.write_text("{}", encoding="utf-8")
    os.utime(old, (NOW - 2 * 86400, NOW - 2 * 86400))
    os.utime(fresh, (NOW - 60, NOW - 60))
    out = gc.run_hook("SessionStart", json.dumps({"hook_event_name": "SessionStart", "cwd": REPO}), env=env, fs=_fs(), now=NOW)
    assert "additionalContext" in json.loads(out)["hookSpecificOutput"]
    assert not old.exists() and fresh.exists()


def test_run_hook_unwritable_guard_dir_still_answers(tmp_path):
    env = _runner_env(tmp_path)
    (tmp_path / "lad").write_text("a file where the dir should be", encoding="utf-8")
    out = gc.run_hook("PreToolUse", json.dumps(_case("D4_write_automem")["payload"]), env=env, fs=_fs(), now=NOW)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_main_reads_stdin_and_always_returns_zero(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    monkeypatch.setenv("USERPROFILE", "C:\\Users\\13144")
    monkeypatch.delenv("MERIDIAN_GUARD", raising=False)
    monkeypatch.delenv("MERIDIAN_GUARD_DISABLE", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_case("G8_serena_write_memory")["payload"])))
    assert gc.main(["PreToolUse"]) == 0
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    monkeypatch.setattr("sys.stdin", io.StringIO("garbage"))
    assert gc.main([]) == 0
    assert capsys.readouterr().out == ""


def test_main_survives_unreadable_stdin(monkeypatch, capsys):
    class Broken:
        def read(self):
            raise OSError("closed")

    monkeypatch.setattr("sys.stdin", Broken())
    assert gc.main([]) == 0
    assert capsys.readouterr().out == ""


def test_module_has_no_exit_2():
    src = Path(gc.__file__).read_text(encoding="utf-8")
    assert not re.search(r"(?:sys\.)?exit\(\s*2\s*\)|SystemExit\(\s*2\s*\)", src)
