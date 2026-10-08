#!/usr/bin/env python3
"""Background QThread workers for the Vula! Print app.

All blocking I/O happens in these workers so the Qt main thread stays
responsive. Workers communicate results back via pyqtSignals carrying
primitives only (never shared Python objects across thread boundaries).
"""
from __future__ import annotations

import logging

import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from PyQt6.QtCore import QThread, pyqtSignal
from vula_device_io import write_to_device


class PrinterScanner(QThread):
    """Background thread to scan for printer devices.

    Detects:
      * USB printer-class devices under /dev/usb/  (lp0, lp1, ...)
      * USB-serial adapters: /dev/ttyUSB*, /dev/ttyACM*
      * Stable udev symlinks under /dev/serial/by-id/*

    Both raw serial device paths and their by-id symlinks may appear in the
    results. If a device is present via both, prefer the by-id symlink when
    assigning roles — it is stable across reboots and re-plugs.

    Fingerprinting and human-readable descriptions are computed here (on
    the worker thread) rather than in the slot that receives the result.
    Both operations can walk sysfs and shell out to ``udevadm``, so doing
    them on the Qt main thread caused a visible stutter on every scan tick
    with multiple printers attached.
    """

    # devices, {device_path: fingerprint}, {device_path: description}
    printers_found = pyqtSignal(list, dict, dict)

    def run(self):
        devices = []
        try:
            usb_dir = Path("/dev/usb")
            if usb_dir.exists():
                devices.extend(str(p) for p in usb_dir.glob("lp*"))

            dev_dir = Path("/dev")
            if dev_dir.exists():
                devices.extend(str(p) for p in dev_dir.glob("ttyUSB*"))
                devices.extend(str(p) for p in dev_dir.glob("ttyACM*"))

            serial_by_id = Path("/dev/serial/by-id")
            if serial_by_id.exists():
                devices.extend(
                    str(p) for p in serial_by_id.iterdir() if p.is_symlink()
                )

            seen = set()
            unique = []
            for d in devices:
                if d not in seen:
                    seen.add(d)
                    unique.append(d)
            devices = sorted(unique)
        except Exception as e:
            print(f"Error scanning for printers: {e}")

        # Compute the fingerprint + description table here, off the UI
        # thread. Both calls can be expensive for USB printer-class nodes
        # (sysfs walk, possibly udevadm subprocess); the per-device cost is
        # bounded but the aggregate was noticeable on multi-printer boxes.
        from vula_device_io import fingerprint_for_path, describe_device

        fingerprints: Dict[str, str] = {}
        descriptions: Dict[str, str] = {}
        for d in devices:
            try:
                fingerprints[d] = fingerprint_for_path(d) or ""
            except Exception:
                fingerprints[d] = ""
            try:
                descriptions[d] = describe_device(d)
            except Exception:
                descriptions[d] = d

        slog = logging.getLogger("vula.scan")
        slog.info("discovered %d device(s): %s",
                  len(devices), devices or "(none)")
        self.printers_found.emit(devices, fingerprints, descriptions)


