"""QR code rendering for the pairing screen.

Thin wrapper over the battle-tested `qrcode` package (pure Python, no PIL
needed for SVG output).  The pairing QR encodes the device-pairing URL,
e.g. http://192.168.1.20:8765/pair?code=123456 — scanning it on a phone
opens the pair page with the code pre-filled.
"""
import io

import qrcode
import qrcode.image.svg


def encode(text: str) -> str:
    """Return a standalone SVG document containing the QR code for `text`."""
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage,
                      error_correction=qrcode.constants.ERROR_CORRECT_L,
                      box_size=10, border=4)
    buf = io.BytesIO()  # the ElementTree writer emits UTF-8 bytes
    img.save(buf)
    return buf.getvalue().decode("utf-8")
