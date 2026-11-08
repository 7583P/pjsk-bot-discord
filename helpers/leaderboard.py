"""Portrait PNG pages for the existing, ordered Discord leaderboard."""

import hashlib
import io
import json
import math
from collections import OrderedDict
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from helpers.image import hex_to_rgb, strip_unsupported_chars
from helpers.leaderboard_palette import WEB_RANK_GRADIENTS, WEB_TITLE_GRADIENTS

ROOT = Path(__file__).resolve().parents[1]
BACKGROUND = ROOT / "assets" / "top100_stars_vertical.png"
FONT = ROOT / "assets" / "rodinntlg_eb.otf"
PER_PAGE = 10


@lru_cache(maxsize=1)
def _base_canvas():
    """Decode + fit the background once; every render starts from a cheap copy."""
    with Image.open(BACKGROUND) as background:
        return ImageOps.fit(background.convert("RGB"), (1024, 1536))


@lru_cache(maxsize=2048)
def _gradient_strip(start, end, width, padding, span):
    strip = Image.new("RGB", (width, 1))
    strip.putdata([
        tuple(round(a + (b - a) * max(0, min(1, (x - padding) / span))) for a, b in zip(start, end))
        for x in range(width)
    ])
    return strip


def equipped_title(row):
    """Resolve the selected title; a stale CURRENT selection stays hidden."""
    row = dict(row)
    if row.get("title_label"):
        return str(row["title_label"]), row.get("title_style") or "top-white"
    key = str(row.get("effective_title_key") or "").strip()
    if not key or key.lower().startswith(("current:", "current ")):
        return None, None
    match = re.fullmatch(r"(?:season|reward):[^:]+:(?:(place|top):)?(\d+)", key, re.I)
    if match and row.get("title_season_name"):
        kind, number = match.groups()
        number = int(number)
        season = row["title_season_name"]
        if kind == "top":
            return f"{season} TOP {number}", "top-white"
        suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
        style = {1: "gold", 2: "silver", 3: "bronze", 4: "place-4", 5: "place-5"}.get(number, "place-6-10" if number <= 10 else "top-white")
        return f"{season} {number}{suffix} Place", style
    return (key, "top-white") if ":" not in key else (None, None)


@lru_cache(maxsize=64)
def _font(size):
    return ImageFont.truetype(str(FONT), size)


def _fit(draw, text, size, width, *, minimum=18):
    text = str(text or "").replace("\n", " ").strip()
    for point in range(size, minimum - 1, -1):
        font = _font(point)
        if draw.textlength(text, font=font) <= width:
            return text, font
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text + "…", font


def _name(row):
    for value in (row.get("member_name"), row.get("player_name")):
        text = unicodedata.normalize("NFKC", str(value or ""))
        supported = strip_unsupported_chars(text, str(FONT))
        if any(char.isalnum() for char in supported):
            return supported
    return f"Player {row['user_id']}"


def rank_gradient(role):
    key = str(role or "").replace("\u2b06", "Up").replace("\u2191", "Up").replace(" ", "")
    return WEB_RANK_GRADIENTS.get(key, ("#FFFFFF", "#FFFFFF"))


def _draw_gradient_text(canvas, xy, value, font, colors, *, anchor=None, neon=False):
    """Paint a sharp gradient face with optional, tightly bounded neon bloom."""
    value = str(value or "")
    if not value.strip():
        return
    bbox = font.getbbox(value, anchor=anchor)
    padding = 12 if neon else 2
    width = max(1, bbox[2] - bbox[0]) + 2 * padding
    height = max(1, bbox[3] - bbox[1]) + 2 * padding
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).text(
        (padding - bbox[0], padding - bbox[1]), value, font=font, fill=255, anchor=anchor,
    )
    start, end = map(hex_to_rgb, colors)
    span = max(1, width - 2 * padding - 1)
    gradient = _gradient_strip(start, end, width, padding, span).resize((width, height))
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if neon:
        for radius, opacity in ((4.5, .55), (1.5, .85)):
            halo = gradient.convert("RGBA")
            halo.putalpha(mask.filter(ImageFilter.GaussianBlur(radius)).point(lambda alpha: round(alpha * opacity)))
            layer = Image.alpha_composite(layer, halo)
    face = gradient.convert("RGBA")
    face.putalpha(mask)
    layer = Image.alpha_composite(layer, face)
    canvas.paste(layer, (round(xy[0] + bbox[0] - padding), round(xy[1] + bbox[1] - padding)), layer)


