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
import os
import re
import shutil
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
    "Mozilla may still have received this version, so bump the version in manifest.json to sign a new build. "
    "Nothing was stored."
)
SOURCE_CHANGED_TEXT = (
    "The plugin source changed while Mozilla was signing it, so nothing was stored. Mozilla may have received "
    "this version, so bump the version in manifest.json to sign a new build."
)
REUSED_TEXT = "This version has already been submitted to Mozilla. Bump the version in manifest.json to sign a new build."
INTEGRITY_TEXT = "The stored signed file failed its integrity check. Bump the version in manifest.json to sign a new build."
RANGE_TEXT = (
    "--timeout-minutes must be a number of minutes greater than 0 and at most 1440. Nothing was signed or uploaded."
)
UNSIGNED_ZIP_TEXT = (
    "Mozilla signing finished, but the file it returned has no signature in META-INF/ (.rsa or .sf). "
    "Nothing was stored. Mozilla may have received this version, so bump the version in manifest.json to sign a new build."
)
FAILED_NONE_TEXT = (
    "Mozilla signing did not complete (web-ext exit code: none). Nothing was stored. Mozilla may have received this "
    "version, so bump the version in manifest.json to sign a new build."
)
RECORD_UNREADABLE_TEXT = (
    "The signing record is unreadable, so nothing will be submitted. Check "
    "%USERPROFILE%\\.meridian\\local_runner\\zotero_plugin\\signed.json (or zotero_plugin\\signed.json in the folder "
    "named by MERIDIAN_LOCAL_RUNNER_STATE_DIR, when that is set), then try again."
)
NPX_MISSING_TEXT = (
    "npx was not found on PATH, so the signing tool cannot run. Install Node.js, which includes npx, then try again. "
    "Nothing was signed or uploaded."
)
BUNDLE_UNREADABLE_TEXT = "The plugin bundle's manifest could not be read. Nothing was signed or uploaded."
FAKE_NPX = "C:/node/npx.cmd"
# The real lookup, captured before the autouse fixture replaces it, for the tests that search real folders.
_REAL_WHICH = shutil.which
NPX_UNSAFE_TEXT = (
    "The npx program path contains characters the signing tool cannot take safely. Move Node.js to a folder path "
    "without those characters, then try again."
)
UNSAFE_PATH_TEXT = (
    "The temporary folder path contains characters that the signing tool cannot take safely. Set TEMP to a folder "
    "path without those characters, then try again."
)
SCRATCH_TEXT = "The temporary folder for signing could not be created. Nothing was signed or uploaded."
SUBMIT_RECORD_TEXT = "The signing record could not be written, so nothing was submitted to Mozilla."
VERSION_USED_TEXT = (
    "This plugin version was already submitted to Mozilla, and no verified signed file is stored for it. "
    "Bump the version in manifest.json to sign a new build. Nothing was written."
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
    """No real process, no real npx lookup, and every default Meridian path points into tmp_path."""

    def _blocked(*args, **kwargs):
        raise RuntimeError("real process calls are blocked in tests")

    monkeypatch.setattr(sign.subprocess, "run", _blocked)
    monkeypatch.setattr(sign.subprocess, "Popen", _blocked)
    monkeypatch.setattr(sign.shutil, "which", lambda name: FAKE_NPX)
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


def _records(state: Path) -> list[dict]:
    return json.loads((state / "signed.json").read_text(encoding="utf-8"))["records"]


def _only_a_submitted_record(state: Path, version: str = "0.1.0") -> None:
    """Nothing is stored, and the one record is the submission for ``version``: the version number is used up."""
    assert not (state / "signed").exists()
    (record,) = _records(state)
    assert record["addon_id"] == sign.PLUGIN_ADDON_ID
    assert record["version"] == version
    assert record["status"] == "submitted"


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


def test_an_orphan_signed_file_with_no_record_is_a_used_version_and_makes_no_runner_call(source, state):
    _write(state / "signed" / SIGNED_NAME, SIGNED_BYTES)
    before = _snapshot(state)
    assert sign.stored_signed_artifact(state, "0.1.0").status == sign.USED_STATUS
    runner = FakeRunner()
    out = _sign(source, state, runner=runner)
    assert out["status"] == "refused_version_reused"
    assert out["message"] == REUSED_TEXT
    assert runner.calls == []
    assert _snapshot(state) == before


# ---------------------------------------------------------------------------
# One submission per version: the record is written before web-ext runs, and any record refuses a new submission
# ---------------------------------------------------------------------------


def test_the_submitted_record_is_on_disk_before_web_ext_runs(source, state):
    seen = {}

    class _Recording(FakeRunner):
        def __call__(self, argv, env, *, timeout_s, cwd):
            seen["records"] = _records(state)
            return super().__call__(argv, env, timeout_s=timeout_s, cwd=cwd)

    assert _sign(source, state, runner=_Recording())["status"] == "signed"
    assert [record["status"] for record in seen["records"]] == ["submitted"]
    assert seen["records"][0]["source_sha256"] == sign.source_fingerprint(source)


def test_a_failed_run_leaves_a_submitted_record_and_the_next_run_is_refused(source, state):
    assert _sign(source, state, runner=FakeRunner(result=sign.SignRunResult(returncode=7)))["status"] == "failed"
    _only_a_submitted_record(state)
    retry = FakeRunner()
    out = _sign(source, state, runner=retry)
    assert out["status"] == "refused_version_reused"
    assert out["message"] == REUSED_TEXT
    assert retry.calls == []
    _only_a_submitted_record(state)


def test_a_failed_run_is_refused_again_even_when_the_source_changed(source, state):
    assert _sign(source, state, runner=FakeRunner(result=sign.SignRunResult(returncode=7)))["status"] == "failed"
    _write(source / "bootstrap.js", "// changed after the failed run\n")
    retry = FakeRunner()
    out = _sign(source, state, runner=retry)
    assert out["status"] == "refused_version_reused"
    assert out["message"] == REUSED_TEXT
    assert retry.calls == []


def test_a_timed_out_run_is_refused_the_same_way_with_the_source_unchanged_or_changed(source, state):
    timed = FakeRunner(result=sign.SignRunResult(returncode=None, timed_out=True))
    assert _sign(source, state, runner=timed)["status"] == "timeout"
    same = FakeRunner()
    assert _sign(source, state, runner=same)["status"] == "refused_version_reused"
    assert same.calls == []
    _write(source / "bootstrap.js", "// edited after the timeout\n")
    changed = FakeRunner()
    assert _sign(source, state, runner=changed)["status"] == "refused_version_reused"
    assert changed.calls == []


def test_a_submitted_record_refuses_before_the_credentials_are_even_looked_at(source, state):
    assert _sign(source, state, runner=FakeRunner(result=sign.SignRunResult(returncode=3)))["status"] == "failed"
    runner = FakeRunner()
    out = _sign(source, state, env={}, runner=runner)
    assert out["status"] == "refused_version_reused"
    assert runner.calls == []


def test_a_tampered_stored_file_is_never_re_signed_for_the_same_version(source, state):
    assert _sign(source, state)["status"] == "signed"
    (state / "signed" / SIGNED_NAME).write_bytes(b"tampered")
    runner = FakeRunner()
    out = _sign(source, state, runner=runner)
    assert out["status"] == "refused_version_reused"
    assert out["message"] == REUSED_TEXT
    assert runner.calls == []


def test_a_fresh_version_still_signs_after_an_earlier_version_was_used_up(source, state):
    timed = FakeRunner(result=sign.SignRunResult(returncode=None, timed_out=True))
    assert _sign(source, state, runner=timed)["status"] == "timeout"
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = "0.2.0"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    runner = FakeRunner(
        produce=lambda artifacts: (artifacts / f"{sign.PLUGIN_ADDON_ID}-0.2.0.xpi").write_bytes(SIGNED_BYTES),
    )
    out = _sign(source, state, runner=runner)
    assert out["status"] == "signed"
    assert out["version"] == "0.2.0"
    assert len(runner.calls) == 1
    assert {record["version"]: record["status"] for record in _records(state)} == {"0.1.0": "submitted", "0.2.0": "signed"}


def test_a_record_write_failure_submits_nothing(source, state, monkeypatch):
    def _cannot_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(sign, "record_submission", _cannot_write)
    runner = FakeRunner()
    out = _sign(source, state, runner=runner)
    assert out["status"] == "failed"
    assert out["message"] == SUBMIT_RECORD_TEXT
    assert runner.calls == []
    assert _snapshot(state) == {}


def test_the_submitted_record_carries_the_source_fingerprint_and_a_utc_time(source, state):
    assert _sign(source, state, runner=FakeRunner(result=sign.SignRunResult(returncode=5)))["status"] == "failed"
    (record,) = _records(state)
    assert record["source_sha256"] == sign.source_fingerprint(source)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", record["submitted_at"])


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
    _only_a_submitted_record(state)


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
    assert entry["status"] == "signed"
    assert entry["sha256"] == hashlib.sha256(SIGNED_BYTES).hexdigest()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", entry["signed_at"])
    stored_text = (state / "signed.json").read_text(encoding="utf-8")
    assert KEY_VALUE not in stored_text and SECRET_VALUE not in stored_text
    assert KEY_VALUE not in json.dumps(out) and SECRET_VALUE not in json.dumps(out)


def test_a_timeout_result_stores_nothing_but_uses_up_the_version(source, state):
    runner = FakeRunner(result=sign.SignRunResult(returncode=None, timed_out=True))
    out = _sign(source, state, runner=runner)
    assert out["status"] == "timeout"
    _only_a_submitted_record(state)


def test_a_failed_exit_reports_the_exit_code_only_and_stores_nothing(source, state):
    runner = FakeRunner(result=sign.SignRunResult(returncode=7))
    out = _sign(source, state, runner=runner)
    assert out["status"] == "failed"
    assert "7" in out["message"]
    _only_a_submitted_record(state)


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
    _only_a_submitted_record(state)


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


MALFORMED_RECORD_TEXTS = [
    "{not json",
    "[]",
    '{"records": "x"}',
    '{"records": [1]}',
    '{"records": [{"addon_id": "meridian-zotero@usemeridian.us", "version": "0.1.0"}]}',
    '{"records": [{"addon_id": "meridian-zotero@usemeridian.us", "version": "0.1.0", "status": "bogus", '
    '"source_sha256": "' + "0" * 64 + '"}]}',
]


@pytest.mark.parametrize(
    "text", MALFORMED_RECORD_TEXTS,
    ids=["not_json", "a_list", "records_not_a_list", "entry_not_an_object", "entry_missing_fields", "unknown_status"],
)
def test_a_malformed_record_file_refuses_and_is_never_treated_as_empty(source, state, text):
    state.mkdir(parents=True, exist_ok=True)
    (state / "signed.json").write_text(text, encoding="utf-8")
    before = _snapshot(state)
    runner = FakeRunner()
    out = _sign(source, state, runner=runner)
    assert out["status"] == "refused_unreadable_record"
    assert out["message"] == RECORD_UNREADABLE_TEXT
    assert runner.calls == []
    assert _snapshot(state) == before
    assert sign.signed_artifact_for_version(state, "0.1.0") is None


def test_a_malformed_record_is_not_bypassed_by_a_signed_file_on_disk(source, state):
    assert _sign(source, state)["status"] == "signed"
    (state / "signed.json").write_text("{not json", encoding="utf-8")
    runner = FakeRunner()
    out = _sign(source, state, runner=runner)
    assert out["status"] == "refused_unreadable_record"
    assert runner.calls == []


def test_the_recovery_text_names_the_real_record_location_and_never_says_to_remove_it():
    assert "remove" not in RECORD_UNREADABLE_TEXT.lower()
    assert "%USERPROFILE%\\.meridian\\local_runner\\zotero_plugin\\signed.json" in RECORD_UNREADABLE_TEXT
    assert "MERIDIAN_LOCAL_RUNNER_STATE_DIR" in RECORD_UNREADABLE_TEXT
    assert "state/signed.json" not in RECORD_UNREADABLE_TEXT
    assert zpi.RECORD_UNREADABLE_MESSAGE == RECORD_UNREADABLE_TEXT
    assert sign.RECORD_UNREADABLE_MESSAGE == RECORD_UNREADABLE_TEXT


def test_a_record_written_before_the_status_field_still_counts_as_signed(source, state):
    assert _sign(source, state)["status"] == "signed"
    (entry,) = _records(state)
    del entry["status"]
    (state / "signed.json").write_text(json.dumps({"records": [entry]}), encoding="utf-8")
    runner = FakeRunner()
    out = _sign(source, state, runner=runner)
    assert out["status"] == "already_signed"
    assert runner.calls == []


def test_a_malformed_record_also_refuses_the_install_and_writes_nothing(default_zotero, source):
    state = zpi.signing_state_dir()
    assert _sign(source, state)["status"] == "signed"
    (state / "signed.json").write_text("[]", encoding="utf-8")
    before = _snapshot(default_zotero)
    result = zpi.install_default_profile(running=lambda: False)
    assert result["status"] == "record_unreadable"
    assert result["message"] == RECORD_UNREADABLE_TEXT
    assert _snapshot(default_zotero) == before
    assert not (default_zotero / "extensions").exists()


def test_install_after_the_record_is_deleted_reports_the_version_used_and_writes_nothing(source, default_zotero):
    state = zpi.signing_state_dir()
    assert _sign(source, state)["status"] == "signed"
    (state / "signed.json").unlink()
    before = _snapshot(default_zotero)
    result = zpi.install_default_profile(running=lambda: False)
    assert result["status"] == "version_used"
    assert "Bump the version in manifest.json to sign a new build." in result["message"]
    assert _snapshot(default_zotero) == before
    assert not (default_zotero / "extensions").exists()


def test_a_same_source_rerun_reports_already_signed_and_exits_zero_with_no_run(source, monkeypatch, capsys):
    monkeypatch.setattr(zpi, "_bundled_source_dir", lambda: source)
    monkeypatch.setattr(sign, "_run_web_ext", lambda *a, **k: pytest.fail("no run for an already signed source"))
    assert _sign(source, zpi.signing_state_dir())["status"] == "signed"
    rc = sign.cli_main([])
    text = capsys.readouterr().out
    assert rc == 0
    assert "already signed" in text
    assert "Nothing was uploaded." in text


def test_dry_run_reports_a_version_already_submitted_to_mozilla(monkeypatch, capsys):
    bundle = zpi._bundled_source_dir()
    state = zpi.signing_state_dir()
    sign.record_submission(state, sign.bundle_version(bundle), sign.source_fingerprint(bundle), clock=lambda: FIXED_NOW)
    rc = sign.cli_main(["--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "already submitted to Mozilla" in out
    assert "Signed artifact stored: no" in out


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
    _only_a_submitted_record(state)


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
    _only_a_submitted_record(state)
    (child,) = launched
    assert child.args[0] == "C:/node/npx.cmd"
    if sys.platform == "win32":
        assert taskkill == [["taskkill", "/PID", "4242", "/T", "/F"]]
        assert killpg == []
    else:
        assert killpg == [(4242, signal.SIGKILL)]
        assert taskkill == []


def test_a_keyboard_interrupt_kills_the_signing_tree_and_still_propagates(source, state, monkeypatch):
    launched, taskkill, killpg, interrupted = [], [], [], []

    def _interrupted_once(child, timeout):
        if not interrupted:
            interrupted.append(child.pid)
            raise KeyboardInterrupt()
        return 0

    def _record_taskkill(argv, **kwargs):
        taskkill.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(sign.shutil, "which", lambda name: "C:/node/npx.cmd")
    _fake_popen(monkeypatch, _interrupted_once, launched)
    monkeypatch.setattr(sign.subprocess, "run", _record_taskkill)
    monkeypatch.setattr(sign.os, "killpg", lambda pid, sig: killpg.append((pid, sig)), raising=False)
    with pytest.raises(KeyboardInterrupt):
        sign.sign_plugin(env=CREDS, state_dir=state, source_dir=source, timeout_s=60)
    (child,) = launched
    assert interrupted == [4242]
    if sys.platform == "win32":
        assert taskkill == [["taskkill", "/PID", "4242", "/T", "/F"]]
        assert killpg == []
    else:
        assert killpg == [(4242, signal.SIGKILL)]
        assert taskkill == []
    assert child.args[0] == "C:/node/npx.cmd"
    _only_a_submitted_record(state)


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


def test_a_missing_npx_writes_no_record_and_starts_no_process(source, state, monkeypatch):
    monkeypatch.setattr(sign.shutil, "which", lambda name: None)
    monkeypatch.setattr(sign.subprocess, "Popen", lambda *a, **k: pytest.fail("no process without npx"))
    out = sign.sign_plugin(env=CREDS, state_dir=state, source_dir=source)
    assert out["status"] == "failed"
    assert out["message"] == NPX_MISSING_TEXT
    assert _snapshot(state) == {}


def test_a_missing_npx_makes_no_runner_call_and_submits_nothing(source, state):
    runner = FakeRunner()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sign.shutil, "which", lambda name: None)
        out = _sign(source, state, runner=runner)
    assert out["status"] == "failed"
    assert out["message"] == NPX_MISSING_TEXT
    assert runner.calls == []
    assert _snapshot(state) == {}


# ---------------------------------------------------------------------------
# npx comes from PATH only: never from the folder the command runs in, never from an empty or a relative entry
# ---------------------------------------------------------------------------


def _fake_npx(folder: Path) -> Path:
    """A stand-in npx in ``folder``: npx.cmd on Windows, an executable npx elsewhere. Nothing ever starts it."""
    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        target = folder / "npx.cmd"
        target.write_text("@echo off\r\n", encoding="utf-8")
    else:
        target = folder / "npx"
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        target.chmod(0o755)
    return target


def _use_real_which(monkeypatch) -> None:
    """Put back the real shutil.which, which the autouse fixture replaces with a fake.

    On Windows, which() searches the current folder first unless NoDefaultCurrentDirectoryInExePath is set. Some
    machines set it, so the tests clear it, to match a default Windows install.
    """
    monkeypatch.setattr(sign.shutil, "which", _REAL_WHICH)
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)


def _same_file(text: str, path: Path) -> bool:
    return os.path.normcase(os.path.abspath(text)) == os.path.normcase(os.path.abspath(str(path)))


def test_npx_in_the_current_folder_is_never_started(source, state, tmp_path, monkeypatch):
    _use_real_which(monkeypatch)
    cwd = tmp_path / "cwd"
    _fake_npx(cwd)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setattr(sign.subprocess, "Popen", lambda *a, **k: pytest.fail("no process for a cwd npx"))
    runner = FakeRunner()
    out = _sign(source, state, runner=runner)
    assert out["status"] == "failed"
    assert out["message"] == NPX_MISSING_TEXT
    assert runner.calls == []
    assert _snapshot(state) == {}


def test_npx_on_a_path_folder_is_the_program_that_runs(source, state, tmp_path, monkeypatch):
    _use_real_which(monkeypatch)
    npx = _fake_npx(tmp_path / "node")
    monkeypatch.setenv("PATH", str(tmp_path / "node"))
    runner = FakeRunner()
    out = _sign(source, state, runner=runner)
    assert out["status"] == "signed"
    (call,) = runner.calls
    assert _same_file(call["argv"][0], npx)
    assert call["argv"][1:4] == ["--yes", sign.WEB_EXT_PACKAGE, "sign"]


def test_an_npx_in_the_current_folder_does_not_beat_the_one_on_path(source, state, tmp_path, monkeypatch):
    _use_real_which(monkeypatch)
    cwd = tmp_path / "cwd"
    _fake_npx(cwd)
    npx = _fake_npx(tmp_path / "node")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATH", str(tmp_path / "node"))
    runner = FakeRunner()
    out = _sign(source, state, runner=runner)
    assert out["status"] == "signed"
    (call,) = runner.calls
    assert _same_file(call["argv"][0], npx)


def test_an_empty_path_entry_is_skipped_so_the_current_folder_is_not_searched(source, state, tmp_path, monkeypatch):
    _use_real_which(monkeypatch)
    _fake_npx(tmp_path / "cwd")
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setenv("PATH", os.pathsep + os.pathsep)
    runner = FakeRunner()
    out = _sign(source, state, runner=runner)
    assert out["status"] == "failed"
    assert out["message"] == NPX_MISSING_TEXT
    assert runner.calls == []
    assert _snapshot(state) == {}


def test_a_relative_path_entry_is_skipped(source, state, tmp_path, monkeypatch):
    _use_real_which(monkeypatch)
    _fake_npx(tmp_path / "relbin")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "relbin")
    runner = FakeRunner()
    out = _sign(source, state, runner=runner)
    assert out["status"] == "failed"
    assert out["message"] == NPX_MISSING_TEXT
    assert runner.calls == []
    assert _snapshot(state) == {}