class PrintJob(QThread):
    """Background thread for printing labels."""

    progress = pyqtSignal(int, int)      # current, total
    completed = pyqtSignal(bool, str)    # success, message

    def __init__(self, printer_device: str, items: List[Dict[str, Any]]):
        super().__init__()
        self.printer_device = printer_device
        self.items = items
        self.label_width_dots = 320
        self.horizontal_shift_dots = 16
        # Qt-idiomatic teardown. QThread.finished is the built-in 0-arg signal;
        # deleteLater is scheduled on the main-thread event loop AFTER the
        # C++ QThread has fully unwound. Without this, Python GC can destroy
        # the QThread while it's still running → SIGABRT.
        self.finished.connect(self.deleteLater)

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
        """Execute print job.

        Per-item failures do NOT abort the whole job. Instead they are
        collected and reported in the final message; the job is marked
        failed only if at least one label could not be rendered or written.
        This preserves partial-queue progress when one item has bad data.
        """
        log = logging.getLogger("vula.print")
        import time as _t
        _t0 = _t.monotonic()
        failures: List[str] = []
        printed = 0
        total = 0
        try:
            total = sum(int(item.get("qty_to_print", 0) or 0) for item in self.items)
            log.info("PrintJob start: %d items / %d labels -> %s",
                     len(self.items), total, self.printer_device)

            for item in self.items:
                qty = int(item.get("qty_to_print", 0) or 0)

                for i in range(qty):
                    # Render — a bad item must not kill the batch.
                    try:
                        tspl = self._generate_label_tspl(item)
                    except Exception as e:
                        sku = item.get("sku") or item.get("title") or "?"
                        failures.append(f"{sku}: render failed ({e})")
                        log.error("PrintJob render failed for %s: %s", sku, e)
                        continue

                    # Write — permission errors abort cleanly; other I/O
                    # errors are recorded but the batch continues.
                    try:
                        write_to_device(self.printer_device, tspl.encode('utf-8'))
                    except PermissionError:
                        self.completed.emit(
                            False,
                            f"Permission denied: cannot write to {self.printer_device}.\n\n"
                            f"The printer device requires the user to be in the 'lp' group.\n"
                            f"Re-run the install script to fix this automatically, or run:\n"
                            f"  sudo usermod -aG lp $USER  (then log out and back in)"
                        )
                        return
                    except Exception as e:
                        sku = item.get("sku") or item.get("title") or "?"
                        failures.append(f"{sku}: write failed ({e})")
                        log.error("PrintJob write failed for %s: %s", sku, e)
                        continue

                    printed += 1
                    self.progress.emit(printed, total)

                    # Small delay between labels
                    time.sleep(0.2)

            elapsed = _t.monotonic() - _t0
            log.info("PrintJob done: %d/%d labels in %.2fs (failures=%d)",
                     printed, total, elapsed, len(failures))

            if failures:
                summary = (f"Printed {printed}/{total} labels. "
                           f"{len(failures)} item(s) skipped:\n  - " +
                           "\n  - ".join(failures[:8]))
                if len(failures) > 8:
                    summary += f"\n  … and {len(failures) - 8} more."
                self.completed.emit(False, summary)
            else:
                self.completed.emit(True, f"Successfully printed {total} labels")

        except Exception as e:
            log.exception("PrintJob crashed")
            self.completed.emit(False, f"Print job failed: {e}")


