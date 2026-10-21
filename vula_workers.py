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

            # CUPS queues — enumerated from lpstat. Each queue yields two
            # assignment strings (raw + driver) so the operator picks the
            # mode in the role dialog. list_cups_queues() returns [] when
            # CUPS isn't installed or has no queues.
            try:
                from vula_printer_target import list_cups_queues
                devices.extend(list_cups_queues())
            except Exception as exc:
                logging.getLogger("vula.scan").debug(
                    "CUPS enumeration failed: %s", exc
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

    def __init__(
        self,
        printer_device: str,
        items: List[Dict[str, Any]],
        label_layout: Optional[Dict[str, Any]] = None,
    ):
        super().__init__()
        self.printer_device = printer_device
        self.items = items

        # Label layout — callers pass self.label_layout (from settings);
        # fall back to DEFAULT_LABEL_LAYOUT so ad-hoc jobs (preview
        # rendering, tests) still work without explicit wiring.
        from vula_config import DEFAULT_LABEL_LAYOUT
        layout = dict(DEFAULT_LABEL_LAYOUT)
        if label_layout:
            for k, v in label_layout.items():
                if k in layout:
                    try:
                        layout[k] = int(v)
                    except (TypeError, ValueError):
                        pass
        self.label_layout = layout

        self.label_width_dots = layout["label_width_dots"]
        self.horizontal_shift_dots = 16  # kept for backward compat; unused
        # Qt-idiomatic teardown. QThread.finished is the built-in 0-arg signal;
        # deleteLater is scheduled on the main-thread event loop AFTER the
        # C++ QThread has fully unwound. Without this, Python GC can destroy
        # the QThread while it's still running → SIGABRT.
        self.finished.connect(self.deleteLater)

    def _tspl_escape(self, s: str) -> str:
        """Escape a string for TSPL command literals.

        TSPL is line-oriented: every command lives on its own line. A
        newline or CR inside a quoted string therefore terminates the
        string AND starts a new command. A backend-supplied field
        containing "\\nCLS\\n" is a real TSPL command — it blanks the
        label or, depending on the interpreter, executes arbitrary TSPL.
        Same for other control bytes. Reject anything outside printable
        ASCII + space, then escape backslash and double quote.
        """
        raw = s or ""
        cleaned = "".join(
            c if c == " " or 0x20 <= ord(c) < 0x7F else " "
            for c in raw
        )
        return cleaned.replace("\\", "\\\\").replace('"', '\\"')

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

    # Code39 alphabet — exactly what the symbology supports. Anything
    # outside this set has undefined behaviour on the printer: some
    # models reject the whole job, some encode garbage, some silently
    # truncate to the last valid character. A corrupted barcode scans
    # as the wrong SKU or fails at the till.
    _CODE39_ALPHABET = frozenset("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-. $/+%")

    def _generate_label_tspl(self, item: Dict[str, Any]) -> str:
        """Generate TSPL commands for a single label."""
        title         = (item.get("title") or "")
        variant_label = item.get("variant_label") or ""
        sku           = item.get("sku") or ""
        code39_raw    = str(item.get("code39") or sku or "")

        # Code39 sanitisation:
        #   * uppercase first — Code39 is uppercase-only by spec, and
        #     lowercase SKUs are common from the backend;
        #   * filter to the supported alphabet — drops `@`, `!`, `*`,
        #     and any non-ASCII or whitespace outside the one allowed
        #     space;
        #   * if the result is empty, RAISE — some printer firmware
        #     crashes on `BARCODE ...,""`, others print a starburst.
        #     A render failure per-item is caught and reported by
        #     PrintJob.run; that is the correct outcome here.
        code39 = "".join(
            c for c in code39_raw.upper() if c in self._CODE39_ALPHABET
        )
        if not code39:
            raise ValueError(
                f"empty code39 after sanitisation "
                f"(raw={code39_raw!r}, sku={sku!r})"
            )
        if code39 != code39_raw:
            logging.getLogger("vula.print").warning(
                "code39 sanitised %r -> %r (sku=%r)",
                code39_raw, code39, sku,
            )

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

        # Layout values come from self.label_layout (settings.json →
        # "label_layout"). Tunable from the Printers tab → Label card →
        # Layout… dialog. All Y coordinates are also shifted by TOP.
        LM         = int(self.label_layout.get("left_margin_dots", 30))
        TOP        = int(self.label_layout.get("top_offset_dots", 0))
        BN         = int(self.label_layout.get("barcode_narrow", 1))
        BW         = int(self.label_layout.get("barcode_wide", 2))

        USABLE_W   = self.label_width_dots - LM * 2
        TITLE_FONT = "3"                                 # 16 dots/char
        TITLE_LINE_H = 26                                # font-3 height (24) + 2 gap

        # ── Title (wraps to 2 lines if needed) ───────────────────────
        title_lines = self._wrap_text(title, TITLE_FONT, USABLE_W)
        tspl.append(f'TEXT {LM},{5 + TOP},"{TITLE_FONT}",0,1,1,"{self._tspl_escape(title_lines[0])}"')
        if len(title_lines) > 1:
            tspl.append(f'TEXT {LM},{5 + TITLE_LINE_H + TOP},"{TITLE_FONT}",0,1,1,"{self._tspl_escape(title_lines[1])}"')

        # Shift all elements below the title down when title occupies 2 lines
        extra = TITLE_LINE_H if len(title_lines) > 1 else 0

        # ── Variant label ─────────────────────────────────────────────
        if variant_label:
            tspl.append(f'TEXT {LM},{27 + extra + TOP},"2",0,1,1,"{self._tspl_escape(variant_label)}"')

        # ── Separator bar — full printable width ─────────────────────
        tspl.append(f"BAR {LM},{44 + extra + TOP},{USABLE_W},2")

        # ── Price (font 4, one step up from font 3) ───────────────────
        tspl.append(f'TEXT {LM},{56 + extra + TOP},"4",0,1,1,"{self._tspl_escape(price)}"')

        # ── Code39 barcode ────────────────────────────────────────────
        tspl.append(f'BARCODE {LM},{95 + extra + TOP},"39",70,0,0,{BN},{BW},"{self._tspl_escape(code39)}"')

        # ── SKU (bottom, small font) ──────────────────────────────────
        tspl.append(f'TEXT {LM},{215 + TOP},"1",0,1,1,"{self._tspl_escape(sku)}"')

        # PRINT <sets>,<copies> — the single-argument form works on
        # some interpreters but is silently rejected by others (TSC
        # firmware variants differ). Standardise on the two-argument
        # form. One set, one copy.
        tspl.append("PRINT 1,1")
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
                        from vula_printer_target import PrinterTarget, dispatch_print
                        target = PrinterTarget.from_assignment(self.printer_device)
                        dispatch_print(target, tspl.encode('utf-8'), doc_class="label")
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
        api_base_url: str = "",
    ):
        super().__init__()
        self.printer_device = printer_device
        self.detail_payload = detail_payload
        self.width_chars = int(width_chars)
        self.qr_mode = str(qr_mode).lower()
        self.qr_module_px = int(qr_module_px)
        # Origin used by _logo_bytes to reject non-backend logo URLs
        # (SSRF guard, audit C9). Empty string disables logo download
        # entirely — a caller that doesn't pass this cannot
        # accidentally fetch from an arbitrary origin.
        self.api_base_url = str(api_base_url or "").rstrip("/")
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
        # Strip control bytes (ESC/POS injection vectors) but preserve
        # non-ASCII — encode(..., errors="replace") turns any non-ASCII
        # char into "?" rather than silently dropping it. A previous
        # revision stripped everything >= 0x7F, which turned "Café" into
        # "Caf" — silent character loss is worse than a visible "?" on a
        # tax-relevant receipt.
        #
        # Vectors explicitly rejected:
        #   * 0x00-0x1F  — C0 controls. 0x1B=ESC, 0x1D=GS, 0x10=DLE. An
        #                  injection here resets the printer, fires the
        #                  cutter, opens the cash drawer.
        #   * 0x7F       — DEL.
        #   * 0x80-0x9F  — C1 controls (rare but interpreted by some
        #                  ESC/POS clones as extended commands).
        # \t and \n are harmless and preserved.
        cleaned_chars = []
        for c in str(text):
            if c == "\t" or c == "\n":
                cleaned_chars.append(c)
                continue
            o = ord(c)
            if o < 0x20:
                continue
            if o == 0x7F:
                continue
            if 0x80 <= o < 0xA0:
                continue
            cleaned_chars.append(c)
        cleaned = "".join(cleaned_chars)
        if len(cleaned) > self.width_chars:
            cleaned = cleaned[: self.width_chars]
        return (cleaned + "\n").encode("ascii", errors="replace")

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

    # Logo hard limits. A compressed image can decode to a bitmap
    # orders of magnitude larger than the file — a 10 MB PNG at 8-bit
    # RGBA decompresses to hundreds of MB. Cap on both the wire body
    # and the decoded pixel count. 2 MB / 20 MP is generous for any
    # legitimate receipt logo (a 600×200 PNG is <50 KB / 0.12 MP).
    _LOGO_MAX_BODY_BYTES = 2_000_000
    _LOGO_MAX_PIXELS = 20_000_000
    _LOGO_ALLOWED_FORMATS = frozenset({"PNG", "JPEG", "GIF"})

    def _logo_bytes(self, logo_url: str, max_width_dots: int) -> bytes:
        """Download + rasterise the receipt logo as GS v 0 bytes.

        Three safety guards (audit C9 + H19 + N16):

          * SSRF: only URLs whose origin matches the connection's
            api_base_url are fetched. Blocks a compromised backend
            from coaxing the client to fetch AWS metadata
            (169.254.169.254), localhost admin endpoints, or the
            local intranet.
          * Resource exhaustion: body capped at 2 MB (streaming
            iter_content, not buffered), decoded pixels capped at
            20 MP. Together these defeat "small file, huge decode"
            on low-RAM POS terminals.
          * Parser surface: only PNG/JPEG/GIF accepted. Exotic
            Pillow parsers (PDF, EPS, TIFF) are disabled — a backend-
            controlled URL should not be able to drive them.

        Returns b"" (receipt prints without a logo) on any failure —
        a logo is decorative, never worth failing a print job over.
        """
        log = logging.getLogger("vula.print")

        if not logo_url:
            return b""
        # Relative URLs need resolving by the caller to an absolute
        # form we can origin-check. Bail rather than guess.
        if logo_url.startswith("/"):
            return b""

        # ── Origin allowlist ─────────────────────────────────────────
        if not self.api_base_url:
            return b""
        try:
            from urllib.parse import urlparse
            logo_parts = urlparse(logo_url)
            api_parts = urlparse(self.api_base_url)
            # Both must be HTTP(S) with a netloc, and the scheme +
            # netloc (host:port) triple must match. Comparing netloc
            # — not just host — prevents port-confusion tricks.
            # Comparing scheme prevents downgrade (https backend
            # serving a logo over http, which would leak the fetch
            # via a passive attacker).
            if logo_parts.scheme not in ("http", "https"):
                log.warning("logo URL scheme %r rejected: %s",
                            logo_parts.scheme, logo_url)
                return b""
            if not logo_parts.netloc or not api_parts.netloc:
                return b""
            if (
                logo_parts.scheme != api_parts.scheme
                or logo_parts.netloc != api_parts.netloc
            ):
                log.warning(
                    "logo URL origin mismatch — refusing to fetch: %s "
                    "(expected origin from %s)",
                    logo_url, self.api_base_url,
                )
                return b""
        except Exception:
            return b""

        try:
            # ── Capped streaming download ────────────────────────────
            with requests.get(logo_url, timeout=8, stream=True) as resp:
                if resp.status_code != 200:
                    return b""
                try:
                    cl = int(resp.headers.get("Content-Length", "0") or "0")
                    if cl > self._LOGO_MAX_BODY_BYTES:
                        log.warning(
                            "logo Content-Length %d exceeds cap %d — refusing",
                            cl, self._LOGO_MAX_BODY_BYTES,
                        )
                        return b""
                except (TypeError, ValueError):
                    pass

                chunks = []
                total = 0
                for chunk in resp.iter_content(65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self._LOGO_MAX_BODY_BYTES:
                        log.warning(
                            "logo body exceeded cap %d bytes — aborting",
                            self._LOGO_MAX_BODY_BYTES,
                        )
                        return b""
                    chunks.append(chunk)
                body = b"".join(chunks)
            if not body:
                return b""

            # ── Decode with pixel cap + format check ────────────────
            from io import BytesIO
            try:
                from PIL import Image
            except ImportError:
                return b""
            from vula_rendering_qr import render_pil_image_gs_v0

            # Pillow's decompression-bomb guard — set once per call;
            # idempotent (module global) and stricter than Pillow's
            # 89 MP default, so this is a tightening.
            Image.MAX_IMAGE_PIXELS = self._LOGO_MAX_PIXELS

            img = Image.open(BytesIO(body))
            fmt = (img.format or "").upper()
            if fmt not in self._LOGO_ALLOWED_FORMATS:
                log.warning(
                    "logo format %r not in whitelist %s — refusing",
                    fmt, sorted(self._LOGO_ALLOWED_FORMATS),
                )
                return b""
            # verify() forces a header + structural parse; it closes
            # the file, so reopen for the render pass.
            try:
                img.verify()
            except Exception as exc:
                log.warning("logo failed Pillow verify(): %s", exc)
                return b""
            img = Image.open(BytesIO(body))
            return render_pil_image_gs_v0(img, max_width_dots=max_width_dots)
        except Exception as exc:
            log.debug("logo fetch/render failed: %s", exc)
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
            # Word-wrap instead of char-boundary slicing. Slicing
            # cut mid-word ("...free deliv" / "ery on orders") —
            # visually broken on a tax receipt. textwrap also handles
            # the edge case of a single word longer than width_chars
            # (break_long_words=True) so nothing is silently dropped.
            import textwrap as _tw
            wrapped = _tw.wrap(
                footer_note,
                width=self.width_chars,
                break_long_words=True,
                break_on_hyphens=False,
            ) or [footer_note[: self.width_chars]]
            for line in wrapped:
                out += self._txt(line)
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
                from vula_printer_target import PrinterTarget, dispatch_print
                target = PrinterTarget.from_assignment(self.printer_device)
                dispatch_print(target, payload, doc_class="pos_slip")
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

    def __init__(
        self,
        printer_device: str,
        detail_payload: Dict[str, Any],
        *,
        width_chars: int = 48,
    ):
        super().__init__()
        self.printer_device = printer_device
        self.detail_payload = detail_payload
        self.width_chars = int(width_chars)
        self.finished.connect(self.deleteLater)

    @staticmethod
    def _cents_to_amount(cents: int) -> str:
        value = Decimal(int(cents)) / Decimal(100)
        value = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"{value:.2f}"

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
        # Same control-byte strip + non-ASCII preservation as
        # POSSlipPrintJob._txt (see that method's comment for the
        # detailed rationale — ESC/POS injection vectors, why stripping
        # non-ASCII here would be worse than visible "?" substitution).
        cleaned_chars = []
        for c in str(text):
            if c == "\t" or c == "\n":
                cleaned_chars.append(c)
                continue
            o = ord(c)
            if o < 0x20:
                continue
            if o == 0x7F:
                continue
            if 0x80 <= o < 0xA0:
                continue
            cleaned_chars.append(c)
        cleaned = "".join(cleaned_chars)
        # Truncate to configured receipt width — without this a 58 mm
        # printer wraps long _col2 lines at its own narrower width and
        # misaligns the money columns.
        if len(cleaned) > self.width_chars:
            cleaned = cleaned[: self.width_chars]
        return (cleaned + "\n").encode("ascii", errors="replace")

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
                from vula_printer_target import PrinterTarget, dispatch_print
                target = PrinterTarget.from_assignment(self.printer_device)
                dispatch_print(target, payload, doc_class="pos_eod")
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
    eod_ensure_attempted = pyqtSignal(str)         # (connection_id) — hourly-gate bookkeeping

    def __init__(
        self,
        connection_id: str,
        api_base: str,
        api_key: str,
        user_id: int,
        in_flight_ids: frozenset,
        eod_in_flight_ids: frozenset,
        *,
        do_eod_ensure: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self._connection_id = connection_id
        self._api_base = api_base.rstrip("/")
        self._api_key = api_key
        self._user_id = user_id
        self._in_flight_ids = in_flight_ids
        self._eod_in_flight_ids = eod_in_flight_ids
        self._do_eod_ensure = bool(do_eod_ensure)
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
        if self._do_eod_ensure:
            try:
                ensure_r = requests.post(
                    f"{self._api_base}/admin/api/pos-eod-reports/ensure-latest",
                    headers={**self._headers(), "Content-Type": "application/json"},
                    json={},
                    timeout=8,
                )
                # Emit attempt regardless of outcome — the caller uses
                # this to update last_eod_ensure_at so a broken endpoint
                # does not retry every 5 s.
                self.eod_ensure_attempted.emit(cid)
                if ensure_r.status_code not in (200, 204):
                    plog.warning(
                        "[%s] ensure-latest -> HTTP %d (EOD generation may "
                        "be stalled on the backend)",
                        cid, ensure_r.status_code,
                    )
                    self.poll_error.emit(
                        cid, f"ensure-latest HTTP {ensure_r.status_code}",
                        ensure_r.status_code,
                    )
            except Exception as exc:
                plog.warning("[%s] ensure-latest failed: %s", cid, exc)
                self.eod_ensure_attempted.emit(cid)

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
        log = logging.getLogger("vula.poll.retry")
        # Rate-limit. Without an inter-request delay, a client that
        # accumulated many ids during an outage hammers the backend in
        # one burst the instant connectivity returns. Thundering-herd
        # risk at fleet scale. 0.25 s keeps individual retries fast but
        # spreads a full flush across minutes, not seconds.
        RETRY_INTERVAL_SECONDS = 0.25
        task_count = len(self._tasks)
        for idx, (cid, base, key, uid, kind, req_id) in enumerate(self._tasks):
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
                if r.status_code in (200, 404):
                    # 200 = completed. 404 = slip no longer exists.
                    # Both are terminal from our side — discard the id.
                    done.append((cid, kind, req_id))
                elif r.status_code == 400:
                    # 400 = malformed completion request. That is a bug
                    # (bad request_id shape, missing field, ...). Do
                    # NOT treat it as silent success — log it for
                    # review, but discard so the retry set cannot grow
                    # unboundedly for a permanently broken request.
                    log.warning(
                        "completion retry %s #%d -> HTTP 400 "
                        "(discarding, flag for review)", kind, req_id,
                    )
                    done.append((cid, kind, req_id))
                else:
                    log.warning(
                        "completion retry %s #%d -> HTTP %d (will retry)",
                        kind, req_id, r.status_code,
                    )
            except Exception as exc:
                # Network hiccup — leave the id in the retry set.
                log.debug("completion retry %s #%d failed: %s",
                          kind, req_id, exc)
            # Rate-limit between requests, not after the last one.
            if idx + 1 < task_count:
                time.sleep(RETRY_INTERVAL_SECONDS)

        if done:
            self.succeeded.emit(done)