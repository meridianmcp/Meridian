"""The Zotero bridge installer, the settings fields for the licence acceptance, the force-close opt-in and the update check, and
the build step that bundles the owner's signed .xpi.

Every test works under tmp_path. The process list, the command runner and the close polls are fakes, so no process
starts, no Zotero is closed, and nothing reaches the real profile or the real token file. No Qt is imported here.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from meridian_desktop import zotero_bridge, zotero_bridge_install as bi
from meridian_desktop.settings_store import SettingsStore, SettingsUnreadable, licence_accepted_for

REPO = Path(__file__).resolve().parents[1]
# The sha256 of the approved licence notice in meridian/zotero_plugin_install.py (LICENCE_NOTICE_TEXT, UTF-8). This test
# pins the DESKTOP copy only, so it fails if the desktop copy changes. The core file is not in this worktree and is not read
# here, so a change to the core copy is not caught by this test. The owner's review keeps the two copies the same.
APPROVED_NOTICE_SHA256 = "9d5a3ba668af96eb175a81adce77e84b7a8be676641b6207d1489e7405ef987b"
PAYLOAD = b"signed bridge bytes for tests, not a real .xpi"
NOTICE_SHA = bi.LICENCE_NOTICE_SHA256
USER_JS_LINES_WRITTEN = [
    'user_pref("extensions.startupScanScopes", 1);',
]
# The entries a Mozilla-signed .xpi holds. The build checks for the first two only (a presence check, not a signature check).
SIGNATURE_ENTRIES = ("META-INF/mozilla.rsa", "META-INF/mozilla.sf", "META-INF/cose.sig", "META-INF/cose.manifest")
ZOTERO_RUNNING = "zotero.exe                     4242 Console                    1    210,000 K\n"
ZOTERO_GONE = "INFO: No tasks are running which match the specified criteria.\n"
GRACEFUL_POLLS = round(bi.CLOSE_TIMEOUT_SECONDS / bi.CLOSE_POLL_SECONDS)
FORCED_POLLS = round(bi.FORCE_CLOSE_TIMEOUT_SECONDS / bi.CLOSE_POLL_SECONDS)


class FakeHost:
    """Stands in for the process list, the command runner and the sleep between close polls. Every command is recorded.

    ``close_works`` ends Zotero on the normal close request, and ``force_works`` ends it on the forced one. Nothing else does.
    """

    def __init__(
        self,
        *,
        running=False,
        close_works=True,
        force_works=False,
        taskkill_raises=False,
        listing_fails=False,
        icacls_code=0,
    ):
        self.running = running
        self.close_works = close_works
        self.force_works = force_works
        self.taskkill_raises = taskkill_raises
        self.listing_fails = listing_fails
        self.icacls_code = icacls_code
        self.commands: list[list[str]] = []
        self.sleeps = 0
        self.forced = 0

    def process_list(self) -> str:
        if self.listing_fails:
            raise RuntimeError("tasklist failed")
        return ZOTERO_RUNNING if self.running else ZOTERO_GONE

    def runner(self, argv):
        self.commands.append(list(argv))
        if list(argv) == list(bi.TASKKILL_ARGV):
            if self.taskkill_raises:
                raise OSError("taskkill could not be started")
            if self.close_works:
                self.running = False
            return 0
        if list(argv) == list(bi.FORCE_TASKKILL_ARGV):
            self.forced += 1
            if self.force_works:
                self.running = False
            return 0
        if argv and argv[0] == "icacls":
            return self.icacls_code
        return 1  # anything else is refused, and the tests check that it never happened

    def sleep(self, seconds):
        self.sleeps += 1


class _ExitsBetweenListings:
    """A process listing that says Zotero runs on the first call, and has exited on every call after it."""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return ZOTERO_RUNNING if self.calls == 1 else ZOTERO_GONE


@pytest.fixture
def zotero(tmp_path):
    """A Zotero data root whose profiles.ini names one default profile, as Zotero writes it."""
    root = tmp_path / "Zotero" / "Zotero"
    profile = root / "Profiles" / "abc123.default-release"
    profile.mkdir(parents=True)
    (profile / "prefs.js").write_text("// prefs\n", encoding="utf-8")
    (root / "profiles.ini").write_text(
        "[Install4F96D1932A9F858E]\nDefault=Profiles/abc123.default-release\nLocked=1\n\n"
        "[Profile0]\nName=default-release\nIsRelative=1\nPath=Profiles/abc123.default-release\nDefault=1\n",
        encoding="utf-8",
    )
    return root, profile


def write_registry(profile: Path, version=None, *, disabled=False) -> None:
    """extensions.json with another add-on, and this add-on's entry when a version is given."""
    addons = [{"id": "someone-else@example.org", "version": "2.0", "userDisabled": False, "appDisabled": False}]
    if version is not None:
        addons.append(
            {"id": bi.ADDON_ID, "version": version, "userDisabled": disabled, "appDisabled": False, "active": not disabled}
        )
    (profile / bi.EXTENSIONS_JSON).write_text(json.dumps({"schemaVersion": 33, "addons": addons}), encoding="utf-8")


def make_bundle(tmp_path, *, name="bundle", payload=PAYLOAD, version="0.1.8", record_payload=None):
    """A bundle folder: the file with ``payload``, and a record whose sha256 is ``record_payload`` (default: PAYLOAD)."""
    folder = tmp_path / name / bi.BUNDLE_SUBDIR
    folder.mkdir(parents=True)
    (folder / bi.XPI_FILENAME).write_bytes(payload)
    digest = hashlib.sha256(PAYLOAD if record_payload is None else record_payload).hexdigest()
    record = {"addon_id": bi.ADDON_ID, "file": bi.XPI_FILENAME, "sha256": digest, "version": version}
    (folder / bi.BUNDLE_RECORD).write_text(json.dumps(record), encoding="utf-8")
    return bi.load_bundled_bridge(folder)


def accepted_store(tmp_path, *, digest: str = NOTICE_SHA) -> SettingsStore:
    """A settings file that records an acceptance of the notice shown now, or of ``digest`` when another is given."""
    store = SettingsStore(tmp_path / "settings.json")
    store.set_licence_accepted(True, notice_sha256=digest)
    return store


def make_installer(
    tmp_path,
    bundle,
    host,
    *,
    data_root,
    platform="linux",
    token=None,
    close_timeout_s=None,
    env=None,
    settings=None,
):
    """An installer over the fakes. Its settings store records the licence as accepted, unless another store is given."""
    kwargs = {} if close_timeout_s is None else {"close_timeout_s": close_timeout_s}
    if env is not None:
        kwargs["env"] = env
    return bi.ZoteroBridgeInstaller(
        bundle,
        data_dir=data_root,
        token_path=token if token is not None else tmp_path / "state" / "zotero_bridge" / "api_token",
        process_list=host.process_list,
        runner=host.runner,
        sleep=host.sleep,
        platform=platform,
        settings=accepted_store(tmp_path) if settings is None else settings,
        **kwargs,
    )


def assert_no_paths(message: str, *paths) -> None:
    """The message names no path and no token file, and holds none of the given paths."""
    assert "api_token" not in message
    assert "AppData" not in message
    for path in paths:
        assert str(path) not in message


def expected_api_lines(token: Path) -> list[str]:
    return [
        'user_pref("extensions.zotero.meridianZoteroBridge.apiPort", 23125);',
        f'user_pref("extensions.zotero.meridianZoteroBridge.apiTokenPath", {json.dumps(str(token))});',
    ]