def test_a_path_entry_that_is_the_current_folder_is_skipped(source, state, tmp_path, monkeypatch):
    _use_real_which(monkeypatch)
    cwd = tmp_path / "cwd"
    _fake_npx(cwd)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATH", str(cwd))
    runner = FakeRunner()
    out = _sign(source, state, runner=runner)
    assert out["status"] == "failed"
    assert out["message"] == NPX_MISSING_TEXT
    assert runner.calls == []


def test_every_npx_lookup_names_one_absolute_path_folder_so_windows_adds_no_current_folder(
    source, state, tmp_path, monkeypatch,
):
    seen = []

    def _spy(name, *args, **kwargs):
        seen.append(name)
        return None

    folders = [tmp_path / "a", tmp_path / "b"]
    monkeypatch.setattr(sign.shutil, "which", _spy)
    monkeypatch.setenv("PATH", os.pathsep.join(str(folder) for folder in folders))
    out = _sign(source, state, runner=FakeRunner())
    assert out["message"] == NPX_MISSING_TEXT
    assert seen, "the PATH folders should have been searched"
    for name in seen:
        assert os.path.isabs(name)
        assert os.path.basename(name) == "npx"
        assert any(_same_file(os.path.dirname(name), folder) for folder in folders)


def test_the_npx_path_refusal_names_the_program_path_and_never_the_temp_folder():
    assert "TEMP" not in NPX_UNSAFE_TEXT
    assert "The npx program path contains characters the signing tool cannot take safely." in NPX_UNSAFE_TEXT
    assert "Move Node.js to a folder path without those characters" in NPX_UNSAFE_TEXT


