#!/usr/bin/env python3
"""
Raster QR code generator for ESC/POS printers.

Old thermal POS printers (and almost all "58mm" / "80mm" clone models) do
not implement the native Epson ``GS ( k`` 2D-barcode command set. Sending
those bytes to such a printer can result in:

  * the QR code silently not printing, or
  * garbage glyphs appearing where the QR should be, or
  * the printer's buffer wedging on the next receipt.

The universally compatible approach is to render the QR code as a 1-bit
bitmap on the host and send it via ``GS v 0`` — the same raster command
used for logos. Any printer that can print a logo can print this QR code.
"""

from __future__ import annotations

import qrcode
from qrcode.constants import (
    ERROR_CORRECT_L,
    ERROR_CORRECT_M,
    ERROR_CORRECT_Q,
    ERROR_CORRECT_H,
)

# Map friendly names to qrcode constants.
_EC_MAP = {
    "L": ERROR_CORRECT_L,
    "M": ERROR_CORRECT_M,
    "Q": ERROR_CORRECT_Q,
    "H": ERROR_CORRECT_H,
}


def render_qr_gs_v0(
    data: str,
    module_px: int = 4,
    ec_level: str = "M",
    border_modules: int = 2,
) -> bytes:
    """Render *data* as a QR code in ESC/POS ``GS v 0`` raster format.

    Args:
        data:           text to encode (URL, invoice id, etc.)
        module_px:      pixels per QR module (1-8 recommended). 4 is a good
                        default for 203 dpi printers.
        ec_level:       error correction: "L", "M", "Q", "H".
        border_modules: quiet-zone width in modules (spec says 4, but 2 is
                        usually fine for receipt-sized prints).

    Returns:
        Raw bytes ready to write to the printer: ``GS v 0 m xL xH yL yH``
        header followed by the bitmap data. Returns ``b""`` on any failure
        so the caller can safely concatenate it into a larger buffer.
    """
    if not data:
        return b""

    try:
        ec = _EC_MAP.get(ec_level.upper(), ERROR_CORRECT_M)
        qr = qrcode.QRCode(
            version=None,
            error_correction=ec,
            box_size=1,
            border=border_modules,
        )
        qr.add_data(data)
        qr.make(fit=True)
        matrix = qr.get_matrix()   # list[list[bool]], True = black
    except Exception:
        return b""

    modules = len(matrix)
    if modules == 0:
        return b""

    # Horizontal: module_px pixels per module, rounded up to a multiple of 8
    # because GS v 0 packs 8 horizontal dots per byte.
    width_dots = modules * module_px
    width_bytes = (width_dots + 7) // 8
    width_dots_padded = width_bytes * 8

    # Vertical: module_px lines per module.
    height_dots = modules * module_px

    # Build the bitmap row by row, expanding vertically by module_px.
    bitmap = bytearray()
    for y in range(modules):
        # Precompute this module-row's pixels once, then repeat vertically.
        row = bytearray(width_bytes)
        for x in range(modules):
            if not matrix[y][x]:
                continue
            # Set module_px horizontal pixels at (x * module_px).
            for dx in range(module_px):
                px = x * module_px + dx
                if px >= width_dots_padded:
                    break
                byte_idx = px // 8
                bit_idx = 7 - (px % 8)
                row[byte_idx] |= (1 << bit_idx)
        for _ in range(module_px):
            bitmap += row

    # GS v 0  m  xL xH  yL yH  data
    #   m = 0 (normal size), no double-width/height
    xL = width_bytes & 0xFF
    xH = (width_bytes >> 8) & 0xFF
    yL = height_dots & 0xFF
    yH = (height_dots >> 8) & 0xFF

    header = bytes([0x1D, 0x76, 0x30, 0x00, xL, xH, yL, yH])
    return header + bytes(bitmap)


def render_pil_image_gs_v0(img, max_width_dots: int = 384) -> bytes:
    """Render a PIL image as a 1-bit ``GS v 0`` raster.

    Used for the receipt logo. Downscales to *max_width_dots* if wider,
    converts to 1-bit with Floyd-Steinberg dithering for a nicer result
    than a hard threshold, and packs the result as GS v 0 bytes.

    Returns ``b""`` on any failure.
    """
    if img is None:
        return b""
    try:
        # Flatten alpha onto white, convert to grayscale, then 1-bit.
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = img.new("RGBA", img.size, (255, 255, 255, 255))
            img = bg.alpha_composite(img).convert("L")
        else:
            img = img.convert("L")

        # Downscale if wider than the printer's print area.
        if img.width > max_width_dots:
            ratio = max_width_dots / img.width
            new_h = max(1, int(img.height * ratio))
            img = img.resize((max_width_dots, new_h))

        # 1-bit with dithering — looks much better than a hard cutoff.
        img = img.convert("1", dither=1)  # 1 = Floyd-Steinberg

        width_dots = img.width
        width_bytes = (width_dots + 7) // 8
        height_dots = img.height

        # Pillow gives us 1 bit per pixel already, padded to byte boundary.
        raw = img.tobytes()   # each row is ceil(width/8) bytes, MSB first

        # Rows may not be byte-aligned to our width_bytes if width isn't
        # a multiple of 8 — Pillow pads each row independently, which
        # matches GS v 0's expectation.
        xL = width_bytes & 0xFF
        xH = (width_bytes >> 8) & 0xFF
        yL = height_dots & 0xFF
        yH = (height_dots >> 8) & 0xFF

        header = bytes([0x1D, 0x76, 0x30, 0x00, xL, xH, yL, yH])
        return header + raw
    except Exception:
        return b""