def nothing_written(profile: Path, token: Path) -> bool:
    return (
        not (profile / "extensions" / bi.XPI_FILENAME).exists()
        and not (profile / bi.USER_JS).exists()
        and not token.exists()
    )


# ----------------------------------------------------------------- the approved text and the shared paths


def test_licence_notice_is_the_approved_text_word_for_word():
    digest = hashlib.sha256(bi.LICENCE_NOTICE_TEXT.encode("utf-8")).hexdigest()
    assert digest == APPROVED_NOTICE_SHA256


def test_token_path_matches_the_desktop_bridge_token_path(tmp_path):
    env = {"MERIDIAN_LOCAL_RUNNER_STATE_DIR": str(tmp_path / "runner")}
    assert bi.token_file_path(env) == zotero_bridge.token_file_path(env)
    assert bi.token_file_path({}) == zotero_bridge.token_file_path({})


def test_no_bundle_outside_a_frozen_build():
    assert bi.bundled_bridge_folder(frozen=False) is None
    installer = bi.ZoteroBridgeInstaller.for_this_build()
    assert installer.bundle is None
    assert installer.check().state == bi.STATE_NO_BUNDLE


# ----------------------------------------------------------------- finding the profile


def test_default_profile_is_found_from_profiles_ini(zotero):
    root, profile = zotero
    assert bi.find_default_profile(root) == profile


def test_default_profile_honours_an_absolute_path_with_is_relative_zero(tmp_path):
    root = tmp_path / "Zotero" / "Zotero"
    profile = tmp_path / "elsewhere" / "xyz.default"
    profile.mkdir(parents=True)
    (profile / "zotero.sqlite").write_bytes(b"")
    root.mkdir(parents=True)
    (root / "profiles.ini").write_text(
        f"[Profile0]\nName=default\nIsRelative=0\nPath={profile}\nDefault=1\n", encoding="utf-8"
    )
    assert bi.find_default_profile(root) == profile


def test_no_profile_is_guessed_when_profiles_ini_names_a_missing_folder(tmp_path):
    root = tmp_path / "Zotero" / "Zotero"
    root.mkdir(parents=True)
    (root / "profiles.ini").write_text("[Profile0]\nPath=Profiles/gone\nDefault=1\n", encoding="utf-8")
    assert bi.find_default_profile(root) is None
    assert bi.find_default_profile(tmp_path / "no-such-root") is None
    assert bi.find_default_profile(None) is None


def test_data_root_comes_from_appdata_only(tmp_path):
    assert bi.zotero_data_dir({"APPDATA": str(tmp_path)}) == tmp_path / "Zotero" / "Zotero"
    assert bi.zotero_data_dir({}) is None


# ----------------------------------------------------------------- reading the installed version


@pytest.mark.parametrize(
    ("version", "expected_state", "expected_installed"),
    [
        ("0.1.8", bi.STATE_CURRENT, "0.1.8"),
        ("0.1.7", bi.STATE_OLDER, "0.1.7"),
        ("0.1.9", bi.STATE_NEWER, "0.1.9"),
        ("beta", bi.STATE_UNKNOWN, "beta"),
        (None, bi.STATE_NOT_INSTALLED, None),
    ],
)
def test_installed_version_comes_from_the_extensions_json_entry(tmp_path, zotero, version, expected_state, expected_installed):
    root, profile = zotero
    write_registry(profile, version)
    check = make_installer(tmp_path, make_bundle(tmp_path), FakeHost(), data_root=root).check()
    assert check.state == expected_state
    assert check.installed_version == expected_installed
    assert check.bundled_version == "0.1.8"


def test_missing_extensions_json_is_not_installed(tmp_path, zotero):
    root, _profile = zotero
    check = make_installer(tmp_path, make_bundle(tmp_path), FakeHost(), data_root=root).check()
    assert check.state == bi.STATE_NOT_INSTALLED


def test_unreadable_extensions_json_is_reported_not_guessed(tmp_path, zotero):
    root, profile = zotero
    (profile / bi.EXTENSIONS_JSON).write_text("{ not json", encoding="utf-8")
    check = make_installer(tmp_path, make_bundle(tmp_path), FakeHost(), data_root=root).check()
    assert check.state == bi.STATE_UNREADABLE


def test_check_reports_no_profile_without_writing(tmp_path):
    check = make_installer(tmp_path, make_bundle(tmp_path), FakeHost(), data_root=tmp_path / "none").check()
    assert check.state == bi.STATE_NO_PROFILE
    assert check.profile_found is False


def test_registration_states_are_read_from_the_entry(tmp_path, zotero):
    root, profile = zotero
    write_registry(profile, "0.1.8", disabled=True)
    check = make_installer(tmp_path, make_bundle(tmp_path), FakeHost(), data_root=root).check()
    assert check.registration == bi.REG_DISABLED
    write_registry(profile, "0.1.8")
    check = make_installer(tmp_path, make_bundle(tmp_path, name="b2"), FakeHost(), data_root=root).check()
    assert check.registration == bi.REG_ENABLED


@pytest.mark.parametrize(
    ("installed", "bundled", "order"),
    [("0.1.7", "0.1.8", -1), ("0.1.8", "0.1.8", 0), ("0.1.10", "0.1.9", 1), ("0.1", "0.1.0", 0), ("beta", "0.1", None)],
)
def test_versions_compare_as_plain_numbers(installed, bundled, order):
    assert bi.compare_versions(installed, bundled) == order


def test_a_damaged_bundle_record_is_refused_not_guessed(tmp_path):
    folder = tmp_path / "damaged" / bi.BUNDLE_SUBDIR
    folder.mkdir(parents=True)
    (folder / bi.BUNDLE_RECORD).write_text(json.dumps({"addon_id": "someone@else", "version": "1"}), encoding="utf-8")
    with pytest.raises(bi.BundleError):
        bi.load_bundled_bridge(folder)
    damaged = bi.ZoteroBridgeInstaller(
        None, bundle_problem=bi.BUNDLE_DAMAGED_MESSAGE, data_dir=tmp_path, settings=accepted_store(tmp_path)
    )
    assert damaged.check().state == bi.STATE_DAMAGED
    assert damaged.install().status == bi.ST_BUNDLE_DAMAGED


def test_the_update_check_reads_only_and_installs_nothing(tmp_path, zotero):
    """The update check compares the installed bridge with the bundled one. It writes nothing and closes nothing."""
    root, profile = zotero
    write_registry(profile, "0.1.7")
    host = FakeHost(running=True)
    check = make_installer(tmp_path, make_bundle(tmp_path), host, data_root=root).check_for_update()
    assert check.state == bi.STATE_OLDER
    assert check.installed_version == "0.1.7"
    assert check.bundled_version == "0.1.8"
    assert host.commands == []
    assert not (profile / "extensions").exists()
    assert not (profile / bi.USER_JS).exists()


# ----------------------------------------------------------------- the install


def test_fresh_install_writes_the_plugin_the_scan_line_the_token_and_the_api_prefs(tmp_path, zotero):
    root, profile = zotero
    host = FakeHost()
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    result = make_installer(tmp_path, make_bundle(tmp_path), host, data_root=root, token=token).install()
    assert result.status == bi.ST_INSTALLED
    assert result.closed_zotero is False
    assert result.can_force_close is False
    assert (profile / "extensions" / bi.XPI_FILENAME).read_bytes() == PAYLOAD
    user_js = (profile / bi.USER_JS).read_text(encoding="latin-1").splitlines()
    assert user_js == USER_JS_LINES_WRITTEN + expected_api_lines(token)
    token_text = token.read_text(encoding="ascii").strip()
    assert token_text.startswith(bi.TOKEN_PREFIX)
    assert bi.token_is_usable(token)
    # Nothing was closed, and nothing was started: only the icacls step could run, and this platform has none.
    assert host.commands == []
    assert_no_paths(result.message, profile, token, root)
    assert "Tools > Plugins" in result.message


