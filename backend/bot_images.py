from __future__ import annotations

import io
import logging

import httpx
from PIL import Image, ImageDraw

from backend.models import ClarificationOption

logger = logging.getLogger(__name__)

BRAND_PICK_MAX = 5

_SWIGGY_UPLOAD = "/image/upload/"
_COLLAGE_CELL = 130
_COLLAGE_LABEL = 32
_COLLAGE_PAD = 10
_COLLAGE_COLS = 3


def product_grid_url(url: str | None, *, width: int = 160) -> str | None:
    """Small image for collage tiles."""
    return _resize_swiggy_image(url, width)


def product_thumb_url(url: str | None, *, width: int = 280) -> str | None:
    """Medium preview when enlarging one product."""
    return _resize_swiggy_image(url, width)


def product_full_url(url: str | None, *, width: int = 900) -> str | None:
    """Full-size image for enlarge."""
    return _resize_swiggy_image(url, width) or url


def collage_public_url(base_url: str, draft_item_id: int) -> str:
    return f"{base_url.rstrip('/')}/api/collage/{draft_item_id}.png"


def _resize_swiggy_image(url: str | None, width: int) -> str | None:
    if not url or not url.startswith("http"):
        return None
    if _SWIGGY_UPLOAD not in url:
        return url
    base, rest = url.split(_SWIGGY_UPLOAD, 1)
    if rest.startswith("w_") or rest.startswith("c_"):
        return url
    return f"{base}{_SWIGGY_UPLOAD}w_{width},h_{width},c_fit/{rest}"


def format_pick_caption(
    option: ClarificationOption,
    number: int,
    *,
    enlarge_hint: bool = False,
) -> str:
    lines = [f"{number}. {option.name}"]
    meta: list[str] = []
    if option.pack_size:
        meta.append(option.pack_size)
    if option.unit_price is not None:
        meta.append(f"₹{option.unit_price:.0f}")
    if meta:
        lines.append(" · ".join(meta))
    if enlarge_hint:
        lines.append("Tap 🔍 # to enlarge · ✅ # to pick")
    else:
        lines.append("Tap image to zoom · choose below")
    return "\n".join(lines)


def format_collage_caption(raw_query: str, note: str, count: int) -> str:
    return (
        f"🛒 Pick: {raw_query}\n{note}\n\n"
        f"{count} options above — tap 🔍 # to enlarge, ✅ # to add."
    )


async def build_pick_collage(options: list[ClarificationOption]) -> bytes:
    """Build a compact numbered grid of product thumbnails."""
    opts = options[:BRAND_PICK_MAX]
    if not opts:
        raise ValueError("No options for collage")

    n = len(opts)
    cols = min(_COLLAGE_COLS, n)
    grid_rows = (n + cols - 1) // cols
    cell_h = _COLLAGE_CELL + _COLLAGE_LABEL
    width = cols * _COLLAGE_CELL + (cols + 1) * _COLLAGE_PAD
    height = grid_rows * cell_h + (grid_rows + 1) * _COLLAGE_PAD

    canvas = Image.new("RGB", (width, height), (18, 20, 28))
    draw = ImageDraw.Draw(canvas)

    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
        for idx, opt in enumerate(opts):
            row, col = divmod(idx, cols)
            x = _COLLAGE_PAD + col * (_COLLAGE_CELL + _COLLAGE_PAD)
            y = _COLLAGE_PAD + row * (cell_h + _COLLAGE_PAD)

            tile = Image.new("RGB", (_COLLAGE_CELL, _COLLAGE_CELL), (32, 36, 48))
            thumb_url = product_grid_url(opt.image_url)
            if thumb_url:
                try:
                    resp = await client.get(thumb_url)
                    resp.raise_for_status()
                    img = Image.open(io.BytesIO(resp.content)).convert("RGB")
                    img = img.resize(
                        (_COLLAGE_CELL, _COLLAGE_CELL), Image.Resampling.LANCZOS
                    )
                    tile = img
                except Exception:
                    logger.warning("Collage thumb fetch failed for %s", opt.name)

            canvas.paste(tile, (x, y))

            badge_r = 22
            draw.ellipse(
                [x + 6, y + 6, x + 6 + badge_r, y + 6 + badge_r],
                fill=(252, 128, 25),
            )
            draw.text((x + 13, y + 9), str(idx + 1), fill=(255, 255, 255))

            price = f"₹{opt.unit_price:.0f}" if opt.unit_price is not None else ""
            name = (opt.name or "Item")[:16]
            draw.text((x, y + _COLLAGE_CELL + 4), name, fill=(220, 224, 232))
            if price:
                draw.text((x, y + _COLLAGE_CELL + 18), price, fill=(252, 128, 25))

    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
