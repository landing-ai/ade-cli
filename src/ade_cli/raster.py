"""Page rasters for the viewer: the recorded source rendered at build time.

Sources are referenced, never copied (review decision #1): the viewer
resolves the recorded source path when it builds and degrades fail-safe —
a missing, URL-only, or unrenderable source yields pages without images,
never a failed build. Stored artifacts always render; only the page
imagery weakens.
"""

from __future__ import annotations

import base64
import hashlib
import io
from pathlib import Path


class CropError(Exception):
    """A crop that cannot be produced honestly. ``kind`` is the
    machine-readable cause (``source_missing`` | ``source_unrenderable`` |
    ``page_missing``) — the issue #10 rule: a missing source is a clear
    error, never a stale image."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def crop_box(source: str | None, page: int, box: dict, *, dpi: int):
    """Render the source page and crop the element's normalized box.

    Mirrors vision-agent-service's DPT-3 crop semantics — ``crop_region``
    (packages/pdf-processing/pdf_processing/image.py:59-107): each 0-1
    edge is multiplied by the raster dimensions, clamped to the image
    bounds, and cropped; ``crop_image_region_dpt3``
    (apps/dpt3-chunk-worker/dpt3_chunk_worker/utils.py:51-73) is the
    wrapper that feeds it normalized boxes, exactly the shape our wire
    contract carries. Returns a PIL image; raises CropError instead of
    ever producing a stale or approximated crop.
    """
    if not source:
        raise CropError("source_missing", "no local source recorded")
    path = Path(source)
    if not path.is_file():
        if source.startswith(("http://", "https://")):
            raise CropError(
                "source_missing",
                "parsed from a URL — the CLI never had the document bytes "
                "to crop from; download the file and parse it locally "
                "(ade parse -d <file>)",
            )
        raise CropError("source_missing", f"{source} no longer exists")
    try:
        image = _render_page(path, page, dpi=dpi)
    except CropError:
        raise
    except Exception as error:
        raise CropError("source_unrenderable", str(error)) from error
    width, height = image.size
    # Pixel conversion + clamp, as in crop_region: left/top floor toward
    # the origin, right/bottom never precede them.
    left = max(0, min(int(box["xmin"] * width), width))
    top = max(0, min(int(box["ymin"] * height), height))
    right = max(left, min(int(box["xmax"] * width), width))
    bottom = max(top, min(int(box["ymax"] * height), height))
    return image.crop((left, top, right, bottom))


def _render_page(path: Path, page: int, *, dpi: int):
    """One page of the source as a PIL image (1-indexed, per the wire)."""
    from PIL import Image

    with path.open("rb") as head:
        is_pdf = head.read(5).startswith(b"%PDF")
    if is_pdf:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(path)
        try:
            if not 1 <= page <= len(pdf):
                raise CropError(
                    "page_missing", f"source has {len(pdf)} page(s); page {page} not in it"
                )
            return pdf[page - 1].render(scale=dpi / 72).to_pil()
        finally:
            pdf.close()
    if page != 1:
        raise CropError("page_missing", f"an image source only has page 1, not {page}")
    with Image.open(path) as image:
        # Return a copy detached from the file handle — the `with` closes
        # the source on exit, and a closed PIL image can fail downstream.
        return image.copy()


def source_drift_note(meta: dict | None) -> str | None:
    """A drift warning when the file at ``meta['source']`` no longer holds
    the bytes the item was processed from (``identity.content_hash``), None
    otherwise (issue #119). Only drift is reported: a missing file keeps its
    own surface (CropError / the degradation note), URL sources have no
    content hash by design, and metas recorded before the identity block
    existed are unverifiable, not stale."""
    meta = meta or {}
    source = meta.get("source")
    expected = (meta.get("identity") or {}).get("content_hash")
    if not source or not expected:
        return None
    path = Path(source)
    if not path.is_file():
        return None
    try:
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError:
        # Unreadable is not drift: the render path owns that failure and
        # reports it on its own terms (CropError / the degradation note)
        # — the best-effort check must never fail a build the renderer
        # would have degraded gracefully.
        return None
    if digest == expected:
        return None
    return (
        f"{source} changed after this item was processed; imagery renders "
        "the file's current bytes, not the ones the elements were computed "
        "from — re-run `ade parse` to refresh"
    )


def render_source(
    source: str | None, pages: list[int], *, dpi: int, cap: int
) -> tuple[dict[int, dict], str | None]:
    """Rasters for ``pages`` (1-indexed, per the wire contract) from the
    recorded source.

    Returns ``(images by page, degradation note)``: images map a page
    number to ``{data_uri, width, height}``; the note explains any page
    that could not be embedded (None when everything asked for is there).
    """
    if not source:
        return {}, "source unavailable: no local source recorded"
    path = Path(source)
    if not path.is_file():
        if source.startswith(("http://", "https://")):
            # Not an error to fix in place (#169): URL parses never hand
            # the CLI the document bytes, so there is nothing local to
            # raster — say why, what still works, and the action that
            # gets previews. The "source un" prefix is load-bearing:
            # view.py keys the no-sidecar path off it.
            # Cause + what still works; the id-bearing action (`ade view
            # <id> --download`) is appended by the view layer, which
            # holds the item id.
            return {}, (
                "source unavailable: parsed from a URL — the CLI never had "
                "the document bytes to render page previews (markdown, "
                "elements, and extractions are unaffected)."
            )
        return {}, (
            f"source unavailable: {source} no longer exists — restore the "
            "file at that path and re-run `ade view` to render page previews"
        )
    wanted = pages[:cap] if cap else list(pages)
    try:
        with path.open("rb") as head:
            is_pdf = head.read(5).startswith(b"%PDF")
        if is_pdf:
            images = _render_pdf(path, wanted, dpi=dpi)
        else:
            images = _render_image(path, wanted)
    except Exception as error:  # degrade, never fail the build
        return {}, f"source unrenderable: {error}"
    capped = [p for p in pages if p not in images]
    note = (
        f"{len(capped)} page(s) not embedded (cap {cap}, or absent from the source)"
        if capped
        else None
    )
    return images, note


def _render_pdf(path: Path, wanted: list[int], *, dpi: int) -> dict[int, dict]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(path)
    try:
        # Wire pages are 1-indexed; pdfium indexes from 0.
        return {
            page: _encode(pdf[page - 1].render(scale=dpi / 72).to_pil())
            for page in wanted
            if 1 <= page <= len(pdf)
        }
    finally:
        pdf.close()


def _render_image(path: Path, wanted: list[int]) -> dict[int, dict]:
    """A non-PDF source is a single-image document: it only has page 1."""
    from PIL import Image

    if 1 not in wanted:
        return {}
    with Image.open(path) as image:
        return {1: _encode(image)}


def _encode(image) -> dict:
    """WebP at quality 75 — ~40% smaller than the JPEG-80 it replaced at
    comparable visual quality, which is the size posture everywhere this
    imagery lands: embedded artifacts stay shareable, and the on-demand
    page sidecars (pages-N.js) cost the store less disk. Every browser
    the viewer targets decodes WebP."""
    width, height = image.size
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "WEBP", quality=75)
    data = base64.b64encode(buffer.getvalue()).decode("ascii")
    return {
        "data_uri": f"data:image/webp;base64,{data}",
        "width": width,
        "height": height,
    }