def test_no_command_ever_starts_zotero(tmp_path, zotero):
    root, profile = zotero
    host = FakeHost(running=True)
    make_installer(tmp_path, make_bundle(tmp_path), host, data_root=root).install()
    assert all(argv[0] in ("taskkill", "icacls") for argv in host.commands)
    assert all("zotero.exe" not in argv or argv[0] == "taskkill" for argv in host.commands)


def test_the_close_request_is_the_plain_taskkill_with_no_force_and_no_tree(tmp_path, zotero):
    root, _profile = zotero
    host = FakeHost(running=True)
    make_installer(tmp_path, make_bundle(tmp_path), host, data_root=root).install()
    assert bi.TASKKILL_ARGV == ("taskkill", "/IM", "zotero.exe")
    assert host.commands[0] == ["taskkill", "/IM", "zotero.exe"]
    assert "/F" not in host.commands[0] and "/T" not in host.commands[0]


def test_the_forced_close_request_is_taskkill_with_force_and_no_tree():
    assert bi.FORCE_TASKKILL_ARGV == ("taskkill", "/F", "/IM", "zotero.exe")
    assert "/T" not in bi.FORCE_TASKKILL_ARGV
    assert bi.FORCE_CLOSE_TIMEOUT_SECONDS == 10.0


def test_a_running_zotero_that_does_not_close_leaves_nothing_written(tmp_path, zotero):
    root, profile = zotero
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    host = FakeHost(running=True, close_works=False)
    result = make_installer(tmp_path, make_bundle(tmp_path), host, data_root=root, token=token).install()
    assert result.status == bi.ST_CLOSE_FAILED
    assert result.message == bi.CLOSE_FAILED_MESSAGE
    assert result.can_force_close is True
    assert nothing_written(profile, token)
    assert host.commands == [["taskkill", "/IM", "zotero.exe"]]
    assert host.sleeps == GRACEFUL_POLLS


def test_without_allow_force_close_zotero_is_never_force_closed_even_when_a_force_would_work(tmp_path, zotero):
    root, profile = zotero
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    host = FakeHost(running=True, close_works=False, force_works=True)
    result = make_installer(tmp_path, make_bundle(tmp_path), host, data_root=root, token=token).install()
    assert result.status == bi.ST_CLOSE_FAILED
    assert result.can_force_close is True
    assert host.forced == 0
    assert not any("/F" in argv for argv in host.commands)
    assert nothing_written(profile, token)


def test_allow_force_close_runs_one_forced_close_then_installs(tmp_path, zotero):
    root, profile = zotero
    host = FakeHost(running=True, close_works=False, force_works=True)
    result = make_installer(tmp_path, make_bundle(tmp_path), host, data_root=root).install(
        allow_force_close=True
    )
    assert result.status == bi.ST_INSTALLED
    assert result.closed_zotero is True
    assert bi.RESTART_MESSAGE in result.message
    # The normal close request ran first, then exactly one forced close, with no tree switch.
    assert host.commands == [["taskkill", "/IM", "zotero.exe"], ["taskkill", "/F", "/IM", "zotero.exe"]]
    assert host.forced == 1
    assert "/T" not in host.commands[1]
    # The normal window was polled in full, then the forced close was polled until Zotero was gone: one poll here.
    assert host.sleeps == GRACEFUL_POLLS + 1
    assert (profile / "extensions" / bi.XPI_FILENAME).read_bytes() == PAYLOAD


def test_zotero_still_running_after_the_forced_close_writes_nothing(tmp_path, zotero):
    root, profile = zotero
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    host = FakeHost(running=True, close_works=False, force_works=False)
    result = make_installer(tmp_path, make_bundle(tmp_path), host, data_root=root, token=token).install(
        allow_force_close=True
    )
    assert result.status == bi.ST_CLOSE_FAILED
    assert result.message == bi.FORCE_FAILED_MESSAGE
    # A forced close was already tried, so the owner is not offered another one.
    assert result.can_force_close is False
    assert host.commands == [["taskkill", "/IM", "zotero.exe"], ["taskkill", "/F", "/IM", "zotero.exe"]]
    assert host.forced == 1
    assert host.sleeps == GRACEFUL_POLLS + FORCED_POLLS
    assert nothing_written(profile, token)


def test_a_yes_to_force_goes_straight_to_one_forced_close_with_no_graceful_wait(tmp_path, zotero):
    """The owner's yes to the force-close question skips the normal close request and its 60-second window. It sends one
    taskkill /F, then polls the 10-second window, which ends at the first check because Zotero is gone."""
    root, profile = zotero
    host = FakeHost(running=True, close_works=False, force_works=True)
    result = make_installer(tmp_path, make_bundle(tmp_path), host, data_root=root).install(
        allow_force_close=True, skip_graceful=True
    )
    assert result.status == bi.ST_INSTALLED
    assert result.closed_zotero is True
    assert host.commands == [list(bi.FORCE_TASKKILL_ARGV)]
    assert bi.TASKKILL_ARGV not in host.commands
    assert host.forced == 1
    assert host.sleeps == 1
    assert host.sleeps < GRACEFUL_POLLS
    assert (profile / "extensions" / bi.XPI_FILENAME).read_bytes() == PAYLOAD


def test_a_yes_to_force_that_does_not_end_zotero_waits_only_the_forced_window(tmp_path, zotero):
    root, profile = zotero
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    host = FakeHost(running=True, close_works=False, force_works=False)
    result = make_installer(tmp_path, make_bundle(tmp_path), host, data_root=root, token=token).install(
        allow_force_close=True, skip_graceful=True
    )
    assert result.status == bi.ST_CLOSE_FAILED
    assert result.message == bi.FORCE_FAILED_MESSAGE
    assert result.can_force_close is False
    assert host.commands == [list(bi.FORCE_TASKKILL_ARGV)]
    assert host.sleeps == FORCED_POLLS
    assert nothing_written(profile, token)


def test_skip_graceful_without_the_owners_yes_never_forces_zotero(tmp_path, zotero):
    """Without allow_force_close, skip_graceful changes nothing: Zotero gets the normal close request, its full window, and
    is then left alone when it does not close."""
    root, profile = zotero
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    host = FakeHost(running=True, close_works=False, force_works=True)
    result = make_installer(tmp_path, make_bundle(tmp_path), host, data_root=root, token=token).install(skip_graceful=True)
    assert result.status == bi.ST_CLOSE_FAILED
    assert result.can_force_close is True
    assert host.forced == 0
    assert host.commands == [list(bi.TASKKILL_ARGV)]
    assert host.sleeps == GRACEFUL_POLLS
    assert nothing_written(profile, token)


def test_a_close_that_succeeds_then_installs_and_says_zotero_was_closed(tmp_path, zotero):
    root, profile = zotero
    host = FakeHost(running=True)
    result = make_installer(tmp_path, make_bundle(tmp_path), host, data_root=root).install()
    assert result.status == bi.ST_INSTALLED
    assert result.closed_zotero is True
    assert bi.RESTART_MESSAGE in result.message
    assert (profile / "extensions" / bi.XPI_FILENAME).read_bytes() == PAYLOAD
    assert host.commands[0] == ["taskkill", "/IM", "zotero.exe"]