def generate_top100_image(rows, season_name, page=0, *, per_page=PER_PAGE):
    """Render one page without changing standings, titles or database state."""
    total = min(len(rows), 100)
    pages = max(1, math.ceil(total / per_page))
    page = max(0, min(int(page), pages - 1))
    offset = page * per_page
    selected = rows[offset:min(offset + per_page, 100)]
    canvas = _base_canvas().copy()
    draw = ImageDraw.Draw(canvas)

    def text(xy, value, size, fill, *, anchor=None):
        font = _font(size)
        draw.text(xy, str(value), font=font, fill=fill, anchor=anchor,
                  stroke_width=1, stroke_fill="#020510")

    text((64, 54), "PJSK LOUNGE", 24, "#A8B9F0")
    text((64, 95), "TOP 100", 76, "#FFFFFF")
    label, font = _fit(draw, season_name, 28, 860)
    draw.text((67, 192), label, font=font, fill="#C5B8FF")
    text((66, 252), "POS", 18, "#94A3CC")
    text((151, 252), "PLAYER / TITLE", 18, "#94A3CC")
    text((954, 252), "MMR / RANK", 18, "#94A3CC", anchor="ra")
    draw.line((64, 287, 960, 287), fill="#4C5581", width=2)
    for index, row in enumerate(selected):
        position = offset + index + 1
        y = 309 + index * 108
        podium = {1: "#FFDD75", 2: "#E4E7EF", 3: "#F1A56E"}
        text((69, y + 9), f"{position:02}", 34, podium.get(position, "#879AC9"))
        colors = rank_gradient("Season Placement" if row.get("is_unplayed_sp") else row.get("role"))
        name, font = _fit(draw, _name(row), 32, 560, minimum=22)
        _draw_gradient_text(canvas, (151, y + 4), name, font, colors)
        if row.get("title_label"):
            label, title_font = _fit(draw, row["title_label"], 21, 560, minimum=17)
            title_colors = WEB_TITLE_GRADIENTS.get(row.get("title_style"), WEB_TITLE_GRADIENTS["top-white"])
            _draw_gradient_text(canvas, (153, y + 49), label, title_font, title_colors, neon=True)
        _draw_gradient_text(canvas, (954, y - 1), f"{int(row['visible_mmr']):,}",
                            _font(38), colors, anchor="ra")
        rank = "Season Placement" if row.get("is_unplayed_sp") else row.get("role", "Placement")
        rank_up = "⬆" in rank
        rank = rank.replace("⬆", "").strip()
        label, rank_font = _fit(draw, rank, 21, 222, minimum=16)
        _draw_gradient_text(canvas, (930 if rank_up else 954, y + 50), label, rank_font, colors, anchor="ra")
        if rank_up:
            draw.line((945, y + 73, 945, y + 56), fill=colors[-1], width=3)
            draw.line([(939, y + 62), (945, y + 56), (951, y + 62)], fill=colors[-1], width=3)
        if index < len(selected) - 1:
            draw.line((151, y + 91, 960, y + 91), fill="#242C4B", width=1)
    if not selected:
        text((512, 690), "No players in this season", 30, "#D6DCF4", anchor="mm")
    draw.line((64, 1427, 960, 1427), fill="#4C5581", width=2)
    text((66, 1454), f"{offset + 1 if total else 0}–{min(offset + per_page, total)} / {total} PLAYERS", 20, "#A8B9F0")
    text((954, 1454), f"PAGE {page + 1:02} / {pages:02}", 20, "#C5B8FF", anchor="ra")
    output = io.BytesIO()
    canvas.save(output, format="PNG", optimize=False, compress_level=1)
    output.seek(0)
    return output


_PNG_CACHE: "OrderedDict[str, bytes]" = OrderedDict()
_PNG_CACHE_MAX = 24
_DRAWN_FIELDS = ("user_id", "member_name", "player_name", "role", "visible_mmr", "is_unplayed_sp", "title_label", "title_style")


def render_top100_png(rows, season_name, page=0, *, per_page=PER_PAGE):
    """PNG bytes for one page. Keyed by everything that is drawn, so identical standings reuse the image and
    any change in MMR/order/titles produces a new key (never stale)."""
    total = min(len(rows), 100)
    pages = max(1, math.ceil(total / per_page))
    page = max(0, min(int(page), pages - 1))
    offset = page * per_page
    selected = rows[offset:min(offset + per_page, 100)]
    key = hashlib.sha1(json.dumps([season_name, page, per_page, total, [[r.get(k) for k in _DRAWN_FIELDS] for r in selected]], default=str, sort_keys=True).encode()).hexdigest()
    hit = _PNG_CACHE.get(key)
    if hit is not None:
        _PNG_CACHE.move_to_end(key)
        return hit
    data = generate_top100_image(rows, season_name, page, per_page=per_page).getvalue()
    _PNG_CACHE[key] = data
    while len(_PNG_CACHE) > _PNG_CACHE_MAX:
        _PNG_CACHE.popitem(last=False)
    return data
