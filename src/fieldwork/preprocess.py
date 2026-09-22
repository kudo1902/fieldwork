"""Image normalisation.

Most extraction accuracy is won or lost here, not in the prompt. Phones
produce rotated HEICs, scanners produce multi-page PDFs, and an image that is
too large costs latency without adding legible detail.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

from .config import settings

register_heif_opener()

# Guard against decompression bombs: a 100MB PNG can expand to gigabytes.
Image.MAX_IMAGE_PIXELS = 80_000_000

PDF_MAGIC = b"%PDF"


class UnsupportedFile(ValueError):
    pass


def _pdf_to_images(data: bytes, max_pages: int) -> list[Image.Image]:
    doc = pdfium.PdfDocument(data)
    try:
        n = min(len(doc), max_pages)
        return [
            doc[i].render(scale=settings.pdf_render_scale).to_pil().convert("RGB")
            for i in range(n)
        ]
    finally:
        doc.close()


def _normalise(img: Image.Image, max_dim: int) -> Image.Image:
    # Honour the EXIF orientation tag, then drop it. Without this, every
    # portrait photo taken on a phone arrives sideways.
    img = ImageOps.exif_transpose(img)

    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        flat = Image.new("RGB", img.size, (255, 255, 255))
        flat.paste(img, mask=img.split()[-1])
        img = flat
    else:
        img = img.convert("RGB")

    if max(img.size) > max_dim:
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)
    return img


def to_page_images(
    data: bytes,
    *,
    max_dim: int | None = None,
    max_pages: int | None = None,
) -> list[Image.Image]:
    """Decode any supported input into a list of normalised RGB page images."""
    max_dim = max_dim or settings.max_image_dim
    max_pages = max_pages or settings.max_pdf_pages

    if data[:4] == PDF_MAGIC:
        pages = _pdf_to_images(data, max_pages)
    else:
        try:
            pages = [Image.open(io.BytesIO(data))]
        except Exception as exc:  # noqa: BLE001 - surface a clean API error
            raise UnsupportedFile(f"could not decode image: {exc}") from exc

    return [_normalise(p, max_dim) for p in pages[:max_pages]]


def to_data_urls(pages: list[Image.Image]) -> list[str]:
    urls = []
    for page in pages:
        buf = io.BytesIO()
        page.save(buf, format="JPEG", quality=settings.jpeg_quality, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        urls.append(f"data:image/jpeg;base64,{b64}")
    return urls


def prepare_file(path: str | Path, **kwargs) -> list[str]:
    return to_data_urls(to_page_images(Path(path).read_bytes(), **kwargs))


def prepare_bytes(data: bytes, **kwargs) -> list[str]:
    return to_data_urls(to_page_images(data, **kwargs))