class POSSlipPrintJob(QThread):
    """Background thread for printing POS slips (ESC/POS).

    Printer-width and QR behaviour are driven by instance attributes so the
    same class works across 58 mm / 80 mm printers and across old firmware
    that does not understand native QR commands.

    Attributes:
        width_chars:     Receipt line width in characters. 32 for 58 mm,
                         48 for 80 mm.
        qr_mode:         "raster" (default — host-rendered GS v 0),
                         "native" (Epson GS ( k — only for known-good
                         printers), or "off".
        qr_module_px:    Pixels per QR module for raster mode.
    """

    completed = pyqtSignal(bool, str)

    def __init__(
        self,
        printer_device: str,
        detail_payload: Dict[str, Any],
        *,
        width_chars: int = 48,
        qr_mode: str = "raster",
        qr_module_px: int = 4,
    ):
        super().__init__()
        self.printer_device = printer_device
        self.detail_payload = detail_payload
        self.width_chars = int(width_chars)
        self.qr_mode = str(qr_mode).lower()
        self.qr_module_px = int(qr_module_px)
        self.finished.connect(self.deleteLater)

    # ── Formatting helpers ─────────────────────────────────────────────

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

    def _line_sep(self, ch: str = "-") -> str:
        return ch * self.width_chars

    def _col2(self, left: str, right: str) -> str:
        l = str(left or "")
        r = str(right or "")
        space = max(1, self.width_chars - len(l) - len(r))
        return f"{l}{' ' * space}{r}"

    def _txt(self, text: str = "") -> bytes:
        # Truncate over-wide lines instead of letting the printer wrap them.
        # This is the single most important fix for the "extra new lines"
        # symptom on 58 mm printers.
        if len(text) > self.width_chars:
            text = text[: self.width_chars]
        return (text + "\n").encode("ascii", errors="replace")

    # ── QR / logo raster ───────────────────────────────────────────────

    def _qr_bytes(self, data: str) -> bytes:
        """Return ESC/POS bytes for the QR, honoring qr_mode."""
        if not data or self.qr_mode == "off":
            return b""

        if self.qr_mode == "raster":
            try:
                from vula_rendering_qr import render_qr_gs_v0
                return render_qr_gs_v0(
                    data,
                    module_px=self.qr_module_px,
                    ec_level="M",
                    border_modules=2,
                )
            except Exception:
                return b""

        if self.qr_mode == "native":
            return self._qr_code_escpos_native(data)

        return b""

    @staticmethod
    def _qr_code_escpos_native(data: str, module_size: int = 4, ec_level: int = 49) -> bytes:
        """Native Epson GS ( k QR — only works on printers that support it.

        Kept as an escape hatch for known-good hardware. Do NOT use this
        by default — most cheap thermal printers silently ignore it.
        """
        if not data:
            return b""
        data_bytes = data.encode("utf-8")
        if len(data_bytes) > 7089:
            return b""

        GS, k = 0x1D, 0x6B
        out = bytearray()
        out += bytes([GS, k, 4, 0, 2, 0, 0])                     # model 2
        out += bytes([GS, k, 3, 0, 5, module_size])              # module size
        out += bytes([GS, k, 3, 0, 6, ec_level])                 # EC level
        store_len = 3 + len(data_bytes)
        out += bytes([GS, k, store_len & 0xFF, (store_len >> 8) & 0xFF, 49, 80, 48])
        out += data_bytes
        out += bytes([GS, k, 3, 0, 49, 81, 48])                  # print
        return bytes(out)

    def _logo_bytes(self, logo_url: str, max_width_dots: int) -> bytes:
        """Download + rasterise the receipt logo as GS v 0 bytes.

        Returns b"" if the URL is missing, the download fails, or the
        image can't be parsed — the receipt prints fine without a logo.
        """
        if not logo_url:
            return b""
        try:
            if logo_url.startswith("/"):
                # Relative URL — caller should have made it absolute.
                return b""

            resp = requests.get(logo_url, timeout=8)
            if resp.status_code != 200 or not resp.content:
                return b""

            from io import BytesIO
            from PIL import Image
            from vula_rendering_qr import render_pil_image_gs_v0

            img = Image.open(BytesIO(resp.content))
            return render_pil_image_gs_v0(img, max_width_dots=max_width_dots)
        except Exception:
            return b""

    # ── Receipt body ───────────────────────────────────────────────────

    def _build_receipt_bytes(self) -> bytes:
        req = self.detail_payload.get("request", {})
        business = self.detail_payload.get("business", {})
        store = self.detail_payload.get("store", {})
        totals = self.detail_payload.get("totals", {})
        items = self.detail_payload.get("items", [])

        currency = totals.get("currency", "ZAR")
        cur = "R" if currency == "ZAR" else currency

        # Print area in dots. 58 mm → 384 dots, 80 mm → 576 dots.
        # Derived from width_chars: Font A is 12 dots per char.
        max_width_dots = self.width_chars * 12

        out = bytearray()
        ESC, GS, LF = 0x1B, 0x1D, 0x0A

        out += self._esc(ESC, 0x40)                  # INIT

        # Cash drawer kick for cash payments only.
        payment_type = str(req.get("payment_type", "")).lower()
        if payment_type == "cash":
            out += bytes([ESC, 0x70, 0x00, 0x19, 0xFA])

        # ── Logo (GS v 0 raster, if we have one) ─────────────────────
        logo_url = self.detail_payload.get("logo_url", "")
        logo_bytes = self._logo_bytes(logo_url, max_width_dots)
        if logo_bytes:
            out += self._esc(ESC, 0x61, 0x01)         # center
            out += logo_bytes
            out += b"\n"
        else:
            out += self._esc(ESC, 0x61, 0x01)

        # ── Business header ─────────────────────────────────────────
        out += self._esc(ESC, 0x45, 0x01)             # bold on
        out += self._txt(business.get("brand_name", "POS RECEIPT"))
        out += self._esc(ESC, 0x45, 0x00)             # bold off

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

        # ── Transaction meta ────────────────────────────────────────
        out += self._esc(ESC, 0x61, 0x00)             # left
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

        # ── Items ───────────────────────────────────────────────────
        out += self._txt(self._line_sep())
        out += self._esc(ESC, 0x45, 0x01)
        out += self._txt(self._col2("QTY ITEM", "TOTAL"))
        out += self._esc(ESC, 0x45, 0x00)
        out += self._txt(self._line_sep())

        # Per-item description budget: total width minus qty prefix (4) and
        # price column (14). Clamped to at least 8 chars.
        desc_budget = max(8, self.width_chars - 4 - 14)

        for item in items:
            qty = int(item.get("qty", 0) or 0)
            title = str(item.get("title", ""))
            variant = str(item.get("variant_label", ""))
            sku = str(item.get("sku", ""))
            unit_price = f"{cur} {self._cents_to_amount(item.get('unit_price_cents', 0) or 0)}"
            line_total = f"{cur} {self._cents_to_amount(item.get('line_total_cents', 0) or 0)}"

            out += self._txt(self._col2(f"{qty} x {title[:desc_budget]}", line_total))
            if variant:
                out += self._txt(f"  {variant[: self.width_chars - 2]}")
            if sku:
                out += self._txt(f"  SKU: {sku[: self.width_chars - 7]}")
            out += self._txt(f"  @ {unit_price}")

        # ── Totals ──────────────────────────────────────────────────
        out += self._txt(self._line_sep())
        out += self._txt(self._col2(
            "Subtotal before disc:",
            f"{cur} {self._cents_to_amount(totals.get('subtotal_before_discount_cents', 0) or 0)}",
        ))
        out += self._txt(self._col2(
            "Manual discount:",
            f"{cur} {self._cents_to_amount(totals.get('manual_discount_cents', 0) or 0)}",
        ))
        out += self._txt(self._col2(
            "Voucher discount:",
            f"{cur} {self._cents_to_amount(totals.get('voucher_discount_cents', 0) or 0)}",
        ))
        out += self._txt(self._col2(
            "Subtotal:",
            f"{cur} {self._cents_to_amount(totals.get('subtotal_cents', 0) or 0)}",
        ))

        vat_label = f"VAT ({self._vat_percent_from_bps(totals.get('vat_bps', 0) or 0)}):"
        out += self._txt(self._col2(
            vat_label,
            f"{cur} {self._cents_to_amount(totals.get('tax_cents', 0) or 0)}",
        ))
        out += self._txt(self._line_sep())
        out += self._esc(ESC, 0x45, 0x01)
        out += self._txt(self._col2(
            "TOTAL:",
            f"{cur} {self._cents_to_amount(totals.get('total_cents', 0) or 0)}",
        ))
        out += self._esc(ESC, 0x45, 0x00)

        # ── Footer note ─────────────────────────────────────────────
        footer_note = str(self.detail_payload.get("footer_note", "") or "").strip()
        if footer_note:
            out += self._txt(self._line_sep())
            out += self._esc(ESC, 0x61, 0x01)
            for chunk_start in range(0, len(footer_note), self.width_chars):
                out += self._txt(footer_note[chunk_start : chunk_start + self.width_chars])
            out += self._esc(ESC, 0x61, 0x00)

        # ── QR code ─────────────────────────────────────────────────
        website_url = str(self.detail_payload.get("website_url", "") or "").strip()
        qr_data = str(self.detail_payload.get("qr_data", "") or "").strip()
        if qr_data:
            qr_bytes = self._qr_bytes(qr_data)
            if qr_bytes:
                out += self._txt(self._line_sep())
                out += self._esc(ESC, 0x61, 0x01)     # center
                out += qr_bytes
                out += b"\n"
                if website_url:
                    out += self._txt(website_url[: self.width_chars])
                out += self._esc(ESC, 0x61, 0x00)     # left

        # ── Loyalty note ────────────────────────────────────────────
        out += self._txt(self._line_sep())
        out += self._esc(ESC, 0x61, 0x01)
        for line in (
            "If you have an online account,",
            "you can keep track of all online",
            "or instore orders on your account.",
        ):
            out += self._txt(line[: self.width_chars])
        out += self._esc(ESC, 0x61, 0x00)

        out += bytes([LF, LF, LF])
        out += self._esc(GS, 0x56, 0x41, 0x00)        # full cut
        return bytes(out)

    def run(self):
        log = logging.getLogger("vula.print")
        import time as _t
        _t0 = _t.monotonic()
        try:
            payload = self._build_receipt_bytes()
            log.info("POSSlip: %db, width=%d, qr=%s -> %s",
                     len(payload), self.width_chars, self.qr_mode,
                     self.printer_device)
            try:
                write_to_device(self.printer_device, payload)
            except PermissionError:
                self.completed.emit(
                    False,
                    f"Permission denied: cannot write to {self.printer_device}.\n\n"
                    f"Add the user to the 'lp' group:\n"
                    f"  sudo usermod -aG lp $USER  (then log out and back in)",
                )
                return
            except Exception as e:
                self.completed.emit(False, f"POS printer error: {e}")
                return

            elapsed = _t.monotonic() - _t0
            log.info("POSSlip done in %.2fs", elapsed)
            self.completed.emit(True, "POS slip printed successfully")
        except Exception as e:
            log.exception("POSSlip crashed")
            self.completed.emit(False, f"POS slip print failed: {e}")


