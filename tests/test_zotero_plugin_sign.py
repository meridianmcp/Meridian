"""Tests for the Mozilla unlisted signing of the Zotero bridge plugin (meridian/zotero_plugin_sign.py).

Nothing here reaches Mozilla, npm, the network, the real process list, the keyring or Meridian's real
state folder. subprocess.run and Popen are blocked for the whole file. Every signing runner is a fake,
except the tests that drive the real runner through a patched subprocess.run, which never starts a process.
The credentials used here are fake values; they are checked to never appear in any output.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import signal
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from meridian import zotero_plugin_install as zpi
from meridian import zotero_plugin_sign as sign

REPO_ROOT = Path(zpi.__file__).resolve().parent.parent
REPO_EXTENSION = REPO_ROOT / "extensions" / "zotero-bridge"
REPO_DOCS = REPO_ROOT / "docs" / "zotero-plugin-install.md"
XPI_NAME = "meridian-zotero@usemeridian.us.xpi"
SIGNED_NAME = f"{sign.PLUGIN_ADDON_ID}-0.1.0.xpi"
KEY_VALUE = "KEY-VALUE-DO-NOT-PRINT-123"
SECRET_VALUE = "SECRET-VALUE-DO-NOT-PRINT-456"
CREDS = {sign.API_KEY_ENV: KEY_VALUE, sign.API_SECRET_ENV: SECRET_VALUE}
FIXED_NOW = 1_800_000_000.0
EXPECTED_ENTRIES = [
    "bootstrap.js",
    "content/preferences.xhtml",
    "lib/bibkey.js",
    "lib/pdf.js",
    "manifest.json",
    "prefs.js",
]
# The exact user-facing texts. A test that changes any of them is a change to the contract.
TIMEOUT_TEXT = (
    "Mozilla did not return a signed file in time. Check this version's page in the developer hub. "
    "Nothing was stored."
)
SOURCE_CHANGED_TEXT = "The plugin source changed while Mozilla was signing it. Run sign-plugin again."
REUSED_TEXT = (
    "This version was already submitted with different source. Bump the version in manifest.json, then sign again."
)
INTEGRITY_TEXT = "The stored signed file failed its integrity check. Sign the plugin again."
RANGE_TEXT = (
    "--timeout-minutes must be a number of minutes greater than 0 and at most 1440. Nothing was signed or uploaded."
)
UNSIGNED_ZIP_TEXT = (
    "Mozilla signing finished, but the file it returned has no signature in META-INF/ (.rsa or .sf). "
    "Nothing was stored."
)
NEEDS_SIGNATURE_LEAD = "This plugin version is not signed yet."


def _zip_with(entries: "dict[str, bytes]") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _signed_zip(tag: bytes) -> bytes:
    """A stand-in for Mozilla's signed .xpi: a real zip with a META-INF signature entry."""
    return _zip_with({"manifest.json": '{"manifest_version": 2}', "META-INF/mozilla.rsa": tag})


SIGNED_BYTES = _signed_zip(b"signed-by-the-stand-in")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """No real process, and every default Meridian path points into tmp_path."""

    def _blocked(*args, **kwargs):
        raise RuntimeError("real process calls are blocked in tests")

    monkeypatch.setattr(sign.subprocess, "run", _blocked)
    monkeypatch.setattr(sign.subprocess, "Popen", _blocked)
    monkeypatch.setenv("MERIDIAN_LOCAL_RUNNER_STATE_DIR", str(tmp_path / "meridian-state"))
    monkeypatch.delenv(sign.API_KEY_ENV, raising=False)
    monkeypatch.delenv(sign.API_SECRET_ENV, raising=False)


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    return path


