"""
apps/services/signature_service.py — Process a user-uploaded signature image
before it's stored on TDSUser.signature_image.

Users upload a signature in whatever format/size their phone or scanner
produces; this normalizes it into something that always renders cleanly and
consistently in the TDS PDF footer (apps/services/pdf_service.py embeds the
result as a base64 data URI — see build_tds_doc_data()).

Always outputs PNG (transparency-safe, universally supported by WeasyPrint)
scaled to fit within a fixed canvas, never upscaled/stretched/cropped -- a
signature's proportions matter, so this pads rather than distorts.
"""
from io import BytesIO

from PIL import Image, UnidentifiedImageError

# The signature renders at roughly 85pt x 26pt in the PDF (see tds.html's
# .sig-img rule) — this is 4x that in pixels, for a crisp result at print
# resolution instead of a blurry 1:1 raster stretched by the browser/WeasyPrint.
MAX_WIDTH_PX  = 480
MAX_HEIGHT_PX = 160

# Guards against a pathological upload tying up a request; the real WAF here
# is Pillow's own Image.MAX_IMAGE_PIXELS decompression-bomb protection, this
# just rejects an oversized file before Pillow even has to decode it.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB


class InvalidSignatureImage(ValueError):
    """Raised when the uploaded bytes aren't a usable image."""


def process_signature_image(raw_bytes: bytes) -> tuple[bytes, str]:
    """
    Validate and normalize an uploaded signature image.

    Returns (png_bytes, 'image/png'). Raises InvalidSignatureImage (a
    ValueError subclass — see apps/api/exceptions.py's _DESCRIBABLE_EXCEPTIONS)
    on anything that isn't a decodable image, so the view can let it surface
    as a clean 400 instead of a 500.
    """
    if not raw_bytes:
        raise InvalidSignatureImage("No file was uploaded.")
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise InvalidSignatureImage(
            f"Signature image is too large ({len(raw_bytes) / 1024 / 1024:.1f} MB); "
            f"the limit is {MAX_UPLOAD_BYTES / 1024 / 1024:.0f} MB."
        )

    try:
        img = Image.open(BytesIO(raw_bytes))
        img.load()  # force full decode now — a truncated file fails here, not later
    except (UnidentifiedImageError, OSError) as exc:
        raise InvalidSignatureImage(
            "Could not read this file as an image. Use a PNG, JPEG, or WEBP file."
        ) from exc

    # Preserve transparency where the source has it (a clean signature scan
    # is usually black-on-transparent); otherwise flatten to plain RGB so a
    # JPEG's implicit white/black background doesn't turn into an unwanted
    # black box when later composited over the PDF's white page.
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
    else:
        img = img.convert("RGB")

    # Fit (not fill) within the target canvas, preserving aspect ratio —
    # never upscale a smaller source image, only shrink an oversized one.
    img.thumbnail((MAX_WIDTH_PX, MAX_HEIGHT_PX), Image.LANCZOS)

    canvas_mode = "RGBA" if img.mode == "RGBA" else "RGB"
    background  = (0, 0, 0, 0) if canvas_mode == "RGBA" else (255, 255, 255)
    canvas = Image.new(canvas_mode, (MAX_WIDTH_PX, MAX_HEIGHT_PX), background)
    offset = ((MAX_WIDTH_PX - img.width) // 2, (MAX_HEIGHT_PX - img.height) // 2)
    canvas.paste(img, offset, img if canvas_mode == "RGBA" else None)

    out = BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue(), "image/png"
