"""The Home cards' texts, from one check result each. Pure Python: no Qt, no socket.

Every card says what it found in plain words, gives at most one action per row, and never shows a traceback, a path
or the old tab names. A card that cannot check says why and what to do.
"""

from datetime import datetime

from meridian_desktop import local_client, updater
from meridian_desktop.home_status import (
    ACTION_CHECK_TOOLS,
    ACTION_LAUNCH_ZOTERO,
    ACTION_RELEASE_PAGE,
    ACTION_SETTINGS,
    CHECK_AGAIN_LABEL,
    OPEN_SETTINGS_LABEL,
    SET_UP_IN_SETTINGS,
    TONE_ATTENTION,
    TONE_CHECKING,
    TONE_IDLE,
    TONE_OFF,
    TONE_OK,
    LocalFacts,
    ServiceFacts,
    checked_text,
    checking_view,
    failed_view,
    local_view,
    services_view,
    stalled_view,
    tools_view,
    tunnel_view,
    updates_view,
    zotero_view,
)
from meridian_desktop.latex_client import ENGINE_CONNECTED, ENGINE_NOT_ANSWERING, ENGINE_NOT_RUNNING, EngineStatus
from meridian_desktop.slot_ports import SlotPortResult
from meridian_desktop.tools_status import NO, NOT_SET, NOT_STARTED, UNCHECKED, YES, ToolRow, ToolsStatus
from meridian_desktop.tunnel_status import NOT_AVAILABLE_REASON, NOT_RUNNING, RUNNING, UNKNOWN, TunnelStatus
from meridian_desktop.zotero_client import API_DISABLED, API_NOT_RUNNING, API_ON, ApiCheck, ZoteroStatus

CLOCK = datetime(2026, 10, 23, 14, 5, 9)
URL = "http://127.0.0.1:7878"
FORBIDDEN = ("Not built yet", "Check services", "Writing", "Services tab", "Tunnel tab", "Tools tab")


def _all_words(view):
    parts = [view.headline, view.detail, view.checked, view.action_label]
    for row in view.rows:
        parts.extend([row.text, row.detail, row.action_label])
    return " ".join(parts)


def _assert_no_old_words(view):
    text = _all_words(view)
    for word in FORBIDDEN:
        assert word not in text, word


# ----------------------------------------------------------------- shared

def test_checked_text_reads_the_time_and_is_empty_before_a_check():
    assert checked_text(None) == ""
    assert checked_text(CLOCK) == "Checked at 14:05:09."


def test_checking_stalled_and_failed_views_say_what_is_happening():
    assert checking_view().tone == TONE_CHECKING
    stalled = stalled_view(60)
    assert stalled.headline == "Still checking"
    assert "more than 60 seconds" in stalled.detail
    assert stalled.tone == TONE_ATTENTION
    failed = failed_view("OSError", "Checked at 14:05:09.")
    assert failed.headline == "Could not check"
    assert failed.detail == "The check failed unexpectedly (OSError). Press Check now to try again."
    assert failed.checked == "Checked at 14:05:09."
    _assert_no_old_words(stalled)
    _assert_no_old_words(failed)


# ----------------------------------------------------------------- local server

def test_local_server_running_is_ok_on_its_port():
    view = local_view(LocalFacts(url=URL, health=local_client.HEALTH_RUNNING), checked="c")
    assert view.headline == "Running"
    assert view.detail == "The local Meridian server answers on port 7878."
    assert view.tone == TONE_OK


def test_local_server_not_running_says_how_to_start_it():
    view = local_view(LocalFacts(url=URL, health=local_client.HEALTH_NOT_RUNNING))
    assert view.headline == "Not running"
    assert "Nothing listens on port 7878" in view.detail
    assert "press Check now" in view.detail
    assert view.tone == TONE_ATTENTION
    _assert_no_old_words(view)


def test_another_program_on_the_port_is_named_as_such():
    view = local_view(LocalFacts(url=URL, health=local_client.HEALTH_NOT_MERIDIAN))
    assert view.headline == "Another program answers"
    assert "Port 7878 answers, but not with Meridian's health check" in view.detail


def test_an_address_that_is_not_loopback_is_refused_without_a_probe():
    view = local_view(LocalFacts(url="http://example.com:7878", refusal="The local server address must be 127.0.0.1, localhost or ::1."))
    assert view.headline == "Not checked"
    assert "must be 127.0.0.1" in view.detail
    assert "Change the Local URL in Settings" in view.detail


def test_local_server_with_no_usable_answer_says_could_not_check():
    view = local_view(LocalFacts(url=URL, health=local_client.HEALTH_ERROR))
    assert view.headline == "Could not check"
    assert "no usable answer from port 7878" in view.detail


def test_local_server_failed_read_shows_only_the_class_name():
    view = local_view(None, failure="RuntimeError")
    assert view.headline == "Could not check"
    assert "(RuntimeError)" in view.detail


# ----------------------------------------------------------------- services

def _engine(state):
    return EngineStatus(state=state, message="")