def _fake_source(root: Path) -> Path:
    """A miniature extensions/zotero-bridge tree with a real version and gecko-free shipped manifest."""
    src = root / "zotero-bridge"
    manifest = {
        "manifest_version": 2,
        "name": "Meridian Zotero Bridge",
        "version": "0.1.0",
        "applications": {"zotero": {"id": sign.PLUGIN_ADDON_ID, "strict_min_version": "8.0"}},
    }
    _write(src / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    _write(src / "bootstrap.js", "// boot\n")
    _write(src / "prefs.js", 'pref("x", true);\n')
    _write(src / "content" / "preferences.xhtml", "<xul/>\n")
    _write(src / "lib" / "bibkey.js", "export {};\n")
    _write(src / "lib" / "pdf.js", "export {};\n")
    _write(src / "test" / "bibkey.test.js", "t")
    _write(src / "node_modules" / "left-pad" / "index.js", "x")
    _write(src / "package.json", "{}")
    _write(src / "README.md", "# readme")
    return src


@pytest.fixture
def source(tmp_path):
    return _fake_source(tmp_path / "src")


@pytest.fixture
def state(tmp_path):
    return tmp_path / "state"


class FakeRunner:
    """Records each call and answers with a fixed result; on success it writes a signed file, as web-ext does."""

    def __init__(self, result=None, produce=None):
        self.calls = []
        self.result = result if result is not None else sign.SignRunResult(returncode=0)
        self.produce = produce if produce is not None else self._produce_signed

    @staticmethod
    def _produce_signed(artifacts: Path) -> None:
        (artifacts / SIGNED_NAME).write_bytes(SIGNED_BYTES)

    def __call__(self, argv, env, *, timeout_s, cwd):
        self.calls.append({"argv": list(argv), "env": dict(env), "timeout_s": timeout_s, "cwd": Path(cwd)})
        self.produce(Path(argv[argv.index("--artifacts-dir") + 1]))
        return self.result


def _sign(source, state, *, env=CREDS, runner=None, timeout_s=sign.DEFAULT_TIMEOUT_S, clock=None):
    return sign.sign_plugin(
        env=env,
        runner=runner if runner is not None else FakeRunner(),
        state_dir=state,
        timeout_s=timeout_s,
        clock=clock if clock is not None else (lambda: FIXED_NOW),
        source_dir=source,
    )


def _snapshot(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


# ---------------------------------------------------------------------------
# Identity and staging
# ---------------------------------------------------------------------------


def test_addon_id_equals_the_bridge_manifest_zotero_id():
    manifest = json.loads((REPO_EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    assert sign.PLUGIN_ADDON_ID == manifest["applications"]["zotero"]["id"]
    assert sign.PLUGIN_ADDON_ID == "meridian-zotero@usemeridian.us"


def test_staged_manifest_gets_the_gecko_id_and_the_shipped_manifest_does_not(source, tmp_path):
    shipped_before = (source / "manifest.json").read_bytes()
    staged = sign.stage_for_signing(source, tmp_path / "staged")
    staged_manifest = json.loads((staged / "manifest.json").read_text(encoding="utf-8"))
    assert staged_manifest["browser_specific_settings"]["gecko"]["id"] == sign.PLUGIN_ADDON_ID
    assert (source / "manifest.json").read_bytes() == shipped_before
    assert "browser_specific_settings" not in json.loads(shipped_before)
    real = json.loads((REPO_EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    assert "browser_specific_settings" not in real


def test_staging_packs_exactly_the_files_build_xpi_packs(source, tmp_path):
    staged = sign.stage_for_signing(source, tmp_path / "staged")
    staged_names = sorted(p.relative_to(staged).as_posix() for p in staged.rglob("*") if p.is_file())
    xpi = zpi.build_xpi(tmp_path / XPI_NAME, source_dir=source)
    with zipfile.ZipFile(xpi) as archive:
        assert staged_names == archive.namelist() == EXPECTED_ENTRIES
        for name in EXPECTED_ENTRIES:
            if name != "manifest.json":
                assert (staged / name).read_bytes() == archive.read(name)


def test_staging_normalises_crlf_the_same_way_build_xpi_does(source, tmp_path):
    (source / "lib" / "pdf.js").write_bytes(b"export {};\r\n// two\r\n")
    staged = sign.stage_for_signing(source, tmp_path / "staged")
    assert (staged / "lib" / "pdf.js").read_bytes() == b"export {};\n// two\n"


def test_source_fingerprint_is_the_same_for_crlf_and_lf_checkouts(tmp_path):
    lf = _fake_source(tmp_path / "lf")
    crlf = _fake_source(tmp_path / "crlf")
    for path in crlf.rglob("*"):
        if path.is_file():
            path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert sign.source_fingerprint(lf) == sign.source_fingerprint(crlf)


def test_source_fingerprint_changes_when_a_runtime_file_changes(source):
    before = sign.source_fingerprint(source)
    _write(source / "bootstrap.js", "// changed\n")
    assert sign.source_fingerprint(source) != before


@pytest.mark.parametrize("bad", ["", "   ", "../../evil", "1.0/evil", "x" * 80])
def test_bundle_version_refuses_a_missing_or_unsafe_version(tmp_path, bad):
    src = tmp_path / "src"
    _write(src / "manifest.json", json.dumps({"manifest_version": 2, "version": bad}))
    with pytest.raises(ValueError):
        sign.bundle_version(src)


def test_bundle_version_reads_the_manifest_version(source):
    assert sign.bundle_version(source) == "0.1.0"


# ---------------------------------------------------------------------------
# Credentials: missing or empty means no runner call and no value in any output
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env",
    [
        {},
        {sign.API_KEY_ENV: "", sign.API_SECRET_ENV: SECRET_VALUE},
        {sign.API_KEY_ENV: KEY_VALUE, sign.API_SECRET_ENV: ""},
        {sign.API_KEY_ENV: "   ", sign.API_SECRET_ENV: SECRET_VALUE},
        {sign.API_KEY_ENV: KEY_VALUE},
        {sign.API_SECRET_ENV: SECRET_VALUE},
    ],
)
def test_missing_or_empty_credentials_give_no_credentials_and_call_no_runner(source, state, env):
    runner = FakeRunner()
    out = _sign(source, state, env=env, runner=runner)
    assert out["status"] == "no_credentials"
    assert runner.calls == []
    assert sign.API_KEY_ENV in out["message"] and sign.API_SECRET_ENV in out["message"]
    assert "addons.mozilla.org" in out["message"]
    for value in env.values():
        if value.strip():
            assert value not in json.dumps(out)
    assert _snapshot(state) == {}


def test_the_credentials_are_checked_only_when_nothing_is_signed_yet(source, state):
    assert _sign(source, state)["status"] == "signed"
    runner = FakeRunner()
    out = _sign(source, state, env={}, runner=runner)
    assert out["status"] == "already_signed"
    assert runner.calls == []


# ---------------------------------------------------------------------------
# Already signed, and the runner call itself
# ---------------------------------------------------------------------------


def test_already_signed_makes_no_runner_call(source, state):
    assert _sign(source, state)["status"] == "signed"
    runner = FakeRunner()
    out = _sign(source, state, runner=runner)
    assert out["status"] == "already_signed"
    assert runner.calls == []


def test_a_source_change_without_a_version_bump_is_refused_before_any_upload(source, state):
    assert _sign(source, state)["status"] == "signed"
    _write(source / "bootstrap.js", "// changed without a version bump\n")
    before = _snapshot(state)
    runner = FakeRunner()
    out = _sign(source, state, runner=runner)
    assert out["status"] == "refused_version_reused"
    assert out["message"] == REUSED_TEXT
    assert runner.calls == []
    assert _snapshot(state) == before


def test_a_recorded_version_with_no_stored_file_still_refuses_a_different_source(source, state):
    assert _sign(source, state)["status"] == "signed"
    (state / "signed" / SIGNED_NAME).unlink()
    _write(source / "bootstrap.js", "// changed without a version bump\n")
    before = _snapshot(state)
    runner = FakeRunner()
    out = _sign(source, state, runner=runner)
    assert out["status"] == "refused_version_reused"
    assert out["message"] == REUSED_TEXT
    assert runner.calls == []
    assert _snapshot(state) == before


def test_a_fresh_version_with_no_record_is_signed_normally(source, state):
    runner = FakeRunner()
    out = _sign(source, state, runner=runner)
    assert out["status"] == "signed"
    assert len(runner.calls) == 1


# ---------------------------------------------------------------------------
# Source fixed during the Mozilla wait; the captured version and fingerprint are the ones recorded
# ---------------------------------------------------------------------------


def test_a_source_edit_during_the_mozilla_wait_stores_nothing(source, state):
    class _EditingRunner(FakeRunner):
        def __call__(self, argv, env, *, timeout_s, cwd):
            _write(source / "bootstrap.js", "// edited while Mozilla signs\n")
            return super().__call__(argv, env, timeout_s=timeout_s, cwd=cwd)

    runner = _EditingRunner()
    out = _sign(source, state, runner=runner)
    assert len(runner.calls) == 1
    assert out["status"] == "failed"
    assert out["message"] == SOURCE_CHANGED_TEXT
    assert _snapshot(state) == {}


def test_a_source_edited_and_restored_during_the_wait_is_recorded_under_the_captured_fingerprint(source, state):
    captured = sign.source_fingerprint(source)
    original = (source / "bootstrap.js").read_bytes()

    class _Flicker(FakeRunner):
        def __call__(self, argv, env, *, timeout_s, cwd):
            _write(source / "bootstrap.js", "// flicker\n")
            (source / "bootstrap.js").write_bytes(original)
            return super().__call__(argv, env, timeout_s=timeout_s, cwd=cwd)

    out = _sign(source, state, runner=_Flicker())
    assert out["status"] == "signed"
    (entry,) = json.loads((state / "signed.json").read_text(encoding="utf-8"))["records"]
    assert entry["source_sha256"] == captured


def test_the_runner_argv_signs_unlisted_with_a_pinned_web_ext_and_no_credential(source, state):
    runner = FakeRunner()
    _sign(source, state, runner=runner, timeout_s=90)
    (call,) = runner.calls
    argv = call["argv"]
    assert "sign" in argv
    assert sign.WEB_EXT_PACKAGE in argv
    assert argv[argv.index("--channel") + 1] == "unlisted"
    assert "--source-dir" in argv and "--artifacts-dir" in argv
    assert argv[argv.index("--timeout") + 1] == "90000"  # web-ext's --timeout is milliseconds
    assert call["timeout_s"] == 90
    joined = " ".join(argv)
    assert KEY_VALUE not in joined and SECRET_VALUE not in joined
    assert not any(part.startswith("--api-") for part in argv)


def test_the_sign_argv_helper_matches_the_verified_flags():
    argv = sign.sign_argv("C:/stage", "C:/out", 60)
    assert argv[:4] == ["npx", "--yes", sign.WEB_EXT_PACKAGE, "sign"]
    assert argv[argv.index("--source-dir") + 1] == "C:/stage"
    assert argv[argv.index("--artifacts-dir") + 1] == "C:/out"
    assert argv[argv.index("--timeout") + 1] == "60000"
    assert argv[argv.index("--channel") + 1] == "unlisted"
    assert "--no-config-discovery" in argv


def test_the_credentials_reach_web_ext_through_the_call_environment(source, state):
    runner = FakeRunner()
    env = {**CREDS, "PATH": "/usr/bin", "WEB_EXT_AMO_BASE_URL": "https://elsewhere.example/api/"}
    _sign(source, state, env=env, runner=runner)
    child = runner.calls[0]["env"]
    assert child[sign.API_KEY_ENV] == KEY_VALUE
    assert child[sign.API_SECRET_ENV] == SECRET_VALUE
    assert child["PATH"] == "/usr/bin"
    assert "WEB_EXT_AMO_BASE_URL" not in child  # no other WEB_EXT_ setting can redirect the credentials


# ---------------------------------------------------------------------------
# Outcomes: signed, timeout and failed
# ---------------------------------------------------------------------------


def test_a_signed_result_is_stored_with_a_record_and_never_a_secret(source, state):
    out = _sign(source, state, runner=FakeRunner())
    assert out["status"] == "signed"
    stored = state / "signed" / SIGNED_NAME
    assert stored.read_bytes() == SIGNED_BYTES
    record = json.loads((state / "signed.json").read_text(encoding="utf-8"))
    (entry,) = record["records"]
    assert entry["addon_id"] == sign.PLUGIN_ADDON_ID
    assert entry["version"] == "0.1.0"
    assert entry["sha256"] == hashlib.sha256(SIGNED_BYTES).hexdigest()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", entry["signed_at"])
    stored_text = (state / "signed.json").read_text(encoding="utf-8")
    assert KEY_VALUE not in stored_text and SECRET_VALUE not in stored_text
    assert KEY_VALUE not in json.dumps(out) and SECRET_VALUE not in json.dumps(out)


def test_a_timeout_result_stores_nothing(source, state):
    runner = FakeRunner(result=sign.SignRunResult(returncode=None, timed_out=True))
    out = _sign(source, state, runner=runner)
    assert out["status"] == "timeout"
    assert _snapshot(state) == {}


def test_a_failed_exit_reports_the_exit_code_only_and_stores_nothing(source, state):
    runner = FakeRunner(result=sign.SignRunResult(returncode=7))
    out = _sign(source, state, runner=runner)
    assert out["status"] == "failed"
    assert "7" in out["message"]
    assert _snapshot(state) == {}


@pytest.mark.parametrize(
    "produce",
    [
        lambda artifacts: None,
        lambda artifacts: [(artifacts / f"extra-{i}.xpi").write_bytes(SIGNED_BYTES) for i in range(2)],
        lambda artifacts: (artifacts / SIGNED_NAME).write_bytes(b"not a zip at all"),
    ],
    ids=["none", "two", "not_a_zip"],
)
def test_success_needs_exactly_one_zip_artifact(source, state, produce):
    out = _sign(source, state, runner=FakeRunner(produce=produce))
    assert out["status"] == "failed"
    assert _snapshot(state) == {}


def test_a_stored_file_that_is_tampered_with_is_not_returned(source, state):
    assert _sign(source, state)["status"] == "signed"
    (state / "signed" / SIGNED_NAME).write_bytes(b"tampered after signing")
    assert sign.signed_artifact_for_version(state, "0.1.0") is None


def test_a_record_for_another_addon_or_version_is_not_returned(source, state):
    assert _sign(source, state)["status"] == "signed"
    path = state / "signed.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["records"][0]["addon_id"] = "someone-else@example.org"
    path.write_text(json.dumps(record), encoding="utf-8")
    assert sign.signed_artifact_for_version(state, "0.1.0") is None
    record["records"][0]["addon_id"] = sign.PLUGIN_ADDON_ID
    path.write_text(json.dumps(record), encoding="utf-8")
    assert sign.signed_artifact_for_version(state, "0.1.0") == state / "signed" / SIGNED_NAME
    assert sign.signed_artifact_for_version(state, "0.2.0") is None


def test_a_corrupt_record_file_means_nothing_is_signed(source, state):
    assert _sign(source, state)["status"] == "signed"
    (state / "signed.json").write_text("{not json", encoding="utf-8")
    assert sign.signed_artifact_for_version(state, "0.1.0") is None


def test_signed_artifact_lookup_refuses_an_unsafe_version(source, state):
    assert _sign(source, state)["status"] == "signed"
    assert sign.signed_artifact_for_version(state, "../../evil") is None


def test_a_signed_artifact_for_another_source_fingerprint_is_not_returned(source, state):
    assert _sign(source, state)["status"] == "signed"
    assert sign.signed_artifact_for_version(state, "0.1.0", source_sha256="0" * 64) is None
    current = sign.source_fingerprint(source)
    assert sign.signed_artifact_for_version(state, "0.1.0", source_sha256=current) == state / "signed" / SIGNED_NAME


# ---------------------------------------------------------------------------
# The real runner: credentials stay in the environment, output never reaches a result
# ---------------------------------------------------------------------------


class _FakeChild:
    """A stand-in for the web-ext child process. ``on_wait`` decides what its wait does, and it may write to its log files."""

    pid = 4242

    def __init__(self, argv, kwargs, on_wait):
        self.args = list(argv)
        self.kwargs = kwargs
        self._on_wait = on_wait

    def wait(self, timeout=None):
        return self._on_wait(self, timeout)


def _never_returns(child, timeout):
    """A web-ext that never exits: every wait runs out of time."""
    raise subprocess.TimeoutExpired(child.args, timeout)


def _fake_popen(monkeypatch, on_wait, launched):
    def _popen(argv, **kwargs):
        child = _FakeChild(argv, kwargs, on_wait)
        launched.append(child)
        return child

    monkeypatch.setattr(sign.subprocess, "Popen", _popen)


def test_the_real_runner_never_passes_web_ext_output_into_the_result(source, state, monkeypatch):
    leaked = f"Authorization failed for {KEY_VALUE} and {SECRET_VALUE}"
    launched = []

    def _fails(child, timeout):
        child.kwargs["stderr"].write(leaked.encode("utf-8"))
        return 1

    monkeypatch.setattr(sign.shutil, "which", lambda name: "C:/node/npx.cmd")
    _fake_popen(monkeypatch, _fails, launched)
    out = sign.sign_plugin(env=CREDS, state_dir=state, source_dir=source, timeout_s=120)
    assert out["status"] == "failed"
    assert "Authorization" not in json.dumps(out)
    assert KEY_VALUE not in json.dumps(out) and SECRET_VALUE not in json.dumps(out)
    (child,) = launched
    assert child.kwargs["env"][sign.API_KEY_ENV] == KEY_VALUE
    assert KEY_VALUE not in " ".join(child.args)
    assert child.kwargs.get("shell") in (None, False)
    assert child.kwargs["stdin"] == subprocess.DEVNULL


def test_web_ext_timeout_text_in_the_log_file_is_classified_as_a_timeout(source, state, monkeypatch):
    launched = []

    def _finishes_with_timeout(child, timeout):
        child.kwargs["stderr"].write(b"Validation: timeout exceeded.\n")
        return 1

    monkeypatch.setattr(sign.shutil, "which", lambda name: "C:/node/npx.cmd")
    _fake_popen(monkeypatch, _finishes_with_timeout, launched)
    out = sign.sign_plugin(env=CREDS, state_dir=state, source_dir=source)
    assert out["status"] == "timeout"
    assert out["message"] == TIMEOUT_TEXT
    assert _snapshot(state) == {}


def test_the_child_writes_to_log_files_not_pipes_so_no_inherited_handle_can_block_the_wait(source, state, monkeypatch):
    launched = []
    monkeypatch.setattr(sign.shutil, "which", lambda name: "C:/node/npx.cmd")
    _fake_popen(monkeypatch, lambda child, timeout: 0, launched)
    sign.sign_plugin(env=CREDS, state_dir=state, source_dir=source)
    (child,) = launched
    for name in ("stdout", "stderr"):
        handle = child.kwargs[name]
        assert isinstance(handle, io.IOBase)
        assert handle is not subprocess.PIPE
        assert Path(handle.name).name == f"{name}.txt"


def test_a_hung_web_ext_is_killed_as_its_own_tree_and_nothing_is_stored(source, state, monkeypatch):
    launched, taskkill, killpg = [], [], []
    monkeypatch.setattr(sign.shutil, "which", lambda name: "C:/node/npx.cmd")
    _fake_popen(monkeypatch, _never_returns, launched)

    def _record_taskkill(argv, **kwargs):
        taskkill.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(sign.subprocess, "run", _record_taskkill)
    monkeypatch.setattr(sign.os, "killpg", lambda pid, sig: killpg.append((pid, sig)), raising=False)
    out = sign.sign_plugin(env=CREDS, state_dir=state, source_dir=source, timeout_s=60)
    assert out["status"] == "timeout"
    assert out["message"] == TIMEOUT_TEXT
    assert _snapshot(state) == {}
    (child,) = launched
    assert child.args[0] == "C:/node/npx.cmd"
    if sys.platform == "win32":
        assert taskkill == [["taskkill", "/PID", "4242", "/T", "/F"]]
        assert killpg == []
    else:
        assert killpg == [(4242, signal.SIGKILL)]
        assert taskkill == []


def test_the_forced_kill_never_names_zotero(source, state, monkeypatch):
    taskkill = []
    monkeypatch.setattr(sign.shutil, "which", lambda name: "C:/node/npx.cmd")
    _fake_popen(monkeypatch, _never_returns, [])
    monkeypatch.setattr(
        sign.subprocess, "run",
        lambda argv, **kw: taskkill.append(list(argv)) or subprocess.CompletedProcess(argv, 0, b"", b""),
    )
    monkeypatch.setattr(sign.os, "killpg", lambda pid, sig: None, raising=False)
    sign.sign_plugin(env=CREDS, state_dir=state, source_dir=source, timeout_s=60)
    for argv in taskkill:
        assert "zotero.exe" not in " ".join(argv).lower()
        assert "/IM" not in argv


def test_a_missing_npx_is_a_failure_with_no_exit_code_invented(source, state, monkeypatch):
    monkeypatch.setattr(sign.shutil, "which", lambda name: None)
    out = sign.sign_plugin(env=CREDS, state_dir=state, source_dir=source)
    assert out["status"] == "failed"
    assert _snapshot(state) == {}


# ---------------------------------------------------------------------------
# CLI: sign-plugin, --dry-run and --timeout-minutes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("creds_set", [True, False])
def test_dry_run_prints_yes_or_no_and_never_the_values_and_calls_no_runner(monkeypatch, capsys, creds_set):
    if creds_set:
        monkeypatch.setenv(sign.API_KEY_ENV, KEY_VALUE)
        monkeypatch.setenv(sign.API_SECRET_ENV, SECRET_VALUE)
    monkeypatch.setattr(sign, "sign_plugin", lambda **kwargs: pytest.fail("the dry run must not sign"))
    rc = sign.cli_main(["--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "dry run" in out.lower()
    assert "0.1.0" in out
    credential_lines = [line for line in out.splitlines() if line.lower().startswith("mozilla credentials")]
    assert len(credential_lines) == 1
    assert credential_lines[0].rstrip().endswith("yes" if creds_set else "no")
    assert KEY_VALUE not in out and SECRET_VALUE not in out


def test_dry_run_reports_an_existing_signed_artifact(monkeypatch, capsys):
    state = zpi.signing_state_dir()
    sign.record_signed_artifact(state, _write(state / "produced.xpi", SIGNED_BYTES), source_dir=zpi._bundled_source_dir())
    rc = sign.cli_main(["--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "signed artifact" in out.lower() and "yes" in out.lower()


@pytest.mark.parametrize(
    ("status", "code"),
    [("signed", 0), ("already_signed", 0), ("no_credentials", 1), ("timeout", 1), ("failed", 1)],
)
def test_the_cli_exit_code_follows_the_outcome(monkeypatch, capsys, status, code):
    seen = {}

    def _fake_sign(**kwargs):
        seen.update(kwargs)
        return {"status": status, "version": "0.1.0", "path": None, "message": "M-" + status}

    monkeypatch.setattr(sign, "sign_plugin", _fake_sign)
    rc = sign.cli_main(["--timeout-minutes", "5"])
    out = capsys.readouterr().out
    assert rc == code
    assert seen["timeout_s"] == 300
    if code == 1:
        assert "M-" + status in out
    else:
        assert "Zotero bridge plugin" in out and "M-" not in out


def test_the_cli_default_timeout_is_sixty_minutes(monkeypatch):
    seen = {}
    monkeypatch.setattr(sign, "sign_plugin", lambda **kwargs: seen.update(kwargs) or {"status": "failed", "message": "x"})
    sign.cli_main([])
    assert seen["timeout_s"] == 3600


@pytest.mark.parametrize("bad", ["inf", "-inf", "nan", "0", "-5", "1441", "1e400", "abc", ""])
def test_cli_refuses_an_unusable_timeout_before_any_staging_and_never_with_a_traceback(monkeypatch, capsys, bad):
    monkeypatch.setattr(sign, "sign_plugin", lambda **kwargs: pytest.fail("no signing with an unusable timeout"))
    monkeypatch.setattr(sign, "_stage_snapshot", lambda *a, **k: pytest.fail("no staging with an unusable timeout"))
    rc = sign.cli_main([f"--timeout-minutes={bad}"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out.strip() == RANGE_TEXT
    assert "Traceback" not in captured.out + captured.err


@pytest.mark.parametrize(("raw", "minutes"), [("1", 1.0), ("0.5", 0.5), ("1440", 1440.0), (60.0, 60.0)])
def test_timeout_minutes_accepts_a_finite_number_up_to_1440(raw, minutes):
    assert sign.parse_timeout_minutes(raw) == minutes


@pytest.mark.parametrize("raw", ["inf", "nan", "0", "-0.1", "1440.0001", "1e400", "x", None])
def test_timeout_minutes_rejects_everything_else_with_the_one_message(raw):
    with pytest.raises(ValueError) as caught:
        sign.parse_timeout_minutes(raw)
    assert str(caught.value) == RANGE_TEXT


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"), 0.0, -1.0, 1440 * 60 + 1.0])
def test_sign_plugin_refuses_an_unusable_timeout_before_staging_and_never_raises(source, state, monkeypatch, bad):
    monkeypatch.setattr(sign, "_stage_snapshot", lambda *a, **k: pytest.fail("no staging"))
    runner = FakeRunner()
    out = sign.sign_plugin(env=CREDS, runner=runner, state_dir=state, timeout_s=bad, source_dir=source)
    assert out["status"] == "failed"
    assert out["message"] == RANGE_TEXT
    assert runner.calls == []
    assert _snapshot(state) == {}


def test_a_failure_while_building_the_argv_is_a_failed_result_not_a_crash(source, state, monkeypatch):
    def _boom(*args, **kwargs):
        raise OverflowError("int too large to convert")

    monkeypatch.setattr(sign, "sign_argv", _boom)
    runner = FakeRunner()
    out = _sign(source, state, runner=runner)
    assert out["status"] == "failed"
    assert runner.calls == []
    assert _snapshot(state) == {}


def test_a_timeout_result_gives_the_developer_hub_message_and_no_promise_to_retry(source, state):
    out = _sign(source, state, runner=FakeRunner(result=sign.SignRunResult(returncode=None, timed_out=True)))
    assert out["status"] == "timeout"
    assert out["message"] == TIMEOUT_TEXT
    assert "later" not in out["message"]
    assert _snapshot(state) == {}


def test_dispatch_routes_zotero_sign_plugin_to_the_signer(capsys):
    from meridian import __main__ as entry

    rc = entry._dispatch_subcommand(["zotero", "sign-plugin", "--dry-run"])
    assert rc == 0
    assert "dry run" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# The installer: an unsigned version is never installed, a signed one is copied byte for byte
# ---------------------------------------------------------------------------


@pytest.fixture
def default_zotero(monkeypatch, tmp_path, source):
    """A Zotero data root with a default profile, and the fake bundle as the bridge source."""
    data = tmp_path / "ZoteroData"
    profile = data / "Profiles" / "abc.default"
    _write(profile / "prefs.js", 'user_pref("keep", true);\n')
    _write(data / "profiles.ini", "[Profile0]\nName=default\nIsRelative=1\nPath=Profiles/abc.default\nDefault=1\n")
    monkeypatch.setattr(zpi, "zotero_data_dir", lambda: data)
    monkeypatch.setattr(zpi, "_bundled_source_dir", lambda: source)
    return profile


def test_install_without_a_signed_artifact_writes_nothing_and_says_it_needs_a_signature(
    monkeypatch, default_zotero,
):
    monkeypatch.setattr(sign, "sign_plugin", lambda **kwargs: pytest.fail("install never signs"))
    before = _snapshot(default_zotero)
    result = zpi.install_default_profile(running=lambda: False)
    assert result["status"] == "needs_signature"
    assert "This plugin version is not signed yet." in result["message"]
    assert "python -m meridian zotero sign-plugin" in result["message"]
    assert str(zpi.default_xpi_path()) in result["message"]
    assert _snapshot(default_zotero) == before


def test_install_with_a_signed_artifact_copies_the_exact_signed_bytes(default_zotero, source):
    assert _sign(source, zpi.signing_state_dir())["status"] == "signed"
    result = zpi.install_default_profile(running=lambda: False)
    assert result["status"] == "installed"
    assert (default_zotero / "extensions" / XPI_NAME).read_bytes() == SIGNED_BYTES


def test_the_default_install_refuses_an_unsigned_plugin_before_any_close(monkeypatch, default_zotero):
    before = _snapshot(default_zotero)
    result = zpi.install_default_with_close(
        {"autoinstall": False, "decided": True, "close_zotero_for_install": True},
        close_zotero=True,
        running=lambda: True,
        close=lambda running=None: pytest.fail("Zotero must not be closed for an unsigned plugin"),
        restart=lambda: pytest.fail("nothing is restarted when Meridian closed nothing"),
    )
    assert result["status"] == "needs_signature"
    assert result["closed_zotero"] is False
    assert result["restart"] is None
    assert _snapshot(default_zotero) == before


def test_the_cli_install_without_a_signature_closes_nothing(monkeypatch, capsys, default_zotero):
    monkeypatch.setattr(zpi, "close_zotero_gracefully", lambda **kwargs: pytest.fail("no close for an unsigned plugin"))
    monkeypatch.setattr(zpi, "zotero_process_state", lambda runner=None: True)
    before = _snapshot(default_zotero)
    rc = zpi.cli_main(["--profile", str(default_zotero)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "python -m meridian zotero sign-plugin" in out
    assert "Meridian closed" not in out
    assert _snapshot(default_zotero) == before
    assert not (default_zotero / "extensions").exists()


def test_the_cli_install_with_a_signature_installs_the_signed_bytes(monkeypatch, default_zotero, source, capsys):
    monkeypatch.setattr(zpi, "zotero_process_state", lambda runner=None: False)
    assert _sign(source, zpi.signing_state_dir())["status"] == "signed"
    assert zpi.cli_main(["--profile", str(default_zotero), "--keep-zotero-open"]) == 0
    assert (default_zotero / "extensions" / XPI_NAME).read_bytes() == SIGNED_BYTES


def test_a_tampered_signed_file_is_never_installed_and_gets_the_integrity_message(default_zotero, source):
    assert _sign(source, zpi.signing_state_dir())["status"] == "signed"
    (zpi.signing_state_dir() / "signed" / SIGNED_NAME).write_bytes(b"tampered")
    before = _snapshot(default_zotero)
    result = zpi.install_default_profile(running=lambda: False)
    assert result["status"] == "integrity_failed"
    assert result["message"] == INTEGRITY_TEXT
    assert NEEDS_SIGNATURE_LEAD not in result["message"]
    assert _snapshot(default_zotero) == before
    assert not (default_zotero / "extensions").exists()


def test_a_missing_signed_file_gets_the_needs_signature_message_not_the_integrity_one(default_zotero, source):
    assert _sign(source, zpi.signing_state_dir())["status"] == "signed"
    (zpi.signing_state_dir() / "signed" / SIGNED_NAME).unlink()
    result = zpi.install_default_profile(running=lambda: False)
    assert result["status"] == "needs_signature"
    assert NEEDS_SIGNATURE_LEAD in result["message"]
    assert result["message"] != INTEGRITY_TEXT
    assert not (default_zotero / "extensions").exists()


def test_a_tampered_file_in_the_cli_says_so_and_closes_nothing(monkeypatch, capsys, default_zotero, source):
    assert _sign(source, zpi.signing_state_dir())["status"] == "signed"
    (zpi.signing_state_dir() / "signed" / SIGNED_NAME).write_bytes(b"tampered")
    monkeypatch.setattr(zpi, "zotero_process_state", lambda runner=None: False)
    monkeypatch.setattr(zpi, "close_zotero_gracefully", lambda **kwargs: pytest.fail("no close for a tampered file"))
    before = _snapshot(default_zotero)
    rc = zpi.cli_main(["--profile", str(default_zotero)])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out.strip() == INTEGRITY_TEXT
    assert _snapshot(default_zotero) == before


# ---------------------------------------------------------------------------
# Copy-time integrity: the copy reads the signed bytes once, checks them, and writes those same bytes
# ---------------------------------------------------------------------------


def test_a_signed_file_replaced_between_preparation_and_the_copy_is_refused_and_nothing_is_written(
    default_zotero, source,
):
    state = zpi.signing_state_dir()
    assert _sign(source, state)["status"] == "signed"
    stored = state / "signed" / SIGNED_NAME
    before = _snapshot(default_zotero)

    def _close_and_replace(running=None):
        stored.write_bytes(b"replaced between preparation and the copy")
        return "closed"

    states = iter([True, False])
    result = zpi.install_default_with_close(
        {"autoinstall": True, "decided": True, "close_zotero_for_install": True},
        close_zotero=True,
        running=lambda: next(states),
        close=_close_and_replace,
        restart=lambda: "started",
    )
    assert result["status"] == "integrity_failed"
    assert result["message"] == INTEGRITY_TEXT
    assert result["closed_zotero"] is True
    assert _snapshot(default_zotero) == before
    assert not (default_zotero / "extensions").exists()


def test_a_copy_whose_bytes_do_not_match_the_record_writes_nothing(default_zotero, tmp_path):
    xpi = tmp_path / "stand-in.xpi"
    xpi.write_bytes(SIGNED_BYTES)
    before = _snapshot(default_zotero)
    result = zpi.install_into_profile(default_zotero, xpi, running=lambda: False, expected_sha256="0" * 64)
    assert result["status"] == "integrity_failed"
    assert result["message"] == INTEGRITY_TEXT
    assert _snapshot(default_zotero) == before


def test_the_copy_reads_the_signed_file_exactly_once_and_writes_those_bytes(monkeypatch, default_zotero, source):
    state = zpi.signing_state_dir()
    assert _sign(source, state)["status"] == "signed"
    stored = state / "signed" / SIGNED_NAME
    reads = []
    real_read_bytes = Path.read_bytes

    def _counting_read_bytes(self):
        if self == stored:
            reads.append(self)
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _counting_read_bytes)
    result = zpi.install_into_profile(
        default_zotero, stored, running=lambda: False, expected_sha256=hashlib.sha256(SIGNED_BYTES).hexdigest(),
    )
    assert result["status"] == "installed"
    assert len(reads) == 1
    assert (default_zotero / "extensions" / XPI_NAME).read_bytes() == SIGNED_BYTES


# ---------------------------------------------------------------------------
# Zip shape before storing: a signature entry under META-INF/ is required
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entries",
    [
        {"manifest.json": b'{"manifest_version": 2}'},
        {"manifest.json": b"{}", "META-INF/manifest.mf": b"Manifest-Version: 1.0\n"},
        {"manifest.json": b"{}", "mozilla.rsa": b"a signature outside META-INF is not one"},
        {"manifest.json": b"{}", "META-INF/nested/mozilla.rsa": b"a signature in a sub-folder is not one"},
    ],
    ids=["unsigned", "meta_inf_without_a_signature", "signature_at_the_root", "signature_too_deep"],
)
def test_a_returned_zip_without_a_meta_inf_signature_is_refused_and_nothing_is_stored(source, state, entries):
    data = _zip_with(entries)
    runner = FakeRunner(produce=lambda artifacts: (artifacts / SIGNED_NAME).write_bytes(data))
    out = _sign(source, state, runner=runner)
    assert out["status"] == "failed"
    assert out["message"] == UNSIGNED_ZIP_TEXT
    assert _snapshot(state) == {}


def test_a_signed_looking_zip_with_meta_inf_signature_entries_is_stored(source, state):
    data = _zip_with({
        "manifest.json": b"{}",
        "META-INF/manifest.mf": b"m",
        "META-INF/mozilla.rsa": b"r",
        "META-INF/mozilla.sf": b"s",
    })
    runner = FakeRunner(produce=lambda artifacts: (artifacts / SIGNED_NAME).write_bytes(data))
    out = _sign(source, state, runner=runner)
    assert out["status"] == "signed"
    assert (state / "signed" / SIGNED_NAME).read_bytes() == data


# ---------------------------------------------------------------------------
# Wording: the docs promise what the code does, and the menu name is the same everywhere
# ---------------------------------------------------------------------------


def _docs() -> str:
    return REPO_DOCS.read_text(encoding="utf-8")


def test_docs_state_the_network_use_plainly_and_drop_the_talks_only_to_mozilla_claim():
    docs = _docs()
    assert "talks only to Mozilla" not in docs
    assert "pinned to 8.10.0" in docs
    assert "npm registry" in docs
    assert "not pinned" in docs


def test_docs_name_the_only_forced_kill_and_say_it_touches_the_signing_child_only():
    docs = _docs()
    assert "only forced kill" in docs
    assert "signing child" in docs
    assert "never Zotero" in docs


def test_docs_promise_only_the_version_refusal_and_no_later_retry():
    docs = _docs()
    assert "Bump the version in manifest.json, then sign again." in docs
    assert "the next `sign-plugin` run signs the new source" not in docs
    assert "again later" not in docs


def test_docs_say_the_unsigned_build_writes_nothing_to_the_zotero_profile():
    assert "writes nothing to the Zotero profile" in _docs()


def test_docs_and_the_bridge_readme_use_tools_plugins_and_install_plugin_from_file():
    readme = (REPO_EXTENSION / "README.md").read_text(encoding="utf-8")
    for text in (_docs(), readme):
        assert "Tools > Add-ons" not in text
        assert "Install Add-on From File" not in text
        assert "Tools > Plugins" in text
        assert "Install Plugin From File" in text


def test_the_unverified_zotero_10_menu_note_appears_once_in_the_docs():
    assert _docs().count("Zotero 10") == 1


def test_no_user_message_in_the_install_or_the_tray_names_the_add_ons_menu():
    from meridian import tray_main

    install_source = Path(zpi.__file__).read_text(encoding="utf-8")
    tray_source = Path(tray_main.__file__).read_text(encoding="utf-8")
    for text in (install_source, tray_source):
        assert "Tools > Add-ons" not in text
        assert "Install Add-on From File" not in text
