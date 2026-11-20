"""POS slip layout tuner.

The slip equivalent of label_layout.py: a dialog that sets the receipt paper
width the slip is formatted for, plus the QR settings that have to match it.
Changes apply live and are written to settings.json immediately, so the next
slip picks them up with no restart.

Why width is a character count and not millimetres: ESC/POS positions text in
characters. Font A is 12 dots per character at 203 dpi, so an 80 mm head
(576 printable dots) fits 48 characters and a 58 mm head (384 dots) fits 32.
The printer's head width is set on the printer itself; this dialog can only
match it. Choosing wider than the head wraps every line and breaks the money
column — which is why the note at the bottom says so out loud.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QSpinBox, QVBoxLayout,
)

from vula_config import (
    DEFAULT_SLIP_WIDTH_CHARS,
    MAX_SLIP_WIDTH_CHARS,
    MIN_SLIP_WIDTH_CHARS,
)

#: (label shown in the combo, characters per line)
_PROFILE_ROWS = [
    ("58 mm  (32 characters)", 32),
    ("80 mm / 88 mm paper  (48 characters)", 48),
    ("112 mm / 110 mm paper  (64 characters)", 64),
]

#: Sentinel userData on the "Custom" row, so a preset is never confused with 0.
_CUSTOM = 0
_CUSTOM_LABEL = "Custom…"

_QR_MODES = [
    ("Raster — works on any printer", "raster"),
    ("Native QR command — known-good printers only", "native"),
    ("Off", "off"),
]


class SlipLayoutMixin:
    """See vula_app.py for composition."""

    def _open_pos_slip_layout_dialog(self) -> None:
        """Open the POS slip layout tuner.

        Bound to the "Slip layout…" button on the POS printer card. Every
        control writes through to self.pos_* and to disk immediately, so the
        operator can iterate with the test-slip button without a Save click.
        """
        pos_printer = self.printer_roles.get("pos_slip")

        dlg = QDialog(self)
        dlg.setWindowTitle("POS Slip — POS Printer")
        dlg.setMinimumSize(470, 430)
        dlg.setStyleSheet(f"background:{self.C_BG}; color:{self.C_TEXT};")

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(10)

        heading = QLabel("POS slip layout")
        heading.setStyleSheet(
            f"color:{self.C_TEXT}; font-size:13px; font-weight:700;"
        )
        layout.addWidget(heading)

        sub = QLabel(
            "Choose the paper width this slip is formatted for. "
            "Changes apply immediately — print a test slip to check."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color:{self.C_TEXT_DIM}; font-size:11px;")
        layout.addWidget(sub)

        form = QFormLayout()
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # ── Paper width ─────────────────────────────────────────────
        width_combo = QComboBox()
        width_combo.setStyleSheet(self._input_style())
        for text, chars in _PROFILE_ROWS:
            width_combo.addItem(text, chars)
        width_combo.addItem(_CUSTOM_LABEL, _CUSTOM)

        custom_spin = QSpinBox()
        custom_spin.setRange(MIN_SLIP_WIDTH_CHARS, MAX_SLIP_WIDTH_CHARS)
        custom_spin.setStyleSheet(self._input_style())
        custom_spin.setMinimumWidth(100)

        # Seed the controls from whatever is saved. An unrecognised value
        # (hand-edited settings.json) falls into Custom rather than being
        # silently snapped to a preset the operator never chose.
        current = int(getattr(self, "pos_width_chars", DEFAULT_SLIP_WIDTH_CHARS))
        preset_index = None
        for idx, (_text, chars) in enumerate(_PROFILE_ROWS):
            if chars == current:
                preset_index = idx
                break

        if preset_index is None:
            width_combo.setCurrentIndex(width_combo.count() - 1)
            custom_spin.setValue(
                max(MIN_SLIP_WIDTH_CHARS, min(MAX_SLIP_WIDTH_CHARS, current))
            )
            custom_spin.setEnabled(True)
            custom_spin.setToolTip(
                "Characters per line at Font A. 58 mm ≈ 32, 80 mm ≈ 48, "
                "112 mm ≈ 64."
            )
        else:
            width_combo.setCurrentIndex(preset_index)
            custom_spin.setValue(current)
            custom_spin.setEnabled(False)
            custom_spin.setToolTip("Set by the paper width preset.")

        form.addRow("Paper width:", width_combo)
        form.addRow("Characters per line:", custom_spin)

        # ── QR ─────────────────────────────────────────────────────
        qr_mode_combo = QComboBox()
        qr_mode_combo.setStyleSheet(self._input_style())
        for text, value in _QR_MODES:
            qr_mode_combo.addItem(text, value)
        mode = str(getattr(self, "pos_qr_mode", "raster") or "raster").lower()
        for idx in range(qr_mode_combo.count()):
            if qr_mode_combo.itemData(idx) == mode:
                qr_mode_combo.setCurrentIndex(idx)
                break
        form.addRow("QR mode:", qr_mode_combo)

        qr_px_spin = QSpinBox()
        qr_px_spin.setRange(2, 10)
        qr_px_spin.setValue(int(getattr(self, "pos_qr_module_px", 4) or 4))
        qr_px_spin.setStyleSheet(self._input_style())
        qr_px_spin.setToolTip(
            "Pixels per QR module in raster mode. Larger is easier to scan but "
            "takes more of the slip width."
        )
        qr_px_spin.setMinimumWidth(100)
        form.addRow("QR module size:", qr_px_spin)

        layout.addLayout(form)

        note = QLabel(
            "The printer's physical width is set on the printer itself, not here. "
            "Match it: a 58 mm head prints 32 characters in Font A, an 80 mm head "
            "48. Choosing wider than the head wraps every line and breaks the "
            "right-hand money column."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{self.C_TEXT_DIM}; font-size:10px;")
        layout.addWidget(note)

        layout.addStretch()

        # ── Live-apply: every control writes through ────────────────
        def _apply(*_args) -> None:
            preset = width_combo.currentData()
            if preset in (None, _CUSTOM):
                self.pos_width_chars = int(custom_spin.value())
            else:
                self.pos_width_chars = int(preset)
            self.pos_qr_mode = str(qr_mode_combo.currentData() or "raster")
            self.pos_qr_module_px = int(qr_px_spin.value())
            self.save_settings()

        def _on_width_changed(_index: int) -> None:
            preset = width_combo.currentData()
            is_custom = preset in (None, _CUSTOM)
            custom_spin.setEnabled(is_custom)
            if is_custom:
                custom_spin.setToolTip(
                    "Characters per line at Font A. 58 mm ≈ 32, 80 mm ≈ 48, "
                    "112 mm ≈ 64."
                )
            else:
                # Mirror the preset so the field always shows the effective
                # value. Signals blocked: this is display, not user input.
                custom_spin.blockSignals(True)
                custom_spin.setValue(int(preset))
                custom_spin.blockSignals(False)
                custom_spin.setToolTip("Set by the paper width preset.")
            _apply()

        width_combo.currentIndexChanged.connect(_on_width_changed)
        custom_spin.valueChanged.connect(_apply)
        qr_mode_combo.currentIndexChanged.connect(_apply)
        qr_px_spin.valueChanged.connect(_apply)

        # ── Buttons ────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        reset_btn = QPushButton("Reset defaults")
        reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_btn.setStyleSheet(self._btn_secondary())
        reset_btn.setToolTip("Restore the built-in slip layout.")
        btn_row.addWidget(reset_btn)

        btn_row.addStretch()

        test_btn = QPushButton("Print test slip")
        test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        test_btn.setStyleSheet(self._btn_primary())
        test_btn.setEnabled(bool(pos_printer))
        test_btn.setToolTip(
            "Send a six-item sample slip to the POS printer so you can verify "
            "the current width."
            if pos_printer
            else "Assign a POS printer first."
        )
        btn_row.addWidget(test_btn)

        close_btn = QPushButton("Close")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(self._btn_secondary())
        btn_row.addWidget(close_btn)

        layout.addLayout(btn_row)

        def _reset() -> None:
            for idx, (_text, chars) in enumerate(_PROFILE_ROWS):
                if chars == DEFAULT_SLIP_WIDTH_CHARS:
                    width_combo.setCurrentIndex(idx)
                    break
            else:
                width_combo.setCurrentIndex(width_combo.count() - 1)
                custom_spin.setValue(DEFAULT_SLIP_WIDTH_CHARS)
            for idx in range(qr_mode_combo.count()):
                if qr_mode_combo.itemData(idx) == "raster":
                    qr_mode_combo.setCurrentIndex(idx)
                    break
            qr_px_spin.setValue(4)
            _apply()

        def _test_print() -> None:
            if not self.printer_roles.get("pos_slip"):
                QMessageBox.warning(
                    self, "No Printer",
                    "Assign a POS slip printer before printing a test slip."
                )
                return
            self.print_test_pos_slip()

        reset_btn.clicked.connect(_reset)
        test_btn.clicked.connect(_test_print)
        close_btn.clicked.connect(dlg.accept)

        dlg.exec()