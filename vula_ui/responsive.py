"""Responsive layout behaviour for VulaPrintApp.

Handles:
  * Breakpoint detection (small / medium / large) based on window width.
  * Queue table column hiding on narrow widths.
  * Printer card grid reflow (2 per row → 1 per row).
  * Emoji font detection with plain-text fallback.

The mixin is intentionally self-contained: it reaches into widgets that
other mixins created (self.requests_table, self._printer_card_grid) and
tolerates their absence so it can be called safely at any point in startup.
"""
from __future__ import annotations

import subprocess
from typing import Optional


# ── Emoji detection (cached for process lifetime) ───────────────

_EMOJI_OK_CACHE: Optional[bool] = None


def _detect_emoji_support() -> bool:
    """Return True if the system has an emoji font installed."""
    try:
        result = subprocess.run(
            ["fc-list"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return False
        out = (result.stdout or "").lower()
        return ("emoji" in out) or ("symbola" in out)
    except Exception:
        return False


def emoji_ok() -> bool:
    global _EMOJI_OK_CACHE
    if _EMOJI_OK_CACHE is None:
        _EMOJI_OK_CACHE = _detect_emoji_support()
    return _EMOJI_OK_CACHE


# ── Breakpoints ─────────────────────────────────────────────────

SMALL_MAX = 900     # window narrower than this → small
MEDIUM_MAX = 1200   # between SMALL_MAX and MEDIUM_MAX → medium
                    # wider than MEDIUM_MAX → large

# Column indices in the queue table
QUEUE_COL_TYPE = 0
QUEUE_COL_ID = 1
QUEUE_COL_STORE = 2
QUEUE_COL_SOURCE = 3
QUEUE_COL_SUMMARY = 4
QUEUE_COL_STATUS = 5
QUEUE_COL_CREATED = 6
QUEUE_COL_ACTION = 7


class ResponsiveMixin:
    """See vula_app.py for composition."""

    # ── Entry point ─────────────────────────────────────────────

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_responsive_layout()
        # Sidebar width is independent of the content breakpoints above —
        # it has its own tier thresholds in ThemeMixin._responsive_sidebar_width
        # (1000 / 1300 / 1900 px), which don't align with the small /
        # medium / large breakpoints that control column hiding. So we
        # fire the width update on every resize, not just on breakpoint
        # changes. The method itself is cheap: it only calls setFixedWidth
        # when the target width actually differs from the current one.
        self._update_sidebar_width()

    def _current_breakpoint(self) -> str:
        w = self.width() if self.width() > 0 else 9999
        if w < SMALL_MAX:
            return "small"
        if w < MEDIUM_MAX:
            return "medium"
        return "large"

    def _apply_responsive_layout(self) -> None:
        bp = self._current_breakpoint()
        if getattr(self, "_last_breakpoint", None) == bp:
            return
        self._last_breakpoint = bp
        self._apply_queue_columns(bp)
        self._reflow_printer_cards(bp)

    # ── Queue table columns ─────────────────────────────────────

    def _apply_queue_columns(self, breakpoint: str) -> None:
        table = getattr(self, "requests_table", None)
        if table is None:
            return

        if breakpoint == "small":
            hidden = {QUEUE_COL_STORE, QUEUE_COL_SOURCE, QUEUE_COL_CREATED}
        elif breakpoint == "medium":
            hidden = {QUEUE_COL_SOURCE, QUEUE_COL_CREATED}
        else:
            hidden = set()

        for idx in range(table.columnCount()):
            table.setColumnHidden(idx, idx in hidden)

    # ── Printer card grid reflow ────────────────────────────────

    def _reflow_printer_cards(self, breakpoint: str) -> None:
        grid = getattr(self, "_printer_card_grid", None)
        widgets = getattr(self, "_printer_card_widgets", None)
        if grid is None or not widgets:
            return

        # Remove all cards from the grid first, then re-add at target positions.
        for card in widgets.values():
            grid.removeWidget(card)

        if breakpoint == "small":
            for i, card in enumerate(widgets.values()):
                grid.addWidget(card, i, 0, 1, 2)
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 0)
        else:
            for i, card in enumerate(widgets.values()):
                grid.addWidget(card, i // 2, i % 2)
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1)

    # ── Emoji-aware icons ───────────────────────────────────────

    def _icon_for_type(self, type_key: str) -> str:
        if emoji_ok():
            return {
                "label": "🏷️",
                "pos_slip": "🧾",
                "pos_eod": "📊",
            }.get(type_key, "•")
        return {
            "label": "[L]",
            "pos_slip": "[P]",
            "pos_eod": "[E]",
        }.get(type_key, "[ ]")
