"""Label rendering: icon + text to a 1-bit PNG.

Two geometries:
  diecut  fixed-size labels (NIIMBOT rolls): width = mm across the printhead, height = mm along the feed.
  tape    continuous tape (Brother P-touch): height = printable tape width in px, length grows with content.

Fonts: IBM Plex Sans Bold first (Debian package fonts-ibm-plex), DejaVu Sans Bold fallback.
Icons: Iconify ids (prefix:name; a bare name means material-symbols), fetched once and cached in /data/cache/icons.
"""
from __future__ import annotations

import io
import os
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

DPMM = 8  # 203 dpi, true for the NIIMBOT B1 and close enough to the P-touch's 180 dpi for layout purposes
DATA = Path(os.environ.get("HUB_DATA", "/data"))
ICON_CACHE = DATA / "cache" / "icons"
FONTS = [
    os.environ.get("HUB_FONT", ""),
    "/usr/share/fonts/truetype/ibm-plex/IBMPlexSans-Bold.ttf",
    "/usr/share/fonts/opentype/ibm-plex/IBMPlexSans-Bold.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
UA = {"User-Agent": "lan-print-hub/1 (label renderer)"}


class RenderError(ValueError):
    pass


def font_path(explicit: str | None = None) -> str:
    for p in [explicit or ""] + FONTS:
        if p and Path(p).exists():
            return p
    raise RenderError("no bold font installed")


def icon_svg(icon_id: str) -> str:
    if ":" not in icon_id:
        icon_id = f"material-symbols:{icon_id}"
    prefix, name = icon_id.split(":", 1)
    ICON_CACHE.mkdir(parents=True, exist_ok=True)
    cached = ICON_CACHE / f"{prefix}__{name}.svg"
    if cached.exists():
        return cached.read_text()
    url = f"https://api.iconify.design/{prefix}/{name}.svg?color=%23000000"
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
        svg = r.read().decode()
    if not svg.lstrip().startswith("<svg"):
        raise RenderError(f"icon not found: {icon_id}")
    cached.write_text(svg)
    return svg


def icon_image(icon_id: str, size: int) -> Image.Image:
    import resvg_py

    png = bytes(resvg_py.svg_to_bytes(svg_string=icon_svg(icon_id), width=size, height=size))
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    return Image.alpha_composite(Image.new("RGBA", im.size, "white"), im).convert("L")


def split_lines(text: str) -> list[str]:
    return [ln for ln in (text or "").replace("\\n", "\n").split("\n") if ln.strip()]


def fit_text(draw, lines, fp, max_w, max_h, gap_ratio=0.15):
    lo, hi, best = 6, 400, 6
    while lo <= hi:
        mid = (lo + hi) // 2
        f = ImageFont.truetype(fp, mid)
        boxes = [draw.textbbox((0, 0), ln, font=f) for ln in lines]
        tot = sum(b[3] - b[1] for b in boxes) + int(mid * gap_ratio) * (len(lines) - 1)
        if max(b[2] - b[0] for b in boxes) <= max_w and tot <= max_h:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    return ImageFont.truetype(fp, best), best


def draw_lines(d, lines, f, size, box, gap_ratio=0.15):
    x0, y0, bw, bh = box
    gap = int(size * gap_ratio)
    boxes = [d.textbbox((0, 0), ln, font=f) for ln in lines]
    tot = sum(b[3] - b[1] for b in boxes) + gap * (len(lines) - 1)
    y = y0 + (bh - tot) // 2
    for ln, b in zip(lines, boxes):
        d.text((x0 + (bw - (b[2] - b[0])) // 2 - b[0], y - b[1]), ln, font=f, fill=0)
        y += (b[3] - b[1]) + gap


def finish(img: Image.Image, invert=False) -> Image.Image:
    img = img.point(lambda p: 0 if p < 128 else 255).convert("1")
    if invert:
        img = ImageOps.invert(img.convert("L")).convert("1")
    return img


def diecut(text: str, icon: str | None, across_mm: float, along_mm: float, *, head_px: int = 384,
           layout: str = "auto", margin_mm: float = 1.0, offset_mm: float = 0.0, icon_ratio: float = 0.45,
           font: str | None = None, invert: bool = False, border: bool = False) -> Image.Image:
    """Port of niimbot-labels niimlabel.py render_label (same layouts), plus offset calibration."""
    w = min(round(across_mm * DPMM), head_px)
    h = round(along_mm * DPMM)
    m = round(margin_mm * DPMM)
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    lines = split_lines(text)
    fp = font_path(font)
    iw, ih = w - 2 * m, h - 2 * m
    if layout == "auto":
        layout = "left" if (icon and lines and w >= 1.6 * h) else "top"
    if icon and not lines:
        layout = "icon"
    if not icon and layout != "text":
        layout = "text"

    def text_in(box):
        if lines:
            cand = [lines]
            if len(lines) == 1 and " " in lines[0]:
                ws = lines[0].split()
                cand += [[" ".join(ws[:i]), " ".join(ws[i:])] for i in range(1, len(ws))]
            (f, size), use = max(((fit_text(d, c, fp, box[2], box[3]), c) for c in cand), key=lambda t: t[0][1])
            draw_lines(d, use, f, size, box)

    if layout == "icon":
        s = min(iw, ih)
        img.paste(icon_image(icon, s), (m + (iw - s) // 2, m + (ih - s) // 2))
    elif layout == "left":
        s = ih
        img.paste(icon_image(icon, s), (m, m))
        g = round(1.5 * DPMM)
        text_in((m + s + g, m, iw - s - g, ih))
    elif layout == "top":
        s = min(int(ih * icon_ratio), iw)
        img.paste(icon_image(icon, s), (m + (iw - s) // 2, m))
        g = round(1 * DPMM)
        text_in((m, m + s + g, iw, ih - s - g))
    else:
        text_in((m, m, iw, ih))
    if border:
        d.rounded_rectangle((1, 1, w - 2, h - 2), radius=round(1.5 * DPMM), outline=0, width=3)
    dx = round(offset_mm * DPMM)  # print-position calibration; negative = left
    if dx:
        shifted = Image.new("L", (w, h), 255)
        shifted.paste(img, (dx, 0))
        img = shifted
    return finish(img, invert)


def tape(text: str, icon: str | None, height_px: int, *, font: str | None = None,
         invert: bool = False) -> Image.Image:
    """Landscape tape label: icon on the left, centred text lines on the right."""
    lines = split_lines(text)
    if not lines and not icon:
        raise RenderError("nothing to print: give text, an icon or a png")
    pad = max(2, height_px // 16)
    inner = height_px - 2 * pad
    fp = font_path(font)
    tmp = ImageDraw.Draw(Image.new("L", (1, 1)))
    f, size, tw = None, 0, 0
    if lines:
        f, size = fit_text(tmp, lines, fp, 10_000, inner, gap_ratio=0.12)
        tw = max(tmp.textbbox((0, 0), ln, font=f)[2] - tmp.textbbox((0, 0), ln, font=f)[0] for ln in lines)
    isz = inner if icon else 0
    igap = pad * 2 if icon and lines else 0
    w = pad + isz + igap + tw + pad
    img = Image.new("L", (w, height_px), 255)
    d = ImageDraw.Draw(img)
    if icon:
        img.paste(icon_image(icon, isz), (pad, pad))
    if lines:
        draw_lines(d, lines, f, size, (pad + isz + igap, pad, tw, inner), gap_ratio=0.12)
    return finish(img, invert)


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("L").save(buf, "PNG")
    return buf.getvalue()
