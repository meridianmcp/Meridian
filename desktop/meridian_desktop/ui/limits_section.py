"""The Limits block on the Settings tab: the download cap, typed as a number with a unit (MB, GB or TB). Qt code, in ui/.

The owner types a number and presses Enter, or moves to another field, and the value is saved. The unit only changes how
the number is shown: the settings file always keeps megabytes (settings_store.LIMIT_SPECS), so the store clamps the saved
value to its range. Changing the unit shows the saved value in the new unit and saves nothing.

Enforcement happens on the server and is not active yet, so this value does not stop a download now.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QGridLayout, QLabel, QVBoxLayout, QWidget

from meridian_desktop.settings_store import LIMIT_SPECS, SettingsStore, SettingsUnreadable
from meridian_desktop.ui.text import make_selectable

LIMITS_TITLE = "Limits"
LIMITS_INTRO = (
    "The download cap is saved on this PC. Type a number and press Enter, and pick MB, GB or TB. "
    "It is not enforced yet, so it does not stop a download now."
)
NOT_SAVED_TEXT = "Not saved: the settings file could not be written ({name})."
# The units a limit can be typed in, with how many megabytes one of them is. The settings file keeps megabytes.
UNITS = (("MB", 1), ("GB", 1024), ("TB", 1024 * 1024))


@dataclass(frozen=True)
class LimitRow:
    """One limit on the Limits block: the settings key it saves to, its caption, and the unit it opens in."""

    key: str
    caption: str
    unit: str = "MB"


LIMIT_ROWS = (LimitRow("limit_zip_download_mb", "Download cap (estimated zip size)"),)


def to_megabytes(amount: float, factor: int) -> int:
    """The typed amount in megabytes, rounded to a whole megabyte, which is what the settings file keeps."""
    return round(amount * factor)


def from_megabytes(megabytes: int, factor: int) -> float:
    """The saved megabytes as a number in the chosen unit."""
    return megabytes / factor


def _plain(number: float) -> str:
    """A number with thousands separators and at most two decimals, with no trailing zeros."""
    text = f"{number:,.2f}".rstrip("0").rstrip(".")
    return text or "0"


class LimitsSection(QWidget):
    """The Limits block. Build it once; call refresh() to show the saved values again."""

    def __init__(self, store: SettingsStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("limits-section")
        self._store = store
        self._specs = {spec.key: spec for spec in LIMIT_SPECS}
        self._amounts: dict[str, QDoubleSpinBox] = {}
        self._units: dict[str, QComboBox] = {}
        self._ranges: dict[str, QLabel] = {}

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        intro = make_selectable(QLabel(LIMITS_INTRO, self))
        intro.setObjectName("limits-intro")
        intro.setWordWrap(True)
        column.addWidget(intro)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(0, 1)
        for index, row in enumerate(LIMIT_ROWS):
            self._add_row(grid, index, row)
        column.addLayout(grid)

        self._status = make_selectable(QLabel(self))
        self._status.setObjectName("limits-status")
        self._status.setWordWrap(True)
        self._status.setVisible(False)
        column.addWidget(self._status)
        self.refresh()

    # ----------------------------------------------------------- layout

    def _add_row(self, grid: QGridLayout, index: int, row: LimitRow) -> None:
        caption = make_selectable(QLabel(row.caption, self))
        caption.setObjectName(f"limit-caption-{row.key}")
        caption.setWordWrap(True)
        grid.addWidget(caption, index, 0)

        amount = QDoubleSpinBox(self)
        amount.setObjectName(f"limit-amount-{row.key}")
        amount.setAccessibleName(row.caption)
        amount.setDecimals(4)  # enough for a terabyte value in a few megabytes, such as 0.005 TB
        amount.setMinimum(0.0)
        amount.setMaximum(1e12)
        amount.setKeyboardTracking(False)  # the value is read when the owner presses Enter or leaves the box
        amount.editingFinished.connect(lambda key=row.key: self._on_amount_entered(key))
        self._amounts[row.key] = amount
        grid.addWidget(amount, index, 1)

        unit = QComboBox(self)
        unit.setObjectName(f"limit-unit-{row.key}")
        unit.setAccessibleName(f"Unit for {row.caption}")
        for name, _factor in UNITS:
            unit.addItem(name)
        unit.setCurrentIndex(self._unit_index(row.unit))
        unit.currentIndexChanged.connect(lambda _index, key=row.key: self._on_unit_changed(key))
        self._units[row.key] = unit
        grid.addWidget(unit, index, 2)

        allowed = make_selectable(QLabel(self))
        allowed.setObjectName(f"limit-range-{row.key}")
        self._ranges[row.key] = allowed
        grid.addWidget(allowed, index, 3)

    @staticmethod
    def _unit_index(name: str) -> int:
        return next(index for index, (unit, _factor) in enumerate(UNITS) if unit == name)

    def _factor(self, key: str) -> int:
        return UNITS[self._units[key].currentIndex()][1]

    def _unit_name(self, key: str) -> str:
        return UNITS[self._units[key].currentIndex()][0]

    # ----------------------------------------------------------- showing the saved values

    def refresh(self) -> None:
        """Show the saved values in the chosen unit. Showing them never saves anything."""
        settings = self._store.load().settings
        for row in LIMIT_ROWS:
            amount = self._amounts[row.key]
            amount.blockSignals(True)  # showing the saved value must not save it again
            amount.setValue(from_megabytes(getattr(settings, row.key), self._factor(row.key)))
            amount.blockSignals(False)
            self._show_range(row.key)

    def _show_range(self, key: str) -> None:
        spec = self._specs[key]
        factor = self._factor(key)
        low = _plain(from_megabytes(spec.minimum, factor))
        high = _plain(from_megabytes(spec.maximum, factor))
        self._ranges[key].setText(f"{low} to {high} {self._unit_name(key)}")

    # ----------------------------------------------------------- saving

    def _on_amount_entered(self, key: str) -> None:
        """Save the typed amount, converted to megabytes. The store clamps it to the range, and the box shows what was kept."""
        megabytes = to_megabytes(self._amounts[key].value(), self._factor(key))
        try:
            self._store.set_limit(key, megabytes)
        except SettingsUnreadable as exc:
            # The text is the owner's message: a copy of the unreadable file is kept, and nothing is written.
            self._show_status(str(exc))
        except OSError as exc:
            self._show_status(NOT_SAVED_TEXT.format(name=exc.__class__.__name__))
        else:
            self._show_status("")
        self.refresh()

    def _on_unit_changed(self, key: str) -> None:
        """Show the saved value in the new unit and save nothing."""
        self._show_range(key)
        self.refresh()

    def _show_status(self, text: str) -> None:
        self._status.setText(text)
        self._status.setVisible(bool(text))


__all__ = ["LIMITS_INTRO", "LIMITS_TITLE", "LIMIT_ROWS", "LimitRow", "LimitsSection", "UNITS", "from_megabytes", "to_megabytes"]
