"""Label layout tuner.

Adds a "Layout…" button to the Label printer card that opens a dialog
for nudging the TSPL label content on the physical label. Changes apply
live to self.label_layout and are saved to settings.json immediately,
so the next print job picks them up with no restart.

The dialog also has a "Print test label" button that closes the
iterating loop: adjust → print → look → adjust.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QFormLayout, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QSpinBox, QVBoxLayout,
)

from vula_config import DEFAULT_LABEL_LAYOUT


# (setting key, human label, min, max, tooltip)
_FIELDS = [
    ("left_margin_dots", "Left margin (X)",
     0, 320, "Left edge of the label content, in dots. Increase to move right."),
    ("top_offset_dots", "Top offset (Y)",
     -60, 200, "Vertical shift for the whole label, in dots. Rarely needed."),
    ("label_width_dots", "Label width",
     200, 600, "Physical label width in dots (203 dpi → 40 mm ≈ 320)."),
    ("barcode_narrow", "Barcode narrow module",
     1, 4, "Width of the thin bars in the Code 39 barcode."),
    ("barcode_wide", "Barcode wide module",
     2, 8, "Width of the thick bars in the Code 39 barcode."),
]


class LabelLayoutMixin:
    """See vula_app.py for composition."""

    def _open_label_layout_dialog(self) -> None:
        """Open the label layout tuner.

        Bound to the "Layout…" button on the Label printer card. Every
        spinbox change immediately updates self.label_layout and writes
        to disk, so the user can iterate with the test-print button
        without needing a Save click.
        """
        label_printer = self.printer_roles.get("label")

        dlg = QDialog(self)
        dlg.setWindowTitle("Label Layout — Label Printer")
        dlg.setMinimumSize(440, 400)
        dlg.setStyleSheet(f"background:{self.C_BG}; color:{self.C_TEXT};")

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(10)

        heading = QLabel("Label layout")
        heading.setStyleSheet(
            f"color:{self.C_TEXT}; font-size:13px; font-weight:700;"
        )
        layout.addWidget(heading)

        sub = QLabel(
            "Adjust where the label content sits on the physical label. "
            "Changes apply immediately — print a test label to check."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color:{self.C_TEXT_DIM}; font-size:11px;")
        layout.addWidget(sub)

        form = QFormLayout()
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        spinboxes = {}

        def _make_spin(key: str, lo: int, hi: int, tooltip: str) -> QSpinBox:
            sb = QSpinBox()
            sb.setRange(lo, hi)
            sb.setValue(int(self.label_layout.get(key, DEFAULT_LABEL_LAYOUT[key])))
            sb.setStyleSheet(self._input_style())
            sb.setToolTip(tooltip)
            sb.setMinimumWidth(100)
            return sb

        for key, label, lo, hi, tip in _FIELDS:
            sb = _make_spin(key, lo, hi, tip)
            spinboxes[key] = sb
            form.addRow(f"{label}:", sb)

        layout.addLayout(form)

        dots_note = QLabel(
            "Units are printer dots at 203 dpi — about 8 dots per mm. "
            "The default 30-dot left margin is roughly 3.75 mm from the "
            "label edge."
        )
        dots_note.setWordWrap(True)
        dots_note.setStyleSheet(f"color:{self.C_TEXT_DIM}; font-size:10px;")
        layout.addWidget(dots_note)

        layout.addStretch()

        # ── Live-apply: every spinbox change writes through ─────────
        def _apply(_value: int) -> None:
            for k, sb in spinboxes.items():
                self.label_layout[k] = int(sb.value())
            self.save_settings()

        for sb in spinboxes.values():
            sb.valueChanged.connect(_apply)

        # ── Buttons ─────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        reset_btn = QPushButton("Reset defaults")
        reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_btn.setStyleSheet(self._btn_secondary())
        reset_btn.setToolTip("Restore the built-in default label layout.")
        btn_row.addWidget(reset_btn)

        btn_row.addStretch()

        test_btn = QPushButton("Print test label")
        test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        test_btn.setStyleSheet(self._btn_primary())
        test_btn.setEnabled(bool(label_printer))
        test_btn.setToolTip(
            "Send a sample label to the Label printer so you can verify "
            "the current layout."
            if label_printer
            else "Assign a Label printer first."
        )
        btn_row.addWidget(test_btn)

        close_btn = QPushButton("Close")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(self._btn_secondary())
        btn_row.addWidget(close_btn)

        layout.addLayout(btn_row)

        def _reset() -> None:
            for k, sb in spinboxes.items():
                sb.setValue(int(DEFAULT_LABEL_LAYOUT[k]))
            _apply(0)

        reset_btn.clicked.connect(_reset)
        test_btn.clicked.connect(self._label_layout_test_print)
        close_btn.clicked.connect(dlg.accept)

        dlg.exec()

    def _label_layout_test_print(self) -> None:
        """Called from the Layout dialog — prints one sample label."""
        if not self.printer_roles.get("label"):
            QMessageBox.warning(
                self, "No Printer",
                "Assign a Label printer before printing a test label."
            )
            return
        self.print_test_label_standalone()