def test_services_engine_running_and_ports_counted():
    facts = ServiceFacts(
        engine_url="http://127.0.0.1:8471",
        engine=_engine(ENGINE_CONNECTED),
        ports=(SlotPortResult("a", 1, True), SlotPortResult("b", 2, False), SlotPortResult("c", 3, True)),
    )
    view = services_view(facts)
    assert view.headline == "LaTeX engine running"
    assert view.tone == TONE_OK
    assert view.rows[0].text == "LaTeX engine on port 8471."
    assert view.rows[1].text == "Slot ports: 2 of 3 listening on 127.0.0.1."
    _assert_no_old_words(view)


def test_services_engine_not_running_is_idle_not_an_error():
    facts = ServiceFacts(engine_url="http://127.0.0.1:8471", engine=_engine(ENGINE_NOT_RUNNING), ports=())
    view = services_view(facts)
    assert view.headline == "LaTeX engine not running"
    assert view.tone == TONE_IDLE
    assert "Start the LaTeX engine you installed" in view.rows[0].detail


def test_services_another_program_on_the_engine_port_needs_attention():
    facts = ServiceFacts(engine_url="http://127.0.0.1:8471", engine=_engine(ENGINE_NOT_ANSWERING), ports=())
    view = services_view(facts)
    assert view.headline == "Another program answers on the engine port"
    assert view.tone == TONE_ATTENTION


def test_services_a_failed_engine_read_is_named_and_the_ports_still_show():
    facts = ServiceFacts(
        engine_url="http://127.0.0.1:8471",
        engine=None,
        engine_failure="OSError",
        ports=(SlotPortResult("a", 1, True),),
    )
    view = services_view(facts)
    assert view.headline == "Could not check"
    assert view.rows[0].detail == "The check failed unexpectedly (OSError). Press Check now to try again."
    assert view.rows[1].text == "Slot ports: 1 of 1 listening on 127.0.0.1."


def test_services_with_no_facts_at_all_says_could_not_check():
    view = services_view(None, failure="ValueError")
    assert view.headline == "Could not check"
    assert "(ValueError)" in view.detail


# ----------------------------------------------------------------- tunnel

def test_tunnel_running_names_what_needs_it():
    view = tunnel_view(TunnelStatus(state=RUNNING, reason="The tunnel process is running.", checked_at=CLOCK))
    assert view.headline == "Running"
    assert view.tone == TONE_OK
    assert "codebase-memory" in view.detail


def test_tunnel_not_running_says_how_to_start_it_by_hand_and_names_the_confirm():
    view = tunnel_view(TunnelStatus(state=NOT_RUNNING, reason="The tunnel is stopped.", checked_at=CLOCK))
    assert view.headline == "Not running"
    assert view.tone == TONE_IDLE
    assert "Tunnel panel" in view.detail
    assert "asks first" in view.detail
    assert "pixi run" not in view.detail
    _assert_no_old_words(view)


def test_tunnel_with_no_core_setup_is_not_checked_not_unknown():
    view = tunnel_view(TunnelStatus(state=UNKNOWN, reason=NOT_AVAILABLE_REASON, checked_at=CLOCK))
    assert view.headline == "Not checked"
    assert view.tone == TONE_IDLE
    assert "Set them in Settings, then press Check now." in view.detail


def test_tunnel_unknown_with_a_real_reason_stays_unknown_and_attention():
    view = tunnel_view(TunnelStatus(state=UNKNOWN, reason="The state is odd.", checked_at=CLOCK))
    assert view.headline == "Unknown"
    assert view.tone == TONE_ATTENTION


def test_tunnel_failed_check_shows_the_class_name_only():
    view = tunnel_view(None, failure="TimeoutError")
    assert view.headline == "Could not check"
    assert "TimeoutError" in view.detail


# ----------------------------------------------------------------- zotero

def _zotero(exe, running, api_state=API_ON):
    return ZoteroStatus(exe=exe, process_running=running, api=ApiCheck(api_state, "message"))


def test_zotero_running_with_api_on_offers_no_launch():
    view = zotero_view(_zotero("C:/Zotero/zotero.exe", True))
    assert view.headline == "Running"
    assert view.detail == "The local API answers on 127.0.0.1:23119."
    assert view.action == ""


def test_zotero_not_running_offers_launch_when_installed():
    view = zotero_view(_zotero("C:/Zotero/zotero.exe", False, API_NOT_RUNNING))
    assert view.headline == "Not running"
    assert view.action == ACTION_LAUNCH_ZOTERO
    assert view.action_label == "Launch Zotero"


def test_zotero_unknown_process_still_offers_launch_when_installed():
    view = zotero_view(_zotero("C:/Zotero/zotero.exe", None))
    assert view.headline == "Process not checked"
    assert view.action == ACTION_LAUNCH_ZOTERO


def test_zotero_not_installed_says_where_to_get_it_and_offers_no_launch():
    view = zotero_view(_zotero(None, False))
    assert view.headline == "Not installed"
    assert view.tone == TONE_ATTENTION
    assert "zotero.org" in view.detail
    assert view.action == ""