class POSEODReportPrintJob(QThread):
    """Background thread for printing receipt-width POS EOD reports (ESC/POS)."""

    completed = pyqtSignal(bool, str)

    def __init__(self, printer_device: str, detail_payload: Dict[str, Any]):
        super().__init__()
        self.printer_device = printer_device
        self.detail_payload = detail_payload
        self.finished.connect(self.deleteLater)

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
        # ── CRITICAL: define log + _t0 here. Their absence was the root
        # cause of the EOD reprint loop: the NameError on the line after
        # write_to_device() was caught by the outer except, and the job
        # always reported failure, so the backend never saw a completion
        # and the client kept re-fetching the same EOD report.
        log = logging.getLogger("vula.print")
        import time as _t
        _t0 = _t.monotonic()
        try:
            payload = self._build_receipt_bytes()
            log.info("POSEOD: %db -> %s", len(payload), self.printer_device)
            try:
                write_to_device(self.printer_device, payload)
            except PermissionError:
                self.completed.emit(
                    False,
                    f"Permission denied: cannot write to {self.printer_device}.\n\n"
                    f"The printer device requires the user to be in the 'lp' group.\n"
                    f"Re-run the install script to fix this automatically, or run:\n"
                    f"  sudo usermod -aG lp $USER  (then log out and back in)",
                )
                return
            except Exception as e:
                self.completed.emit(False, f"POS EOD printer error: {e}")
                return

            elapsed = _t.monotonic() - _t0
            log.info("EOD report done in %.2fs", elapsed)
            self.completed.emit(True, "POS EOD report printed successfully")
        except Exception as e:
            log.exception("POSEOD crashed")
            self.completed.emit(False, f"POS EOD print failed: {e}")


