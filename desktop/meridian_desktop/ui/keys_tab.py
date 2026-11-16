"""The Keys tab: the RunPod entry card and one card per saved key name, on a tab of its own. Qt code.

The tab is a scroll area around the Keys section (ui/keys_section.py), so a long list of names scrolls inside the tab and
never stretches the window. The RunPod panel (ui/runpod_panel.py) keeps its jobs and signals. Its entry card is handed to
the section, which places it first in the list, so the card looks and works as it did when it sat in Settings. The panel
itself stays hidden: it has no rows left, and it is kept only as the owner of the card's jobs.

The RunPod probe of the local server runs the first time this tab is shown, as it did when the card sat in Settings.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QFrame, QScrollArea, QWidget

from meridian_desktop import local_client
from meridian_desktop.api_keys import ApiKeys
from meridian_desktop.secret_store import SecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.keys_section import KeysSection
from meridian_desktop.ui.runpod_panel import RunPodPanel

# The width the Keys column never shrinks below, so a label wraps instead of being cut off. The same width as Settings.
KEYS_MIN_WIDTH = 640


class KeysTab(QScrollArea):
    """The Keys tab: the Keys section in a scroll area, with the RunPod card placed by the section."""

    def __init__(
        self,
        settings_store: SettingsStore,
        secret_store: SecretStore,
        parent: QWidget | None = None,
        *,
        client: Any = local_client,
        confirm: Callable[[QWidget, str, str], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("keys-tab")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        # The RunPod panel is a child of this tab, so it lives as long as the tab. The Yes or No before its key is removed
        # is the one passed here; the named keys keep their own questions.
        self._runpod = RunPodPanel(settings_store, secret_store, client=client, parent=self, confirm=confirm)
        section = KeysSection(ApiKeys(settings_store, secret_store), self._runpod.take_card())
        section.setMinimumWidth(KEYS_MIN_WIDTH)
        self._runpod.hide()
        self.setWidget(section)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        # The RunPod probe runs once, on the first show of this tab, as the card's own panel would have run it.
        self._runpod.ensure_probed()


__all__ = ["KEYS_MIN_WIDTH", "KeysTab"]