def test_zotero_that_exits_between_the_two_listings_is_not_counted_as_closed_by_us(tmp_path, zotero):
    """Zotero runs when the install checks, and has gone by the time the close step checks. Meridian closed nothing, so the
    result is not marked as a close and no restart is asked for."""
    root, profile = zotero
    host = FakeHost()
    listing = _ExitsBetweenListings()
    installer = bi.ZoteroBridgeInstaller(
        make_bundle(tmp_path),
        data_dir=root,
        token_path=tmp_path / "state" / "zotero_bridge" / "api_token",
        process_list=listing,
        runner=host.runner,
        sleep=host.sleep,
        platform="linux",
        settings=accepted_store(tmp_path),
    )
    result = installer.install()
    assert result.status == bi.ST_INSTALLED
    assert result.closed_zotero is False
    assert bi.RESTART_MESSAGE not in result.message
    assert host.commands == []  # no close request was sent: Zotero had already gone
    assert listing.calls == 2
    assert (profile / "extensions" / bi.XPI_FILENAME).read_bytes() == PAYLOAD


def test_a_running_zotero_is_refused_when_closing_is_not_allowed(tmp_path, zotero):
    root, profile = zotero
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    host = FakeHost(running=True)
    result = make_installer(tmp_path, make_bundle(tmp_path), host, data_root=root, token=token).install(
        allow_close=False
    )
    assert result.status == bi.ST_REFUSED_RUNNING
    assert host.commands == []
    assert nothing_written(profile, token)


def test_a_process_check_that_fails_refuses_before_anything_is_written(tmp_path, zotero):
    root, profile = zotero
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    host = FakeHost(listing_fails=True)
    result = make_installer(tmp_path, make_bundle(tmp_path), host, data_root=root, token=token).install()
    assert result.status == bi.ST_CHECK_FAILED
    assert host.commands == []
    assert nothing_written(profile, token)


def test_a_sha256_mismatch_is_refused_and_nothing_is_written(tmp_path, zotero):
    root, profile = zotero
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    tampered = make_bundle(tmp_path, name="tampered", payload=b"changed after signing", record_payload=PAYLOAD)
    host = FakeHost(running=True)
    result = make_installer(tmp_path, tampered, host, data_root=root, token=token).install()
    assert result.status == bi.ST_INTEGRITY_FAILED
    assert host.commands == []  # refused before anything is closed
    assert nothing_written(profile, token)


def test_a_user_js_that_sets_the_scan_pref_to_another_value_is_refused_before_close(tmp_path, zotero):
    root, profile = zotero
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    (profile / bi.USER_JS).write_text('user_pref("extensions.startupScanScopes", 2);\n', encoding="utf-8")
    before = (profile / bi.USER_JS).read_bytes()
    host = FakeHost(running=True)
    result = make_installer(tmp_path, make_bundle(tmp_path), host, data_root=root, token=token).install()
    assert result.status == bi.ST_PREF_CONFLICT
    assert host.commands == []
    assert (profile / bi.USER_JS).read_bytes() == before
    assert not (profile / "extensions" / bi.XPI_FILENAME).exists()


def test_a_commented_out_scan_line_is_not_a_conflict(tmp_path, zotero):
    root, profile = zotero
    (profile / bi.USER_JS).write_text('// user_pref("extensions.startupScanScopes", 2);\n', encoding="utf-8")
    result = make_installer(tmp_path, make_bundle(tmp_path), FakeHost(), data_root=root).install()
    assert result.status == bi.ST_INSTALLED


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('user_pref("extensions.startupScanScopes", 1);\n', bi.SCAN_PRESENT),
        ("user_pref('extensions.startupScanScopes', 1);\n", bi.SCAN_PRESENT),
        ("user_pref('extensions.startupScanScopes',1);\n", bi.SCAN_PRESENT),
        ("user_pref('extensions.startupScanScopes', 2);\n", bi.SCAN_CONFLICT),
        ('user_pref("extensions.startupScanScopes",2);\n', bi.SCAN_CONFLICT),
        ('user_pref("extensions.startupScanScopes", 2); // a note\n', bi.SCAN_CONFLICT),
        ('user_pref("extensions.startupScanScopes", 2\n', bi.SCAN_CONFLICT),  # a mention this module cannot parse is refused
        ('// user_pref("extensions.startupScanScopes", 2);\n', bi.SCAN_ADDED),
        ("   // user_pref('extensions.startupScanScopes', 2);\n", bi.SCAN_ADDED),
        ("", bi.SCAN_ADDED),
    ],
)
def test_the_scan_pref_is_read_whatever_the_quotes_and_spacing(text, expected):
    assert bi.scan_state(text) == expected


@pytest.mark.parametrize(
    "line",
    [
        "user_pref('extensions.startupScanScopes', 2);",
        'user_pref("extensions.startupScanScopes",2);',
    ],
)
def test_a_scan_pref_other_than_one_is_refused_before_anything_closes(tmp_path, zotero, line):
    root, profile = zotero
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    (profile / bi.USER_JS).write_text(line + "\n", encoding="utf-8")
    before = (profile / bi.USER_JS).read_bytes()
    host = FakeHost(running=True)
    result = make_installer(tmp_path, make_bundle(tmp_path), host, data_root=root, token=token).install()
    assert result.status == bi.ST_PREF_CONFLICT
    assert host.commands == []
    assert host.sleeps == 0
    assert (profile / bi.USER_JS).read_bytes() == before
    assert not (profile / "extensions" / bi.XPI_FILENAME).exists()
    assert not token.exists()


def test_a_single_quoted_scan_pref_of_one_is_accepted_and_not_written_twice(tmp_path, zotero):
    root, profile = zotero
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    (profile / bi.USER_JS).write_text("user_pref('extensions.startupScanScopes', 1);\n", encoding="utf-8")
    result = make_installer(tmp_path, make_bundle(tmp_path), FakeHost(), data_root=root, token=token).install()
    assert result.status == bi.ST_INSTALLED
    lines = (profile / bi.USER_JS).read_text(encoding="utf-8").splitlines()
    assert lines[0] == "user_pref('extensions.startupScanScopes', 1);"
    assert sum(1 for line in lines if "startupScanScopes" in line) == 1
    assert lines[1:] == expected_api_lines(token)


def test_a_single_quoted_commented_scan_line_is_not_a_conflict(tmp_path, zotero):
    root, profile = zotero
    (profile / bi.USER_JS).write_text("// user_pref('extensions.startupScanScopes', 2);\n", encoding="utf-8")
    result = make_installer(tmp_path, make_bundle(tmp_path), FakeHost(), data_root=root).install()
    assert result.status == bi.ST_INSTALLED