class POSPollWorker(QThread):
    """Off-main-thread worker for one connection's complete POS poll cycle.

    Performs all blocking HTTP I/O (pending check + detail fetch, EOD reports)
    for a SINGLE StoreConnection in a background QThread so the Qt main event
    loop — and the UI — remain fully responsive at all times.

    Only primitive fields (str/int/frozenset) are passed in; the worker never
    receives or emits a StoreConnection object. Every signal carries
    `connection_id` (a plain string) so the main thread can look up which
    StoreConnection the result belongs to.
    """

    slip_ready        = pyqtSignal(str, int, dict)   # (connection_id, request_id, detail_payload)
    eod_slip_ready    = pyqtSignal(str, int, dict)   # (connection_id, request_id, detail_payload)
    pending_list_ready = pyqtSignal(str, list)       # (connection_id, [pending_summary_dicts])
    eod_pending_ready  = pyqtSignal(str, list)       # (connection_id, [pending_summary_dicts])
    all_clear      = pyqtSignal(str)              # (connection_id)
    poll_error     = pyqtSignal(str, str, int)     # (connection_id, message, http_status) 0=network
    poll_fatal     = pyqtSignal(str, str, int)     # (connection_id, message, http_status) auth/config

    def __init__(
        self,
        connection_id: str,
        api_base: str,
        api_key: str,
        user_id: int,
        in_flight_ids: frozenset,
        eod_in_flight_ids: frozenset,
        parent=None,
    ):
        super().__init__(parent)
        self._connection_id = connection_id
        self._api_base = api_base.rstrip("/")
        self._api_key = api_key
        self._user_id = user_id
        self._in_flight_ids = in_flight_ids
        self._eod_in_flight_ids = eod_in_flight_ids
        self.finished.connect(self.deleteLater)

    def _headers(self) -> Dict[str, str]:
        headers = {"X-Printer-API-Key": self._api_key}
        if self._user_id:
            headers["X-Printer-User-Id"] = str(self._user_id)
        return headers

    def run(self):
        cid = self._connection_id
        plog = logging.getLogger("vula.poll")

        # ── 1. Poll POS slips ──────────────────────────────────────────────
        try:
            r = requests.get(
                f"{self._api_base}/admin/api/pos-slips/pending",
                headers=self._headers(),
                timeout=10,
            )
        except Exception as e:
            plog.warning("[%s] poll network error: %s", cid, e)
            self.poll_error.emit(cid, str(e), 0)
            return

        if r.status_code in (401, 503, 400):
            plog.error("[%s] POS pending -> HTTP %d (auth/config) — backing off",
                       cid, r.status_code)
            try:
                snippet = r.text[:200].replace(chr(10), " ")
                if snippet:
                    plog.error("[%s]   response: %s", cid, snippet)
            except Exception:
                pass
            self.poll_fatal.emit(cid, f"HTTP {r.status_code}", r.status_code)
            return
        if r.status_code != 200:
            plog.warning("[%s] POS pending -> HTTP %d (transient)", cid, r.status_code)
            self.poll_error.emit(cid, f"HTTP {r.status_code}", r.status_code)
            return

        pending = sorted(
            (r.json() if isinstance(r.json(), list) else []),
            key=lambda x: str(x.get("created_at", "")),
        )
        # Surface the whole list to the UI for display, then continue with
        # the existing "grab the first one to print" behaviour.
        self.pending_list_ready.emit(cid, list(pending))
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
                self.poll_error.emit(cid, f"Detail #{req_id}: {e}", 0)
                return
            if dr.status_code == 404:
                continue  # already gone, try next slip
            if dr.status_code != 200:
                plog.warning("[%s] detail #%d -> HTTP %d", cid, req_id, dr.status_code)
                self.poll_error.emit(cid, f"Detail #{req_id}: HTTP {dr.status_code}", dr.status_code)
                return
            plog.info("[%s] slip #%d fetched -> printing", cid, req_id)
            self.slip_ready.emit(cid, req_id, dr.json())
            return

        # ── 2. No POS slips — check EOD reports ───────────────────────────
        self.pending_list_ready.emit(cid, [])
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
            self.all_clear.emit(cid)
            return

        if eod_r.status_code != 200:
            self.all_clear.emit(cid)
            return

        eod_pending = sorted(
            (eod_r.json() if isinstance(eod_r.json(), list) else []),
            key=lambda x: str(x.get("created_at", "")),
        )
        self.eod_pending_ready.emit(cid, list(eod_pending))
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
                self.all_clear.emit(cid)
                return
            if dr.status_code != 200:
                self.eod_pending_ready.emit(cid, [])
                self.all_clear.emit(cid)
                return
            plog.info("[%s] EOD #%d fetched -> printing", cid, req_id)
            self.eod_slip_ready.emit(cid, req_id, dr.json())
            return

        self.eod_pending_ready.emit(cid, [])
        self.all_clear.emit(cid)