# ---------------------------------------------------------------------------
# Never-raises: a runner, a start, or a scratch folder that fails is a failed result, with no traceback and no secret
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error", [ValueError("bad value"), TypeError("bad type"), OSError("no such program")],
    ids=["value_error", "type_error", "os_error"],
)
def test_a_popen_that_raises_is_a_failed_run_with_no_traceback_and_no_secret(source, state, monkeypatch, error):
    monkeypatch.setattr(sign.shutil, "which", lambda name: "C:/node/npx.cmd")

    def _raises(*args, **kwargs):
        raise error

    monkeypatch.setattr(sign.subprocess, "Popen", _raises)
    out = sign.sign_plugin(env=CREDS, state_dir=state, source_dir=source, timeout_s=120)
    text = json.dumps(out)
    assert out["status"] == "failed"
    assert out["message"] == FAILED_NONE_TEXT
    assert "Traceback" not in text
    assert str(error) not in text
    assert KEY_VALUE not in text and SECRET_VALUE not in text
    _only_a_submitted_record(state)


@pytest.mark.parametrize(
    "error", [ValueError("bad value"), TypeError("bad type")], ids=["value_error", "type_error"],
)
def test_run_web_ext_turns_a_popen_error_into_a_result_without_a_return_code(monkeypatch, tmp_path, error):
    monkeypatch.setattr(sign.shutil, "which", lambda name: "C:/node/npx.cmd")

    def _raises(*args, **kwargs):
        raise error

    monkeypatch.setattr(sign.subprocess, "Popen", _raises)
    result = sign._run_web_ext(["npx", "--yes", sign.WEB_EXT_PACKAGE, "sign"], dict(CREDS), timeout_s=60, cwd=tmp_path)
    assert result == sign.SignRunResult(returncode=None)


