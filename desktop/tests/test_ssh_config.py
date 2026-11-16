"""The ssh config suggestions for the GPU hosts: the parser (meridian_desktop/ssh_config.py), and the Settings button that
adds them to the box without saving. Offscreen Qt, temporary folders, an in-memory keychain. No real ~/.ssh is read."""

from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton

from _fakes import MemoryKeyring
from meridian_desktop import ssh_config
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui import settings_tab
from meridian_desktop.ui.runner_actions import RunnerActions
from meridian_desktop.ui.settings_tab import SSH_ADDED_TEXT, SSH_NONE_FOUND_TEXT, SettingsTab

CONFIG = """
# the owner's own file
Host gpu-box lab-a100
    HostName 10.0.0.5
    ProxyCommand ssh -W %h:%p bastion
Host *
    User someone
Host !bad good.one
Host dev?
Host = box1
HostName not-a-host
"""


def test_the_host_lines_give_their_usable_names_in_file_order():
    assert ssh_config.host_names(CONFIG) == ("gpu-box", "lab-a100", "good.one", "box1")


def test_wildcards_negations_comments_and_other_keywords_are_never_names():
    text = "Host *\nHost ?box\nHost !neg\nHostName nope\nUser nope\n# Host commented\n"
    assert ssh_config.host_names(text) == ()


def test_a_missing_config_gives_no_names_and_raises_nothing(tmp_path):
    assert ssh_config.read_host_names(home=tmp_path) == ()


def test_the_names_come_from_the_config_in_the_home_folder(tmp_path):
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "config").write_text("Host gpu-box\nHost lab\n", encoding="utf-8")

    assert ssh_config.read_host_names(home=tmp_path) == ("gpu-box", "lab")


def test_the_default_path_is_the_ssh_config_under_the_home_folder(tmp_path):
    assert ssh_config.default_config_path(home=tmp_path) == tmp_path / ".ssh" / "config"


def _no_list(*args):
    return []


def _tab(qtbot, settings_path):
    actions = RunnerActions(SettingsStore(settings_path), locate=lambda store: None, choose_folder=lambda parent, start: "")
    tab = SettingsTab(
        SettingsStore(settings_path),
        secrets=KeyringSecretStore(backend=MemoryKeyring()),
        list_hosted=_no_list,
        actions=actions,
    )
    qtbot.addWidget(tab)
    return tab


def test_the_button_adds_the_config_names_to_the_box_and_saves_nothing(qtbot, settings_path, monkeypatch):
    monkeypatch.setattr(settings_tab, "read_ssh_config_names", lambda: ("gpu-box", "lab"))
    tab = _tab(qtbot, settings_path)
    field = tab.findChild(QLineEdit, "settings-gpu-hosts")
    field.setText("typed-by-hand")

    tab.findChild(QPushButton, "settings-add-ssh-names").click()

    assert field.text() == "typed-by-hand, gpu-box, lab"
    message = tab.findChild(QLabel, "settings-gpu-hosts-message")
    assert message.text() == SSH_ADDED_TEXT.format(count=2, names="gpu-box, lab")
    assert SettingsStore(settings_path).load().settings.gpu_hosts == ()


def test_the_button_says_so_when_the_config_has_no_names(qtbot, settings_path, monkeypatch):
    monkeypatch.setattr(settings_tab, "read_ssh_config_names", lambda: ())
    tab = _tab(qtbot, settings_path)

    tab.findChild(QPushButton, "settings-add-ssh-names").click()

    assert tab.findChild(QLabel, "settings-gpu-hosts-message").text() == SSH_NONE_FOUND_TEXT