def test_zotero_api_off_says_turn_it_on():
    view = zotero_view(_zotero("C:/Zotero/zotero.exe", True, API_DISABLED))
    assert "local API is off" in view.detail


def test_zotero_failed_check_shows_the_class_name_only():
    view = zotero_view(None, failure="OSError")
    assert view.headline == "Could not check"
    assert "(OSError)" in view.detail


# ----------------------------------------------------------------- tools

def _rows():
    return (
        ToolRow("Codex", "Meridian entries", NO, UNCHECKED, "Not in Codex's settings yet."),
        ToolRow("Claude Desktop", "Meridian entries", NOT_SET, UNCHECKED, "The Meridian core checkout is not set."),
        ToolRow("This computer", "Local Meridian server", YES, NO, "The local Meridian server is not running. Press Refresh."),
        ToolRow("This computer", "Tunnel", YES, NOT_STARTED, "The desktop did not check the tunnel."),
    )


def test_tools_every_row_has_one_action_and_the_row_words_match_the_status():
    view = tools_view(ToolsStatus(rows=_rows(), message="Checked. No tool is started or stopped.", checked_at=CLOCK))
    assert len(view.rows) == 4
    assert view.rows[0].text == "Codex: Meridian entries. Not configured."
    assert view.rows[2].text == "This computer: Local Meridian server. Configured but not live."
    actions = [(row.action, row.action_label) for row in view.rows]
    assert actions == [
        (ACTION_SETTINGS, SET_UP_IN_SETTINGS),
        (ACTION_SETTINGS, OPEN_SETTINGS_LABEL),
        (ACTION_CHECK_TOOLS, CHECK_AGAIN_LABEL),
        (ACTION_CHECK_TOOLS, CHECK_AGAIN_LABEL),
    ]


def test_tools_headline_counts_what_needs_attention():
    view = tools_view(ToolsStatus(rows=_rows(), message="m", checked_at=CLOCK))
    assert view.headline == "2 need attention"
    assert view.tone == TONE_ATTENTION


def test_tools_with_nothing_read_yet_is_not_checked():
    rows = (ToolRow("Codex", "Meridian entries", UNCHECKED, UNCHECKED, "x"),)
    view = tools_view(ToolsStatus(rows=rows, message="m"))
    assert view.headline == "Not checked yet"
    assert view.tone == TONE_IDLE


def test_tools_all_configured_and_live_is_set_up():
    rows = (ToolRow("This computer", "Local Meridian server", YES, YES, "answers"),)
    view = tools_view(ToolsStatus(rows=rows, message="m", checked_at=CLOCK))
    assert view.headline == "All set up"
    assert view.tone == TONE_OK


def test_tools_row_details_say_check_now_not_refresh():
    view = tools_view(ToolsStatus(rows=_rows(), message="Press Refresh to read the tools.", checked_at=CLOCK))
    assert "refresh" not in _all_words(view).lower()
    assert "Press Check now" in view.rows[2].detail
    assert view.detail == "Press Check now to read the tools."


def test_tools_failed_read_shows_the_class_name_only():
    view = tools_view(None, failure="RuntimeError")
    assert view.headline == "Could not check"
    assert "(RuntimeError)" in view.detail


# ----------------------------------------------------------------- updates

def test_updates_a_newer_release_is_ready_and_has_the_release_action():
    result = updater.UpdateResult("update_staged", version="0.3.0")
    view = updates_view("0.2.0", result, enabled=True, checking=False)
    assert view.headline == "Update ready: v0.3.0"
    assert view.action == ACTION_RELEASE_PAGE
    assert view.action_label == "Open release page"
    assert view.tone == TONE_ATTENTION
    assert "Restart to update" in view.detail


def test_updates_up_to_date_says_so():
    view = updates_view("0.2.0", updater.UpdateResult("up_to_date", version="0.2.0"), enabled=True, checking=False)
    assert view.headline == "Version 0.2.0"
    assert view.detail == "This is the latest published release."
    assert view.tone == TONE_OK
    assert view.action == ""


def test_updates_offline_says_why_and_to_try_later():
    view = updates_view(
        "0.2.0", updater.UpdateResult("offline", reason="GitHub is limiting requests right now."), enabled=True, checking=False
    )
    assert view.headline == "Could not check"
    assert view.detail == "GitHub is limiting requests right now. Try again later."


def test_updates_off_when_check_updates_is_off():
    view = updates_view("0.2.0", None, enabled=False, checking=False)
    assert view.tone == TONE_OFF
    assert "check_updates" in view.detail


def test_updates_while_the_one_check_runs_says_checking_then_not_checked():
    checking = updates_view("0.2.0", None, enabled=True, checking=True)
    assert checking.tone == TONE_CHECKING
    idle = updates_view("0.2.0", None, enabled=True, checking=False)
    assert idle.detail == "Not checked yet"
