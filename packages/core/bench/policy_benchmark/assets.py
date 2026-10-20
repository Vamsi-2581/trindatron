"""Test media for the multimodal tasks, generated rather than stored.

Every asset here is produced deterministically at runtime and handed to the
model as a data URI. Nothing is committed: a benchmark that ships binary
fixtures acquires a maintenance burden and a licensing question for no gain,
and generated assets have a property stored ones do not — the expected answer
is known by construction, because this module drew it.

That matters for grading. A stock photograph would need a human to decide
what the right answer is; a square this module painted red has exactly one
correct colour, and the checker can assert it without judgement.
"""
from __future__ import annotations

import base64
import io
import zlib

from PIL import Image, ImageDraw

#: The answers the checkers assert against. Defined here, beside the drawing
#: code, so a change to one cannot silently diverge from the other.
SHAPE_COLOR = "red"
SHAPE_KIND = "circle"
BACKGROUND_COLOR = "white"
IMAGE_TEXT = "TRINDATRON 42"
DOC_INVOICE_TOTAL = "1847.50"
DOC_INVOICE_ID = "INV-2026-0917"


def _png_circle() -> bytes:
    """A red circle on white. Unambiguous colour and shape."""
    img = Image.new("RGB", (320, 320), "white")
    draw = ImageDraw.Draw(img)
    draw.ellipse((60, 60, 260, 260), fill="red")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_text() -> bytes:
    """Large black text on white — an OCR-ish read, not a recognition task.

    The default bitmap font is used deliberately: it renders identically
    everywhere, so the task does not become "is this machine's font stack
    installed".
    """
    img = Image.new("RGB", (640, 200), "white")
    draw = ImageDraw.Draw(img)
    # Scaled up from the default font so it survives JPEG compression.
    tmp = Image.new("RGB", (160, 50), "white")
    ImageDraw.Draw(tmp).text((6, 18), IMAGE_TEXT, fill="black")
    img.paste(tmp.resize((620, 180), Image.LANCZOS), (10, 10))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def _webp_counting() -> bytes:
    """Exactly three blue squares — a counting task, which is where weaker
    vision models actually fail."""
    img = Image.new("RGB", (480, 160), "white")
    draw = ImageDraw.Draw(img)
    for i in range(3):
        x = 30 + i * 150
        draw.rectangle((x, 40, x + 100, 120), fill="blue")
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=95)
    return buf.getvalue()


def _pdf_invoice() -> bytes:
    """A minimal, valid single-page PDF carrying two extractable fields.

    Hand-built rather than pulled from a PDF library: the whole asset is ~1 KB,
    the structure is fixed, and adding a dependency to draw one page of text
    would be more moving parts than the thing it produces.
    """
    lines = [
        "BT /F1 16 Tf 60 700 Td (ACME SUPPLIES LTD) Tj ET",
        "BT /F1 12 Tf 60 670 Td (Invoice ID: %s) Tj ET" % DOC_INVOICE_ID,
        "BT /F1 12 Tf 60 650 Td (Date: 2026-09-17) Tj ET",
        "BT /F1 12 Tf 60 620 Td (Widgets x 40 .......... 1200.00) Tj ET",
        "BT /F1 12 Tf 60 600 Td (Freight ............... 350.00) Tj ET",
        "BT /F1 12 Tf 60 580 Td (Tax ................... 297.50) Tj ET",
        "BT /F1 14 Tf 60 550 Td (TOTAL DUE: USD %s) Tj ET" % DOC_INVOICE_TOTAL,
    ]
    content = ("\n".join(lines)).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


def _data_uri(media_type: str, payload: bytes) -> str:
    return f"data:{media_type};base64," + base64.b64encode(payload).decode("ascii")


def png_circle_uri() -> str:
    return _data_uri("image/png", _png_circle())


def jpeg_text_uri() -> str:
    return _data_uri("image/jpeg", _jpeg_text())


def webp_squares_uri() -> str:
    return _data_uri("image/webp", _webp_counting())


def pdf_invoice_uri() -> str:
    return _data_uri("application/pdf", _pdf_invoice())


def pdf_invoice_bytes() -> bytes:
    return _pdf_invoice()