def test_an_oserror_creating_the_scratch_folder_is_a_failed_result_that_submits_nothing(source, state, monkeypatch):
    def _no_folder(*args, **kwargs):
        raise OSError("the temporary folder is not writable")

    monkeypatch.setattr(sign.tempfile, "TemporaryDirectory", _no_folder)
    runner = FakeRunner()
    out = _sign(source, state, runner=runner)
    assert out["status"] == "failed"
    assert out["message"] == SCRATCH_TEXT
    assert runner.calls == []
    assert _snapshot(state) == {}


def _deeply_nested_manifest(source: Path) -> None:
    deep = "[" * 100_000 + "]" * 100_000
    _write(source / "manifest.json", '{"manifest_version": 2, "version": "0.1.0", "deep": ' + deep + "}")


def test_a_deeply_nested_manifest_is_a_failed_result_with_no_traceback(source, state):
    _deeply_nested_manifest(source)
    runner = FakeRunner()
    out = _sign(source, state, runner=runner)
    assert out["status"] == "failed"
    assert out["message"] == BUNDLE_UNREADABLE_TEXT
    assert "Traceback" not in json.dumps(out)
    assert runner.calls == []
    assert _snapshot(state) == {}


def test_a_deeply_nested_manifest_is_reported_by_the_dry_run_without_a_traceback(source, monkeypatch, capsys):
    _deeply_nested_manifest(source)
    monkeypatch.setattr(zpi, "_bundled_source_dir", lambda: source)
    rc = sign.cli_main(["--dry-run"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "cannot be read from the bundle manifest" in captured.out
    assert "Traceback" not in captured.out + captured.err


# ---------------------------------------------------------------------------
# Safe staging path: no character that cmd.exe reads specially may reach the web-ext argv
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("char", ["&", "^", "%", "!"], ids=["ampersand", "caret", "percent", "bang"])
def test_a_temp_folder_path_with_an_unsafe_character_is_refused_before_any_argv_or_submission(
    source, state, tmp_path, monkeypatch, char,
):
    temp = tmp_path / f"Temp {char} Build"
    temp.mkdir()
    monkeypatch.setattr(sign.tempfile, "tempdir", str(temp))
    monkeypatch.setattr(sign, "sign_argv", lambda *a, **k: pytest.fail("no argv for an unsafe path"))
    runner = FakeRunner()
    out = _sign(source, state, runner=runner)
    assert out["status"] == "failed"
    assert out["message"] == UNSAFE_PATH_TEXT
    assert runner.calls == []
    assert _snapshot(state) == {}


def test_the_unsafe_character_check_covers_every_listed_character():
    for char in '&^%!"<>|':
        assert sign._unsafe_for_argv(f"C:/temp/a{char}b") is True
    assert sign._unsafe_for_argv("C:/temp/a b (c)") is False


@pytest.mark.parametrize("char", ["&", "^", "%", "!"], ids=["ampersand", "caret", "percent", "bang"])
def test_an_npx_path_with_an_unsafe_character_is_refused_before_any_submission(source, state, monkeypatch, char):
    monkeypatch.setattr(sign.shutil, "which", lambda name: f"C:/node {char} tools/npx.cmd")
    monkeypatch.setattr(sign.subprocess, "Popen", lambda *a, **k: pytest.fail("no process for an unsafe npx path"))
    runner = FakeRunner()
    out = _sign(source, state, runner=runner)
    assert out["status"] == "failed"
    assert out["message"] == NPX_UNSAFE_TEXT
    assert runner.calls == []
    assert _snapshot(state) == {}


def test_the_resolved_npx_path_is_the_program_the_argv_starts(source, state):
    runner = FakeRunner()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sign.shutil, "which", lambda name: "C:/Program Files/nodejs/npx.cmd")
        out = _sign(source, state, runner=runner)
    assert out["status"] == "signed"
    (call,) = runner.calls
    assert call["argv"][0] == "C:/Program Files/nodejs/npx.cmd"
    assert call["argv"][1:4] == ["--yes", sign.WEB_EXT_PACKAGE, "sign"]


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
    _only_a_submitted_record(state)


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


def test_a_deleted_signed_file_is_a_used_version_and_never_a_missing_one(default_zotero, source):
    state = zpi.signing_state_dir()
    assert _sign(source, state)["status"] == "signed"
    (state / "signed" / SIGNED_NAME).unlink()
    result = zpi.install_default_profile(running=lambda: False)
    assert result["status"] == "version_used"
    assert result["message"] == VERSION_USED_TEXT
    assert NEEDS_SIGNATURE_LEAD not in result["message"]
    assert not (default_zotero / "extensions").exists()
    # The same version cannot be signed again, so the install must not send the user to sign-plugin.
    runner = FakeRunner()
    assert _sign(source, state, runner=runner)["status"] == "refused_version_reused"
    assert runner.calls == []


def test_an_orphan_signed_file_with_no_record_is_a_used_version_for_the_install_too(default_zotero, source):
    state = zpi.signing_state_dir()
    _write(state / "signed" / SIGNED_NAME, SIGNED_BYTES)
    result = zpi.install_default_profile(running=lambda: False)
    assert result["status"] == "version_used"
    assert result["message"] == VERSION_USED_TEXT
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


def test_install_into_profile_has_no_default_for_the_expected_hash(default_zotero, tmp_path):
    xpi = tmp_path / "stand-in.xpi"
    xpi.write_bytes(SIGNED_BYTES)
    with pytest.raises(TypeError):
        zpi.install_into_profile(default_zotero, xpi, running=lambda: False)
    with pytest.raises(TypeError):
        zpi.install_into_profile(default_zotero, xpi, lambda: False, hashlib.sha256(SIGNED_BYTES).hexdigest())
    assert not (default_zotero / "extensions").exists()


def test_every_production_install_passes_the_recorded_hash(monkeypatch, default_zotero, source):
    state = zpi.signing_state_dir()
    assert _sign(source, state)["status"] == "signed"
    recorded = hashlib.sha256(SIGNED_BYTES).hexdigest()
    given = []
    real_install = zpi.install_into_profile

    def _spy(profile_dir, xpi_path, **kwargs):
        given.append(kwargs.get("expected_sha256"))
        return real_install(profile_dir, xpi_path, **kwargs)

    target = default_zotero / "extensions" / XPI_NAME
    monkeypatch.setattr(zpi, "install_into_profile", _spy)
    monkeypatch.setattr(zpi, "zotero_process_state", lambda runner=None: False)
    assert zpi.install_default_profile(running=lambda: False)["status"] == "installed"
    target.unlink()
    assert zpi.install_default_with_close(
        {"autoinstall": False, "decided": True, "close_zotero_for_install": True},
        close_zotero=False, running=lambda: False,
    )["status"] == "installed"
    target.unlink()
    assert zpi.cli_main(["--profile", str(default_zotero)]) == 0
    assert given == [recorded, recorded, recorded]


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
    _only_a_submitted_record(state)


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


def _section_4a(docs: str) -> str:
    """The signing section of the install guide: from its heading up to section 5."""
    return docs[docs.index("## 4a."):docs.index("## 5. Verify")]


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
    assert "Bump the version in manifest.json to sign a new build." in docs
    assert "the next `sign-plugin` run signs the new source" not in docs
    assert "again later" not in docs


def test_docs_drop_the_signed_once_claim_and_the_only_state_folder_claim_and_name_the_temp_and_npm_writes():
    docs = _docs()
    assert "A version is signed once" not in docs
    assert "writes only into Meridian's own state folder" not in docs
    assert "system temporary folder" in docs
    assert "npm cache" in docs


def test_docs_say_a_failed_or_timed_out_submission_uses_up_the_version_number():
    docs = _docs()
    assert "the version number is used up" in docs
    assert "Bump the version in manifest.json to sign a new build." in docs


def test_docs_name_the_zotero_minimum_and_keep_one_menu_note_for_zotero_10_0_6():
    docs = _docs()
    assert "Zotero 8 or later (the manifest's minimum)" in docs
    assert "Zotero 8.0 or later" not in docs
    assert docs.count("Zotero 10") == 1
    assert "Zotero 10.0.6" in docs


def test_docs_state_the_negative_timeout_form_and_the_process_tree_kill():
    docs = _docs()
    assert "--timeout-minutes=-5" in docs
    assert "may be written with an equals sign" in docs
    assert "must be written with an equals sign" not in docs
    assert "process tree" in docs
    assert "only forced kill" in docs


def _bullet(docs: str, lead: str) -> str:
    return next(line for line in docs.splitlines() if line.startswith(f"- **{lead}"))


def test_docs_say_deleting_the_record_removes_the_guarantee_and_that_a_bump_signs_again():
    docs = _docs()
    assert "Deleting `signed.json` removes the guarantee against resubmitting a version" in docs
    assert "cannot detect a fully deleted record" in docs
    assert "To sign again, bump the version in `manifest.json`." in docs


def test_docs_say_a_version_is_matched_exactly_and_that_a_near_duplicate_is_not_verified():
    docs = _docs()
    assert "matched exactly as written" in docs
    assert "`0.1` and `0.1.0` are different versions" in docs
    assert "Mozilla would probably reject a near-duplicate" not in docs
    assert "Mozilla's behaviour for a near-duplicate version is not verified" in _section_4a(docs)


def test_docs_say_a_same_source_rerun_exits_zero_and_only_a_changed_source_or_a_failure_is_refused():
    section = _section_4a(_docs())
    assert "reports it as already signed, exits 0, and uploads nothing" in section
    assert "Any other later run for that version is refused with:" in section
    assert "Any later run for that version is refused with" not in section


def test_docs_say_install_is_blocked_after_the_record_is_deleted_while_the_signed_file_is_kept():
    section = _section_4a(_docs())
    assert "Install is also blocked after the record is deleted, while the signed file is kept" in section
    assert "reports the version as used and says to bump it" in section


def test_docs_say_the_one_submission_guarantee_lives_in_the_state_folder_and_a_new_machine_starts_empty():
    section = _section_4a(_docs())
    assert "The one-submission guarantee lives in the state folder" in section
    assert "A different machine" in section
    assert "a different Windows user" in section
    assert "`MERIDIAN_LOCAL_RUNNER_STATE_DIR` override" in section
    assert "starts with no record" in section


def test_docs_name_the_check_then_write_race_as_a_plain_limit_in_section_4a():
    section = _section_4a(_docs())
    assert "`sign-plugin` runs for the same version at the same time" in section
    assert "both can submit" in section
    assert "This build does not lock against it." in section


def test_docs_say_a_version_submitted_before_the_record_existed_is_not_protected():
    docs = _docs()
    assert "before this record existed" in docs
    assert "is not protected" in docs


def test_docs_signing_no_longer_claims_it_writes_the_unsigned_bundle_to_the_state_folder():
    bullet = _bullet(_docs(), "Signing does not close Zotero.")
    assert "unsigned bundle" not in bullet
    assert "`signed.json`" in bullet


def test_the_bridge_readme_says_a_failed_submission_uses_up_the_version_number():
    readme = (REPO_EXTENSION / "README.md").read_text(encoding="utf-8")
    assert "the version number is used up" in readme
    assert "Bump the version in manifest.json to sign a new build." in readme


def test_the_bridge_readme_carries_the_record_deletion_version_and_pre_record_facts():
    readme = (REPO_EXTENSION / "README.md").read_text(encoding="utf-8")
    assert "Deleting `signed.json` removes the guarantee against resubmitting a version" in readme
    assert "cannot detect a fully deleted record" in readme
    assert "To sign again, bump the version in `manifest.json`." in readme
    assert "matched exactly as written" in readme
    assert "`0.1` and `0.1.0` are different versions" in readme
    assert "Mozilla would probably reject a near-duplicate" not in readme
    assert "Mozilla's behaviour for a near-duplicate version is not verified" in readme
    assert "before this record existed" in readme
    assert "is not protected" in readme


def test_the_bridge_readme_says_install_is_blocked_after_a_deletion_and_where_the_guarantee_lives():
    readme = (REPO_EXTENSION / "README.md").read_text(encoding="utf-8")
    assert "Install is also blocked after the record is deleted, while the signed file is kept" in readme
    assert "reports the version as used and says to bump it" in readme
    assert "The one-submission guarantee lives in the state folder" in readme
    assert "A different machine" in readme
    assert "a different Windows user" in readme
    assert "starts with no record" in readme
    assert "reports it as already signed, exits 0, and uploads nothing" in readme


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
