#!/usr/bin/env python3
"""
Vula! Print Label Printer Desktop Application
Modern PyQt6 GUI for managing and printing label requests
"""

import sys
import json
import re
import os
import subprocess
import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime
from urllib.parse import urljoin

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QMessageBox, QFrame,
    QProgressBar, QTextEdit, QLineEdit, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QSizePolicy, QStatusBar,
    QScrollArea, QDialog
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QSize, QProcess
from PyQt6.QtGui import QFont, QIcon, QPalette, QColor, QPixmap, QPainter, QPen, QBrush, QImage

import requests



def _load_env_file(env_file: Path) -> None:
    """Load simple KEY=VALUE pairs from .env into process environment."""
    if not env_file.exists():
        return
    try:
        with open(env_file, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception as e:
        print(f"Warning: failed to load .env file {env_file}: {e}")


# Configuration
APP_ROOT = Path(__file__).parent
_load_env_file(APP_ROOT / ".env")

API_BASE_URL = os.getenv("PRINTER_API_BASE_URL", "https://store.baytalemirati.co.za")
API_KEY = os.getenv("PRINTER_API_KEY", "")
APP_CONFIG_FILE = Path.home() / ".config" / "vula_print" / "settings.json"
APP_HISTORY_FILE = Path.home() / ".config" / "vula_print" / "print_history.json"


# ── Code 39 encoding table ──────────────────────────────────────────────────
# Each character → 9-char string of '0'(narrow) / '1'(wide)
# Bit positions: bar, space, bar, space, bar, space, bar, space, bar
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


class PrinterScanner(QThread):
    """Background thread to scan for USB printers."""
    
    printers_found = pyqtSignal(list)
    
    def run(self):
        """Scan for available USB printers."""
        printers = []
        try:
            usb_path = Path("/dev/usb")
            if usb_path.exists():
                printers = sorted([str(p) for p in usb_path.glob("lp*")])
        except Exception as e:
            print(f"Error scanning for printers: {e}")
        
        self.printers_found.emit(printers)


class PrintJob(QThread):
    """Background thread for printing labels."""
    
    progress = pyqtSignal(int, int)  # current, total
    finished = pyqtSignal(bool, str)  # success, message
    
    def __init__(self, printer_device: str, items: List[Dict[str, Any]]):
        super().__init__()
        self.printer_device = printer_device
        self.items = items
        self.label_width_dots = 320
        self.horizontal_shift_dots = 16
    
    def _tspl_escape(self, s: str) -> str:
        """Escape a string for TSPL commands."""
        return (s or "").replace('\\', '\\\\').replace('"', '\\"')
    
    def _center_x_for_text(self, text: str, font: str = "4", xmul: int = 1) -> int:
        """Calculate centered X position for text."""
        font_char_width = {
            '1': 8, '2': 12, '3': 16, '4': 24, '5': 32,
            '6': 14, '7': 14, '8': 14,
        }
        char_w = font_char_width.get(str(font), 8) * max(1, int(xmul))
        width = len(text or "") * char_w
        x = int((self.label_width_dots - width) / 2)
        return max(0, x) + self.horizontal_shift_dots
    
    def _center_x_for_code39(self, data: str, narrow: int = 2, wide: int = 4) -> int:
        """Calculate centered X position for Code39 barcode."""
        n = max(1, int(narrow))
        w = max(n, int(wide))
        char_count = len(data or "") + 2
        per_char_modules = (3 * w) + (6 * n)
        inter_gap = n
        width = (char_count * per_char_modules) + ((char_count - 1) * inter_gap)
        x = int((self.label_width_dots - width) / 2)
        return max(0, x) + self.horizontal_shift_dots
    
    def _format_price(self, price_cents: int, currency: str = "ZAR") -> str:
        """Format price for display."""
        symbol = "R" if currency == "ZAR" else currency
        return f"{symbol}{price_cents / 100:.2f}"

    _FONT_CHAR_W = {'1': 8, '2': 12, '3': 16, '4': 24, '5': 32}

    def _wrap_text(self, text: str, font: str, max_dots: int) -> list:
        """
        Wrap *text* to at most 2 lines so each line fits within *max_dots*.
        Splits at word boundaries; hard-breaks a single long word if needed.
        """
        cw = self._FONT_CHAR_W.get(str(font), 8)
        max_chars = max(1, max_dots // cw)

        if len(text) <= max_chars:
            return [text]

        # Try to split at a word boundary
        words = text.split()
        line1 = ''
        for word in words:
            candidate = (line1 + ' ' + word).strip()
            if len(candidate) <= max_chars:
                line1 = candidate
            else:
                break

        if not line1:                      # single word longer than max_chars
            line1 = text[:max_chars]
        line2 = text[len(line1):].strip()[:max_chars]  # hard-truncate remainder
        return [line1, line2] if line2 else [line1]
    
    def _generate_label_tspl(self, item: Dict[str, Any]) -> str:
        """Generate TSPL commands for a single label."""
        title         = (item.get("title") or "")
        variant_label = item.get("variant_label") or ""
        sku           = item.get("sku") or ""
        code39        = item.get("code39") or sku
        price         = self._format_price(item.get("price_cents", 0), item.get("currency", "ZAR"))

        tspl = []
        tspl.append("SIZE 40 mm, 30 mm")
        tspl.append("GAP 2 mm, 0 mm")
        tspl.append("DIRECTION 0")
        tspl.append("REFERENCE 0, 0")
        tspl.append("OFFSET 0 mm")
        tspl.append("SET PEEL OFF")
        tspl.append("SET CUTTER OFF")
        tspl.append("SET PARTIAL_CUTTER OFF")
        tspl.append("SET TEAR ON")
        tspl.append("CLS")

        LM         = 10                                  # left margin (dots)
        USABLE_W   = self.label_width_dots - LM * 2     # 300 dots printable width
        TITLE_FONT = "3"                                 # 16 dots/char
        TITLE_LINE_H = 26                                # font-3 height (24) + 2 gap

        # ── Title (wraps to 2 lines if needed) ───────────────────────
        title_lines = self._wrap_text(title, TITLE_FONT, USABLE_W)
        tspl.append(f'TEXT {LM},5,"{TITLE_FONT}",0,1,1,"{self._tspl_escape(title_lines[0])}"')
        if len(title_lines) > 1:
            tspl.append(f'TEXT {LM},{5 + TITLE_LINE_H},"{TITLE_FONT}",0,1,1,"{self._tspl_escape(title_lines[1])}"')

        # Shift all elements below the title down when title occupies 2 lines
        extra = TITLE_LINE_H if len(title_lines) > 1 else 0

        # ── Variant label ─────────────────────────────────────────────
        if variant_label:
            tspl.append(f'TEXT {LM},{27 + extra},"2",0,1,1,"{self._tspl_escape(variant_label)}"')

        # ── Separator bar — full printable width ─────────────────────
        tspl.append(f"BAR {LM},{44 + extra},{USABLE_W},2")

        # ── Price (font 4, one step up from font 3) ───────────────────
        tspl.append(f'TEXT {LM},{56 + extra},"4",0,1,1,"{self._tspl_escape(price)}"')

        # ── Code39 barcode ────────────────────────────────────────────
        tspl.append(f'BARCODE {LM},{95 + extra},"39",70,0,0,1,2,"{self._tspl_escape(code39)}"')

        # ── SKU (bottom, small font) ──────────────────────────────────
        tspl.append(f'TEXT {LM},215,"1",0,1,1,"{self._tspl_escape(sku)}"')

        tspl.append("PRINT 1")
        return "\n".join(tspl) + "\n"
    
    def run(self):
        """Execute print job."""
        try:
            total = sum(item.get("qty_to_print", 0) for item in self.items)
            current = 0
            
            for item in self.items:
                qty = item.get("qty_to_print", 0)
                
                for i in range(qty):
                    # Generate label
                    tspl = self._generate_label_tspl(item)
                    
                    # Send to printer
                    try:
                        with open(self.printer_device, 'wb') as printer:
                            printer.write(tspl.encode('utf-8'))
                    except PermissionError:
                        self.finished.emit(
                            False,
                            f"Permission denied: cannot write to {self.printer_device}.\n\n"
                            f"The printer device requires the user to be in the 'lp' group.\n"
                            f"Re-run the install script to fix this automatically, or run:\n"
                            f"  sudo usermod -aG lp $USER  (then log out and back in)"
                        )
                        return
                    except Exception as e:
                        self.finished.emit(False, f"Printer error: {e}")
                        return
                    
                    current += 1
                    self.progress.emit(current, total)
                    
                    # Small delay between labels
                    time.sleep(0.2)
            
            self.finished.emit(True, f"Successfully printed {total} labels")
            
        except Exception as e:
            self.finished.emit(False, f"Print job failed: {e}")


class POSSlipPrintJob(QThread):
    """Background thread for printing POS slips (ESC/POS)."""

    finished = pyqtSignal(bool, str)

    def __init__(self, printer_device: str, detail_payload: Dict[str, Any]):
        super().__init__()
        self.printer_device = printer_device
        self.detail_payload = detail_payload

    @staticmethod
    def _cents_to_amount(cents: int) -> str:
        value = Decimal(int(cents)) / Decimal(100)
        value = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"{value:.2f}"

    @staticmethod
    def _vat_percent_from_bps(vat_bps: int) -> str:
        value = Decimal(int(vat_bps)) / Decimal(100)
        value = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"{value:.2f}%"

    @staticmethod
    def _esc(*values: int) -> bytes:
        return bytes(values)

    @staticmethod
    def _line_sep(width: int = 48, ch: str = "-") -> str:
        return ch * width

    @staticmethod
    def _col2(left: str, right: str, width: int = 48) -> str:
        l = str(left or "")
        r = str(right or "")
        space = max(1, width - len(l) - len(r))
        return f"{l}{' ' * space}{r}"

    def _txt(self, text: str = "") -> bytes:
        return (text + "\n").encode("ascii", errors="replace")

    def _build_receipt_bytes(self) -> bytes:
        req = self.detail_payload.get("request", {})
        business = self.detail_payload.get("business", {})
        store = self.detail_payload.get("store", {})
        totals = self.detail_payload.get("totals", {})
        items = self.detail_payload.get("items", [])

        currency = totals.get("currency", "ZAR")
        cur = "R" if currency == "ZAR" else currency

        out = bytearray()
        ESC = 0x1B
        GS = 0x1D
        LF = 0x0A

        out += self._esc(ESC, 0x40)  # init / reset printer

        # Cash drawer kick — ESC p pin t1 t2
        # Pulses the RJ12 DK solenoid (pin 2) before receipt content is sent.
        # Only fires for cash transactions; card/EFT/account do not open the drawer.
        payment_type = str(req.get("payment_type", "")).lower()
        if payment_type == "cash":
            out += bytes([ESC, 0x70, 0x00, 0x19, 0xFA])  # pin 2, 50 ms ON, 500 ms max

        out += self._esc(ESC, 0x61, 0x01)  # center
        # Print logo if available
        logo_url = self.detail_payload.get("logo_url", "")
        if logo_url:
            logo_raster = self._logo_to_escpos_raster(logo_url)
            if logo_raster:
                out += logo_raster
                out += b"\n"
        out += self._esc(ESC, 0x45, 0x01)  # bold on
        out += self._txt(business.get("brand_name", "POS RECEIPT"))
        out += self._esc(ESC, 0x45, 0x00)  # bold off

        if business.get("phone"):
            out += self._txt(f"Tel: {business['phone']}")
        if business.get("email"):
            out += self._txt(str(business.get("email", "")))
        if business.get("vat_number"):
            out += self._txt(f"VAT: {business['vat_number']}")

        addr_parts = [
            business.get("address_line1", ""),
            business.get("address_line2", ""),
            business.get("city", ""),
            business.get("province", ""),
            business.get("postal_code", ""),
            business.get("country", ""),
        ]
        for line in [p for p in addr_parts if p]:
            out += self._txt(str(line))

        out += self._esc(ESC, 0x61, 0x00)  # left
        out += self._txt(self._line_sep())
        out += self._txt(self._col2("Invoice:", str(req.get("invoice_number", ""))))
        out += self._txt(self._col2("Created:", str(req.get("created_at", ""))))
        out += self._txt(self._col2("Cashier:", str(self.detail_payload.get("cashier_username", ""))))
        out += self._txt(self._col2("Payment:", str(req.get("payment_type", ""))))

        customer_email = str(self.detail_payload.get("customer_email", "") or "").strip()
        if customer_email:
            out += self._txt(self._col2("Customer:", customer_email))

        if store.get("name"):
            out += self._txt(self._line_sep())
            out += self._txt(str(store.get("name", "")))
            for store_line in str(store.get("address", "")).splitlines():
                if store_line.strip():
                    out += self._txt(store_line.strip())
            if store.get("phone"):
                out += self._txt(f"Store Tel: {store['phone']}")
            if store.get("email"):
                out += self._txt(f"Store Email: {store['email']}")

        out += self._txt(self._line_sep())
        out += self._esc(ESC, 0x45, 0x01)
        out += self._txt(self._col2("QTY ITEM", "TOTAL"))
        out += self._esc(ESC, 0x45, 0x00)
        out += self._txt(self._line_sep())

        for item in items:
            qty = int(item.get("qty", 0) or 0)
            title = str(item.get("title", ""))
            variant = str(item.get("variant_label", ""))
            sku = str(item.get("sku", ""))
            unit_price = f"{cur} {self._cents_to_amount(item.get('unit_price_cents', 0) or 0)}"
            line_total = f"{cur} {self._cents_to_amount(item.get('line_total_cents', 0) or 0)}"

            out += self._txt(self._col2(f"{qty} x {title[:26]}", line_total))
            if variant:
                out += self._txt(f"  {variant[:42]}")
            if sku:
                out += self._txt(f"  SKU: {sku[:36]}")
            out += self._txt(f"  @ {unit_price}")

        out += self._txt(self._line_sep())
        out += self._txt(self._col2("Subtotal before disc:", f"{cur} {self._cents_to_amount(totals.get('subtotal_before_discount_cents', 0) or 0)}"))
        out += self._txt(self._col2("Manual discount:", f"{cur} {self._cents_to_amount(totals.get('manual_discount_cents', 0) or 0)}"))
        out += self._txt(self._col2("Voucher discount:", f"{cur} {self._cents_to_amount(totals.get('voucher_discount_cents', 0) or 0)}"))
        out += self._txt(self._col2("Subtotal:", f"{cur} {self._cents_to_amount(totals.get('subtotal_cents', 0) or 0)}"))

        vat_label = f"VAT ({self._vat_percent_from_bps(totals.get('vat_bps', 0) or 0)}):"
        out += self._txt(self._col2(vat_label, f"{cur} {self._cents_to_amount(totals.get('tax_cents', 0) or 0)}"))
        out += self._txt(self._line_sep())
        out += self._esc(ESC, 0x45, 0x01)
        out += self._txt(self._col2("TOTAL:", f"{cur} {self._cents_to_amount(totals.get('total_cents', 0) or 0)}"))
        out += self._esc(ESC, 0x45, 0x00)

        footer_note = str(self.detail_payload.get("footer_note", "") or "").strip()
        if footer_note:
            out += self._txt(self._line_sep())
            out += self._esc(ESC, 0x61, 0x01)
            out += self._txt(footer_note[:48])
            out += self._esc(ESC, 0x61, 0x00)

        # QR code and website
        website_url = str(self.detail_payload.get("website_url", "") or "").strip()
        qr_data = str(self.detail_payload.get("qr_data", "") or "").strip()
        if qr_data:
            out += self._txt(self._line_sep())
            out += self._esc(ESC, 0x61, 0x01)  # center
            qr_cmd = self._qr_code_escpos(qr_data)
            if qr_cmd:
                out += qr_cmd
                out += b"\n"
            if website_url:
                out += self._txt(website_url[:48])
            out += self._esc(ESC, 0x61, 0x00)  # left

        # Loyalty note
        out += self._txt(self._line_sep())
        out += self._esc(ESC, 0x61, 0x01)
        out += self._txt("If you have an online account,")
        out += self._txt("you can keep track of all online")
        out += self._txt("or instore orders on your account.")
        out += self._esc(ESC, 0x61, 0x00)

        out += bytes([LF, LF, LF])
        out += self._esc(GS, 0x56, 0x41, 0x00)  # full cut
        return bytes(out)

    def _logo_to_escpos_raster(self, logo_url: str, max_width: int = 380) -> bytes:
        """Download logo, convert to 1-bit, and return ESC/POS raster bytes."""
        if not logo_url:
            return b""
        try:
            # Use requests to fetch; logo_url may be relative, but backend sends absolute via media_url
            # In case it's relative, handle base URL? For now assume absolute.
            if logo_url.startswith("/"):
                # Fallback: use API base? But we don't have base in this class.
                # We'll skip; backend should provide absolute URL.
                return b""

            resp = requests.get(logo_url, timeout=8)
            if resp.status_code != 200:
                return b""

            img = QImage()
            if not img.loadFromData(resp.content):
                return b""

            # Convert to grayscale and scale to fit max_width
            img = img.convertToFormat(QImage.Format.Format_Grayscale8)
            if img.width() > max_width:
                img = img.scaledToWidth(max_width, Qt.TransformationMode.SmoothTransformation)

            width = img.width()
            height = img.height()
            # Ensure height is multiple of 8 for raster
            padded_height = (height + 7) // 8 * 8

            raster = bytearray()
            for y in range(0, padded_height, 8):
                row_bytes = bytearray()
                for x in range(width):
                    byte_val = 0
                    for bit in range(8):
                        py = y + bit
                        if py >= height:
                            break
                        # QImage grayscale: pixel value 0-255; threshold 128
                        pixel = img.pixelColor(x, py).red()  # grayscale
                        if pixel < 128:
                            byte_val |= (1 << (7 - bit))
                    row_bytes.append(byte_val)
                # ESC * command: raster bit image
                raster += bytes([0x1B, 0x2A, 0x00, width & 0xFF, width >> 8]) + bytes(row_bytes)

            return bytes(raster)
        except Exception:
            return b""

    def run(self):
        try:
            payload = self._build_receipt_bytes()
            try:
                with open(self.printer_device, "wb") as printer:
                    printer.write(payload)
            except PermissionError:
                self.finished.emit(
                    False,
                    f"Permission denied: cannot write to {self.printer_device}.\n\n"
                    f"The printer device requires the user to be in the 'lp' group.\n"
                    f"Re-run the install script to fix this automatically, or run:\n"
                    f"  sudo usermod -aG lp $USER  (then log out and back in)",
                )
                return
            except Exception as e:
                self.finished.emit(False, f"POS printer error: {e}")
                return

            self.finished.emit(True, "POS slip printed successfully")
        except Exception as e:
            self.finished.emit(False, f"POS slip print failed: {e}")


class POSEODReportPrintJob(QThread):
    """Background thread for printing receipt-width POS EOD reports (ESC/POS)."""

    finished = pyqtSignal(bool, str)

    def __init__(self, printer_device: str, detail_payload: Dict[str, Any]):
        super().__init__()
        self.printer_device = printer_device
        self.detail_payload = detail_payload

    @staticmethod
    def _cents_to_amount(cents: int) -> str:
        value = Decimal(int(cents)) / Decimal(100)
        value = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"{value:.2f}"

    @staticmethod
    def _esc(*values: int) -> bytes:
        return bytes(values)

    @staticmethod
    def _line_sep(width: int = 48, ch: str = "-") -> str:
        return ch * width

    @staticmethod
    def _col2(left: str, right: str, width: int = 48) -> str:
        l = str(left or "")
        r = str(right or "")
        space = max(1, width - len(l) - len(r))
        return f"{l}{' ' * space}{r}"

    def _txt(self, text: str = "") -> bytes:
        return (text + "\n").encode("ascii", errors="replace")

    def _build_receipt_bytes(self) -> bytes:
        req = self.detail_payload.get("request", {})
        report = self.detail_payload.get("report_data", {})
        payment = report.get("payment_split", {})

        out = bytearray()
        ESC = 0x1B
        GS = 0x1D
        LF = 0x0A

        out += self._esc(ESC, 0x40)
        out += self._esc(ESC, 0x61, 0x01)
        out += self._esc(ESC, 0x45, 0x01)
        out += self._txt("POS END OF DAY")
        out += self._esc(ESC, 0x45, 0x00)
        out += self._txt(str(report.get("date", "")))
        out += self._txt(str(report.get("timezone", "")))

        out += self._esc(ESC, 0x61, 0x00)
        out += self._txt(self._line_sep())
        out += self._txt(self._col2("Request:", str(req.get("id", ""))))
        out += self._txt(self._col2("Source:", str(req.get("source", "manual"))))
        out += self._txt(self._line_sep())

        out += self._txt(self._col2("Invoices:", str(int(report.get("invoices_created", 0) or 0))))
        out += self._txt(self._col2("Items Sold:", str(int(report.get("items_sold", 0) or 0))))
        out += self._txt(self._col2("Sales:", f"R {self._cents_to_amount(int(report.get('total_sales_cents', 0) or 0))}"))
        out += self._txt(self._col2("COGS:", f"R {self._cents_to_amount(int(report.get('total_cost_cents', 0) or 0))}"))
        out += self._esc(ESC, 0x45, 0x01)
        out += self._txt(self._col2("Gross Profit:", f"R {self._cents_to_amount(int(report.get('total_profit_cents', 0) or 0))}"))
        out += self._esc(ESC, 0x45, 0x00)

        cash = payment.get("cash", {})
        card = payment.get("card", {})
        out += self._txt(self._line_sep())
        out += self._txt("Payment Split")
        out += self._txt(self._col2("Cash:", f"R {self._cents_to_amount(int(cash.get('sales_cents', 0) or 0))}"))
        out += self._txt(self._col2("  Invoices", str(int(cash.get("invoices", 0) or 0))))
        out += self._txt(self._col2("Card:", f"R {self._cents_to_amount(int(card.get('sales_cents', 0) or 0))}"))
        out += self._txt(self._col2("  Invoices", str(int(card.get("invoices", 0) or 0))))

        staff_rows = report.get("staff", []) or []
        if staff_rows:
            out += self._txt(self._line_sep())
            out += self._txt("Top Staff")
            for row in staff_rows[:8]:
                name = str(row.get("name", ""))[:20]
                sales_cents = int(row.get("sales_cents", 0) or 0)
                out += self._txt(self._col2(name, f"R {self._cents_to_amount(sales_cents)}"))

        out += bytes([LF, LF, LF])
        out += self._esc(GS, 0x56, 0x41, 0x00)
        return bytes(out)

    def run(self):
        try:
            payload = self._build_receipt_bytes()
            try:
                with open(self.printer_device, "wb") as printer:
                    printer.write(payload)
            except PermissionError:
                self.finished.emit(
                    False,
                    f"Permission denied: cannot write to {self.printer_device}.\n\n"
                    f"The printer device requires the user to be in the 'lp' group.\n"
                    f"Re-run the install script to fix this automatically, or run:\n"
                    f"  sudo usermod -aG lp $USER  (then log out and back in)",
                )
                return
            except Exception as e:
                self.finished.emit(False, f"POS EOD printer error: {e}")
                return

            self.finished.emit(True, "POS EOD report printed successfully")
        except Exception as e:
            self.finished.emit(False, f"POS EOD print failed: {e}")


class POSPollWorker(QThread):
    """Off-main-thread worker for the complete POS poll cycle.

    Performs all blocking HTTP I/O (pending check + detail fetch, EOD reports)
    in a background QThread so the Qt main event loop — and the UI — remain
    fully responsive at all times.  Emits exactly one signal per run().
    """

    slip_ready     = pyqtSignal(int, dict)   # (request_id, detail_payload)
    eod_slip_ready = pyqtSignal(int, dict)   # (request_id, detail_payload)
    all_clear      = pyqtSignal()
    poll_error     = pyqtSignal(str, int)    # (message, http_status)  0=network
    poll_fatal     = pyqtSignal(str, int)    # (message, http_status)  auth/config

    def __init__(
        self,
        api_base: str,
        api_key: str,
        user_id: int,
        in_flight_ids: frozenset,
        eod_in_flight_ids: frozenset,
        parent=None,
    ):
        super().__init__(parent)
        self._api_base = api_base.rstrip("/")
        self._api_key = api_key
        self._user_id = user_id
        self._in_flight_ids = in_flight_ids
        self._eod_in_flight_ids = eod_in_flight_ids

    def _headers(self) -> Dict[str, str]:
        headers = {"X-Printer-API-Key": self._api_key}
        if self._user_id:
            headers["X-Printer-User-Id"] = str(self._user_id)
        return headers

    def run(self):
        # ── 1. Poll POS slips ──────────────────────────────────────────────
        try:
            r = requests.get(
                f"{self._api_base}/admin/api/pos-slips/pending",
                headers=self._headers(),
                timeout=10,
            )
        except Exception as e:
            self.poll_error.emit(str(e), 0)
            return

        if r.status_code in (401, 503, 400):
            self.poll_fatal.emit(f"HTTP {r.status_code}", r.status_code)
            return
        if r.status_code != 200:
            self.poll_error.emit(f"HTTP {r.status_code}", r.status_code)
            return

        pending = sorted(
            (r.json() if isinstance(r.json(), list) else []),
            key=lambda x: str(x.get("created_at", "")),
        )
        for item in pending:
            req_id = int(item.get("id", 0) or 0)
            if req_id <= 0 or req_id in self._in_flight_ids:
                continue
            # Fetch full detail payload
            try:
                dr = requests.get(
                    f"{self._api_base}/admin/api/pos-slips/request/{req_id}",
                    headers=self._headers(),
                    timeout=10,
                )
            except Exception as e:
                self.poll_error.emit(f"Detail #{req_id}: {e}", 0)
                return
            if dr.status_code == 404:
                continue  # already gone, try next slip
            if dr.status_code != 200:
                self.poll_error.emit(f"Detail #{req_id}: HTTP {dr.status_code}", dr.status_code)
                return
            self.slip_ready.emit(req_id, dr.json())
            return

        # ── 2. No POS slips — check EOD reports ───────────────────────────
        try:
            requests.post(
                f"{self._api_base}/admin/api/pos-eod-reports/ensure-latest",
                headers={**self._headers(), "Content-Type": "application/json"},
                json={},
                timeout=8,
            )
        except Exception:
            pass  # best-effort

        try:
            eod_r = requests.get(
                f"{self._api_base}/admin/api/pos-eod-reports/pending",
                headers=self._headers(),
                timeout=10,
            )
        except Exception:
            self.all_clear.emit()
            return

        if eod_r.status_code != 200:
            self.all_clear.emit()
            return

        eod_pending = sorted(
            (eod_r.json() if isinstance(eod_r.json(), list) else []),
            key=lambda x: str(x.get("created_at", "")),
        )
        for item in eod_pending:
            req_id = int(item.get("id", 0) or 0)
            if req_id <= 0 or req_id in self._eod_in_flight_ids:
                continue
            try:
                dr = requests.get(
                    f"{self._api_base}/admin/api/pos-eod-reports/request/{req_id}",
                    headers=self._headers(),
                    timeout=10,
                )
            except Exception:
                self.all_clear.emit()
                return
            if dr.status_code != 200:
                self.all_clear.emit()
                return
            self.eod_slip_ready.emit(req_id, dr.json())
            return

        self.all_clear.emit()


class VulaPrintApp(QMainWindow):
    """Main application window."""
    
    def __init__(self):
        super().__init__()
        
        self.api_base_url = API_BASE_URL
        self.api_key = API_KEY
        self.selected_printer = None
        self.pos_selected_printer = None
        self.printer_calibrated = False
        self.pending_requests: List[Dict[str, Any]] = []
        self.last_selected_printer: Optional[str] = None
        self.last_selected_pos_printer: Optional[str] = None
        self.auto_connect_on_startup = True
        self.printer_user_id: Optional[int] = None
        self.pos_poll_interval_seconds = 2  # default 2s; HTTP is off-thread so low interval is safe
        env_user_id = os.getenv("PRINTER_USER_ID", "").strip()
        if env_user_id.isdigit() and int(env_user_id) > 0:
            self.printer_user_id = int(env_user_id)
        self.calibration_job: Optional[PrintJob] = None
        self.print_job: Optional[PrintJob] = None
        self.pos_print_job: Optional[POSSlipPrintJob] = None
        self.pos_eod_print_job: Optional[POSEODReportPrintJob] = None
        self._pos_poll_worker: Optional[POSPollWorker] = None
        self.pos_in_flight_ids: set[int] = set()
        self.pos_completion_retry_ids: set[int] = set()
        self.pos_eod_in_flight_ids: set[int] = set()
        self.pos_eod_completion_retry_ids: set[int] = set()
        self.pos_backoff_seconds = 1
        self.pos_backoff_until = 0.0
        self.last_successful_pos_poll_at: Optional[datetime] = None
        self.last_successful_pos_print_at: Optional[datetime] = None
        self._selected_request: Optional[Dict[str, Any]] = None   # tracks table selection
        self._current_print_request: Optional[Dict[str, Any]] = None  # for history

        self.printer_print_type = ""
        self.printer_role = ""
        self.printer_store_id: Optional[int] = None
        self.config_version = 0
        self.synced_config_version = 0
        self.logo_dark_url = ""
        self.logo_light_url = ""
        self.discovered_printers: list[str] = []
        self.brand_logo_path = str(Path(__file__).parent / "assets" / "Vula_Logo.png")

        self.load_settings()
        self.apply_brand_theme_from_css()
        
        self.init_ui()
        self.setup_auto_refresh()
        
        # Auto-scan for printers on startup
        self.scan_for_printers()
        QTimer.singleShot(500, self.ensure_onboarded)

    def load_settings(self):
        """Load persisted app settings."""
        try:
            if not APP_CONFIG_FILE.exists():
                return

            with open(APP_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.api_base_url = (data.get("api_base_url") or self.api_base_url).strip()
            self.api_key = (data.get("api_key") or self.api_key).strip()
            self.brand_logo_path = data.get("brand_logo_path") or self.brand_logo_path
            roles = data.get("printer_roles") or {}
            self.last_selected_printer = roles.get("label") or data.get("label_printer_device") or None
            self.last_selected_pos_printer = roles.get("pos_slip") or data.get("pos_slip_printer_device") or None
            self.auto_connect_on_startup = bool(data.get("auto_connect_on_startup", True))
            raw_user_id = data.get("printer_user_id")
            if raw_user_id is not None and str(raw_user_id).strip().isdigit():
                parsed_user_id = int(str(raw_user_id).strip())
                self.printer_user_id = parsed_user_id if parsed_user_id > 0 else None
            self.pos_poll_interval_seconds = int(data.get("pos_poll_interval_seconds", 5) or 5)
        except Exception as e:
            print(f"Warning: failed to load settings: {e}")

    def save_settings(self):
        """Persist app settings."""
        try:
            APP_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "api_base_url": self.api_base_url,
                "api_key": self.api_key,
                "brand_logo_path": self.brand_logo_path,
                "label_printer_device": self.last_selected_printer,
                "pos_slip_printer_device": self.last_selected_pos_printer,
                "auto_connect_on_startup": self.auto_connect_on_startup,
                "printer_user_id": self.printer_user_id,
                "pos_poll_interval_seconds": self.pos_poll_interval_seconds,
                "printer_roles": {
                    "label": self.last_selected_printer,
                    "pos_slip": self.last_selected_pos_printer,
                },
            }
            with open(APP_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"Warning: failed to save settings: {e}")

    def _set_connection_status(self, connected: bool, status_code: Optional[int] = None):
        """Update API connection indicators in the UI."""
        if connected:
            self.connection_status.setText("Connected")
            self.connection_status.setStyleSheet(
                f"background:#0f2a1a; color:{self.C_GREEN}; border:1px solid #1a5a2a;"
                f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
            )
            self.header_connection_status.setText("● Connected")
            self.header_connection_status.setStyleSheet(
                f"color:{self.C_GREEN}; font-size:10px; font-weight:600;"
            )
            return

        err_label = f"Error {status_code}" if status_code is not None else "Disconnected"
        self.connection_status.setText(err_label)
        self.connection_status.setStyleSheet(
            f"background:#2a1a1a; color:{self.C_RED}; border:1px solid #5a2a2a;"
            f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
        )
        self.header_connection_status.setText("● Disconnected")
        self.header_connection_status.setStyleSheet(
            f"color:{self.C_RED}; font-size:10px; font-weight:600;"
        )

    def ensure_onboarded(self):
        """Fetch backend config when settings already exist.

        First-run setup is handled by onboarding.py before the main app starts,
        so we do not show that wizard here again.
        """
        if self.api_base_url and self.api_key:
            self.fetch_printer_config(show_dialogs=False)
            return

        self._set_connection_status(False)
        self._update_pos_worker_status("Run onboarding first")
        self.status_bar.showMessage(
            "Run the setup wizard to configure backend URL and printer API key."
        )

    def check_api_connection(self, show_dialogs: bool = True, fetch_queue_on_success: bool = True) -> bool:
        """Check API connectivity and update status indicators."""
        if not self.api_key:
            self._set_connection_status(False)
            self._update_pos_worker_status("Set PRINTER_API_KEY in .env")
            if show_dialogs:
                QMessageBox.warning(
                    self,
                    "Missing API Key",
                    "PRINTER_API_KEY is not configured.\n"
                    "Create .env from .env.example and set PRINTER_API_KEY.",
                )
            return False

        try:
            headers = self._headers()
            label_response = requests.get(
                f"{self.api_base_url}/admin/api/label-printing/pending",
                headers=headers,
                timeout=5
            )

            if label_response.status_code == 200:
                self._set_connection_status(True)
                if fetch_queue_on_success:
                    self.pending_requests = label_response.json()
                    self.update_requests_table()
                    self.status_bar.showMessage(f"Loaded {len(self.pending_requests)} pending request(s)")

                pos_status_msg = "POS pending check skipped (set PRINTER_USER_ID)"
                if self.printer_user_id:
                    pos_headers = self._pos_headers()
                    pos_response = requests.get(
                        f"{self.api_base_url}/admin/api/pos-slips/pending",
                        headers=pos_headers,
                        timeout=5,
                    )
                    if pos_response.status_code == 200:
                        pos_status_msg = "POS API connected"
                    elif pos_response.status_code == 400:
                        pos_status_msg = "POS API rejected PRINTER_USER_ID"
                    elif pos_response.status_code in (401, 503):
                        pos_status_msg = f"POS API unavailable ({pos_response.status_code})"
                    else:
                        pos_status_msg = f"POS API error ({pos_response.status_code})"

                self._update_pos_worker_status(pos_status_msg)
                if show_dialogs:
                    QMessageBox.information(
                        self,
                        "Connection Success",
                        f"Label API connected successfully.\n{pos_status_msg}",
                    )
                return True

            self._set_connection_status(False, status_code=label_response.status_code)
            self._update_pos_worker_status(f"Label API error ({label_response.status_code})")
            if show_dialogs:
                QMessageBox.warning(self, "Connection Error", f"Server returned: {label_response.status_code}")
            return False

        except Exception as e:
            self._set_connection_status(False)
            self._update_pos_worker_status("Connection failed")
            if show_dialogs:
                QMessageBox.critical(self, "Connection Failed", f"Failed to connect: {e}")
            return False

    def auto_connect_to_api(self):
        """Attempt API connection on startup without interrupting users."""
        if not self.auto_connect_on_startup:
            return
        if not self.api_base_url:
            return
        self.fetch_printer_config(show_dialogs=False)


    def fetch_printer_config(self, show_dialogs: bool = False) -> bool:
        """Fetch this printer registration's backend config."""
        if not self.api_base_url or not self.api_key:
            self._set_connection_status(False)
            self._update_pos_worker_status("Setup required")
            return False

        try:
            response = requests.get(
                f"{self.api_base_url}/admin/api/printer-app/config",
                headers={"X-Printer-API-Key": self.api_key},
                timeout=8,
            )
        except Exception as e:
            self._set_connection_status(False)
            self._update_pos_worker_status("Connection failed")
            if show_dialogs:
                QMessageBox.critical(self, "Connection Failed", str(e))
            return False

        if response.status_code == 200:
            cfg = response.json()

            self.printer_user_id = int(cfg.get("user_id") or 0) or None
            self.printer_print_type = cfg.get("print_type", "")
            self.printer_role = cfg.get("role", "")
            self.printer_store_id = cfg.get("store_id")
            self.config_version = int(cfg.get("config_version") or 0)
            self.synced_config_version = int(cfg.get("synced_config_version") or 0)
            self.logo_dark_url = cfg.get("logo_dark_url", "")
            self.logo_light_url = cfg.get("logo_light_url", "")

            self.fetch_brand_css()
            self.apply_brand_theme_from_css()
            self.download_brand_logo()

            self.api_url_input.setText(self.api_base_url)
            self.printer_user_id_input.setText(str(self.printer_user_id or ""))
            self.save_settings()

            self._set_connection_status(True)
            self._update_pos_worker_status("Config loaded")
            self.status_bar.showMessage(
                f"Printer config loaded · role={self.printer_role} type={self.printer_print_type}"
            )

            if self.config_version > self.synced_config_version:
                self.ack_printer_config(self.config_version)

            self.fetch_pending_requests()
            self.upload_discovered_printers_if_ready()
            return True

        self._set_connection_status(False, status_code=response.status_code)

        if response.status_code == 401:
            message = "Invalid printer API key"
        else:
            message = f"Printer config error: HTTP {response.status_code}"

        self._update_pos_worker_status(message)
        if show_dialogs:
            QMessageBox.warning(self, "Printer Config Error", message)

        return False

    def ack_printer_config(self, config_version: int) -> None:
        try:
            requests.post(
                f"{self.api_base_url}/admin/api/printer-app/config/ack",
                headers=self._headers(include_json=True),
                json={"config_version": int(config_version)},
                timeout=8,
            )
            self.synced_config_version = int(config_version)
        except Exception:
            pass

    def fetch_brand_css(self) -> None:
        """Fetch and cache the current branded CSS from the backend."""
        try:
            css_path = "/admin/api/printer-app/brand-css"
            response = requests.get(
                f"{self.api_base_url}{css_path}",
                headers={"X-Printer-API-Key": self.api_key},
                timeout=8,
            )
            if response.status_code == 200:
                css_file = Path.home() / ".config" / "vula_print" / "brand.css"
                css_file.parent.mkdir(parents=True, exist_ok=True)
                css_file.write_text(response.text, encoding="utf-8")
        except Exception:
            pass

    def apply_brand_theme_from_css(self) -> None:
        """Parse the saved backend brand.css and override Qt colours.

        Uses the first occurrence of each variable so dark-theme values win
        for this dark PyQt application.
        """
        css_file = Path.home() / ".config" / "vula_print" / "brand.css"
        tokens: dict[str, str] = {}

        try:
            css_text = css_file.read_text(encoding="utf-8")
        except Exception:
            css_text = ""

        seen: set[str] = set()
        for match in re.finditer(r"(--[a-zA-Z0-9_-]+)\s*:\s*([^;]+);", css_text):
            name = match.group(1)
            value = match.group(2).strip()

            if name in seen:
                continue
            seen.add(name)

            if re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
                tokens[name] = value

        def _colour(name: str, fallback: str) -> str:
            return tokens.get(name) or fallback

        self.C_ORANGE = _colour("--accent-primary", self.C_ORANGE)
        self.C_ORANGE_HI = QColor(self.C_ORANGE).lighter(115).name()
        self.C_ORANGE_DIM = QColor(self.C_ORANGE).darker(115).name()

        self.C_BG = _colour("--bg-base", self.C_BG)
        self.C_SURFACE = _colour("--bg-surface", self.C_SURFACE)
        self.C_SURFACE2 = _colour("--bg-input", self.C_SURFACE2)
        self.C_BORDER = _colour("--border", self.C_BORDER)
        self.C_TEXT = _colour("--text-primary", self.C_TEXT)
        self.C_TEXT_DIM = _colour("--text-muted", self.C_TEXT_DIM)
        self.C_GREEN = _colour("--status-success-text", self.C_GREEN)
        self.C_RED = _colour("--status-error-text", self.C_RED)
        self.C_WARNING = _colour("--status-warn-text", self.C_WARNING)
        self.C_SIDEBAR = _colour("--bg-sidebar", self.C_SIDEBAR)

    def download_brand_logo(self) -> None:
        """Download and cache the backend-provided printer brand logo.

        Prefers the dark logo because the printer app uses a dark UI.
        """
        relative = self.logo_dark_url or self.logo_light_url
        if not relative or not self.api_base_url:
            return

        relative = relative.lstrip("/")
        url = urljoin(self.api_base_url.rstrip("/") + "/", relative)

        try:
            response = requests.get(url, timeout=8)
            if response.status_code != 200:
                return

            logo_file = Path.home() / ".config" / "vula_print" / "brand_logo.png"
            logo_file.parent.mkdir(parents=True, exist_ok=True)
            logo_file.write_bytes(response.content)

            self.brand_logo_path = str(logo_file)
            self.save_settings()

            if hasattr(self, "logo_label"):
                pixmap = QPixmap(self.brand_logo_path)
                if not pixmap.isNull():
                    self.logo_label.setPixmap(
                        pixmap.scaledToWidth(
                            max(120, self.SIDEBAR_W - 36),
                            Qt.TransformationMode.SmoothTransformation,
                        )
                    )
        except Exception:
            pass

    def upload_discovered_printers_if_ready(self) -> None:
        if not self.api_base_url or not self.api_key or not self.discovered_printers:
            return

        payload = []
        for device in self.discovered_printers:
            payload.append(
                {
                    "name": Path(device).name.upper(),
                    "device_uri": device,
                    "connection_type": "usb",
                    "driver": "auto",
                    "meta": {"path": device},
                }
            )

        try:
            requests.post(
                f"{self.api_base_url}/admin/api/printer-app/discovered-printers",
                headers=self._headers(include_json=True),
                json=payload,
                timeout=8,
            )
        except Exception:
            pass

    
    # ─────────────────────────────────────────────────────────────
    # Shared style constants
    # ─────────────────────────────────────────────────────────────
    C_BG        = "#111318"   # window background
    C_SURFACE   = "#1c1f26"   # card / panel surface
    C_SURFACE2  = "#242830"   # slightly lighter surface
    C_BORDER    = "#2e3340"   # subtle border
    C_ORANGE    = "#ff6b35"   # primary accent
    C_ORANGE_HI = "#ff8c5a"   # hover accent
    C_ORANGE_DIM= "#cc5528"   # pressed / dim accent
    C_TEXT      = "#e8e8e8"   # primary text
    C_TEXT_DIM  = "#7a7f8e"   # secondary / muted text
    C_GREEN     = "#4caf7d"   # success
    C_RED       = "#e05252"   # error
    C_WARNING   = "#e09a2a"   # warning
    C_SIDEBAR   = "#13161c"   # sidebar

    SIDEBAR_W   = 220
    SIDEBAR_MIN_W = 170
    SIDEBAR_MAX_W = 280

    def _screen_size(self) -> QSize:
        screen = QApplication.primaryScreen()
        if screen is None:
            return QSize(1366, 768)
        return screen.availableGeometry().size()

    def _responsive_sidebar_width(self) -> int:
        width = self.width() if self.width() > 0 else self._screen_size().width()
        if width <= 980:
            return 178
        if width >= 1900:
            return 258
        return 220

    def _dialog_size(self, width_ratio: float, height_ratio: float, min_w: int, min_h: int, max_w: int, max_h: int) -> QSize:
        screen_size = self._screen_size()
        desired_w = max(min_w, min(int(screen_size.width() * width_ratio), max_w))
        desired_h = max(min_h, min(int(screen_size.height() * height_ratio), max_h))
        return QSize(desired_w, desired_h)

    def init_ui(self):
        """Initialize the user interface."""
        self.setWindowTitle("Vula! Print · Print Manager")
        screen_size = self._screen_size()
        min_w = max(920, int(screen_size.width() * 0.62))
        min_h = max(620, int(screen_size.height() * 0.72))
        self.setMinimumSize(min_w, min_h)
        self.resize(min(1500, int(screen_size.width() * 0.86)), min(960, int(screen_size.height() * 0.9)))

        logo_path = Path(__file__).parent / "assets" / "Vula_Logo.png"
        if logo_path.exists():
            self.setWindowIcon(QIcon(str(logo_path)))

        # ── Global palette ──────────────────────────────────────
        pal = QPalette()
        pal.setColor(QPalette.ColorRole.Window,         QColor(self.C_BG))
        pal.setColor(QPalette.ColorRole.WindowText,     QColor(self.C_TEXT))
        pal.setColor(QPalette.ColorRole.Base,           QColor(self.C_SURFACE))
        pal.setColor(QPalette.ColorRole.AlternateBase,  QColor(self.C_SURFACE2))
        pal.setColor(QPalette.ColorRole.Text,           QColor(self.C_TEXT))
        pal.setColor(QPalette.ColorRole.Button,         QColor(self.C_SURFACE2))
        pal.setColor(QPalette.ColorRole.ButtonText,     QColor(self.C_TEXT))
        pal.setColor(QPalette.ColorRole.Highlight,      QColor(self.C_ORANGE))
        pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#000000"))
        self.setPalette(pal)

        # ── Root layout: sidebar | content ──────────────────────
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        sidebar = self._build_sidebar()
        root_layout.addWidget(sidebar)

        # thin separator line
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFixedWidth(1)
        sep.setStyleSheet(f"background:{self.C_BORDER};")
        root_layout.addWidget(sep)

        content = self._build_content()
        root_layout.addWidget(content, stretch=1)

        # ── Status bar ──────────────────────────────────────────
        self.status_bar = QStatusBar()
        self.status_bar.setStyleSheet(
            f"QStatusBar {{ background:{self.C_SURFACE}; color:{self.C_TEXT_DIM};"
            f" border-top:1px solid {self.C_BORDER}; font-size:11px; padding:2px 10px; }}"
        )
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")

    # ── Stylesheet helpers ────────────────────────────────────────
    def _btn_primary(self) -> str:
        return (
            f"QPushButton {{"
            f"  background:{self.C_ORANGE}; color:#000; border:none;"
            f"  border-radius:6px; padding:9px 16px;"
            f"  font-size:12px; font-weight:700; letter-spacing:0.3px;"
            f"}} "
            f"QPushButton:hover {{ background:{self.C_ORANGE_HI}; }} "
            f"QPushButton:pressed {{ background:{self.C_ORANGE_DIM}; color:#000; }}"
        )

    def _btn_secondary(self) -> str:
        return (
            f"QPushButton {{"
            f"  background:{self.C_SURFACE2}; color:{self.C_ORANGE};"
            f"  border:1px solid {self.C_BORDER};"
            f"  border-radius:6px; padding:8px 16px;"
            f"  font-size:12px; font-weight:600;"
            f"}} "
            f"QPushButton:hover {{ border-color:{self.C_ORANGE}; background:{self.C_SURFACE2}; color:{self.C_ORANGE_HI}; }} "
            f"QPushButton:pressed {{ background:{self.C_BG}; }}"
        )

    def _card_style(self, radius: int = 10) -> str:
        return (
            f"background:{self.C_SURFACE};"
            f"border:1px solid {self.C_BORDER};"
            f"border-radius:{radius}px;"
        )

    def _label_style(self, small: bool = False) -> str:
        size = 10 if small else 12
        return f"color:{self.C_TEXT_DIM}; font-size:{size}px; font-weight:600; letter-spacing:0.6px;"

    def _input_style(self) -> str:
        return (
            f"QLineEdit, QComboBox {{"
            f"  background:{self.C_SURFACE2}; color:{self.C_TEXT};"
            f"  border:1px solid {self.C_BORDER}; border-radius:6px;"
            f"  padding:7px 10px; font-size:12px;"
            f"}} "
            f"QLineEdit:focus, QComboBox:focus {{ border-color:{self.C_ORANGE}; }} "
            f"QComboBox::drop-down {{ border:none; width:24px; }} "
            f"QComboBox::down-arrow {{ width:10px; height:10px; }}"
        )

    # ── Sidebar ───────────────────────────────────────────────────
    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        self.SIDEBAR_W = self._responsive_sidebar_width()
        sidebar.setMinimumWidth(self.SIDEBAR_MIN_W)
        sidebar.setMaximumWidth(self.SIDEBAR_MAX_W)
        sidebar.setFixedWidth(self.SIDEBAR_W)
        sidebar.setStyleSheet(f"QWidget {{ background:{self.C_SIDEBAR}; }}")

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Stacked logos ─────────────────────────────────────────
        logo_container = QWidget()
        logo_container.setStyleSheet(
            f"background:{self.C_SIDEBAR};"
            f"border-bottom:1px solid {self.C_BORDER};"
        )
        logo_layout = QVBoxLayout(logo_container)
        logo_layout.setContentsMargins(18, 24, 18, 20)
        logo_layout.setSpacing(10)
        logo_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        assets = Path(__file__).parent / "assets"
        logo_w = max(120, self.SIDEBAR_W - 36)

        def _make_logo_label(img_path: Path) -> QLabel:
            lbl = QLabel()
            lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            lbl.setStyleSheet("background:transparent; border:none;")
            if img_path.exists():
                px = QPixmap(str(img_path))
                lbl.setPixmap(
                    px.scaledToWidth(logo_w, Qt.TransformationMode.SmoothTransformation)
                )
            return lbl

        self.logo_label = _make_logo_label(Path(self.brand_logo_path))
        logo_layout.addWidget(self.logo_label)
        layout.addWidget(logo_container)

        # ── Config section ────────────────────────────────────────
        config_scroll = QScrollArea()
        config_scroll.setWidgetResizable(True)
        config_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        config_scroll.setStyleSheet(
            "QScrollArea { border:none; background:transparent; }"
            "QScrollBar:vertical { width:4px; background:transparent; }"
            f"QScrollBar::handle:vertical {{ background:{self.C_BORDER}; border-radius:2px; }}"
        )

        config_inner = QWidget()
        config_inner.setStyleSheet(f"background:{self.C_SIDEBAR};")
        config_layout = QVBoxLayout(config_inner)
        config_layout.setContentsMargins(16, 16, 16, 16)
        config_layout.setSpacing(16)

        # ── Printer card ────────────────────────────────
        config_layout.addWidget(self._section_heading("PRINTER"))

        printer_card = QWidget()
        printer_card.setStyleSheet(self._card_style(8))
        pc_layout = QVBoxLayout(printer_card)
        pc_layout.setContentsMargins(12, 12, 12, 12)
        pc_layout.setSpacing(8)

        self.printer_combo = QComboBox()
        self.printer_combo.addItem("No printer detected")
        self.printer_combo.currentIndexChanged.connect(self.on_printer_selected)
        self.printer_combo.setStyleSheet(self._input_style())

        self.pos_printer_combo = QComboBox()
        self.pos_printer_combo.addItem("No POS printer detected")
        self.pos_printer_combo.currentIndexChanged.connect(self.on_pos_printer_selected)
        self.pos_printer_combo.setStyleSheet(self._input_style())

        # calibration status pill
        self.calibration_status = QLabel("Not calibrated")
        self.calibration_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.calibration_status.setStyleSheet(
            f"background:#2a1a1a; color:{self.C_RED}; border:1px solid #5a2a2a;"
            f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
        )

        scan_btn = QPushButton("Scan for Printers")
        scan_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        scan_btn.setStyleSheet(self._btn_secondary())
        scan_btn.clicked.connect(self.scan_for_printers)

        calibrate_btn = QPushButton("Calibrate Printer")
        calibrate_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        calibrate_btn.setStyleSheet(self._btn_primary())
        calibrate_btn.clicked.connect(self.calibrate_printer)

        test_label_btn = QPushButton("Print Test Label")
        test_label_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        test_label_btn.setStyleSheet(self._btn_secondary())
        test_label_btn.clicked.connect(self.print_test_label_standalone)

        test_pos_btn = QPushButton("Test POS Printer")
        test_pos_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        test_pos_btn.setStyleSheet(self._btn_secondary())
        test_pos_btn.clicked.connect(self.print_test_pos_slip)

        pc_layout.addWidget(self.printer_combo)
        pc_layout.addWidget(self.pos_printer_combo)
        pc_layout.addWidget(self.calibration_status)
        pc_layout.addWidget(scan_btn)
        pc_layout.addWidget(calibrate_btn)
        pc_layout.addWidget(test_label_btn)
        pc_layout.addWidget(test_pos_btn)
        config_layout.addWidget(printer_card)

        # ── Connection card ─────────────────────────────
        config_layout.addWidget(self._section_heading("API CONNECTION"))

        conn_card = QWidget()
        conn_card.setStyleSheet(self._card_style(8))
        cc_layout = QVBoxLayout(conn_card)
        cc_layout.setContentsMargins(12, 12, 12, 12)
        cc_layout.setSpacing(8)

        # connection status pill
        self.connection_status = QLabel("Disconnected")
        self.connection_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.connection_status.setStyleSheet(
            f"background:#2a1a1a; color:{self.C_RED}; border:1px solid #5a2a2a;"
            f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
        )

        url_lbl = QLabel("SERVER URL")
        url_lbl.setStyleSheet(self._label_style(small=True))
        self.api_url_input = QLineEdit(self.api_base_url)
        self.api_url_input.setPlaceholderText("https://example.com")
        self.api_url_input.setStyleSheet(self._input_style())
        self.api_url_input.textChanged.connect(self.on_api_url_changed)

        user_id_lbl = QLabel("PRINTER USER ID")
        user_id_lbl.setStyleSheet(self._label_style(small=True))
        self.printer_user_id_input = QLineEdit("" if self.printer_user_id is None else str(self.printer_user_id))
        self.printer_user_id_input.setPlaceholderText("Fetched from backend")
        self.printer_user_id_input.setReadOnly(True)
        self.printer_user_id_input.setStyleSheet(self._input_style())

        self.pos_worker_status = QLabel("POS worker paused")
        self.pos_worker_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pos_worker_status.setStyleSheet(
            f"background:#2a1f1a; color:{self.C_WARNING}; border:1px solid #5a3b2a;"
            f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
        )

        connect_btn = QPushButton("Test Connection")
        connect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        connect_btn.setStyleSheet(self._btn_secondary())
        connect_btn.clicked.connect(self.test_api_connection)

        cc_layout.addWidget(self.connection_status)
        cc_layout.addWidget(url_lbl)
        cc_layout.addWidget(self.api_url_input)
        cc_layout.addWidget(user_id_lbl)
        cc_layout.addWidget(self.printer_user_id_input)
        cc_layout.addWidget(self.pos_worker_status)
        cc_layout.addWidget(connect_btn)
        config_layout.addWidget(conn_card)

        # ── Update / version card ────────────────────────────────
        config_layout.addWidget(self._section_heading("APP"))

        update_card = QWidget()
        update_card.setStyleSheet(self._card_style(8))
        uc_layout = QVBoxLayout(update_card)
        uc_layout.setContentsMargins(12, 12, 12, 12)
        uc_layout.setSpacing(8)

        self.version_label = QLabel(self._current_version())
        self.version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.version_label.setStyleSheet(
            f"color:{self.C_TEXT_DIM}; font-size:10px; background:transparent; border:none;"
        )

        update_btn = QPushButton("\u21ea  Update App")
        update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        update_btn.setStyleSheet(self._btn_primary())
        update_btn.clicked.connect(self._do_update)

        uc_layout.addWidget(self.version_label)
        uc_layout.addWidget(update_btn)
        config_layout.addWidget(update_card)

        config_layout.addStretch()
        config_scroll.setWidget(config_inner)
        layout.addWidget(config_scroll, stretch=1)

        # ── Bottom status strip ───────────────────────────────────
        status_strip = QWidget()
        status_strip.setMinimumHeight(40)
        status_strip.setMaximumHeight(52)
        status_strip.setStyleSheet(
            f"background:{self.C_SURFACE}; border-top:1px solid {self.C_BORDER};"
        )
        ss_layout = QVBoxLayout(status_strip)
        ss_layout.setContentsMargins(14, 0, 14, 0)
        ss_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.header_connection_status = QLabel("● Disconnected")
        self.header_connection_status.setStyleSheet(
            f"color:{self.C_RED}; font-size:10px; font-weight:600;"
        )
        self.header_printer_status = QLabel("⬡  No printer")
        self.header_printer_status.setStyleSheet(
            f"color:{self.C_TEXT_DIM}; font-size:10px;"
        )

        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        status_row.addWidget(self.header_connection_status)
        status_row.addStretch()
        status_row.addWidget(self.header_printer_status)
        ss_layout.addLayout(status_row)
        layout.addWidget(status_strip)

        return sidebar

    def _section_heading(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color:{self.C_TEXT_DIM}; font-size:9px; font-weight:700;"
            f"letter-spacing:1.2px; background:transparent; border:none;"
        )
        return lbl

    # ── Main content area ─────────────────────────────────────────
    def _build_content(self) -> QWidget:
        content = QWidget()
        content.setStyleSheet(f"background:{self.C_BG};")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 12)
        layout.setSpacing(14)

        # ── Top bar ──────────────────────────────────────────────
        top_bar = self._build_top_bar()
        layout.addWidget(top_bar)

        # thin divider
        div = QFrame()
        div.setFrameShape(QFrame.Shape.HLine)
        div.setFixedHeight(1)
        div.setStyleSheet(f"background:{self.C_BORDER}; border:none;")
        layout.addWidget(div)

        # ── Queue panel ──────────────────────────────────────────
        layout.addWidget(self._build_queue_panel(), stretch=1)

        return content

    def _build_top_bar(self) -> QWidget:
        bar = QWidget()
        bar.setStyleSheet("background:transparent;")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(0, 0, 0, 0)
        bar_layout.setSpacing(10)

        title = QLabel("Print Queue")
        title.setStyleSheet(
            f"color:{self.C_TEXT}; font-size:20px; font-weight:700; background:transparent;"
        )
        bar_layout.addWidget(title)
        bar_layout.addStretch()

        refresh_btn = QPushButton("↻   Refresh")
        refresh_btn.setMinimumSize(110, 36)
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.setStyleSheet(self._btn_secondary())
        refresh_btn.clicked.connect(self.fetch_pending_requests)
        bar_layout.addWidget(refresh_btn)

        preview_btn = QPushButton("Preview TSPL")
        preview_btn.setMinimumHeight(36)
        preview_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        preview_btn.setStyleSheet(self._btn_secondary())
        preview_btn.clicked.connect(self.show_tspl_preview)
        bar_layout.addWidget(preview_btn)

        visual_btn = QPushButton("⬜ Visual Preview")
        visual_btn.setMinimumHeight(36)
        visual_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        visual_btn.setStyleSheet(self._btn_primary())
        visual_btn.clicked.connect(self.show_visual_preview)
        bar_layout.addWidget(visual_btn)

        history_btn = QPushButton("History / Reprint")
        history_btn.setMinimumHeight(36)
        history_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        history_btn.setStyleSheet(self._btn_secondary())
        history_btn.clicked.connect(self.show_print_history)
        bar_layout.addWidget(history_btn)

        return bar
    
    # (create_header removed — replaced by _build_sidebar / _build_top_bar)
    
    # (create_printer_panel removed — replaced by _build_sidebar)
    
    def _build_queue_panel(self) -> QWidget:
        """Build the print queue panel (right / main content area)."""
        panel = QWidget()
        panel.setStyleSheet("background:transparent;")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # ── Table ────────────────────────────────────────────────
        self.requests_table = QTableWidget()
        self.requests_table.setColumnCount(6)
        self.requests_table.setHorizontalHeaderLabels(
            ["ID", "Source", "Created By", "Labels", "Created At", ""]
        )
        hdr = self.requests_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self.requests_table.setColumnWidth(5, 118)
        self.requests_table.verticalHeader().setVisible(False)
        self.requests_table.setShowGrid(False)
        self.requests_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.requests_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.requests_table.setAlternatingRowColors(False)
        self.requests_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.requests_table.verticalHeader().setDefaultSectionSize(46)
        self.requests_table.itemSelectionChanged.connect(self._on_request_selection_changed)
        self.requests_table.setStyleSheet(f"""
            QTableWidget {{
                background:{self.C_SURFACE};
                border:1px solid {self.C_BORDER};
                border-radius:8px;
                color:{self.C_TEXT};
                font-size:12px;
                outline:none;
                gridline-color:transparent;
            }}
            QTableWidget::item {{
                padding:0 12px;
                border-bottom:1px solid {self.C_BORDER};
            }}
            QTableWidget::item:selected {{
                background:{self.C_SURFACE2};
                color:{self.C_ORANGE};
            }}
            QHeaderView::section {{
                background:{self.C_SURFACE};
                color:{self.C_TEXT_DIM};
                font-size:10px; font-weight:700;
                letter-spacing:0.8px;
                padding:10px 12px;
                border:none;
                border-bottom:1px solid {self.C_BORDER};
            }}
            QScrollBar:vertical {{
                width:6px; background:transparent;
            }}
            QScrollBar::handle:vertical {{
                background:{self.C_BORDER}; border-radius:3px;
            }}
        """)
        layout.addWidget(self.requests_table, stretch=1)

        # ── Detail card ──────────────────────────────────────────
        detail_card = QWidget()
        detail_card.setMinimumHeight(116)
        detail_card.setMaximumHeight(220)
        detail_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        detail_card.setStyleSheet(
            f"background:{self.C_SURFACE}; border:1px solid {self.C_BORDER}; border-radius:8px;"
        )
        dc_layout = QVBoxLayout(detail_card)
        dc_layout.setContentsMargins(14, 10, 14, 10)
        dc_layout.setSpacing(4)

        detail_heading = QLabel("REQUEST DETAILS")
        detail_heading.setStyleSheet(
            f"color:{self.C_TEXT_DIM}; font-size:9px; font-weight:700;"
            f"letter-spacing:1.1px; background:transparent; border:none;"
        )
        self.details_text = QTextEdit()
        self.details_text.setReadOnly(True)
        self.details_text.setFrameShape(QFrame.Shape.NoFrame)
        self.details_text.setStyleSheet(
            f"background:transparent; color:{self.C_TEXT_DIM};"
            f"font-family:'Courier New',monospace; font-size:11px; border:none;"
        )
        dc_layout.addWidget(detail_heading)
        dc_layout.addWidget(self.details_text)
        layout.addWidget(detail_card)

        # ── Progress bar ─────────────────────────────────────────
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setMinimumHeight(6)
        self.progress_bar.setMaximumHeight(10)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet(f"""
            QProgressBar {{
                background:{self.C_SURFACE2};
                border:none; border-radius:3px;
            }}
            QProgressBar::chunk {{
                background:{self.C_ORANGE}; border-radius:3px;
            }}
        """)
        layout.addWidget(self.progress_bar)

        return panel
    
    def setup_auto_refresh(self):
        """Setup automatic refresh timer."""
        self.refresh_timer = QTimer()
        self.refresh_timer.timeout.connect(self.fetch_pending_requests)
        self.refresh_timer.start(30000)  # Refresh every 30 seconds

        self.pos_refresh_timer = QTimer()
        self.pos_refresh_timer.timeout.connect(self.poll_pos_slips)
        self.pos_refresh_timer.start(max(1, int(self.pos_poll_interval_seconds)) * 1000)

        # Poll backend config version so token/config updates are picked up quickly.
        self.config_refresh_timer = QTimer()
        self.config_refresh_timer.timeout.connect(
            lambda: self.fetch_printer_config(show_dialogs=False)
        )
        self.config_refresh_timer.start(60_000)
    
    def scan_for_printers(self):
        """Scan for available USB printers."""
        self.status_bar.showMessage("Scanning for printers...")
        self.scanner = PrinterScanner()
        self.scanner.printers_found.connect(self.on_printers_found)
        self.scanner.start()
    
    def on_printers_found(self, printers: List[str]):
        """Handle printer scan results."""
        self.printer_combo.clear()
        self.pos_printer_combo.clear()
        
        self.discovered_printers = list(printers or [])

        if not printers:
            self.printer_combo.addItem("No printers found")
            self.pos_printer_combo.addItem("No POS printers found")
            self.status_bar.showMessage("No printers found")
        else:
            self.printer_combo.addItem("Select a printer...")
            self.pos_printer_combo.addItem("Select POS slip printer...")
            for printer in printers:
                self.printer_combo.addItem(printer)
                self.pos_printer_combo.addItem(printer)
            self.status_bar.showMessage(f"Found {len(printers)} printer(s)")

            if self.last_selected_printer and self.last_selected_printer in printers:
                index = self.printer_combo.findText(self.last_selected_printer)
                if index >= 0:
                    self.printer_combo.setCurrentIndex(index)

            if self.last_selected_pos_printer and self.last_selected_pos_printer in printers:
                pos_index = self.pos_printer_combo.findText(self.last_selected_pos_printer)
                if pos_index >= 0:
                    self.pos_printer_combo.setCurrentIndex(pos_index)

            self.upload_discovered_printers_if_ready()
    
    def on_printer_selected(self, index: int):
        """Handle printer selection."""
        if index > 0:  # Skip placeholder
            self.selected_printer = self.printer_combo.currentText()
            self.last_selected_printer = self.selected_printer
            self.save_settings()
            self.status_bar.showMessage(f"Selected printer: {self.selected_printer}")
            self.printer_calibrated = False
            self.calibration_status.setText("Not calibrated")
            self.calibration_status.setStyleSheet(
                f"background:#2a1a1a; color:{self.C_RED}; border:1px solid #5a2a2a;"
                f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
            )
            self.header_printer_status.setText(
                f"⬡  {self.selected_printer.split('/')[-1].upper()}"
            )
            self.header_printer_status.setStyleSheet(
                f"color:{self.C_ORANGE}; font-size:10px;"
            )
        else:
            self.selected_printer = None
            self.header_printer_status.setText("⬡  No printer")
            self.header_printer_status.setStyleSheet(
                f"color:{self.C_TEXT_DIM}; font-size:10px;"
            )

    def on_pos_printer_selected(self, index: int):
        """Handle POS printer selection."""
        if index > 0:
            self.pos_selected_printer = self.pos_printer_combo.currentText()
            self.last_selected_pos_printer = self.pos_selected_printer
            self.save_settings()
            self._update_pos_worker_status()
        else:
            self.pos_selected_printer = None
            self._update_pos_worker_status()

    def on_printer_user_id_changed(self, text: str):
        """Persist printer user id for POS queue scoping."""
        value = text.strip()
        if value.isdigit() and int(value) > 0:
            self.printer_user_id = int(value)
        else:
            self.printer_user_id = None
        self.save_settings()
        self._update_pos_worker_status()

    def _update_pos_worker_status(self, extra_note: Optional[str] = None):
        """Refresh POS worker readiness indicator."""
        ready = bool(self.pos_selected_printer and self.printer_user_id)
        if ready:
            text = "POS worker ready"
            if extra_note:
                text = f"POS worker ready · {extra_note}"
            self.pos_worker_status.setText(text)
            self.pos_worker_status.setStyleSheet(
                f"background:#0f2a1a; color:{self.C_GREEN}; border:1px solid #1a5a2a;"
                f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
            )
        else:
            text = "POS worker paused"
            if extra_note:
                text = f"POS worker paused · {extra_note}"
            self.pos_worker_status.setText(text)
            self.pos_worker_status.setStyleSheet(
                f"background:#2a1f1a; color:{self.C_WARNING}; border:1px solid #5a3b2a;"
                f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
            )

    def _headers(self, include_json: bool = False) -> Dict[str, str]:
        headers = {"X-Printer-API-Key": self.api_key}
        if self.printer_user_id:
            headers["X-Printer-User-Id"] = str(self.printer_user_id)
        if include_json:
            headers["Content-Type"] = "application/json"
        return headers

    def _pos_headers(self, include_json: bool = False) -> Dict[str, str]:
        return self._headers(include_json=include_json)

    def _complete_pos_request(self, request_id: int) -> bool:
        """Mark a POS request as complete. Returns True when resolved."""
        try:
            response = requests.post(
                f"{self.api_base_url}/admin/api/pos-slips/complete",
                headers=self._pos_headers(include_json=True),
                json={"request_id": request_id},
                timeout=10,
            )
            if response.status_code == 200:
                return True
            if response.status_code in (400, 404):
                return True
            return False
        except Exception:
            return False

    def _complete_pos_eod_request(self, request_id: int) -> bool:
        """Mark a POS EOD report request as complete. Returns True when resolved."""
        try:
            response = requests.post(
                f"{self.api_base_url}/admin/api/pos-eod-reports/complete",
                headers=self._pos_headers(include_json=True),
                json={"request_id": request_id},
                timeout=10,
            )
            if response.status_code == 200:
                return True
            if response.status_code in (400, 404):
                return True
            return False
        except Exception:
            return False

    def _ensure_latest_pos_eod_report(self) -> None:
        """Ask backend to auto-queue/update previous-day EOD report for this printer user."""
        try:
            requests.post(
                f"{self.api_base_url}/admin/api/pos-eod-reports/ensure-latest",
                headers=self._pos_headers(include_json=True),
                json={},
                timeout=8,
            )
        except Exception:
            # Best effort only; polling pending queue remains source of truth.
            return

    def poll_pos_slips(self):
        """Dispatch a POSPollWorker (non-blocking) to handle the full poll cycle.

        All blocking HTTP I/O is performed inside POSPollWorker.run() on a
        background QThread.  This method returns immediately so the Qt event
        loop — and the UI — are never frozen by network calls.
        """
        if self.pos_print_job and self.pos_print_job.isRunning():
            return
        if self.pos_eod_print_job and self.pos_eod_print_job.isRunning():
            return
        if self._pos_poll_worker and self._pos_poll_worker.isRunning():
            return
        if not self.api_key:
            self._update_pos_worker_status("Missing PRINTER_API_KEY")
            return
        if not self.pos_selected_printer or not self.printer_user_id:
            self._update_pos_worker_status()
            return
        if time.time() < self.pos_backoff_until:
            return

        # Flush any pending completion retries (lightweight POSTs)
        if self.pos_completion_retry_ids:
            retry_ids = sorted(list(self.pos_completion_retry_ids))
            for req_id in retry_ids:
                if self._complete_pos_request(req_id):
                    self.pos_completion_retry_ids.discard(req_id)
        if self.pos_eod_completion_retry_ids:
            retry_ids = sorted(list(self.pos_eod_completion_retry_ids))
            for req_id in retry_ids:
                if self._complete_pos_eod_request(req_id):
                    self.pos_eod_completion_retry_ids.discard(req_id)

        self._pos_poll_worker = POSPollWorker(
            api_base=self.api_base_url,
            api_key=self.api_key,
            user_id=self.printer_user_id,
            in_flight_ids=frozenset(self.pos_in_flight_ids),
            eod_in_flight_ids=frozenset(self.pos_eod_in_flight_ids),
        )
        self._pos_poll_worker.slip_ready.connect(self._on_poll_slip_ready)
        self._pos_poll_worker.eod_slip_ready.connect(self._on_poll_eod_slip_ready)
        self._pos_poll_worker.all_clear.connect(self._on_poll_all_clear)
        self._pos_poll_worker.poll_error.connect(self._on_poll_error)
        self._pos_poll_worker.poll_fatal.connect(self._on_poll_fatal)
        self._pos_poll_worker.start()

    def _register_pos_backoff(self):
        """Apply exponential backoff for transient POS API failures."""
        self.pos_backoff_until = time.time() + min(self.pos_backoff_seconds, 30)
        self.pos_backoff_seconds = min(self.pos_backoff_seconds * 2, 30)

    def _start_pos_print(self, request_id: int):
        """Fetch detail payload and start POS print job."""
        try:
            response = requests.get(
                f"{self.api_base_url}/admin/api/pos-slips/request/{request_id}",
                headers=self._pos_headers(),
                timeout=10,
            )
        except Exception as e:
            self.pos_in_flight_ids.discard(request_id)
            self.status_bar.showMessage(f"POS detail error #{request_id}: {e}")
            return

        if response.status_code == 404:
            self.pos_in_flight_ids.discard(request_id)
            return
        if response.status_code != 200:
            self.pos_in_flight_ids.discard(request_id)
            self.status_bar.showMessage(f"POS detail failed #{request_id}: {response.status_code}")
            return

        detail = response.json()
        self.pos_print_job = POSSlipPrintJob(self.pos_selected_printer, detail)
        self.pos_print_job.finished.connect(lambda s, m: self._on_pos_print_finished(s, m, request_id))
        self.pos_print_job.start()
        self.status_bar.showMessage(f"Printing POS slip #{request_id}...")

    def _on_pos_print_finished(self, success: bool, message: str, request_id: int):
        """Handle POS print completion and completion API semantics."""
        self.pos_print_job = None

        if success:
            self.last_successful_pos_print_at = datetime.now()
            complete_ok = self._complete_pos_request(request_id)
            if not complete_ok:
                self.pos_completion_retry_ids.add(request_id)
                self.status_bar.showMessage(
                    f"POS slip #{request_id} printed; completion retry scheduled"
                )
            else:
                self.status_bar.showMessage(f"POS slip #{request_id} printed and completed")
        else:
            self.status_bar.showMessage(f"POS slip #{request_id} failed: {message}")

        self.pos_in_flight_ids.discard(request_id)

    def _start_pos_eod_print(self, request_id: int):
        """Fetch EOD report payload and start receipt print job."""
        try:
            response = requests.get(
                f"{self.api_base_url}/admin/api/pos-eod-reports/request/{request_id}",
                headers=self._pos_headers(),
                timeout=10,
            )
        except Exception as e:
            self.pos_eod_in_flight_ids.discard(request_id)
            self.status_bar.showMessage(f"POS EOD detail error #{request_id}: {e}")
            return

        if response.status_code == 404:
            self.pos_eod_in_flight_ids.discard(request_id)
            return
        if response.status_code != 200:
            self.pos_eod_in_flight_ids.discard(request_id)
            self.status_bar.showMessage(f"POS EOD detail failed #{request_id}: {response.status_code}")
            return

        detail = response.json()
        self.pos_eod_print_job = POSEODReportPrintJob(self.pos_selected_printer, detail)
        self.pos_eod_print_job.finished.connect(lambda s, m: self._on_pos_eod_print_finished(s, m, request_id))
        self.pos_eod_print_job.start()
        self.status_bar.showMessage(f"Printing POS EOD report #{request_id}...")

    def _on_pos_eod_print_finished(self, success: bool, message: str, request_id: int):
        """Handle POS EOD receipt print completion semantics."""
        self.pos_eod_print_job = None

        if success:
            complete_ok = self._complete_pos_eod_request(request_id)
            if not complete_ok:
                self.pos_eod_completion_retry_ids.add(request_id)
                self.status_bar.showMessage(
                    f"POS EOD report #{request_id} printed; completion retry scheduled"
                )
            else:
                self.status_bar.showMessage(f"POS EOD report #{request_id} printed and completed")
        else:
            self.status_bar.showMessage(f"POS EOD report #{request_id} failed: {message}")

        self.pos_eod_in_flight_ids.discard(request_id)

    # ── POSPollWorker signal handlers ──────────────────────────────────────

    def _on_poll_slip_ready(self, request_id: int, detail: dict):
        """POS slip detail fetched off-thread; start print job on main thread."""
        self.pos_in_flight_ids.add(request_id)
        self.last_successful_pos_poll_at = datetime.now()
        self.pos_backoff_seconds = 1
        self.pos_backoff_until = 0.0
        self._update_pos_worker_status("POS API connected")
        self.pos_print_job = POSSlipPrintJob(self.pos_selected_printer, detail)
        self.pos_print_job.finished.connect(
            lambda s, m: self._on_pos_print_finished(s, m, request_id)
        )
        self.pos_print_job.start()
        self.status_bar.showMessage(f"Printing POS slip #{request_id}...")

    def _on_poll_eod_slip_ready(self, request_id: int, detail: dict):
        """EOD report detail fetched off-thread; start print job on main thread."""
        self.pos_eod_in_flight_ids.add(request_id)
        self.last_successful_pos_poll_at = datetime.now()
        self._update_pos_worker_status("POS API connected")
        self.pos_eod_print_job = POSEODReportPrintJob(self.pos_selected_printer, detail)
        self.pos_eod_print_job.finished.connect(
            lambda s, m: self._on_pos_eod_print_finished(s, m, request_id)
        )
        self.pos_eod_print_job.start()
        self.status_bar.showMessage(f"Printing POS EOD report #{request_id}...")

    def _on_poll_all_clear(self):
        """Nothing pending — update poll timestamp and reset backoff."""
        self.last_successful_pos_poll_at = datetime.now()
        self.pos_backoff_seconds = 1
        self.pos_backoff_until = 0.0
        self._update_pos_worker_status("POS API connected")

    def _on_poll_error(self, message: str, status_code: int):
        """Transient poll error — apply exponential backoff."""
        self._register_pos_backoff()
        self._update_pos_worker_status("POS API network retry")
        self.status_bar.showMessage(f"POS poll error: {message}")

    def _on_poll_fatal(self, message: str, status_code: int):
        """Unrecoverable auth/config error — log and let operator fix config."""
        self._update_pos_worker_status(f"POS API error {status_code}")
        self.status_bar.showMessage(f"POS worker stopped: {message}")

    def calibrate_printer(self):
        """Calibrate printer and print test label."""
        if not self.selected_printer:
            QMessageBox.warning(self, "No Printer", "Please select a printer first.")
            return

        if self.calibration_job and self.calibration_job.isRunning():
            QMessageBox.information(self, "Calibration In Progress", "Calibration is already running.")
            return

        # ── 1. Send the TSPL calibration sequence ────────────────────
        calibration_tspl = (
            "SIZE 40 mm,30 mm\n"
            "GAP 2 mm,0\n"
            "DIRECTION 0\n"
            "REFERENCE 0,0\n"
            "SET TEAR ON\n"
            "SPEED 4\n"
            "DENSITY 8\n"
            "GAPDETECT\n"   # physically feeds and measures the gap
            "HOME\n"        # advance to first clean label start
        )
        try:
            with open(self.selected_printer, 'wb') as printer:
                printer.write(calibration_tspl.encode('utf-8'))
        except PermissionError:
            QMessageBox.critical(
                self, "Permission Denied",
                f"Cannot write to {self.selected_printer}.\n\n"
                f"The printer device requires your user account to be in the 'lp' group.\n\n"
                f"Re-run the install script to fix this automatically, or run:\n"
                f"  sudo usermod -aG lp $USER\n\n"
                f"Then log out and back in (or reboot) for the change to take effect."
            )
            return
        except Exception as e:
            QMessageBox.critical(self, "Calibration Error", f"Failed to calibrate: {e}")
            return

        # Give the printer time to run the gap-detection feed (~1.5 s typical)
        time.sleep(1.5)

        # ── 2. Print a test label ─────────────────────────────────────
        test_item = {
            "title": "VULA! PRINT",
            "variant_label": "Calibration Test",
            "sku": "CALIB-TEST",
            "code39": "CALIBTEST",
            "price_cents": 95000,
            "currency": "ZAR"
        }

        self.calibration_job = PrintJob(self.selected_printer, [test_item])
        self.calibration_job.finished.connect(self.on_test_print_finished)
        self.calibration_job.start()

        self.status_bar.showMessage("Calibrating printer…")

    
    def on_test_print_finished(self, success: bool, message: str):
        """Handle test print completion."""
        self.calibration_job = None
        if success:
            reply = QMessageBox.question(
                self,
                "Test Print",
                "Test label printed. Does it look correct?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.printer_calibrated = True
                self.calibration_status.setText("Calibrated")
                self.calibration_status.setStyleSheet(
                    f"background:#0f2a1a; color:{self.C_GREEN}; border:1px solid #1a5a2a;"
                    f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
                )
                self.status_bar.showMessage("Printer calibrated successfully")
                self.header_printer_status.setText(
                    f"✓  {self.selected_printer.split('/')[-1].upper()}"
                )
                self.header_printer_status.setStyleSheet(
                    f"color:{self.C_GREEN}; font-size:10px;"
                )
            else:
                QMessageBox.information(
                    self,
                    "Calibration Help",
                    "Please check:\n"
                    "- Label size is 40mm x 30mm\n"
                    "- Gap is 2mm\n"
                    "- Printer alignment settings\n\n"
                    "Try calibrating again or adjust printer settings."
                )
        else:
            QMessageBox.critical(self, "Test Print Failed", message)
    
    def test_api_connection(self):
        """Test connection to backend API."""
        self.fetch_printer_config(show_dialogs=True)
    
    def on_api_url_changed(self, text: str):
        """Handle API URL change."""
        self.api_base_url = text.strip()
        self.save_settings()
    
    def fetch_pending_requests(self):
        """Fetch pending print requests from API."""
        try:
            headers = self._headers()
            response = requests.get(
                f"{self.api_base_url}/admin/api/label-printing/pending",
                headers=headers,
                timeout=10
            )
            
            if response.status_code == 200:
                self.pending_requests = response.json()
                self.update_requests_table()
                self.status_bar.showMessage(f"Loaded {len(self.pending_requests)} pending request(s)")
            else:
                self.status_bar.showMessage(f"Failed to fetch requests: {response.status_code}")
                
        except Exception as e:
            self.status_bar.showMessage(f"Error fetching requests: {e}")
    
    def update_requests_table(self):
        """Update the requests table with pending requests."""
        self.requests_table.setRowCount(len(self.pending_requests))

        for row, request in enumerate(self.pending_requests):
            def _cell(text: str, align=Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft) -> QTableWidgetItem:
                item = QTableWidgetItem(text)
                item.setTextAlignment(align)
                return item

            self.requests_table.setItem(row, 0, _cell(
                str(request.get("id", "")),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter
            ))
            source = request.get("source", "").replace("_", " ").title()
            self.requests_table.setItem(row, 1, _cell(source))
            self.requests_table.setItem(row, 2, _cell(request.get("created_by_username", "")))
            self.requests_table.setItem(row, 3, _cell(
                str(request.get("total_labels", 0)),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter
            ))

            created_at = request.get("created_at", "")
            if created_at:
                try:
                    dt_obj = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    created_at = dt_obj.strftime("%d %b %Y  %H:%M")
                except Exception:
                    pass
            self.requests_table.setItem(row, 4, _cell(created_at))

            print_btn = QPushButton("Print")
            print_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            print_btn.setStyleSheet(self._btn_primary())
            print_btn.clicked.connect(lambda checked, r=request: self.print_request(r))
            # Wrap in a widget so padding looks right
            btn_wrap = QWidget()
            btn_wrap.setStyleSheet(f"background:{self.C_SURFACE};")
            bw_layout = QHBoxLayout(btn_wrap)
            bw_layout.setContentsMargins(8, 5, 8, 5)
            bw_layout.addWidget(print_btn)
            self.requests_table.setCellWidget(row, 5, btn_wrap)

        if self.pending_requests:
            self.requests_table.selectRow(0)
            self.show_request_details(self.pending_requests[0])
    
    def show_request_details(self, request: Dict[str, Any]):
        """Show details of selected request."""
        try:
            headers = self._headers()
            response = requests.get(
                f"{self.api_base_url}/admin/api/label-printing/request/{request['id']}",
                headers=headers,
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                items = data.get("items", [])
                
                details = f"Request ID: {request['id']}\n"
                details += f"Source: {request.get('source', '')}\n"
                details += f"Note: {request.get('note', '')}\n"
                details += f"Total Labels: {request.get('total_labels', 0)}\n\n"
                details += "Items:\n"
                details += "-" * 50 + "\n"
                
                for item in items:
                    details += f"• {item.get('title', '')} - {item.get('variant_label', '')}\n"
                    details += f"  SKU: {item.get('sku', '')} | Qty: {item.get('qty_to_print', 0)}\n"
                
                self.details_text.setText(details)
            
        except Exception as e:
            self.details_text.setText(f"Error loading details: {e}")
    
    def print_request(self, request: Dict[str, Any]):
        """Print labels for a specific request."""
        if not self.selected_printer:
            QMessageBox.warning(self, "No Printer", "Please select a printer first.")
            return
        
        if not self.printer_calibrated:
            reply = QMessageBox.question(
                self,
                "Printer Not Calibrated",
                "Printer has not been calibrated. Print anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                return
        
        try:
            # Fetch request details
            headers = self._headers()
            response = requests.get(
                f"{self.api_base_url}/admin/api/label-printing/request/{request['id']}",
                headers=headers,
                timeout=10
            )
            
            if response.status_code != 200:
                QMessageBox.critical(self, "Error", "Failed to fetch print job details")
                return
            
            data = response.json()
            items = data.get("items", [])
            
            if not items:
                QMessageBox.warning(self, "No Items", "This request has no items to print.")
                return
            
            # Track for history saving
            self._current_print_request = request

            # Start print job
            self.progress_bar.setVisible(True)
            self.progress_bar.setValue(0)

            self.print_job = PrintJob(self.selected_printer, items)
            self.print_job.progress.connect(self.on_print_progress)
            self.print_job.finished.connect(lambda s, m: self.on_print_finished(s, m, request['id']))
            self.print_job.start()
            
            self.status_bar.showMessage(f"Printing request #{request['id']}...")
            
        except Exception as e:
            QMessageBox.critical(self, "Print Error", f"Failed to start print job: {e}")
            self.progress_bar.setVisible(False)
    
    def on_print_progress(self, current: int, total: int):
        """Update progress bar."""
        if total > 0:
            percentage = int((current / total) * 100)
            self.progress_bar.setValue(percentage)
            self.status_bar.showMessage(f"Printing: {current}/{total} labels")
    
    def on_print_finished(self, success: bool, message: str, request_id: int):
        """Handle print job completion."""
        self.progress_bar.setVisible(False)
        
        if success:
            # Mark as completed on server
            try:
                headers = self._headers(include_json=True)
                response = requests.post(
                    f"{self.api_base_url}/admin/api/label-printing/complete",
                    headers=headers,
                    json={"request_id": request_id},
                    timeout=10
                )
                
                if response.status_code == 200:
                    self._save_to_history(self._current_print_request)
                    QMessageBox.information(self, "Success", message)
                    self.fetch_pending_requests()  # Refresh list
                else:
                    QMessageBox.warning(
                        self,
                        "Print Complete",
                        f"{message}\n\nWarning: Failed to mark as completed on server."
                    )
            except Exception as e:
                QMessageBox.warning(
                    self,
                    "Print Complete",
                    f"{message}\n\nWarning: Failed to communicate with server: {e}"
                )
        else:
            QMessageBox.critical(self, "Print Failed", message)
        
        self.status_bar.showMessage("Ready")

    # ─────────────────────────────────────────────────────────────
    # Selection tracking
    # ─────────────────────────────────────────────────────────────
    def _on_request_selection_changed(self):
        """Track the currently selected row so Preview TSPL knows which request to show."""
        row = self.requests_table.currentRow()
        if 0 <= row < len(self.pending_requests):
            self._selected_request = self.pending_requests[row]
            self.show_request_details(self._selected_request)
        else:
            self._selected_request = None

    # ─────────────────────────────────────────────────────────────
    # TSPL Visualizer
    # ─────────────────────────────────────────────────────────────
    def show_tspl_preview(self):
        """Open a dialog showing the raw TSPL commands for the selected print request."""
        request = self._selected_request
        if not request:
            if self.pending_requests:
                request = self.pending_requests[0]
            else:
                QMessageBox.information(self, "No Request Selected",
                    "Select a request from the queue first, or refresh to load requests.")
                return

        try:
            headers = self._headers()
            response = requests.get(
                f"{self.api_base_url}/admin/api/label-printing/request/{request['id']}",
                headers=headers, timeout=10
            )
            if response.status_code != 200:
                QMessageBox.warning(self, "Cannot Load", f"Server returned {response.status_code}.")
                return
            items = response.json().get("items", [])
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to fetch items: {e}")
            return

        if not items:
            QMessageBox.information(self, "No Items", "This request has no items.")
            return

        # Build a temporary PrintJob just to use _generate_label_tspl
        preview_job = PrintJob("", items)
        lines = []
        lines.append(f"=== TSPL PREVIEW: Request #{request['id']} ===")
        lines.append(f"Total items: {len(items)}  |  Total labels: "
                     f"{sum(i.get('qty_to_print', 0) for i in items)}")
        lines.append("")
        for idx, item in enumerate(items[:10], 1):   # preview first 10
            lines.append(f"{'─' * 60}")
            lines.append(f"[{idx}]  {item.get('title','')}  "
                         f"({item.get('variant_label','')})  "
                         f"x{item.get('qty_to_print', 0)}")
            lines.append("")
            lines.append(preview_job._generate_label_tspl(item))
        if len(items) > 10:
            lines.append(f"... and {len(items) - 10} more items (showing first 10)")

        dialog = _TextDialog(
            parent=self,
            title=f"TSPL Preview — Request #{request['id']}",
            content="\n".join(lines),
            color_bg=self.C_BG,
            color_text=self.C_TEXT,
            color_border=self.C_BORDER,
            color_surface=self.C_SURFACE,
            color_orange=self.C_ORANGE,
        )
        dialog.exec()

    # ─────────────────────────────────────────────────────────────
    # Visual QPainter label preview
    # ─────────────────────────────────────────────────────────────
    def show_visual_preview(self):
        """Open a rendered visual preview of how labels will look when printed."""
        request = self._selected_request
        if not request:
            if self.pending_requests:
                request = self.pending_requests[0]
            else:
                QMessageBox.information(
                    self, "No Request Selected",
                    "Select a request from the queue first, or refresh to load requests.",
                )
                return

        try:
            headers  = {"X-API-Key": self.api_key}
            response = requests.get(
                f"{self.api_base_url}/admin/api/label-printing/request/{request['id']}",
                headers=headers, timeout=10,
            )
            if response.status_code != 200:
                QMessageBox.warning(
                    self, "Cannot Load",
                    f"Server returned {response.status_code}.",
                )
                return
            items = response.json().get("items", [])
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to fetch items: {e}")
            return

        if not items:
            QMessageBox.information(self, "No Items", "This request has no items.")
            return

        dialog = _VisualPreviewDialog(
            parent         = self,
            request_id     = request['id'],
            items          = items,
            color_bg       = self.C_BG,
            color_text     = self.C_TEXT,
            color_text_dim = self.C_TEXT_DIM,
            color_border   = self.C_BORDER,
            color_surface  = self.C_SURFACE,
            color_orange   = self.C_ORANGE,
        )
        dialog.exec()

    # ─────────────────────────────────────────────────────────────
    # Standalone Test Label (not coupled to calibration)
    # ─────────────────────────────────────────────────────────────
    def print_test_label_standalone(self):
        """Print a single representative test label to check layout without calibrating."""
        if not self.selected_printer:
            QMessageBox.warning(self, "No Printer", "Please select a printer first.")
            return

        test_item = {
            "title": "Vula! Print",
            "variant_label": "Al Maisa Cape - Black",
            "sku": "ALM-CAP-SIN-BLK-L",
            "code39": "99001",
            "price_cents": 95000,
            "currency": "ZAR",
            "qty_to_print": 1,
        }

        reply = QMessageBox.question(
            self, "Print Test Label",
            "This will print 1 test label using sample data.\n"
            "SKU: ALM-CAP-SIN-BLK-L  |  Price: R950.00\n\n"
            "Proceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        job = PrintJob(self.selected_printer, [test_item])
        job.finished.connect(self._on_test_label_standalone_finished)
        self.status_bar.showMessage("Printing test label…")
        job.start()
        # Keep a reference so it isn't GC'd
        self._test_label_job = job

    def _on_test_label_standalone_finished(self, success: bool, message: str):
        self._test_label_job = None
        if success:
            QMessageBox.information(self, "Test Label Sent",
                "Test label sent to printer.\n\n"
                "Check the label for:\n"
                "  • Title and variant text at top\n"
                "  • Price in font 3 (medium, not giant)\n"
                "  • Barcode fits on the 40 mm width\n"
                "  • SKU readable at bottom")
        else:
            QMessageBox.critical(self, "Test Label Failed", message)
        self.status_bar.showMessage("Ready")

    def _build_sample_pos_payload(self) -> Dict[str, Any]:
        """Build a six-item sample payload for POS printer testing."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return {
            "request": {
                "id": 999999,
                "invoice_number": "TEST-POS-0001",
                "created_at": now,
                "payment_type": "card",
            },
            "business": {
                "brand_name": "Vula! Print Demo Store",
                "phone": "+27 11 555 0101",
                "email": "info@vula.local",
                "vat_number": "4555555555",
                "address_line1": "1 Orange Street",
                "address_line2": "Unit B",
                "city": "Johannesburg",
                "province": "Gauteng",
                "postal_code": "2000",
                "country": "ZA",
            },
            "store": {
                "name": "Sandton Demo Counter",
                "address": "123 Example Ave\nSandton\nGauteng\n2196\nZA",
                "phone": "+27 11 555 0111",
                "email": "sandton@vula.local",
            },
            "cashier_username": "printer_test",
            "customer_email": "",
            "footer_note": "Test print completed. Please verify alignment and cutter.",
            "items": [
                {"qty": 1, "title": "Premium Hoodie", "variant_label": "Black / M", "sku": "HD-BLK-M", "unit_price_cents": 89900, "line_tax_cents": 11726, "line_total_cents": 89900},
                {"qty": 2, "title": "Athletic Socks", "variant_label": "White / L", "sku": "SOCK-WHT-L", "unit_price_cents": 12900, "line_tax_cents": 3366, "line_total_cents": 25800},
                {"qty": 1, "title": "Sports Bottle", "variant_label": "750ml", "sku": "BOT-750", "unit_price_cents": 14900, "line_tax_cents": 1943, "line_total_cents": 14900},
                {"qty": 1, "title": "Running Cap", "variant_label": "Grey", "sku": "CAP-GRY", "unit_price_cents": 19900, "line_tax_cents": 2596, "line_total_cents": 19900},
                {"qty": 1, "title": "Compression Tee", "variant_label": "Navy / XL", "sku": "TEE-NVY-XL", "unit_price_cents": 34900, "line_tax_cents": 4552, "line_total_cents": 34900},
                {"qty": 1, "title": "Gift Wrap", "variant_label": "Standard", "sku": "WRAP-STD", "unit_price_cents": 2500, "line_tax_cents": 326, "line_total_cents": 2500},
            ],
            "totals": {
                "vat_bps": 1500,
                "tax_cents": 24509,
                "subtotal_before_discount_cents": 198900,
                "manual_discount_cents": 20000,
                "voucher_discount_cents": 0,
                "subtotal_cents": 178900,
                "total_cents": 203409,
                "currency": "ZAR",
            },
            "website_url": "https://www.example.com/",
            "logo_url": "",
            "qr_data": "https://www.example.com/",
        }

    def print_test_pos_slip(self):
        """Print a six-item sample POS slip for cutter/alignment verification."""
        if not self.pos_selected_printer:
            QMessageBox.warning(self, "No POS Printer", "Please select a POS slip printer first.")
            return
        if self.pos_print_job and self.pos_print_job.isRunning():
            QMessageBox.information(self, "POS Print Busy", "A POS slip is already printing.")
            return

        confirm = QMessageBox.question(
            self,
            "Test POS Printer",
            "This prints a sample POS slip with 6 items and performs paper cut.\n\nProceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        sample_payload = self._build_sample_pos_payload()
        self.pos_print_job = POSSlipPrintJob(self.pos_selected_printer, sample_payload)
        self.pos_print_job.finished.connect(self._on_test_pos_finished)
        self.pos_print_job.start()
        self.status_bar.showMessage("Printing sample POS slip...")

    def _on_test_pos_finished(self, success: bool, message: str):
        self.pos_print_job = None
        if success:
            QMessageBox.information(
                self,
                "POS Test Printed",
                "Sample POS slip printed and cut.\n\n"
                "Verify text clarity, spacing, and cutter operation.",
            )
        else:
            QMessageBox.critical(self, "POS Test Failed", message)
        self.status_bar.showMessage("Ready")

    # ─────────────────────────────────────────────────────────────
    # Print History & Reprint
    # ─────────────────────────────────────────────────────────────
    def _save_to_history(self, request: Optional[Dict[str, Any]]):
        """Append a successfully printed request to the local history file."""
        if not request:
            return
        try:
            APP_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            history: list = []
            if APP_HISTORY_FILE.exists():
                try:
                    with open(APP_HISTORY_FILE, "r", encoding="utf-8") as f:
                        history = json.load(f)
                    if not isinstance(history, list):
                        history = []
                except Exception:
                    history = []

            entry = {
                "id": request.get("id"),
                "source": request.get("source", ""),
                "created_by": request.get("created_by_username", ""),
                "total_labels": request.get("total_labels", 0),
                "note": request.get("note", ""),
                "printed_at": datetime.now().isoformat(timespec="seconds"),
            }
            history.insert(0, entry)      # newest first
            history = history[:200]        # keep last 200 entries

            with open(APP_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2)
        except Exception as e:
            print(f"Warning: could not save print history: {e}")

    def show_print_history(self):
        """Open the print history dialog with reprint buttons."""
        try:
            history: list = []
            if APP_HISTORY_FILE.exists():
                with open(APP_HISTORY_FILE, "r", encoding="utf-8") as f:
                    history = json.load(f)
                if not isinstance(history, list):
                    history = []
        except Exception:
            history = []

        dialog = _HistoryDialog(
            parent=self,
            history=history,
            on_reprint=self._reprint_history_entry,
            color_bg=self.C_BG,
            color_text=self.C_TEXT,
            color_text_dim=self.C_TEXT_DIM,
            color_border=self.C_BORDER,
            color_surface=self.C_SURFACE,
            color_surface2=self.C_SURFACE2,
            color_orange=self.C_ORANGE,
            color_orange_hi=self.C_ORANGE_HI,
            color_orange_dim=self.C_ORANGE_DIM,
        )
        dialog.exec()

    def _reprint_history_entry(self, entry: Dict[str, Any]):
        """Re-fetch a previously printed request by ID and print it again."""
        if not self.selected_printer:
            QMessageBox.warning(self, "No Printer", "Please select a printer first.")
            return

        request_id = entry.get("id")
        if not request_id:
            QMessageBox.warning(self, "Missing ID", "This history entry has no request ID.")
            return

        try:
            headers = self._headers()
            response = requests.get(
                f"{self.api_base_url}/admin/api/label-printing/request/{request_id}",
                headers=headers, timeout=10
            )
            if response.status_code != 200:
                QMessageBox.critical(self, "Reprint Failed",
                    f"Server returned {response.status_code}.\n"
                    "The request may have been deleted from the server.\n"
                    "You can only reprint requests that still exist on the server.")
                return
            data = response.json()
            items = data.get("items", [])
        except Exception as e:
            QMessageBox.critical(self, "Reprint Failed", f"Could not fetch request: {e}")
            return

        if not items:
            QMessageBox.warning(self, "No Items", "This request has no items to reprint.")
            return

        confirm = QMessageBox.question(
            self, "Confirm Reprint",
            f"Reprint request #{request_id}?\n"
            f"Originally printed: {entry.get('printed_at', 'unknown')}\n"
            f"Total labels: {entry.get('total_labels', 0)}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self._current_print_request = None   # don't re-save to history for reprints
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.print_job = PrintJob(self.selected_printer, items)
        self.print_job.progress.connect(self.on_print_progress)
        self.print_job.finished.connect(
            lambda s, m: self._on_reprint_finished(s, m, request_id)
        )
        self.print_job.start()
        self.status_bar.showMessage(f"Reprinting request #{request_id}…")

    def _on_reprint_finished(self, success: bool, message: str, request_id: int):
        """Handle reprint job completion."""
        self.progress_bar.setVisible(False)
        if success:
            QMessageBox.information(self, "Reprint Complete",
                f"Request #{request_id} reprinted successfully.")
        else:
            QMessageBox.critical(self, "Reprint Failed", message)
        self.status_bar.showMessage("Ready")

    # ─────────────────────────────────────────────────────────────
    # Window close guard
    # ─────────────────────────────────────────────────────────────
    def closeEvent(self, event):
        """Intercept window close to prevent accidental shutdown.

        The app is managed as a systemd user service; closing the window
        would stop the service.  We instead offer to minimize so the app
        keeps running in the taskbar.  A developer who truly wants to stop
        it can use ``systemctl --user stop vula-print`` or choose
        'Force Quit' here.
        """
        reply = QMessageBox.question(
            self,
            "Close Application?",
            "This app is managed as a system service and should stay running.\n\n"
            "  ▸  Click \"Minimize\" to keep it in the taskbar (recommended).\n"
            "  ▸  Click \"Force Quit\" to stop the process entirely.\n\n"
            "For developers: systemctl --user stop vula-print",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Yes:
            event.accept()   # Force Quit
        else:
            event.ignore()
            self.showMinimized()

    # ─────────────────────────────────────────────────────────────
    # In-app updater
    # ─────────────────────────────────────────────────────────────
    def _current_version(self) -> str:
        """Return the current git short SHA as a version string."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, cwd=Path(__file__).parent,
                timeout=3,
            )
            return f"rev {result.stdout.strip()}" if result.returncode == 0 else "unknown"
        except Exception:
            return "unknown"

    def _do_update(self):
        """Run update.sh in a dialog showing live output, then restart the service."""
        update_script = Path(__file__).parent / "update.sh"
        if not update_script.exists():
            QMessageBox.critical(self, "Update Script Missing",
                f"Could not find update.sh at:\n{update_script}")
            return

        confirm = QMessageBox.question(
            self, "Update App",
            "This will:\n"
            "  1. Pull the latest code from GitHub\n"
            "  2. Refresh Python dependencies\n"
            "  3. Restart the systemd service (app will reload)\n\n"
            "The window will close after the restart is triggered.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        dialog = _UpdateDialog(
            parent=self,
            script_path=str(update_script),
            color_bg=self.C_BG,
            color_text=self.C_TEXT,
            color_border=self.C_BORDER,
            color_surface=self.C_SURFACE,
            color_orange=self.C_ORANGE,
        )
        dialog.exec()

        # Refresh the version label after update
        self.version_label.setText(self._current_version())


# ─────────────────────────────────────────────────────────────────
# Helper dialogs
# ─────────────────────────────────────────────────────────────────

class _VisualPreviewDialog(QDialog):
    """QPainter-rendered visual label preview with item navigation."""

    def __init__(self, parent, request_id: int, items: list,
                 color_bg, color_text, color_text_dim, color_border,
                 color_surface, color_orange):
        super().__init__(parent)
        self.setWindowTitle(f"Visual Label Preview — Request #{request_id}")
        if parent and hasattr(parent, "_dialog_size"):
            size = parent._dialog_size(0.8, 0.86, 700, 520, 1120, 900)
            self.resize(size)
            self.setMinimumSize(700, 520)
        else:
            self.resize(860, 680)
        self.setStyleSheet(f"background:{color_bg}; color:{color_text};")

        self._items    = items
        self._idx      = 0
        self._renderer = TSPLRenderer()
        self._job      = PrintJob("", items)
        self._C = dict(text=color_text, dim=color_text_dim, border=color_border,
                       surface=color_surface, orange=color_orange, bg=color_bg)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)

        # ── Item info row ──────────────────────────────────────────
        info_row = QHBoxLayout()
        self._info_label = QLabel()
        self._info_label.setStyleSheet(
            f"color:{color_text}; font-size:13px; font-weight:600; background:transparent;"
        )
        info_row.addWidget(self._info_label)
        info_row.addStretch()
        self._counter_label = QLabel()
        self._counter_label.setStyleSheet(
            f"color:{color_text_dim}; font-size:12px; background:transparent;"
        )
        info_row.addWidget(self._counter_label)
        layout.addLayout(info_row)

        # ── Rendered pixmap area ───────────────────────────────────
        self._pixmap_label = QLabel()
        self._pixmap_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pixmap_label.setStyleSheet(
            f"background:{color_surface}; border:1px solid {color_border};"
            f" border-radius:8px; padding:12px;"
        )
        self._pixmap_label.setMinimumHeight(400)
        layout.addWidget(self._pixmap_label, stretch=1)

        # ── Navigation row ─────────────────────────────────────────
        nav_row = QHBoxLayout()
        nav_row.setSpacing(8)

        _sec_style = (
            f"QPushButton {{background:{color_surface}; color:{color_text};"
            f" border:1px solid {color_border}; border-radius:6px;"
            f" padding:4px 16px; font-size:13px;}}"
            f"QPushButton:hover {{background:{color_border};}}"
            f"QPushButton:disabled {{color:{color_text_dim}; border-color:{color_border};}}"
        )
        _orange_style = (
            f"QPushButton {{background:{color_orange}; color:#000; border:none;"
            f" border-radius:6px; padding:4px 20px; font-weight:700; font-size:13px;}}"
        )

        self._prev_btn = QPushButton("◀  Prev")
        self._prev_btn.setFixedHeight(36)
        self._prev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._prev_btn.setStyleSheet(_sec_style)
        self._prev_btn.clicked.connect(self._go_prev)
        nav_row.addWidget(self._prev_btn)

        self._next_btn = QPushButton("Next  ▶")
        self._next_btn.setFixedHeight(36)
        self._next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_btn.setStyleSheet(_sec_style)
        self._next_btn.clicked.connect(self._go_next)
        nav_row.addWidget(self._next_btn)

        nav_row.addStretch()

        close_btn = QPushButton("Close")
        close_btn.setFixedHeight(36)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(_orange_style)
        close_btn.clicked.connect(self.accept)
        nav_row.addWidget(close_btn)

        layout.addLayout(nav_row)

        self._refresh()

    # -------------------------------------------------------------- #
    def _refresh(self) -> None:
        item    = self._items[self._idx]
        title   = item.get('title',         '')
        variant = item.get('variant_label', '')
        sku     = item.get('sku',           '')
        qty     = item.get('qty_to_print',   0)

        info = title
        if variant: info += f"  ·  {variant}"
        if sku:     info += f"  ·  SKU: {sku}"
        info += f"  ·  Qty: {qty}"
        self._info_label.setText(info)
        self._counter_label.setText(f"Item {self._idx + 1} of {len(self._items)}")

        tspl   = self._job._generate_label_tspl(item)
        pixmap = self._renderer.render(tspl)

        # Fit pixmap to available area while keeping label aspect ratio
        avail_w = max(100, self._pixmap_label.width()  - 28)
        avail_h = max(100, self._pixmap_label.height() - 28)
        scaled  = pixmap.scaled(
            avail_w, avail_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._pixmap_label.setPixmap(scaled)

        self._prev_btn.setEnabled(self._idx > 0)
        self._next_btn.setEnabled(self._idx < len(self._items) - 1)

    def _go_prev(self) -> None:
        if self._idx > 0:
            self._idx -= 1
            self._refresh()

    def _go_next(self) -> None:
        if self._idx < len(self._items) - 1:
            self._idx += 1
            self._refresh()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._items:
            self._refresh()


class _TextDialog(QDialog):
    """Generic scrollable monospace text preview dialog (TSPL visualiser)."""

    def __init__(self, parent, title: str, content: str,
                 color_bg, color_text, color_border, color_surface, color_orange):
        super().__init__(parent)
        self.setWindowTitle(title)
        if parent and hasattr(parent, "_dialog_size"):
            size = parent._dialog_size(0.76, 0.82, 640, 460, 1040, 860)
            self.resize(size)
            self.setMinimumSize(640, 460)
        else:
            self.resize(820, 640)
        self.setStyleSheet(f"background:{color_bg}; color:{color_text};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)

        heading = QLabel(title)
        heading.setStyleSheet(
            f"color:{color_text}; font-size:14px; font-weight:700;"
        )
        layout.addWidget(heading)

        text_area = QTextEdit()
        text_area.setReadOnly(True)
        text_area.setPlainText(content)
        text_area.setFont(__import__('PyQt6.QtGui', fromlist=['QFont']).QFont("Courier New", 10))
        text_area.setStyleSheet(
            f"background:{color_surface}; color:{color_text};"
            f"border:1px solid {color_border}; border-radius:6px; padding:8px;"
        )
        layout.addWidget(text_area, stretch=1)

        close_btn = QPushButton("Close")
        close_btn.setMinimumHeight(34)
        close_btn.setCursor(__import__('PyQt6.QtCore', fromlist=['Qt']).Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(
            f"QPushButton {{ background:{color_orange}; color:#000; border:none;"
            f" border-radius:6px; padding:6px 20px; font-weight:700; }}"
            f"QPushButton:hover {{ background:{color_orange}; }}"
        )
        close_btn.clicked.connect(self.accept)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)


class _HistoryDialog(QDialog):
    """Print history dialog listing completed jobs with Reprint buttons."""

    def __init__(self, parent, history: list, on_reprint,
                 color_bg, color_text, color_text_dim, color_border,
                 color_surface, color_surface2, color_orange, color_orange_hi, color_orange_dim):
        super().__init__(parent)
        self.setWindowTitle("Print History")
        if parent and hasattr(parent, "_dialog_size"):
            size = parent._dialog_size(0.72, 0.72, 620, 440, 980, 800)
            self.resize(size)
            self.setMinimumSize(620, 440)
        else:
            self.resize(760, 520)
        self.setStyleSheet(f"background:{color_bg}; color:{color_text};")
        self._on_reprint = on_reprint

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)

        heading = QLabel("Print History  —  click Reprint to re-send a previous job")
        heading.setStyleSheet(
            f"color:{color_text}; font-size:14px; font-weight:700;"
        )
        layout.addWidget(heading)

        if not history:
            empty = QLabel("No print history yet. Print a job first.")
            empty.setStyleSheet(f"color:{color_text_dim}; font-size:12px; padding:20px;")
            empty.setAlignment(__import__('PyQt6.QtCore', fromlist=['Qt']).Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(empty)
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setStyleSheet(
                f"QScrollArea {{ border:none; background:transparent; }}"
                f"QScrollBar:vertical {{ width:6px; background:transparent; }}"
                f"QScrollBar::handle:vertical {{ background:{color_border}; border-radius:3px; }}"
            )
            inner = QWidget()
            inner.setStyleSheet(f"background:{color_bg};")
            inner_layout = QVBoxLayout(inner)
            inner_layout.setContentsMargins(0, 0, 8, 0)
            inner_layout.setSpacing(6)

            btn_style = (
                f"QPushButton {{ background:{color_orange}; color:#000; border:none;"
                f" border-radius:5px; padding:5px 14px; font-size:11px; font-weight:700; }}"
                f"QPushButton:hover {{ background:{color_orange_hi}; }}"
                f"QPushButton:pressed {{ background:{color_orange_dim}; }}"
            )
            row_style = (
                f"background:{color_surface}; border:1px solid {color_border};"
                f" border-radius:7px;"
            )

            for entry in history:
                row_widget = QWidget()
                row_widget.setStyleSheet(row_style)
                row_layout = QHBoxLayout(row_widget)
                row_layout.setContentsMargins(14, 10, 10, 10)
                row_layout.setSpacing(12)

                info_layout = QVBoxLayout()
                info_layout.setSpacing(2)

                top_text = (
                    f"#{entry.get('id', '?')}  •  "
                    f"{entry.get('source', '').replace('_', ' ').title()}  —  "
                    f"{entry.get('total_labels', 0)} label(s)"
                )
                top_lbl = QLabel(top_text)
                top_lbl.setStyleSheet(
                    f"color:{color_text}; font-size:12px; font-weight:600;"
                    f" background:transparent; border:none;"
                )
                sub_text = (
                    f"Printed: {entry.get('printed_at', 'unknown')}  •  "
                    f"By: {entry.get('created_by', 'unknown')}"
                )
                if entry.get('note'):
                    sub_text += f"  •  {entry['note']}"
                sub_lbl = QLabel(sub_text)
                sub_lbl.setStyleSheet(
                    f"color:{color_text_dim}; font-size:11px;"
                    f" background:transparent; border:none;"
                )

                info_layout.addWidget(top_lbl)
                info_layout.addWidget(sub_lbl)
                row_layout.addLayout(info_layout, stretch=1)

                reprint_btn = QPushButton("Reprint")
                reprint_btn.setMinimumSize(88, 32)
                reprint_btn.setCursor(
                    __import__('PyQt6.QtCore', fromlist=['Qt']).Qt.CursorShape.PointingHandCursor
                )
                reprint_btn.setStyleSheet(btn_style)
                reprint_btn.clicked.connect(
                    lambda checked, e=entry: self._do_reprint(e)
                )
                row_layout.addWidget(reprint_btn)

                inner_layout.addWidget(row_widget)

            inner_layout.addStretch()
            scroll.setWidget(inner)
            layout.addWidget(scroll, stretch=1)

        close_btn = QPushButton("Close")
        close_btn.setMinimumHeight(34)
        close_btn.setCursor(__import__('PyQt6.QtCore', fromlist=['Qt']).Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(
            f"QPushButton {{ background:{color_orange}; color:#000; border:none;"
            f" border-radius:6px; padding:6px 20px; font-weight:700; }}"
        )
        close_btn.clicked.connect(self.accept)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _do_reprint(self, entry: dict):
        self.accept()  # close dialog first
        self._on_reprint(entry)


class _UpdateDialog(QDialog):
    """Shows live output from update.sh and restarts the service when done."""

    def __init__(self, parent, script_path: str,
                 color_bg, color_text, color_border, color_surface, color_orange):
        super().__init__(parent)
        self.setWindowTitle("Update App")
        if parent and hasattr(parent, "_dialog_size"):
            size = parent._dialog_size(0.72, 0.7, 620, 420, 980, 760)
            self.resize(size)
            self.setMinimumSize(620, 420)
        else:
            self.resize(760, 480)
        self.setStyleSheet(f"background:{color_bg}; color:{color_text};")
        self._script_path = script_path
        self._process: Optional[QProcess] = None
        self._finished = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)

        heading = QLabel("⇡  Updating Vula! Print Label Printer")
        heading.setStyleSheet(f"color:{color_text}; font-size:14px; font-weight:700;")
        layout.addWidget(heading)

        sub = QLabel("Pulling latest code from GitHub and refreshing dependencies…")
        sub.setStyleSheet(f"color:{color_text}; font-size:11px; background:transparent; border:none;")
        layout.addWidget(sub)

        self._output = QTextEdit()
        self._output.setReadOnly(True)
        self._output.setFont(__import__('PyQt6.QtGui', fromlist=['QFont']).QFont("Courier New", 10))
        self._output.setStyleSheet(
            f"background:{color_surface}; color:{color_text};"
            f"border:1px solid {color_border}; border-radius:6px; padding:8px;"
        )
        layout.addWidget(self._output, stretch=1)

        self._status_lbl = QLabel("Running…")
        self._status_lbl.setStyleSheet(
            f"color:{color_text}; font-size:11px; background:transparent; border:none;"
        )
        layout.addWidget(self._status_lbl)

        btn_row = QHBoxLayout()
        self._close_btn = QPushButton("Close")
        self._close_btn.setMinimumHeight(34)
        self._close_btn.setEnabled(False)  # Only enabled after script finishes
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setStyleSheet(
            f"QPushButton {{ background:{color_orange}; color:#000; border:none;"
            f" border-radius:6px; padding:6px 20px; font-weight:700; }}"
            f"QPushButton:disabled {{ background:#555; color:#888; }}"
        )
        self._close_btn.clicked.connect(self.accept)
        btn_row.addStretch()
        btn_row.addWidget(self._close_btn)
        layout.addLayout(btn_row)

        # Start the update script immediately
        self._run()

    def _run(self):
        self._process = QProcess(self)
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._process.readyReadStandardOutput.connect(self._read_output)
        self._process.finished.connect(self._on_finished)
        self._process.start("/bin/bash", [self._script_path])

    def _read_output(self):
        if self._process is None:
            return
        raw = bytes(self._process.readAllStandardOutput())
        text = raw.decode("utf-8", errors="replace")
        self._output.moveCursor(__import__('PyQt6.QtGui', fromlist=['QTextCursor']).QTextCursor.MoveOperation.End)
        self._output.insertPlainText(text)
        self._output.moveCursor(__import__('PyQt6.QtGui', fromlist=['QTextCursor']).QTextCursor.MoveOperation.End)

    def _on_finished(self, exit_code: int, _exit_status):
        self._finished = True
        if exit_code == 0:
            self._status_lbl.setText(
                "✓ Update complete — the service has been restarted. "
                "The UI will refresh automatically."
            )
        else:
            self._status_lbl.setText(
                f"✗ Update script exited with code {exit_code}. "
                "Check the output above for details."
            )
        self._close_btn.setEnabled(True)

    def closeEvent(self, event):
        """Kill the script if the dialog is closed early."""
        if self._process and self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.kill()
        event.accept()


def main():
    """Main entry point."""
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # Inter / system sans-serif fallback chain
    font = QFont("Inter")
    font.setStyleHint(QFont.StyleHint.SansSerif)
    font.setPointSize(10)
    app.setFont(font)

    window = VulaPrintApp()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