def test_a_single_quoted_api_pref_is_replaced_and_not_duplicated(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    token = tmp_path / "api_token"
    (profile / bi.USER_JS).write_text("user_pref('extensions.zotero.meridianZoteroBridge.apiPort', 1111);\n", encoding="utf-8")
    assert bi.ensure_api_prefs(profile, token) == bi.PREFS_UPDATED
    lines = (profile / bi.USER_JS).read_text(encoding="utf-8").splitlines()
    assert lines == expected_api_lines(token)


def test_a_user_js_that_cannot_be_read_is_refused(tmp_path, zotero):
    root, profile = zotero
    (profile / bi.USER_JS).mkdir()  # exists, and cannot be read as a file
    result = make_installer(tmp_path, make_bundle(tmp_path), FakeHost(), data_root=root).install()
    assert result.status == bi.ST_PREF_UNREADABLE


def test_the_licence_must_be_accepted_before_anything_happens(tmp_path, zotero):
    root, profile = zotero
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    host = FakeHost(running=True)
    installer = make_installer(
        tmp_path,
        make_bundle(tmp_path),
        host,
        data_root=root,
        token=token,
        settings=SettingsStore(tmp_path / "settings.json"),  # no acceptance recorded
    )
    result = installer.install()
    assert result.status == bi.ST_LICENCE_NOT_ACCEPTED
    assert result.message.startswith("The Meridian Zotero bridge is released under the Meridian Source License 1.0 (MSL-1.0).")
    assert "accept it there first" in result.message
    assert host.commands == []
    assert host.sleeps == 0
    assert nothing_written(profile, token)


def test_an_acceptance_of_an_older_notice_is_refused_before_anything_closes(tmp_path, zotero):
    root, profile = zotero
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    host = FakeHost(running=True)
    installer = make_installer(
        tmp_path,
        make_bundle(tmp_path),
        host,
        data_root=root,
        token=token,
        settings=accepted_store(tmp_path, digest="0" * 64),
    )
    result = installer.install()
    assert result.status == bi.ST_LICENCE_NOT_ACCEPTED
    assert host.commands == [] and host.sleeps == 0
    assert nothing_written(profile, token)


def test_a_caller_cannot_pass_a_digest_that_matches_a_stale_acceptance(tmp_path, zotero):
    """The gate takes no digest, so a caller cannot name one that matches a stale acceptance. Python refuses the call before
    the installer runs, so nothing is closed or written."""
    root, profile = zotero
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    stale = "1" * 64
    host = FakeHost(running=True)
    installer = make_installer(
        tmp_path,
        make_bundle(tmp_path),
        host,
        data_root=root,
        token=token,
        settings=accepted_store(tmp_path, digest=stale),
    )
    with pytest.raises(TypeError):
        installer.install(licence_notice_sha256=stale)
    assert host.commands == []
    assert host.sleeps == 0
    assert nothing_written(profile, token)


def test_the_install_signature_has_no_digest_parameter():
    assert "licence_notice_sha256" not in inspect.signature(bi.ZoteroBridgeInstaller.install).parameters


def test_the_gate_checks_the_installers_own_digest_and_a_matching_acceptance_passes(tmp_path, zotero):
    """The installer compares the stored acceptance with LICENCE_NOTICE_SHA256 itself. A matching acceptance passes the gate
    with no argument, and the install runs."""
    root, profile = zotero
    host = FakeHost()
    result = make_installer(tmp_path, make_bundle(tmp_path), host, data_root=root).install()
    assert result.status == bi.ST_INSTALLED
    assert (profile / "extensions" / bi.XPI_FILENAME).read_bytes() == PAYLOAD


def test_an_installer_with_no_settings_store_refuses_every_install(tmp_path, zotero):
    root, profile = zotero
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    host = FakeHost(running=True)
    installer = bi.ZoteroBridgeInstaller(
        make_bundle(tmp_path),
        data_dir=root,
        token_path=token,
        process_list=host.process_list,
        runner=host.runner,
        sleep=host.sleep,
        platform="linux",
    )
    result = installer.install()
    assert result.status == bi.ST_LICENCE_NOT_ACCEPTED
    assert host.commands == []
    assert nothing_written(profile, token)


def test_an_unreadable_settings_file_refuses_the_install(tmp_path, zotero):
    root, profile = zotero
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    path = tmp_path / "settings.json"
    path.write_text("not json", encoding="utf-8")
    host = FakeHost(running=True)
    installer = make_installer(tmp_path, make_bundle(tmp_path), host, data_root=root, token=token, settings=SettingsStore(path))
    result = installer.install()
    assert result.status == bi.ST_LICENCE_NOT_ACCEPTED
    assert host.commands == []
    assert nothing_written(profile, token)


def test_no_bundle_writes_nothing(tmp_path, zotero):
    root, profile = zotero
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    host = FakeHost(running=True)
    result = make_installer(tmp_path, None, host, data_root=root, token=token).install()
    assert result.status == bi.ST_NO_BUNDLE
    assert host.commands == []
    assert nothing_written(profile, token)


def test_no_profile_refuses_before_anything_is_closed(tmp_path):
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    host = FakeHost(running=True)
    result = make_installer(tmp_path, make_bundle(tmp_path), host, data_root=tmp_path / "missing", token=token).install()
    assert result.status == bi.ST_NO_PROFILE
    assert host.commands == []
    assert not token.exists()


def test_an_unreadable_extensions_json_refuses_before_anything_is_closed(tmp_path, zotero):
    root, profile = zotero
    (profile / bi.EXTENSIONS_JSON).write_text("[1, 2", encoding="utf-8")
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    host = FakeHost(running=True)
    result = make_installer(tmp_path, make_bundle(tmp_path), host, data_root=root, token=token).install()
    assert result.status == bi.ST_REGISTRY_UNREADABLE
    assert host.commands == []
    assert nothing_written(profile, token)


def test_an_update_from_an_older_version_replaces_the_plugin_and_keeps_the_token(tmp_path, zotero):
    root, profile = zotero
    write_registry(profile, "0.1.7")
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    host = FakeHost()
    installer = make_installer(tmp_path, make_bundle(tmp_path), host, data_root=root, token=token)
    result = installer.install()
    assert result.status == bi.ST_INSTALLED
    assert "Updated the Zotero bridge from 0.1.7 to 0.1.8." in result.message
    first_token = token.read_text(encoding="ascii")
    write_registry(profile, "0.1.7")  # the update is needed again
    assert installer.install().status == bi.ST_INSTALLED
    assert token.read_text(encoding="ascii") == first_token  # a usable token is kept, not replaced


def test_a_current_install_writes_nothing_and_closes_nothing(tmp_path, zotero):
    """The same version with the file, the prefs, the scan line and the token all as written: current. Nothing is written or closed."""
    root, profile = zotero
    write_registry(profile, "0.1.8")
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    bundle = make_bundle(tmp_path)
    # The file is missing on this first run, so it is installed here (the repair tests below cover that case).
    first = make_installer(tmp_path, bundle, FakeHost(), data_root=root, token=token).install()
    assert first.status == bi.ST_INSTALLED
    watched = [profile / "extensions" / bi.XPI_FILENAME, profile / bi.USER_JS, token]
    before = {path: path.read_bytes() for path in watched}
    host = FakeHost(running=True)
    result = make_installer(tmp_path, bundle, host, data_root=root, token=token).install()
    assert result.status == bi.ST_CURRENT
    assert host.commands == []
    assert {path: path.read_bytes() for path in watched} == before


def test_the_same_version_with_the_file_missing_is_installed(tmp_path, zotero):
    root, profile = zotero
    write_registry(profile, "0.1.8")
    host = FakeHost(running=True)
    result = make_installer(tmp_path, make_bundle(tmp_path), host, data_root=root).install()
    assert result.status == bi.ST_INSTALLED
    assert result.closed_zotero is True
    assert (profile / "extensions" / bi.XPI_FILENAME).read_bytes() == PAYLOAD
    assert result.message.startswith("Installed the Zotero bridge 0.1.8.")


@pytest.mark.parametrize("damage", ["changed_file", "no_api_prefs", "no_scan_line", "no_token", "empty_token"])
def test_the_same_version_is_repaired_when_the_file_the_prefs_or_the_token_are_not_as_written(tmp_path, zotero, damage):
    root, profile = zotero
    write_registry(profile, "0.1.8")
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    bundle = make_bundle(tmp_path)
    first = make_installer(tmp_path, bundle, FakeHost(), data_root=root, token=token).install()
    assert first.status == bi.ST_INSTALLED
    xpi = profile / "extensions" / bi.XPI_FILENAME
    user_js = profile / bi.USER_JS
    if damage == "changed_file":
        xpi.write_bytes(b"changed after the install")
    elif damage == "no_api_prefs":
        user_js.write_text(USER_JS_LINES_WRITTEN[0] + "\n", encoding="utf-8")
    elif damage == "no_scan_line":
        user_js.write_text("\n".join(expected_api_lines(token)) + "\n", encoding="utf-8")
    elif damage == "no_token":
        token.unlink()
    else:  # empty_token
        token.write_text("", encoding="ascii")
    result = make_installer(tmp_path, bundle, FakeHost(), data_root=root, token=token).install()
    assert result.status == bi.ST_INSTALLED
    assert result.message.startswith("Installed the Zotero bridge 0.1.8.")
    assert xpi.read_bytes() == PAYLOAD
    assert bi.token_is_usable(token)
    lines = user_js.read_text(encoding="utf-8").splitlines()
    for line in USER_JS_LINES_WRITTEN + expected_api_lines(token):
        assert lines.count(line) == 1


def test_a_newer_installed_bridge_is_never_downgraded(tmp_path, zotero):
    root, profile = zotero
    write_registry(profile, "0.1.9")
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    host = FakeHost(running=True)
    result = make_installer(tmp_path, make_bundle(tmp_path), host, data_root=root, token=token).install()
    assert result.status == bi.ST_NEWER_INSTALLED
    assert host.commands == []
    assert nothing_written(profile, token)


def test_a_disabled_entry_is_reported_and_left_as_it_is(tmp_path, zotero):
    root, profile = zotero
    write_registry(profile, "0.1.7", disabled=True)
    registry_before = (profile / bi.EXTENSIONS_JSON).read_bytes()
    result = make_installer(tmp_path, make_bundle(tmp_path), FakeHost(), data_root=root).install()
    assert result.status == bi.ST_INSTALLED
    assert "turned off in Zotero" in result.message
    assert (profile / bi.EXTENSIONS_JSON).read_bytes() == registry_before  # extensions.json is never written here


def test_a_missing_entry_asks_the_owner_to_enable_the_bridge_once(tmp_path, zotero):
    root, _profile = zotero
    result = make_installer(tmp_path, make_bundle(tmp_path), FakeHost(), data_root=root).install()
    assert result.status == bi.ST_INSTALLED
    assert "Tools > Plugins" in result.message
    assert "does not start Zotero" in result.message


def test_a_failure_in_the_writes_names_the_error_class_and_no_path(tmp_path, zotero, monkeypatch):
    root, profile = zotero

    def broken(*_args, **_kwargs):
        raise OSError(f"cannot write {profile / 'extensions'}")

    monkeypatch.setattr(bi, "_atomic_write", broken)
    result = make_installer(tmp_path, make_bundle(tmp_path), FakeHost(), data_root=root).install()
    assert result.status == bi.ST_FAILED
    assert "OSError" in result.message
    assert_no_paths(result.message, profile)


# ----------------------------------------------------------------- the token file


def test_on_windows_the_token_is_restricted_to_its_owner(tmp_path, zotero):
    root, _profile = zotero
    host = FakeHost()
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    env = {"USERDOMAIN": "CORP", "USERNAME": "alice"}
    installer = make_installer(tmp_path, make_bundle(tmp_path), host, data_root=root, token=token, platform="win32", env=env)
    result = installer.install()
    assert result.status == bi.ST_INSTALLED
    icacls = [argv for argv in host.commands if argv and argv[0] == "icacls"]
    assert len(icacls) == 1
    # The restriction runs on the temp file in the token's own folder, before the token is moved into place.
    assert Path(icacls[0][1]).parent == token.parent
    assert icacls[0][2:] == ["/inheritance:r", "/grant:r", "CORP\\alice:F"]
    assert token.is_file() and bi.token_is_usable(token)


def test_a_token_that_cannot_be_restricted_is_removed_again(tmp_path, zotero):
    root, profile = zotero
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    host = FakeHost(icacls_code=5)
    result = make_installer(tmp_path, make_bundle(tmp_path), host, data_root=root, token=token, platform="win32").install()
    assert result.status == bi.ST_FAILED
    assert not token.exists()  # no token is left open to other accounts


def test_a_timed_out_restriction_leaves_no_token_file(tmp_path):
    token = tmp_path / "state" / "zotero_bridge" / "api_token"

    def runner(argv):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=30)

    with pytest.raises(OSError):
        bi.ensure_token(token, runner=runner, platform="win32")
    assert not token.exists()  # the new bearer token is not left on disk without its owner-only restriction
    assert list(token.parent.glob(".api_token.*")) == []  # and the temp file is removed as well


