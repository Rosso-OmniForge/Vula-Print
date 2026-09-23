#!/usr/bin/env python3
"""TSPL renderer — parses TSPL command strings and renders them as QPixmaps.

Used only by the visual label preview dialog. Not involved in actual printing.
"""
from __future__ import annotations

import re
from typing import Dict

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPixmap


_CODE39_TABLE: Dict[str, str] = {
    '0': '000110100', '1': '100100001', '2': '001100001', '3': '101100000',
    '4': '000110001', '5': '100110000', '6': '001110000', '7': '000100101',
    '8': '100100100', '9': '001100100', 'A': '100001001', 'B': '001001001',
    'C': '101001000', 'D': '000011001', 'E': '100011000', 'F': '001011000',
    'G': '000001101', 'H': '100001100', 'I': '001001100', 'J': '000011100',
    'K': '100000011', 'L': '001000011', 'M': '101000010', 'N': '000010011',
    'O': '100010010', 'P': '001010010', 'Q': '000000111', 'R': '100000110',
    'S': '001000110', 'T': '000010110', 'U': '110000001', 'V': '011000001',
    'W': '111000000', 'X': '010010001', 'Y': '110010000', 'Z': '011010000',
    '-': '010000101', '.': '110000100', ' ': '011000100', '$': '010101000',
    '/': '010100010', '+': '010001010', '%': '000101010', '*': '010010100',
}


class TSPLRenderer:
    """
    Parses a TSPL command string and renders it to a QPixmap using QPainter.
    Supports: CLS, TEXT, BAR, BARCODE (Code 39 / 3of9), BOX commands.
    """

    SCALE: float = 2.5     # dots → screen pixels
    DOT_W: int   = 320     # label width  (40 mm @ 203 dpi)
    DOT_H: int   = 240     # label height (30 mm @ 203 dpi)

    # TSPL built-in font → (char_width_dots, char_height_dots)
    _FONT_DIMS: Dict[str, tuple] = {
        '1': (8,  10),
        '2': (12, 20),
        '3': (16, 24),
        '4': (24, 32),
        '5': (32, 48),
    }

    # ------------------------------------------------------------------ #
    def render(self, tspl: str) -> QPixmap:
        """Return a QPixmap with the label rendered at SCALE×."""
        px_w = int(self.DOT_W * self.SCALE)
        px_h = int(self.DOT_H * self.SCALE)

        pixmap = QPixmap(px_w, px_h)
        pixmap.fill(Qt.GlobalColor.white)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        # Label border
        border_pen = QPen(QColor('#888888'))
        border_pen.setWidth(2)
        painter.setPen(border_pen)
        painter.drawRect(1, 1, px_w - 2, px_h - 2)

        for raw_line in tspl.splitlines():
            self._dispatch(painter, raw_line.strip())

        painter.end()
        return pixmap

    # ------------------------------------------------------------------ #
    def _s(self, dots: int) -> int:
        """Scale dots → integer pixels."""
        return int(dots * self.SCALE)

    # ------------------------------------------------------------------ #
    def _dispatch(self, painter: QPainter, line: str) -> None:
        # TEXT  x,y,"font",rotation,xmul,ymul,"data"
        m = re.match(r'TEXT\s+(\d+),(\d+),"(\w+)",(\d+),(\d+),(\d+),"(.*)"', line)
        if m:
            x, y   = int(m.group(1)), int(m.group(2))
            font   = m.group(3)
            xm, ym = int(m.group(5)), int(m.group(6))
            text   = m.group(7).replace('\\"', '"').replace('\\\\', '\\')
            self._draw_text(painter, x, y, font, xm, ym, text)
            return

        # BAR  x,y,width,height
        m = re.match(r'BAR\s+(\d+),(\d+),(\d+),(\d+)', line)
        if m:
            x, y = self._s(int(m.group(1))), self._s(int(m.group(2)))
            w, h = max(1, self._s(int(m.group(3)))), max(1, self._s(int(m.group(4))))
            painter.fillRect(x, y, w, h, QColor('black'))
            return

        # BARCODE  x,y,"type",height,human,rotation,narrow,wide,"data"
        m = re.match(
            r'BARCODE\s+(\d+),(\d+),"(\w+)",(\d+),(\d+),(\d+),(\d+),(\d+),"(.*)"', line
        )
        if m:
            x, y   = int(m.group(1)), int(m.group(2))
            btype  = m.group(3)
            height = int(m.group(4))
            narrow = int(m.group(7))
            wide   = int(m.group(8))
            data   = m.group(9).replace('\\"', '"').replace('\\\\', '\\')
            if '39' in btype or '3OF9' in btype.upper():
                self._draw_code39(painter, x, y, height, narrow, wide, data)
            return

        # BOX  x1,y1,x2,y2,thickness
        m = re.match(r'BOX\s+(\d+),(\d+),(\d+),(\d+),(\d+)', line)
        if m:
            x1, y1 = self._s(int(m.group(1))), self._s(int(m.group(2)))
            x2, y2 = self._s(int(m.group(3))), self._s(int(m.group(4)))
            t      = max(1, self._s(int(m.group(5))))
            box_pen = QPen(QColor('black'))
            box_pen.setWidth(t)
            painter.setPen(box_pen)
            painter.drawRect(x1, y1, x2 - x1, y2 - y1)

    # ------------------------------------------------------------------ #
    def _draw_text(self, painter: QPainter, x: int, y: int,
                   font: str, xmul: int, ymul: int, text: str) -> None:
        dims    = self._FONT_DIMS.get(font, (8, 10))
        char_h  = dims[1] * max(1, ymul)        # height in dots
        pt_size = max(4, int(char_h * self.SCALE * 0.70))
        qfont   = QFont("Liberation Mono", pt_size)
        qfont.setBold(font in ('3', '4', '5'))
        painter.setFont(qfont)
        pen = QPen(QColor('black'))
        painter.setPen(pen)
        # baseline = top-left y + ascent
        painter.drawText(self._s(x), self._s(y) + pt_size, text)

    # ------------------------------------------------------------------ #
    def _draw_code39(self, painter: QPainter, x: int, y: int,
                     height: int, narrow: int, wide: int, data: str) -> None:
        """Render a Code 39 barcode from its raw data string."""
        full = '*' + data.upper().strip('*') + '*'
        cur_x = x
        no_pen = QPen(Qt.PenStyle.NoPen)
        painter.setPen(no_pen)

        for ch in full:
            pattern = _CODE39_TABLE.get(ch)
            if pattern is None:
                # Unknown char — skip with estimated width
                cur_x += narrow * 5 + wide * 4
                continue
            for i, elem in enumerate(pattern):
                w_dots  = wide if elem == '1' else narrow
                is_bar  = (i % 2 == 0)           # even indices = bars
                if is_bar:
                    painter.fillRect(
                        self._s(cur_x), self._s(y),
                        max(1, self._s(w_dots)), self._s(height),
                        QColor('black')
                    )
                cur_x += w_dots
            # Inter-character gap = 1 narrow module
            cur_x += narrow