class _RetryFlushWorker(QThread):
    """Off-thread completion-retry flush.

    The snapshot of pending retries is taken on the main thread (so we never
    mutate the shared sets from a worker). Each successful completion is
    reported back via ``succeeded`` so the main thread can discard ids safely.
    """

    succeeded = pyqtSignal(list)   # list of (connection_id, kind, request_id)

    def __init__(self, tasks, parent=None):
        # tasks: iterable of
        #   (connection_id, api_base, api_key, user_id, kind, request_id)
        super().__init__(parent)
        self._tasks = list(tasks)
        self.finished.connect(self.deleteLater)

    def run(self):
        done = []
        for cid, base, key, uid, kind, req_id in self._tasks:
            path = (
                "/admin/api/pos-slips/complete"
                if kind == "pos"
                else "/admin/api/pos-eod-reports/complete"
            )
            try:
                r = requests.post(
                    f"{base.rstrip('/')}{path}",
                    headers={
                        "X-Printer-API-Key": key,
                        "X-Printer-User-Id": str(uid or ""),
                        "Content-Type": "application/json",
                    },
                    json={"request_id": req_id},
                    timeout=10,
                )
                if r.status_code in (200, 400, 404):
                    done.append((cid, kind, req_id))
            except Exception:
                # Network hiccup — leave the id in the retry set for next tick.
                pass

        if done:
            self.succeeded.emit(done)