def test_a_timed_out_icacls_during_an_install_reports_failure_and_leaves_no_token(tmp_path, zotero):
    root, profile = zotero
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    host = FakeHost()

    def runner(argv):
        host.commands.append(list(argv))
        if argv and argv[0] == "icacls":
            raise subprocess.TimeoutExpired(cmd=argv, timeout=30)
        return 0

    installer = bi.ZoteroBridgeInstaller(
        make_bundle(tmp_path),
        data_dir=root,
        token_path=token,
        process_list=host.process_list,
        runner=runner,
        sleep=host.sleep,
        platform="win32",
        settings=accepted_store(tmp_path),
    )
    result = installer.install()
    assert result.status == bi.ST_FAILED
    assert not token.exists()
    assert_no_paths(result.message, profile, token, root)


def test_a_token_failure_after_the_close_says_zotero_must_be_started_again(tmp_path, zotero):
    root, profile = zotero
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    host = FakeHost(running=True, icacls_code=5)
    result = make_installer(tmp_path, make_bundle(tmp_path), host, data_root=root, token=token, platform="win32").install()
    assert result.status == bi.ST_FAILED
    assert result.closed_zotero is True
    assert bi.RESTART_MESSAGE in result.message
    assert host.commands[0] == ["taskkill", "/IM", "zotero.exe"]
    assert not token.exists()
    assert_no_paths(result.message, profile, token, root)


def test_the_temp_token_is_restricted_while_it_is_still_empty(tmp_path):
    token = tmp_path / "api_token"
    seen = {}

    def runner(argv):
        if argv and argv[0] == "icacls":
            seen["size"] = Path(argv[1]).stat().st_size
        return 0

    bi.ensure_token(token, runner=runner, platform="win32", env={"USERNAME": "alice"})
    assert seen["size"] == 0  # no token byte exists under the inherited ACL
    assert bi.token_is_usable(token)


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"USERDOMAIN": "CORP", "USERNAME": "alice"}, "CORP\\alice:F"),
        ({"USERNAME": "alice"}, "alice:F"),
    ],
)
def test_the_token_is_granted_by_domain_and_user_name_from_the_environment(tmp_path, env, expected):
    commands = []

    def runner(argv):
        commands.append(list(argv))
        return 0

    bi.ensure_token(tmp_path / "api_token", runner=runner, platform="win32", env=env)
    assert commands[0][2:] == ["/inheritance:r", "/grant:r", expected]


def test_the_bare_user_name_from_getpass_is_the_fallback_only_when_the_variables_are_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(bi.getpass, "getuser", lambda: "fallback-user")
    commands = []

    def runner(argv):
        commands.append(list(argv))
        return 0

    bi.ensure_token(tmp_path / "api_token", runner=runner, platform="win32", env={})
    assert commands[0][2:] == ["/inheritance:r", "/grant:r", "fallback-user:F"]


def test_a_usable_existing_token_is_kept(tmp_path):
    token = tmp_path / "api_token"
    token.write_text("sk_meridian_" + "a" * 40 + "\n", encoding="ascii")
    assert bi.ensure_token(token, runner=lambda argv: 0, platform="linux") == bi.TOKEN_KEPT
    assert token.read_text(encoding="ascii") == "sk_meridian_" + "a" * 40 + "\n"


# ----------------------------------------------------------------- user.js writes


def test_an_existing_api_pref_is_updated_and_other_lines_are_kept(tmp_path, zotero):
    root, profile = zotero
    token = tmp_path / "state" / "zotero_bridge" / "api_token"
    (profile / bi.USER_JS).write_text(
        '// the owner\'s own line\nuser_pref("browser.startup.page", 1);\n'
        'user_pref("extensions.zotero.meridianZoteroBridge.apiPort", 1111);\n',
        encoding="utf-8",
    )
    result = make_installer(tmp_path, make_bundle(tmp_path), FakeHost(), data_root=root, token=token).install()
    assert result.status == bi.ST_INSTALLED
    lines = (profile / bi.USER_JS).read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["// the owner's own line", 'user_pref("browser.startup.page", 1);']
    assert 'user_pref("extensions.zotero.meridianZoteroBridge.apiPort", 1111);' not in lines
    assert lines[-2:] == expected_api_lines(token)


def test_a_second_run_with_the_same_prefs_writes_the_same_bytes(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    token = tmp_path / "api_token"
    assert bi.ensure_api_prefs(profile, token) == bi.PREFS_ADDED
    first = (profile / bi.USER_JS).read_bytes()
    assert bi.ensure_api_prefs(profile, token) == bi.PREFS_PRESENT
    assert (profile / bi.USER_JS).read_bytes() == first


def test_a_crlf_user_js_keeps_its_line_endings(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / bi.USER_JS).write_bytes(b'user_pref("a.b", true);\r\n')
    assert bi.ensure_scan_pref(profile) == bi.SCAN_ADDED
    assert (profile / bi.USER_JS).read_bytes() == b'user_pref("a.b", true);\r\nuser_pref("extensions.startupScanScopes", 1);\r\n'


def test_a_scan_pref_already_at_one_is_left_alone(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    text = 'user_pref("extensions.startupScanScopes", 1);\n'
    (profile / bi.USER_JS).write_text(text, encoding="utf-8")
    assert bi.ensure_scan_pref(profile) == bi.SCAN_PRESENT
    assert (profile / bi.USER_JS).read_text(encoding="utf-8") == text


# ----------------------------------------------------------------- the bundle and the record


def test_a_bundle_record_must_name_this_add_on_and_a_plain_version(tmp_path):
    folder = tmp_path / "rec" / bi.BUNDLE_SUBDIR
    folder.mkdir(parents=True)
    record = {"addon_id": bi.ADDON_ID, "file": bi.XPI_FILENAME, "sha256": "0" * 64, "version": "0.1.8"}
    (folder / bi.BUNDLE_RECORD).write_text(json.dumps(record), encoding="utf-8")
    assert bi.load_bundled_bridge(folder).version == "0.1.8"
    record["version"] = "0.1.8-beta"
    (folder / bi.BUNDLE_RECORD).write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(bi.BundleError):
        bi.load_bundled_bridge(folder)


# ----------------------------------------------------------------- the settings fields


def test_licence_accepted_is_written_only_when_true(tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    assert store.load().settings.licence_accepted is False
    store.set_licence_accepted(True, notice_sha256=NOTICE_SHA)
    assert json.loads(path.read_text(encoding="utf-8"))["licence_accepted"] is True
    assert SettingsStore(path).load().settings.licence_accepted is True
    store.set_licence_accepted(False)
    assert "licence_accepted" not in json.loads(path.read_text(encoding="utf-8"))
    assert "licence_notice_sha256" not in json.loads(path.read_text(encoding="utf-8"))


def test_accepting_the_licence_keeps_every_other_field(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"check_updates": False, "gpu_hosts": ["gpu-a"]}), encoding="utf-8")
    SettingsStore(path).set_licence_accepted(True, notice_sha256=NOTICE_SHA)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["check_updates"] is False and saved["gpu_hosts"] == ["gpu-a"] and saved["licence_accepted"] is True


def test_a_licence_flag_that_is_not_true_or_false_warns_and_reads_false(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"licence_accepted": "yes"}), encoding="utf-8")
    result = SettingsStore(path).load()
    assert result.settings.licence_accepted is False
    assert result.warning is not None and "licence_accepted must be true or false" in result.warning


def test_setting_the_licence_refuses_a_non_boolean_and_writes_nothing(tmp_path):
    path = tmp_path / "settings.json"
    with pytest.raises(ValueError):
        SettingsStore(path).set_licence_accepted("yes")
    assert not path.exists()


def test_setting_the_licence_refuses_an_unreadable_file_and_keeps_a_copy(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(SettingsUnreadable):
        SettingsStore(path).set_licence_accepted(True, notice_sha256=NOTICE_SHA)
    assert path.read_text(encoding="utf-8") == "not json"
    assert (tmp_path / "settings.json.unreadable.bak").read_text(encoding="utf-8") == "not json"


def test_an_acceptance_needs_the_digest_of_the_notice_and_writes_nothing_without_it(tmp_path):
    path = tmp_path / "settings.json"
    with pytest.raises(ValueError):
        SettingsStore(path).set_licence_accepted(True)
    with pytest.raises(ValueError):
        SettingsStore(path).set_licence_accepted(True, notice_sha256="not-a-digest")
    assert not path.exists()


def test_a_stale_digest_is_not_an_acceptance_of_the_notice_shown_now(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"licence_accepted": True, "licence_notice_sha256": "0" * 64}), encoding="utf-8")
    settings = SettingsStore(path).load().settings
    assert settings.licence_accepted is True  # an older file still loads with its flag
    assert licence_accepted_for(settings, NOTICE_SHA) is False


def test_a_matching_digest_is_an_acceptance_of_the_notice_shown_now(tmp_path):
    path = tmp_path / "settings.json"
    SettingsStore(path).set_licence_accepted(True, notice_sha256=NOTICE_SHA)
    settings = SettingsStore(path).load().settings
    assert settings.licence_notice_sha256 == NOTICE_SHA
    assert licence_accepted_for(settings, NOTICE_SHA) is True


def test_an_acceptance_with_no_digest_asks_again(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"licence_accepted": True}), encoding="utf-8")
    result = SettingsStore(path).load()
    assert result.warning is None  # an older file has no digest, and that is not a warning
    assert licence_accepted_for(result.settings, NOTICE_SHA) is False


def test_a_digest_that_is_not_text_warns_and_is_ignored(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"licence_accepted": True, "licence_notice_sha256": 5}), encoding="utf-8")
    result = SettingsStore(path).load()
    assert result.settings.licence_notice_sha256 == ""
    assert result.warning is not None and "licence_notice_sha256 must be text" in result.warning
    assert licence_accepted_for(result.settings, NOTICE_SHA) is False


def test_the_update_check_and_the_force_close_opt_in_default_off_and_are_written_only_when_on(tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    settings = store.load().settings
    assert settings.zotero_update_checks is False
    assert settings.zotero_force_close_allowed is False
    store.set_zotero_update_checks(True)
    store.set_zotero_force_close_allowed(True)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["zotero_update_checks"] is True
    assert saved["zotero_force_close_allowed"] is True
    store.set_zotero_update_checks(False)
    store.set_zotero_force_close_allowed(False)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "zotero_update_checks" not in saved
    assert "zotero_force_close_allowed" not in saved


def test_the_two_switches_load_strictly_and_warn_on_anything_but_true_or_false(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"zotero_update_checks": "yes", "zotero_force_close_allowed": 1}), encoding="utf-8")
    result = SettingsStore(path).load()
    assert result.settings.zotero_update_checks is False
    assert result.settings.zotero_force_close_allowed is False
    assert result.warning is not None
    assert "zotero_update_checks must be true or false" in result.warning
    assert "zotero_force_close_allowed must be true or false" in result.warning


def test_a_true_switch_loads_as_true_with_no_warning(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"zotero_update_checks": True, "zotero_force_close_allowed": True}), encoding="utf-8")
    result = SettingsStore(path).load()
    assert result.settings.zotero_update_checks is True
    assert result.settings.zotero_force_close_allowed is True
    assert result.warning is None


def test_setting_a_switch_keeps_every_other_field_and_refuses_a_non_boolean(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"check_updates": False, "gpu_hosts": ["gpu-a"]}), encoding="utf-8")
    SettingsStore(path).set_zotero_update_checks(True)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["check_updates"] is False and saved["gpu_hosts"] == ["gpu-a"] and saved["zotero_update_checks"] is True
    with pytest.raises(ValueError):
        SettingsStore(path).set_zotero_force_close_allowed("yes")
    assert "zotero_force_close_allowed" not in json.loads(path.read_text(encoding="utf-8"))


def test_setting_a_switch_on_an_unreadable_file_keeps_a_copy_and_writes_nothing(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(SettingsUnreadable):
        SettingsStore(path).set_zotero_force_close_allowed(True)
    assert path.read_text(encoding="utf-8") == "not json"
    assert (tmp_path / "settings.json.unreadable.bak").read_text(encoding="utf-8") == "not json"


# ----------------------------------------------------------------- the build step


def _load_build():
    spec = importlib.util.spec_from_file_location("build_zotero_bridge_under_test", REPO / "packaging" / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_xpi(path: Path, *, addon_id=bi.ADDON_ID, version="0.1.8", not_a_zip=False, signature_entries=SIGNATURE_ENTRIES) -> Path:
    """A test .xpi with the given id and version, and the given signature entries (empty for an unsigned archive).

    The entries hold test bytes, not real signatures. The build checks their presence only.
    """
    if not_a_zip:
        path.write_bytes(b"this is not a zip archive")
        return path
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        manifest = {"manifest_version": 2, "version": version, "applications": {"zotero": {"id": addon_id}}}
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("bootstrap.js", "// test only\n")
        for entry in signature_entries:
            archive.writestr(entry, b"test bytes, not a signature")
    path.write_bytes(buffer.getvalue())
    return path


def test_build_with_no_path_succeeds_and_reports_no_bridge(tmp_path, capsys):
    build = _load_build()
    bundle_dir = tmp_path / "dist" / "MeridianDesktop"
    bundle_dir.mkdir(parents=True)
    assert build.bundle_zotero_bridge(None, bundle_dir) == "none"
    assert "No bundled bridge in this build" in capsys.readouterr().out
    assert not (bundle_dir / "zotero_bridge").exists()


def test_build_bundles_the_signed_file_with_its_record(tmp_path):
    build = _load_build()
    xpi = _make_xpi(tmp_path / "signed.xpi")
    bundle_dir = tmp_path / "dist" / "MeridianDesktop"
    bundle_dir.mkdir(parents=True)
    assert build.bundle_zotero_bridge(xpi, bundle_dir) == "bundled"
    folder = bundle_dir / bi.BUNDLE_SUBDIR
    bundled = bi.load_bundled_bridge(folder)
    assert bundled is not None and bundled.version == "0.1.8"
    assert bundled.sha256 == hashlib.sha256((folder / bi.XPI_FILENAME).read_bytes()).hexdigest()
    assert bundled.sha256 == hashlib.sha256(xpi.read_bytes()).hexdigest()


def test_build_refuses_a_missing_path(tmp_path):
    build = _load_build()
    with pytest.raises(SystemExit):
        build.bundle_zotero_bridge(tmp_path / "absent.xpi", tmp_path)


def test_build_refuses_an_empty_file(tmp_path):
    build = _load_build()
    empty = tmp_path / "empty.xpi"
    empty.write_bytes(b"")
    with pytest.raises(SystemExit):
        build.bundle_zotero_bridge(empty, tmp_path)


def test_build_refuses_a_file_for_another_add_on(tmp_path):
    build = _load_build()
    xpi = _make_xpi(tmp_path / "other.xpi", addon_id="someone@example.org")
    with pytest.raises(SystemExit):
        build.bundle_zotero_bridge(xpi, tmp_path / "dist")


def test_build_refuses_a_file_that_is_not_an_xpi(tmp_path):
    build = _load_build()
    xpi = _make_xpi(tmp_path / "bad.xpi", not_a_zip=True)
    with pytest.raises(SystemExit):
        build.bundle_zotero_bridge(xpi, tmp_path / "dist")


def test_build_refuses_an_unsigned_xpi_with_the_right_id_and_version(tmp_path):
    """The id and the version are right, but there are no Mozilla signature entries, so the file is not a signed .xpi."""
    build = _load_build()
    xpi = _make_xpi(tmp_path / "unsigned.xpi", signature_entries=())
    bundle_dir = tmp_path / "dist"
    with pytest.raises(SystemExit) as refused:
        build.bundle_zotero_bridge(xpi, bundle_dir)
    assert "mozilla.rsa" in str(refused.value)
    assert not (bundle_dir / bi.BUNDLE_SUBDIR).exists()


@pytest.mark.parametrize(
    "signature_entries",
    [("META-INF/mozilla.rsa",), ("META-INF/mozilla.sf",), ("META-INF/cose.sig", "META-INF/cose.manifest")],
)
def test_build_refuses_an_xpi_missing_either_required_signature_entry(tmp_path, signature_entries):
    build = _load_build()
    xpi = _make_xpi(tmp_path / "partial.xpi", signature_entries=signature_entries)
    with pytest.raises(SystemExit):
        build.bundle_zotero_bridge(xpi, tmp_path / "dist")


def test_build_accepts_an_xpi_that_holds_both_signature_entries(tmp_path):
    """A presence check only: the two entries are there, so the file is bundled. The signature itself is not verified."""
    build = _load_build()
    xpi = _make_xpi(tmp_path / "signed.xpi", signature_entries=("META-INF/mozilla.rsa", "META-INF/mozilla.sf"))
    bundle_dir = tmp_path / "dist" / "MeridianDesktop"
    bundle_dir.mkdir(parents=True)
    assert build.bundle_zotero_bridge(xpi, bundle_dir) == "bundled"
