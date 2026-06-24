try:
    from fontTools.ttLib import TTFont
except Exception:
    TTFont = None

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops, ImageOps

import io
import re
import urllib.request
from pathlib import Path
from typing import Literal, List, Any


# ============================================================
# RESULTS IMAGE CONFIG
# ============================================================

rank_colors = {
    "Season Placement": ("#ff8519", "#ff8519"),
    "Placement": ("#ff4600", "#ff7000"),
    "Iron": ("#8d7f79", "#5f514a"),
    "Iron ⬆": ("#917f76", "#775341"),
    "Bronze": ("#9f4600", "#752d00"),
    "Bronze ⬆": ("#c55500", "#a03d00"),
    "Silver": ("#c7c5c5", "#787777"),
    "Silver ⬆": ("#cfcfcf", "#a3a3a3"),
    "Gold": ("#ffcf87", "#e2921c"),
    "Gold ⬆": ("#ffbf60", "#c57600"),
    "Platinum": ("#99a1f1", "#5d69d3"),
    "Platinum ⬆": ("#bf8bff", "#8c6fff"),
    "Diamond": ("#5ec1ff", "#1e75e6"),
    "Diamond ⬆": ("#7dbaff", "#00f6ff"),
    "Crystal": ("#e69bff", "#d52cff"),
    "Crystal ⬆": ("#fab9fa", "#7e3d7e"),
    "Master": ("#c176ff", "#881ffb"),
    "Master ⬆": ("#d6b9ff", "#8e43ff"),
    "Champion": ("#ff5953", "#b10600"),
    "Grand Champion": ("#ff7878", "#b40000"),
    "Star": ("#8f8aff", "#de68ff"),
    "Divine Star": ("#7d94ff", "#de87ff"),
    "Legend": ("#82bcff", "#ff8be4"),
}

level_colors = {
    "Append": ("#FF7BE0", "#AF95FE"),
    "Master": ("#B934ED", "#B934ED"),
    "Expert": ("#EE2C7A", "#EE2C7A"),
    "Hard": ("#FCCA00", "#FCCA00"),
    "Normal": ("#31BAF0", "#31BAF0"),
    "Easy": ("#65DC10", "#65DC10"),
}


REGIONAL_A = 0x1F1E6
REGIONAL_Z = 0x1F1FF
ISO2_RE = re.compile(r"^[A-Za-z]{2}$")

UNKNOWN_FLAG_CODE = "__UNKNOWN_FLAG__"
UNKNOWN_COUNTRY_CODES = {
    "",
    "WW",
    "XX",
    "ZZ",
    "UN",
    "??",
    "--",
    "NONE",
    "NULL",
    "UNKNOWN",
    UNKNOWN_FLAG_CODE,
}


# ============================================================
# STATS IMAGE CONFIG
# ============================================================

ROOT_DIR = Path(__file__).resolve().parents[1]

STATS_ASSETS_DIR = ROOT_DIR / "assets" / "stats"
STATS_BACKGROUND_PATH = STATS_ASSETS_DIR / "stats_background.png"
STATS_RANKS_DIR = STATS_ASSETS_DIR / "ranks"

# Tu repo tiene esta fuente según tu estructura.
# También aceptamos la variante con doble n por compatibilidad.
STATS_FONT_CANDIDATES = [
    ROOT_DIR / "assets" / "rodintlg_eb.otf",
    ROOT_DIR / "assets" / "rodinntlg_eb.otf",
    Path("assets/rodintlg_eb.otf"),
    Path("assets/rodinntlg_eb.otf"),
]

RANK_LOGO_FILES = {
    "Season Placement": "placement.png",
    "Placement": "placement.png",
    "Iron": "iron.png",
    "Iron ⬆": "iron_up.png",
    "Bronze": "bronze.png",
    "Bronze ⬆": "bronze_up.png",
    "Silver": "silver.png",
    "Silver ⬆": "silver_up.png",
    "Gold": "gold.png",
    "Gold ⬆": "gold_up.png",
    "Platinum": "platinum.png",
    "Platinum ⬆": "platinum_up.png",
    "Diamond": "diamond.png",
    "Diamond ⬆": "diamond_up.png",
    "Crystal": "crystal.png",
    "Crystal ⬆": "crystal_up.png",
    "Master": "master.png",
    "Master ⬆": "master_up.png",
    "Champion": "champion.png",
    "Grand Champion": "grand_champion.png",
    "Star": "star.png",
    "Divine Star": "divine_star.png",
    "Legend": "legend.png",
}

RANK_PROGRESS_RANGES = {
    "Season Placement": (0, 50),
    "Placement": (0, 50),
    "Iron": (0, 50),
    "Iron ⬆": (50, 100),
    "Bronze": (100, 150),
    "Bronze ⬆": (150, 200),
    "Silver": (200, 250),
    "Silver ⬆": (250, 300),
    "Gold": (300, 350),
    "Gold ⬆": (350, 400),
    "Platinum": (400, 450),
    "Platinum ⬆": (450, 500),
    "Diamond": (500, 550),
    "Diamond ⬆": (550, 600),
    "Crystal": (600, 650),
    "Crystal ⬆": (650, 700),
    "Master": (700, 750),
    "Master ⬆": (750, 800),
    "Champion": (800, 850),
    "Grand Champion": (850, 900),
    "Star": (900, 950),
    "Divine Star": (950, 1000),
    "Legend": (1000, 1000),
}


# ============================================================
# COMMON HELPERS
# ============================================================

def is_unknown_country_code(value: Any) -> bool:
    return str(value or "").strip().upper() in UNKNOWN_COUNTRY_CODES


def normalize_country_code(value: Any) -> str | None:
    if value is None:
        return None

    code = str(value).strip().upper()
    if not ISO2_RE.match(code):
        return None

    return code


def normalize_results_country_code(value: Any) -> str | None:
    if is_unknown_country_code(value):
        return UNKNOWN_FLAG_CODE

    return normalize_country_code(value)


def flag_emoji_to_iso2(text: str) -> tuple[str, str | None]:
    """
    Si el texto termina con una bandera emoji, la remueve del nombre
    y devuelve el ISO2.
    Ejemplo:
      "Player 🇵🇪" -> ("Player", "PE")
    """
    if not text:
        return text, None

    chars = list(text.rstrip())
    if len(chars) < 2:
        return text, None

    a = ord(chars[-2])
    b = ord(chars[-1])

    if REGIONAL_A <= a <= REGIONAL_Z and REGIONAL_A <= b <= REGIONAL_Z:
        iso = chr((a - REGIONAL_A) + ord("A")) + chr((b - REGIONAL_A) + ord("A"))
        clean = "".join(chars[:-2]).rstrip()
        return clean, iso

    return text, None


def country_flag_emoji(code: str) -> str:
    code = str(code or "").upper()
    if len(code) != 2:
        return ""

    if not ISO2_RE.match(code):
        return ""

    return chr(ord(code[0]) + 127397) + chr(ord(code[1]) + 127397)


def strip_unsupported_chars(text: str, font_path: str | Path) -> str:
    """
    Limpia caracteres que la fuente principal no puede dibujar.
    Esto evita cuadrados raros en nombres.
    Las banderas NO se dibujan con esta fuente; se dibujan como PNG.
    """
    text = str(text or "")

    if TTFont is None:
        return text.strip()

    try:
        font = TTFont(str(font_path))
        cmaps = [table.cmap for table in font["cmap"].tables]

        def supported(char: str) -> bool:
            cp = ord(char)
            return any(cp in cmap for cmap in cmaps)

        filtered = "".join(ch for ch in text if supported(ch))
        return filtered.strip()
    except Exception:
        return text.strip()


def add_stroke(img, stroke_width=4, stroke_color=(0, 0, 0, 255)):
    img = img.convert("RGBA")
    w, h = img.size
    pad = stroke_width

    expanded = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    expanded.paste(img, (pad, pad))

    alpha = expanded.getchannel("A")

    dilated = alpha
    for _ in range(stroke_width):
        dilated = dilated.filter(ImageFilter.MaxFilter(3))

    stroke_mask = ImageChops.subtract(dilated, alpha)

    stroke = Image.new("RGBA", expanded.size, stroke_color)
    stroke.putalpha(stroke_mask)

    out = Image.alpha_composite(stroke, expanded)
    return out


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        raise ValueError("Invalid hex code length")

    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return (r, g, b)


def draw_gradient(img, start, end, direction: Literal["down", "right"] = "down"):
    px = img.load()

    if direction == "down":
        denom = max(1, img.height - 1)
        for y in range(img.height):
            color = tuple(
                int(start[i] + (end[i] - start[i]) * y / denom)
                for i in range(3)
            )
            for x in range(img.width):
                px[x, y] = color

    elif direction == "right":
        denom = max(1, img.width - 1)
        for x in range(img.width):
            color = tuple(
                int(start[i] + (end[i] - start[i]) * x / denom)
                for i in range(3)
            )
            for y in range(img.height):
                px[x, y] = color


def make_gradient_text_image(
    text: str,
    font: ImageFont.FreeTypeFont,
    gradient_start: tuple[int, int, int],
    gradient_end: tuple[int, int, int],
    padding: int = 2,
    direction: Literal["down", "right"] = "right",
) -> Image.Image:
    text = str(text or "").strip()
    if not text:
        text = "?"

    x0, y0, x1, y1 = font.getbbox(text)

    text_w = max(1, x1 - x0)
    text_h = max(1, y1 - y0)

    w = text_w + padding * 2
    h = text_h + padding * 2

    gradient = Image.new("RGB", (w, h))
    draw_gradient(gradient, gradient_start, gradient_end, direction=direction)

    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)

    d.text(
        (padding - x0, padding - y0),
        text,
        font=font,
        fill=255,
    )

    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(gradient, (0, 0), mask)

    return out


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    box = draw.textbbox((0, 0), str(text), font=font)
    return int(box[2] - box[0])


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> str:
    text = str(text or "")

    if _text_width(draw, text, font) <= max_width:
        return text

    while len(text) > 1 and _text_width(draw, text + "...", font) > max_width:
        text = text[:-1]

    return text + "..."


def _center_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill,
    stroke_width: int = 0,
    stroke_fill=(0, 0, 0, 255),
):
    x, y = xy
    box = draw.textbbox((0, 0), str(text), font=font)
    w = box[2] - box[0]
    h = box[3] - box[1]

    draw.text(
        (x - w / 2, y - h / 2),
        str(text),
        font=font,
        fill=fill,
        stroke_width=stroke_width,
        stroke_fill=stroke_fill,
    )


def _draw_glow_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill=(255, 255, 255, 255),
    glow=(55, 180, 255, 180),
    stroke_width: int = 2,
):
    x, y = xy

    for offset in (5, 3):
        draw.text(
            (x, y),
            str(text),
            font=font,
            fill=glow,
            stroke_width=offset,
            stroke_fill=glow,
        )

    draw.text(
        (x, y),
        str(text),
        font=font,
        fill=fill,
        stroke_width=stroke_width,
        stroke_fill=(4, 8, 22, 255),
    )


# ============================================================
# RANK / FLAG HELPERS
# ============================================================

def rank_from_mmr(mmr: int) -> str:
    mmr = int(mmr or 0)

    if mmr <= 49:
        return "Iron"
    if mmr <= 99:
        return "Iron ⬆"
    if mmr <= 149:
        return "Bronze"
    if mmr <= 199:
        return "Bronze ⬆"
    if mmr <= 249:
        return "Silver"
    if mmr <= 299:
        return "Silver ⬆"
    if mmr <= 349:
        return "Gold"
    if mmr <= 399:
        return "Gold ⬆"
    if mmr <= 449:
        return "Platinum"
    if mmr <= 499:
        return "Platinum ⬆"
    if mmr <= 549:
        return "Diamond"
    if mmr <= 599:
        return "Diamond ⬆"
    if mmr <= 649:
        return "Crystal"
    if mmr <= 699:
        return "Crystal ⬆"
    if mmr <= 749:
        return "Master"
    if mmr <= 799:
        return "Master ⬆"
    if mmr <= 849:
        return "Champion"
    if mmr <= 899:
        return "Grand Champion"
    if mmr <= 949:
        return "Star"
    if mmr <= 999:
        return "Divine Star"

    return "Legend"


def normalize_rank_name(rank_name: Any, mmr_new: int, was_placement: bool) -> str:
    if rank_name:
        rank = str(rank_name).strip()

        aliases = {
            "IronUp": "Iron ⬆",
            "BronzeUp": "Bronze ⬆",
            "SilverUp": "Silver ⬆",
            "GoldUp": "Gold ⬆",
            "PlatinumUp": "Platinum ⬆",
            "DiamondUp": "Diamond ⬆",
            "CrystalUp": "Crystal ⬆",
            "MasterUp": "Master ⬆",
            "GrandChampion": "Grand Champion",
            "DivineStar": "Divine Star",
        }

        rank = aliases.get(rank, rank)

        if rank in rank_colors:
            return rank

    if was_placement:
        return "Placement"

    return rank_from_mmr(mmr_new)


def get_unknown_flag_image(
    target_size=(42, 30),
    unknown_flag_path: str | Path = "assets/flags/unknown.jpg",
) -> Image.Image | None:
    """
    Carga la bandera visual para usuarios sin país.
    Se fuerza al mismo tamaño que las banderas normales.
    """
    path = Path(unknown_flag_path)
    if not path.exists():
        return None

    try:
        flag = Image.open(path).convert("RGBA")
        flag = ImageOps.fit(
            flag,
            target_size,
            method=Image.Resampling.LANCZOS,
        )
        return flag
    except Exception:
        return None


def get_flag_image(
    country_code: str,
    target_size=(42, 30),
    unknown_flag_path: str | Path = "assets/flags/unknown.jpg",
) -> Image.Image | None:
    """
    Intenta cargar assets/flags/{iso2}.png.
    Si country_code es unknown, carga assets/flags/unknown.jpg.
    Si no existe una bandera ISO2, intenta descargar desde flagcdn y cachearla.
    Si falla, devuelve None y se dibuja un badge ISO2.
    """
    if is_unknown_country_code(country_code):
        return get_unknown_flag_image(target_size, unknown_flag_path)

    cc = normalize_country_code(country_code)
    if not cc:
        return None

    flags_dir = Path("assets/flags")
    flags_dir.mkdir(parents=True, exist_ok=True)

    flag_path = flags_dir / f"{cc.lower()}.png"

    if not flag_path.exists():
        url = f"https://flagcdn.com/w80/{cc.lower()}.png"
        try:
            with urllib.request.urlopen(url, timeout=2.5) as response:
                data = response.read()
            flag_path.write_bytes(data)
        except Exception:
            return None

    try:
        flag = Image.open(flag_path).convert("RGBA")
        flag = ImageOps.fit(
            flag,
            target_size,
            method=Image.Resampling.LANCZOS,
        )
        return flag
    except Exception:
        return None


def make_iso_badge(country_code: str, font: ImageFont.FreeTypeFont) -> Image.Image:
    """
    Fallback si no se puede cargar la bandera PNG.
    Dibuja un badge pequeño con el ISO2.
    """
    cc = normalize_country_code(country_code) or "??"

    w, h = 48, 30
    badge = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(badge)

    d.rounded_rectangle(
        (0, 0, w - 1, h - 1),
        radius=6,
        fill=(20, 20, 28, 230),
        outline=(255, 255, 255, 180),
        width=2,
    )

    bbox = d.textbbox((0, 0), cc, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    d.text(
        ((w - tw) / 2, (h - th) / 2 - 2),
        cc,
        font=font,
        fill=(255, 255, 255, 255),
    )

    return badge


def _stats_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates: list[str | Path] = []

    for p in STATS_FONT_CANDIDATES:
        candidates.append(p)

    candidates.extend(
        [
            "arialbd.ttf",
            "arial.ttf",
            "DejaVuSans-Bold.ttf",
            "DejaVuSans.ttf",
        ]
    )

    for p in candidates:
        try:
            if isinstance(p, Path) and not p.exists():
                continue
            return ImageFont.truetype(str(p), size=size)
        except Exception:
            continue

    return ImageFont.load_default()


def _rank_progress(rank: str, current_mmr: int) -> tuple[str, float]:
    current_mmr = int(current_mmr or 0)

    if rank == "Legend":
        return f"{current_mmr}/1000+ MMR", 1.0

    low, high = RANK_PROGRESS_RANGES.get(rank, (0, 50))

    if high <= low:
        return f"{current_mmr}/{high} MMR", 1.0

    progress = (current_mmr - low) / float(high - low)
    progress = max(0.0, min(1.0, progress))

    return f"{current_mmr}/{high} MMR", progress


def _rank_theme(rank: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    rank = normalize_rank_name(rank, 0, False)
    start_hex, end_hex = rank_colors.get(rank, rank_colors.get("Gold", ("#ffcf87", "#e2921c")))
    return hex_to_rgb(start_hex), hex_to_rgb(end_hex)


def _rank_display_text(rank: str) -> str:
    rank = normalize_rank_name(rank, 0, False)
    return str(rank).replace(" ⬆", " Up")


def _paste_centered(base: Image.Image, overlay: Image.Image, center_xy: tuple[int, int]):
    x, y = center_xy
    ox = int(x - overlay.size[0] / 2)
    oy = int(y - overlay.size[1] / 2)
    base.paste(overlay, (ox, oy), overlay)


def _draw_gradient_text_centered(
    base: Image.Image,
    center_xy: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    start_rgb: tuple[int, int, int],
    end_rgb: tuple[int, int, int],
    stroke_fill=(10, 10, 20, 255),
    stroke_width: int = 2,
):
    text_img = make_gradient_text_image(
        str(text),
        font,
        start_rgb,
        end_rgb,
        padding=max(2, stroke_width + 2),
        direction="right",
    )

    if stroke_width > 0:
        stroke_layer = Image.new("RGBA", text_img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(stroke_layer)
        x0, y0, x1, y1 = font.getbbox(str(text))
        pad = max(2, stroke_width + 2)
        pos = (pad - x0, pad - y0)
        d.text(
            pos,
            str(text),
            font=font,
            fill=(255, 255, 255, 255),
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )
        # keep only stroke around alpha edges under text_img
        out = Image.new("RGBA", text_img.size, (0, 0, 0, 0))
        out.alpha_composite(stroke_layer)
        out.alpha_composite(text_img)
        text_img = out

    _paste_centered(base, text_img, center_xy)


def _make_horizontal_gradient(size: tuple[int, int], start_rgb: tuple[int, int, int], end_rgb: tuple[int, int, int]) -> Image.Image:
    grad = Image.new("RGB", size)
    draw_gradient(grad, start_rgb, end_rgb, direction="right")
    return grad.convert("RGBA")


def _make_ellipse_ring(size: int, thickness: int, start_rgb: tuple[int, int, int], end_rgb: tuple[int, int, int]) -> Image.Image:
    ring = _make_horizontal_gradient((size, size), start_rgb, end_rgb)
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.ellipse((0, 0, size - 1, size - 1), fill=255)
    inner = max(0, thickness)
    d.ellipse((inner, inner, size - 1 - inner, size - 1 - inner), fill=0)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(ring, (0, 0), mask)
    return out


def _paste_gradient_rounded_border(
    base: Image.Image,
    rect: tuple[int, int, int, int],
    start_rgb: tuple[int, int, int],
    end_rgb: tuple[int, int, int],
    *,
    thickness: int = 4,
    radius: int = 18,
    alpha: int = 255,
):
    x1, y1, x2, y2 = map(int, rect)
    width = max(1, x2 - x1)
    height = max(1, y2 - y1)
    thickness = max(1, int(thickness))
    radius = max(0, int(radius))

    grad = _make_horizontal_gradient((width, height), start_rgb, end_rgb)
    if alpha < 255:
        a = Image.new("L", (width, height), max(0, min(255, int(alpha))))
        grad.putalpha(a)

    mask = Image.new("L", (width, height), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, fill=255)

    if width > thickness * 2 and height > thickness * 2:
        inner_rect = (thickness, thickness, width - 1 - thickness, height - 1 - thickness)
        inner_radius = max(0, radius - thickness)
        d.rounded_rectangle(inner_rect, radius=inner_radius, fill=0)

    base.paste(grad, (x1, y1), mask)


def _draw_centered_segments(
    draw: ImageDraw.ImageDraw,
    center_xy: tuple[int, int],
    segments: list[tuple[str, tuple[int, int, int, int] | tuple[int, int, int]]],
    font: ImageFont.ImageFont,
    gap: int = 0,
    stroke_width: int = 1,
    stroke_fill=(0, 0, 0, 255),
):
    widths = []
    heights = []
    normalized = []
    for text, fill in segments:
        text = str(text)
        bbox = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
        widths.append(bbox[2] - bbox[0])
        heights.append(bbox[3] - bbox[1])
        if len(fill) == 3:
            fill = (*fill, 255)
        normalized.append((text, fill))

    total_width = sum(widths) + gap * max(0, len(widths) - 1)
    max_h = max(heights) if heights else 0
    x = int(center_xy[0] - total_width / 2)
    y = int(center_xy[1] - max_h / 2)

    for (text, fill), seg_w in zip(normalized, widths):
        draw.text(
            (x, y),
            text,
            font=font,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )
        x += seg_w + gap


def _draw_stat_group(
    draw: ImageDraw.ImageDraw,
    rect: tuple[int, int, int, int],
    outline_rgb: tuple[int, int, int],
    fill=(5, 8, 24, 170),
    radius: int = 12,
    width: int = 2,
):
    draw.rounded_rectangle(
        rect,
        radius=radius,
        fill=fill,
        outline=(*outline_rgb, 135),
        width=width,
    )



def _fit_font_size_for_text(text: str, max_width: int, start_size: int, min_size: int = 18):
    size = int(start_size)
    dummy = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    draw = ImageDraw.Draw(dummy)
    while size >= min_size:
        font = _stats_font(size)
        bbox = draw.textbbox((0, 0), str(text), font=font, stroke_width=2)
        width = bbox[2] - bbox[0]
        if width <= max_width:
            return font
        size -= 2
    return _stats_font(min_size)

def _build_alpha_mask_for_rank_logo(base_logo: Image.Image) -> Image.Image:
    """
    Crea la máscara del logo.

    Si el PNG tiene transparencia real, usa el canal alpha.
    Si el PNG viene blanco sobre fondo negro/opaco, usa luminancia para
    evitar pintar todo el cuadrado.
    """
    base_logo = base_logo.convert("RGBA")
    alpha = base_logo.getchannel("A")

    try:
        alpha_min, alpha_max = alpha.getextrema()
    except Exception:
        alpha_min, alpha_max = (255, 255)

    # Si hay transparencia real, usamos alpha directamente.
    if alpha_min < 250:
        return alpha

    # Fallback: logo blanco sobre fondo oscuro/opaco.
    gray = ImageOps.grayscale(base_logo)
    return gray.point(lambda v: 255 if v > 18 else 0)


def _colorize_rank_logo(base_logo: Image.Image, rank: str) -> Image.Image:
    """
    Pinta un logo blanco/base usando el mismo gradiente de rank_colors
    que ya se usa en la imagen de results.
    """
    rank = normalize_rank_name(rank, 0, False)
    start_hex, end_hex = rank_colors.get(rank, rank_colors.get("Gold", ("#ffcf87", "#e2921c")))
    start_rgb = hex_to_rgb(start_hex)
    end_rgb = hex_to_rgb(end_hex)

    base_logo = base_logo.convert("RGBA")
    w, h = base_logo.size

    gradient = Image.new("RGB", (w, h))
    draw_gradient(gradient, start_rgb, end_rgb, direction="right")

    mask = _build_alpha_mask_for_rank_logo(base_logo)

    colored = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    colored.paste(gradient.convert("RGBA"), (0, 0), mask)

    # Brillo suave para que se vea gamer/profesional sobre el panel oscuro.
    glow_mask = mask.filter(ImageFilter.GaussianBlur(max(2, int(min(w, h) * 0.025))))
    glow = Image.new("RGBA", (w, h), (*end_rgb, 95))
    glow.putalpha(glow_mask)

    return Image.alpha_composite(glow, colored)


def _load_rank_logo(rank: str) -> Image.Image | None:
    """
    Carga el logo del rango.

    Prioridad:
    1. Logo específico ya coloreado, si existe: gold.png, master.png, champion.png, etc.
    2. Logo base blanco que se colorea automáticamente:
       assets/stats/ranks/ranks_base.png
       o assets/stats/ranks/rank_base.png

    Con esto puedes usar un solo PNG blanco para todos los rangos.
    """
    rank = normalize_rank_name(rank, 0, False)

    specific_filename = RANK_LOGO_FILES.get(rank)
    specific_path = STATS_RANKS_DIR / specific_filename if specific_filename else None

    # Si existe un logo específico, se usa tal cual.
    if specific_path and specific_path.exists():
        try:
            return Image.open(specific_path).convert("RGBA")
        except Exception:
            pass

    # Tu archivo actual se llama ranks_base.png.
    base_candidates = [
        STATS_RANKS_DIR / "ranks_base.png",
        STATS_RANKS_DIR / "rank_base.png",
        STATS_RANKS_DIR / "base.png",
    ]

    for base_path in base_candidates:
        if not base_path.exists():
            continue

        try:
            base_logo = Image.open(base_path).convert("RGBA")
            return _colorize_rank_logo(base_logo, rank)
        except Exception:
            continue

    return None


# ============================================================
# RESULTS IMAGE GENERATOR
# ============================================================

def normalize_summary_row(row: tuple) -> dict:
    """
    Soporta 5 formatos:

    Nuevo recomendado con badge visual y Season Placement:
      (
        pos,
        name,
        total_points,
        stats_list,
        mmr_prev,
        mmr_delta,
        mmr_final,
        was_placement,
        final_rank_name,
        country_code,
        show_placement_badge,
        is_season_placement
      )

    Compatible con badge visual:
      (
        pos,
        name,
        total_points,
        stats_list,
        mmr_prev,
        mmr_delta,
        mmr_final,
        was_placement,
        final_rank_name,
        country_code,
        show_placement_badge
      )

    Compatible con formato actual:
      (
        pos,
        name,
        total_points,
        stats_list,
        mmr_prev,
        mmr_delta,
        mmr_final,
        was_placement,
        final_rank_name,
        country_code
      )

    Compatible con formato anterior:
      (
        pos,
        name,
        total_points,
        stats_list,
        mmr_prev,
        mmr_delta,
        mmr_final,
        was_placement,
        final_rank_name
      )

    Compatible con formato viejo:
      (
        pos,
        name,
        total_points,
        stats_list,
        mmr_prev,
        mmr_delta,
        mmr_final,
        was_placement
      )
    """
    country_code = None
    rank_name = None
    show_placement_badge = False
    is_season_placement = False

    if len(row) >= 12:
        (
            pos,
            name,
            points,
            results,
            mmr_old,
            mmr_delta,
            mmr_new,
            was_placement,
            rank_name,
            country_code,
            show_placement_badge,
            is_season_placement,
        ) = row[:12]

    elif len(row) >= 11:
        (
            pos,
            name,
            points,
            results,
            mmr_old,
            mmr_delta,
            mmr_new,
            was_placement,
            rank_name,
            country_code,
            show_placement_badge,
        ) = row[:11]

    elif len(row) == 10:
        (
            pos,
            name,
            points,
            results,
            mmr_old,
            mmr_delta,
            mmr_new,
            was_placement,
            rank_name,
            country_code,
        ) = row

    elif len(row) == 9:
        (
            pos,
            name,
            points,
            results,
            mmr_old,
            mmr_delta,
            mmr_new,
            was_placement,
            rank_name,
        ) = row

    elif len(row) == 8:
        (
            pos,
            name,
            points,
            results,
            mmr_old,
            mmr_delta,
            mmr_new,
            was_placement,
        ) = row

    else:
        raise ValueError(
            "summary row must have 8, 9, 10, 11 or 12 values: "
            "(pos, name, points, results, mmr_old, mmr_delta, mmr_new, "
            "was_placement, rank_name?, country_code?, show_placement_badge?, is_season_placement?)"
        )

    name = str(name or "").strip()

    # Si el nombre ya venía como "Player 🇵🇪", capturamos esa bandera
    # y la convertimos a ISO2 para dibujarla como PNG.
    clean_name, iso_from_emoji = flag_emoji_to_iso2(name)
    name = clean_name

    if not country_code and iso_from_emoji:
        country_code = iso_from_emoji

    country_code = normalize_results_country_code(country_code)

    mmr_old = int(mmr_old or 0)
    mmr_delta = int(mmr_delta or 0)
    mmr_new = int(mmr_new or 0)
    was_placement = bool(was_placement)

    final_rank = normalize_rank_name(rank_name, mmr_new, was_placement)

    return {
        "pos": int(pos),
        "name": name,
        "points": int(points or 0),
        "results": list(results or []),
        "mmr_old": mmr_old,
        "mmr_delta": mmr_delta,
        "mmr_new": mmr_new,
        "was_placement": was_placement,
        "rank_name": final_rank,
        "country_code": country_code,
        "show_placement_badge": bool(show_placement_badge),
        "is_season_placement": bool(is_season_placement),
    }


def generate_results_image(
    summary_rows: List[tuple],
    font_path: str,
    side_image: Image.Image,
    submitted_at_text: str | None = None,
    unknown_flag_path: str | Path = "assets/flags/unknown.jpg",
    unknown_flag_code: str = UNKNOWN_FLAG_CODE,
) -> io.BytesIO:
    """
    Dibuja el board de results sobre assets/background.png.

    Nuevo formato recomendado para summary_rows:

    [
        (
            pos,
            name,
            total_points,
            stats_list,
            mmr_prev,
            mmr_delta,
            mmr_final,
            was_placement,
            final_rank_name,
            country_code,
            show_placement_badge,
            is_season_placement
        ),
        ...
    ]

    Importante:
    - country_code puede venir como ISO2 ([PE], [US], [JP]) o como __UNKNOWN_FLAG__.
    - Si country_code es unknown, se dibuja assets/flags/unknown.jpg.
    - final_rank_name debe ser el rango DESPUÉS del cálculo de MMR.
    """

    rows = [normalize_summary_row(row) for row in summary_rows]
    rows = sorted(rows, key=lambda r: r["pos"])

    # ======================
    # FONTS
    # ======================
    title_font = ImageFont.truetype(font_path, 52)
    invite_font = ImageFont.truetype(font_path, 40)
    row_font = ImageFont.truetype(font_path, 40)
    small_font = ImageFont.truetype(font_path, 28)
    smallist_font = ImageFont.truetype(font_path, 23)
    meta_font = ImageFont.truetype(font_path, 24)

    TITLE_COLOR = (255, 255, 255)

    # ======================
    # CANVAS / BACKGROUND
    # ======================
    im = Image.open("assets/background.png").convert("RGBA")
    draw = ImageDraw.Draw(im)

    # ======================
    # SUBMIT DATETIME
    # BELOW "ROOM RESULT"
    # ======================
    if submitted_at_text:
        draw.text(
            (250, 180),
            f"Submitted: {submitted_at_text}",
            font=meta_font,
            fill=(255, 255, 255),
            stroke_width=2,
            stroke_fill=(35, 45, 110),
        )

    # ======================
    # AVERAGE MMR
    # ======================
    if rows:
        average_mmr = round(sum(row["mmr_old"] for row in rows) / len(rows))
    else:
        average_mmr = 0

    # ======================
    # LOUNGE INFO TOP RIGHT
    # ======================
    side_x = 1650
    side_y = 40

    side = ImageOps.fit(
        side_image.convert("RGBA"),
        (249, 210),
        method=Image.Resampling.LANCZOS,
    )
    im.paste(side, (side_x, side_y), side)

    text_anchor_x = side_x - 50

    draw.text(
        (text_anchor_x, side_y),
        "AVERAGE MMR",
        font=title_font,
        fill=TITLE_COLOR,
        anchor="ra",
    )

    draw.text(
        (text_anchor_x, side_y + 60),
        str(average_mmr),
        font=title_font,
        fill=TITLE_COLOR,
        anchor="ra",
    )

    invite_text = "discord.gg/6eZdqDHw5N"
    invite_color_left, invite_color_right = level_colors["Append"]

    invite_img = add_stroke(
        make_gradient_text_image(
            invite_text,
            invite_font,
            hex_to_rgb(invite_color_left),
            hex_to_rgb(invite_color_right),
            direction="right",
        ),
        2,
    )

    im.paste(
        invite_img,
        (text_anchor_x - invite_img.width, side_y + 120),
        invite_img,
    )

    # ======================
    # PLAYER ROW LAYOUT
    # ======================
    ROW_START_Ys = [314, 461, 603, 742, 899]

    X_NAME = 241
    X_RESULTS = 739
    X_POINTS = 1090
    X_MMR = 1360

    FLAG_GAP = 12
    FLAG_SIZE = (42, 30)

    NAME_MAX_PX = X_RESULTS - X_NAME - FLAG_SIZE[0] - FLAG_GAP - 35

    # ======================
    # DRAW PLAYER ROWS
    # ======================
    for i, row in enumerate(rows[: len(ROW_START_Ys)]):
        y = ROW_START_Ys[i]

        name = row["name"]
        points = row["points"]
        results = row["results"]
        mmr_old = row["mmr_old"]
        mmr_delta = row["mmr_delta"]
        mmr_new = row["mmr_new"]
        rank_name = row["rank_name"]
        country_code = row["country_code"]

        color_pair = rank_colors.get(rank_name, rank_colors["Placement"])
        name_gradient_start, name_gradient_end = color_pair

        safe_name = strip_unsupported_chars(name, font_path)
        if not safe_name:
            safe_name = "Unknown"

        # Nombre con color del rango FINAL
        name_img = add_stroke(
            make_gradient_text_image(
                safe_name,
                row_font,
                hex_to_rgb(name_gradient_start),
                hex_to_rgb(name_gradient_end),
            ),
            3,
        )

        t_y = y

        # Si el nombre es muy largo, usar fuente menor.
        if name_img.width > NAME_MAX_PX:
            name_img = add_stroke(
                make_gradient_text_image(
                    safe_name,
                    small_font,
                    hex_to_rgb(name_gradient_start),
                    hex_to_rgb(name_gradient_end),
                ),
                3,
            )
            t_y += 5

        # Si aun así se pasa, recortar visualmente con puntos.
        while name_img.width > NAME_MAX_PX and len(safe_name) > 3:
            safe_name = safe_name[:-2].rstrip() + "…"
            name_img = add_stroke(
                make_gradient_text_image(
                    safe_name,
                    small_font,
                    hex_to_rgb(name_gradient_start),
                    hex_to_rgb(name_gradient_end),
                ),
                3,
            )
            t_y = y + 5

        show_placement_badge = bool(row.get("show_placement_badge", False))
        is_season_placement = bool(row.get("is_season_placement", False))

        # Preparar bandera antes de dibujar para poder calcular el borde.
        flag_img = None
        flag_x = None
        flag_y = None

        if country_code:
            flag_img = get_flag_image(
                country_code,
                target_size=FLAG_SIZE,
                unknown_flag_path=unknown_flag_path,
            )

            if flag_img is None and not is_unknown_country_code(country_code):
                flag_img = make_iso_badge(country_code, smallist_font)

            if flag_img is not None:
                flag_x = X_NAME + name_img.width + FLAG_GAP
                flag_y = t_y + max(0, (name_img.height - flag_img.height) // 2)

        # Badge [PLACEMENT] / [SEASON PLACEMENT] debajo del usuario.
        # IMPORTANTE: el rectángulo NO tiene fill para evitar el fondo negro.
        placement_badge_text = "[SEASON PLACEMENT]" if is_season_placement else "[PLACEMENT]"
        placement_badge_font = smallist_font
        if is_season_placement:
            placement_badge_color = (*hex_to_rgb(rank_colors["Season Placement"][0]), 255)
        else:
            placement_badge_color = (255, 112, 0, 255)  # naranja Placement

        badge_bbox = draw.textbbox((0, 0), placement_badge_text, font=placement_badge_font)
        badge_w = badge_bbox[2] - badge_bbox[0]
        badge_h = badge_bbox[3] - badge_bbox[1]

        content_right = X_NAME + name_img.width
        if flag_img is not None and flag_x is not None:
            content_right = max(content_right, flag_x + flag_img.width)

        content_w = max(1, content_right - X_NAME)
        badge_y = t_y + name_img.height + 6

        visual_w = max(content_w, badge_w)
        visual_left = X_NAME
        visual_right = visual_left + visual_w

        badge_x = visual_left
        if badge_w < visual_w:
            badge_x = visual_left + (visual_w - badge_w) // 2

        if show_placement_badge:
            pad_x = 14
            pad_y = 9

            rect_left = visual_left - pad_x
            rect_top = t_y - pad_y
            rect_right = visual_right + pad_x
            rect_bottom = badge_y + badge_h + pad_y + 2

            # Sin fill: interior 100% transparente.
            # Solo se dibuja el borde blanco alrededor de nombre + bandera + [PLACEMENT].
            draw.rounded_rectangle(
                (rect_left, rect_top, rect_right, rect_bottom),
                radius=14,
                fill=None,
                outline=(255, 255, 255, 235),
                width=3,
            )

        # Nombre
        im.paste(name_img, (X_NAME, t_y), name_img)

        # Bandera a la derecha del nombre
        if flag_img is not None and flag_x is not None and flag_y is not None:
            im.paste(flag_img, (flag_x, flag_y), flag_img)

        # Texto [PLACEMENT] debajo del usuario
        if show_placement_badge:
            draw.text(
                (badge_x, badge_y),
                placement_badge_text,
                font=placement_badge_font,
                fill=placement_badge_color,
                stroke_width=2,
                stroke_fill=(25, 10, 0, 255),
            )

        # Results tuple
        draw.text(
            (X_RESULTS, y + 10),
            "(" + ",".join(map(str, results)) + ")",
            font=smallist_font,
            fill=(255, 255, 255),
        )

        # Points
        draw.text(
            (X_POINTS, y),
            str(points),
            font=row_font,
            fill=(255, 255, 255),
        )

        # MMR delta
        if mmr_delta > 0:
            delta_color = (0, 150, 0)
            delta_str = f"+{mmr_delta}"
        elif mmr_delta < 0:
            delta_color = (220, 20, 60)
            delta_str = str(mmr_delta)
        else:
            delta_color = (220, 180, 30)
            delta_str = "+0"

        draw.text(
            (X_MMR, y),
            f"{mmr_old} ",
            font=row_font,
            fill=(255, 255, 255),
        )

        dx = draw.textlength(f"{mmr_old} ", font=row_font)

        draw.text(
            (X_MMR + dx, y),
            delta_str,
            font=row_font,
            fill=delta_color,
        )

        dx2 = draw.textlength(f"{mmr_old} {delta_str}", font=row_font)

        draw.text(
            (X_MMR + dx2, y),
            f" = {mmr_new}",
            font=row_font,
            fill=(255, 255, 255),
        )

    # ======================
    # OUTPUT
    # ======================
    buf = io.BytesIO()
    im.save(buf, "PNG")
    buf.seek(0)
    return buf



# ============================================================
# COUNTRY DISPLAY HELPERS FOR STATS CARD
# ============================================================

ISO2_COUNTRY_NAMES = {
    "AF": "Afghanistan", "AL": "Albania", "DZ": "Algeria", "AS": "American Samoa", "AD": "Andorra",
    "AO": "Angola", "AI": "Anguilla", "AQ": "Antarctica", "AG": "Antigua and Barbuda", "AR": "Argentina",
    "AM": "Armenia", "AW": "Aruba", "AU": "Australia", "AT": "Austria", "AZ": "Azerbaijan",
    "BS": "Bahamas", "BH": "Bahrain", "BD": "Bangladesh", "BB": "Barbados", "BY": "Belarus",
    "BE": "Belgium", "BZ": "Belize", "BJ": "Benin", "BM": "Bermuda", "BT": "Bhutan",
    "BO": "Bolivia", "BA": "Bosnia and Herzegovina", "BW": "Botswana", "BR": "Brazil", "IO": "British Indian Ocean Territory",
    "BN": "Brunei", "BG": "Bulgaria", "BF": "Burkina Faso", "BI": "Burundi", "KH": "Cambodia",
    "CM": "Cameroon", "CA": "Canada", "CV": "Cape Verde", "KY": "Cayman Islands", "CF": "Central African Republic",
    "TD": "Chad", "CL": "Chile", "CN": "China", "CX": "Christmas Island", "CC": "Cocos Islands",
    "CO": "Colombia", "KM": "Comoros", "CG": "Congo", "CD": "DR Congo", "CK": "Cook Islands",
    "CR": "Costa Rica", "CI": "Côte d’Ivoire", "HR": "Croatia", "CU": "Cuba", "CW": "Curaçao",
    "CY": "Cyprus", "CZ": "Czechia", "DK": "Denmark", "DJ": "Djibouti", "DM": "Dominica",
    "DO": "Dominican Republic", "EC": "Ecuador", "EG": "Egypt", "SV": "El Salvador", "GQ": "Equatorial Guinea",
    "ER": "Eritrea", "EE": "Estonia", "SZ": "Eswatini", "ET": "Ethiopia", "FK": "Falkland Islands",
    "FO": "Faroe Islands", "FJ": "Fiji", "FI": "Finland", "FR": "France", "GF": "French Guiana",
    "PF": "French Polynesia", "GA": "Gabon", "GM": "Gambia", "GE": "Georgia", "DE": "Germany",
    "GH": "Ghana", "GI": "Gibraltar", "GR": "Greece", "GL": "Greenland", "GD": "Grenada",
    "GP": "Guadeloupe", "GU": "Guam", "GT": "Guatemala", "GG": "Guernsey", "GN": "Guinea",
    "GW": "Guinea-Bissau", "GY": "Guyana", "HT": "Haiti", "HN": "Honduras", "HK": "Hong Kong",
    "HU": "Hungary", "IS": "Iceland", "IN": "India", "ID": "Indonesia", "IR": "Iran",
    "IQ": "Iraq", "IE": "Ireland", "IM": "Isle of Man", "IL": "Israel", "IT": "Italy",
    "JM": "Jamaica", "JP": "Japan", "JE": "Jersey", "JO": "Jordan", "KZ": "Kazakhstan",
    "KE": "Kenya", "KI": "Kiribati", "KP": "North Korea", "KR": "South Korea", "KW": "Kuwait",
    "KG": "Kyrgyzstan", "LA": "Laos", "LV": "Latvia", "LB": "Lebanon", "LS": "Lesotho",
    "LR": "Liberia", "LY": "Libya", "LI": "Liechtenstein", "LT": "Lithuania", "LU": "Luxembourg",
    "MO": "Macao", "MG": "Madagascar", "MW": "Malawi", "MY": "Malaysia", "MV": "Maldives",
    "ML": "Mali", "MT": "Malta", "MH": "Marshall Islands", "MQ": "Martinique", "MR": "Mauritania",
    "MU": "Mauritius", "YT": "Mayotte", "MX": "Mexico", "FM": "Micronesia", "MD": "Moldova",
    "MC": "Monaco", "MN": "Mongolia", "ME": "Montenegro", "MS": "Montserrat", "MA": "Morocco",
    "MZ": "Mozambique", "MM": "Myanmar", "NA": "Namibia", "NR": "Nauru", "NP": "Nepal",
    "NL": "Netherlands", "NC": "New Caledonia", "NZ": "New Zealand", "NI": "Nicaragua", "NE": "Niger",
    "NG": "Nigeria", "NU": "Niue", "NF": "Norfolk Island", "MK": "North Macedonia", "MP": "Northern Mariana Islands",
    "NO": "Norway", "OM": "Oman", "PK": "Pakistan", "PW": "Palau", "PS": "Palestine",
    "PA": "Panama", "PG": "Papua New Guinea", "PY": "Paraguay", "PE": "Peru", "PH": "Philippines",
    "PN": "Pitcairn Islands", "PL": "Poland", "PT": "Portugal", "PR": "Puerto Rico", "QA": "Qatar",
    "RE": "Réunion", "RO": "Romania", "RU": "Russia", "RW": "Rwanda", "BL": "Saint Barthélemy",
    "SH": "Saint Helena", "KN": "Saint Kitts and Nevis", "LC": "Saint Lucia", "MF": "Saint Martin", "PM": "Saint Pierre and Miquelon",
    "VC": "Saint Vincent and the Grenadines", "WS": "Samoa", "SM": "San Marino", "ST": "São Tomé and Príncipe", "SA": "Saudi Arabia",
    "SN": "Senegal", "RS": "Serbia", "SC": "Seychelles", "SL": "Sierra Leone", "SG": "Singapore",
    "SX": "Sint Maarten", "SK": "Slovakia", "SI": "Slovenia", "SB": "Solomon Islands", "SO": "Somalia",
    "ZA": "South Africa", "SS": "South Sudan", "ES": "Spain", "LK": "Sri Lanka", "SD": "Sudan",
    "SR": "Suriname", "SE": "Sweden", "CH": "Switzerland", "SY": "Syria", "TW": "Taiwan",
    "TJ": "Tajikistan", "TZ": "Tanzania", "TH": "Thailand", "TL": "Timor-Leste", "TG": "Togo",
    "TK": "Tokelau", "TO": "Tonga", "TT": "Trinidad and Tobago", "TN": "Tunisia", "TR": "Turkey",
    "TM": "Turkmenistan", "TC": "Turks and Caicos Islands", "TV": "Tuvalu", "UG": "Uganda", "UA": "Ukraine",
    "AE": "United Arab Emirates", "GB": "United Kingdom", "US": "United States", "UY": "Uruguay", "UZ": "Uzbekistan",
    "VU": "Vanuatu", "VA": "Vatican City", "VE": "Venezuela", "VN": "Vietnam", "VG": "British Virgin Islands",
    "VI": "U.S. Virgin Islands", "WF": "Wallis and Futuna", "EH": "Western Sahara", "YE": "Yemen", "ZM": "Zambia", "ZW": "Zimbabwe",
}


def country_name_from_code(country_code: Any, country_name: str | None = None) -> str:
    if country_name and str(country_name).strip():
        return str(country_name).strip()

    if is_unknown_country_code(country_code):
        return "Unknown"

    cc = normalize_country_code(country_code)
    if not cc:
        return "Unknown"

    return ISO2_COUNTRY_NAMES.get(cc, cc)


def _draw_sci_fi_card(
    draw: ImageDraw.ImageDraw,
    rect: tuple[int, int, int, int],
    outline_rgb: tuple[int, int, int],
    *,
    fill=(3, 9, 27, 196),
    radius: int = 16,
    width: int = 2,
    cut: int = 18,
):
    x1, y1, x2, y2 = rect
    draw.rounded_rectangle(rect, radius=radius, fill=fill, outline=(*outline_rgb, 120), width=width)
    # small angular corner strokes; this gives a cleaner gamer panel without needing extra PNG assets
    c = max(6, int(cut))
    hi = (*outline_rgb, 210)
    lo = (*outline_rgb, 80)
    draw.line((x1 + c, y1, x1 + c * 2, y1), fill=hi, width=width)
    draw.line((x1, y1 + c, x1, y1 + c * 2), fill=lo, width=width)
    draw.line((x2 - c * 2, y1, x2 - c, y1), fill=hi, width=width)
    draw.line((x2, y1 + c, x2, y1 + c * 2), fill=lo, width=width)
    draw.line((x1 + c, y2, x1 + c * 2, y2), fill=hi, width=width)
    draw.line((x1, y2 - c * 2, x1, y2 - c), fill=lo, width=width)
    draw.line((x2 - c * 2, y2, x2 - c, y2), fill=hi, width=width)
    draw.line((x2, y2 - c * 2, x2, y2 - c), fill=lo, width=width)


def _draw_icon_glow(draw: ImageDraw.ImageDraw, xy: tuple[int, int], size: int, color=(63, 178, 255, 230)):
    x, y = xy
    r = max(6, size // 2)
    draw.ellipse((x - r, y - r, x + r, y + r), fill=(color[0], color[1], color[2], 18))


def _draw_position_icon(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int, rgb=(72, 188, 255)):
    _draw_icon_glow(draw, (cx, cy), size, (*rgb, 230))
    s = size / 54
    col = (*rgb, 235)
    stroke = max(2, int(2 * s))
    # podium
    boxes = [
        (cx - int(21*s), cy + int(8*s), cx - int(7*s), cy + int(24*s)),
        (cx - int(5*s), cy - int(4*s), cx + int(9*s), cy + int(24*s)),
        (cx + int(11*s), cy + int(13*s), cx + int(25*s), cy + int(24*s)),
    ]
    for b in boxes:
        draw.rounded_rectangle(b, radius=max(1, int(3*s)), outline=col, width=stroke, fill=(5, 20, 46, 135))
    _center_text(draw, (cx + int(2*s), cy + int(5*s)), "1", _stats_font(max(8, int(13*s))), fill=col, stroke_width=1)
    _center_text(draw, (cx - int(14*s), cy + int(17*s)), "2", _stats_font(max(7, int(10*s))), fill=col, stroke_width=1)
    _center_text(draw, (cx + int(18*s), cy + int(18*s)), "3", _stats_font(max(7, int(10*s))), fill=col, stroke_width=1)
    draw.ellipse((cx - int(7*s), cy - int(25*s), cx + int(7*s), cy - int(11*s)), outline=col, width=stroke)
    draw.line((cx, cy - int(11*s), cx, cy - int(4*s)), fill=col, width=stroke)


def _draw_swords_icon(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int, rgb=(72, 188, 255)):
    _draw_icon_glow(draw, (cx, cy), size, (*rgb, 230))
    s = size / 54
    col = (*rgb, 235)
    stroke = max(2, int(3*s))
    # crossed blades
    draw.line((cx - int(20*s), cy - int(20*s), cx + int(21*s), cy + int(21*s)), fill=col, width=stroke)
    draw.line((cx + int(20*s), cy - int(20*s), cx - int(21*s), cy + int(21*s)), fill=col, width=stroke)
    draw.line((cx - int(17*s), cy + int(18*s), cx - int(27*s), cy + int(28*s)), fill=col, width=stroke)
    draw.line((cx + int(17*s), cy + int(18*s), cx + int(27*s), cy + int(28*s)), fill=col, width=stroke)
    draw.line((cx - int(21*s), cy + int(10*s), cx - int(9*s), cy + int(22*s)), fill=col, width=max(2, int(2*s)))
    draw.line((cx + int(21*s), cy + int(10*s), cx + int(9*s), cy + int(22*s)), fill=col, width=max(2, int(2*s)))


def _draw_target_icon(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int, rgb=(72, 188, 255)):
    _draw_icon_glow(draw, (cx, cy), size, (*rgb, 230))
    s = size / 54
    col = (*rgb, 235)
    stroke = max(2, int(3*s))
    for r in (23, 14, 5):
        draw.ellipse((cx - int(r*s), cy - int(r*s), cx + int(r*s), cy + int(r*s)), outline=col, width=max(1, int(2*s)))
    draw.line((cx - int(30*s), cy, cx - int(12*s), cy), fill=col, width=stroke)
    draw.line((cx + int(12*s), cy, cx + int(30*s), cy), fill=col, width=stroke)
    draw.line((cx, cy - int(30*s), cx, cy - int(12*s)), fill=col, width=stroke)
    draw.line((cx, cy + int(12*s), cx, cy + int(30*s)), fill=col, width=stroke)


def _draw_chart_icon(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int, rgb=(72, 188, 255)):
    _draw_icon_glow(draw, (cx, cy), size, (*rgb, 230))
    s = size / 54
    col = (*rgb, 235)
    stroke = max(2, int(3*s))
    x0, y0 = cx - int(24*s), cy + int(22*s)
    draw.line((x0, cy - int(24*s), x0, y0), fill=col, width=max(1, int(2*s)))
    draw.line((x0, y0, cx + int(26*s), y0), fill=col, width=max(1, int(2*s)))
    pts = [
        (cx - int(20*s), cy + int(10*s)),
        (cx - int(8*s), cy - int(6*s)),
        (cx + int(3*s), cy + int(2*s)),
        (cx + int(13*s), cy - int(17*s)),
        (cx + int(25*s), cy - int(23*s)),
    ]
    draw.line(pts, fill=col, width=stroke, joint="curve")
    for px, py in pts:
        draw.ellipse((px - int(3*s), py - int(3*s), px + int(3*s), py + int(3*s)), fill=col)


def _draw_crown_icon(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int, rgb=(72, 188, 255)):
    _draw_icon_glow(draw, (cx, cy), size, (*rgb, 230))
    s = size / 54
    col = (*rgb, 235)
    stroke = max(2, int(3*s))
    pts = [
        (cx - int(26*s), cy + int(12*s)),
        (cx - int(20*s), cy - int(18*s)),
        (cx - int(6*s), cy + int(2*s)),
        (cx, cy - int(25*s)),
        (cx + int(6*s), cy + int(2*s)),
        (cx + int(20*s), cy - int(18*s)),
        (cx + int(26*s), cy + int(12*s)),
    ]
    draw.line(pts, fill=col, width=stroke, joint="curve")
    draw.rounded_rectangle((cx - int(24*s), cy + int(13*s), cx + int(24*s), cy + int(24*s)), radius=max(1, int(4*s)), outline=col, width=stroke, fill=(5, 20, 46, 110))


def _draw_shield_icon(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int, rgb=(72, 188, 255), star: bool = False):
    _draw_icon_glow(draw, (cx, cy), size, (*rgb, 230))
    s = size / 54
    col = (*rgb, 235)
    stroke = max(2, int(3*s))
    poly = [
        (cx, cy - int(28*s)),
        (cx + int(24*s), cy - int(16*s)),
        (cx + int(18*s), cy + int(18*s)),
        (cx, cy + int(30*s)),
        (cx - int(18*s), cy + int(18*s)),
        (cx - int(24*s), cy - int(16*s)),
    ]
    draw.polygon(poly, outline=col, fill=(5, 20, 46, 110))
    draw.line(poly + [poly[0]], fill=col, width=stroke)
    if star:
        # 5 point stylized star
        r1, r2 = int(12*s), int(5*s)
        pts = []
        import math
        for i in range(10):
            ang = -math.pi / 2 + i * math.pi / 5
            r = r1 if i % 2 == 0 else r2
            pts.append((cx + int(math.cos(ang) * r), cy + int(math.sin(ang) * r)))
        draw.polygon(pts, outline=col, fill=(20, 50, 90, 160))


def _draw_location_icon(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int, rgb=(72, 188, 255)):
    s = size / 48
    col = (*rgb, 210)
    r = max(4, int(9*s))
    draw.ellipse((cx-r, cy-int(14*s)-r, cx+r, cy-int(14*s)+r), outline=col, width=max(2, int(2*s)), fill=(6, 17, 45, 160))
    draw.polygon([(cx, cy + int(14*s)), (cx - int(10*s), cy - int(2*s)), (cx + int(10*s), cy - int(2*s))], fill=(6, 17, 45, 160), outline=col)
    draw.ellipse((cx-int(3*s), cy-int(17*s), cx+int(3*s), cy-int(11*s)), fill=col)


def _draw_country_badge(
    base: Image.Image,
    rect: tuple[int, int, int, int],
    *,
    country_code: str | None,
    country_name: str | None,
    font: ImageFont.ImageFont,
    small_font: ImageFont.ImageFont,
    outline_rgb: tuple[int, int, int],
):
    draw = ImageDraw.Draw(base)
    x1, y1, x2, y2 = rect
    h = y2 - y1
    _draw_sci_fi_card(draw, rect, outline_rgb, fill=(3, 8, 25, 160), radius=max(10, h // 4), width=max(1, h // 20), cut=max(10, h // 3))
    _draw_location_icon(draw, x1 + int(h * 0.48), y1 + h // 2 + int(h * 0.06), max(24, int(h * 0.70)), outline_rgb)

    flag_w = int(h * 1.15)
    flag_h = int(h * 0.62)
    flag_x = x1 + int(h * 0.95)
    flag_y = y1 + (h - flag_h) // 2
    flag = get_flag_image(country_code or "", target_size=(flag_w, flag_h))
    if flag is None:
        flag = make_iso_badge(country_code or "??", small_font).resize((flag_w, flag_h), Image.Resampling.LANCZOS)
    base.paste(flag, (flag_x, flag_y), flag)

    name = country_name_from_code(country_code, country_name)
    name = _fit_text(draw, name, font, max(40, x2 - (flag_x + flag_w + int(h * 0.36)) - int(h * 0.35)))
    draw.text((flag_x + flag_w + int(h * 0.35), y1 + int(h * 0.19)), name, font=font, fill=(255, 255, 255, 250), stroke_width=1, stroke_fill=(0, 0, 0, 255))

# ============================================================
# STATS IMAGE GENERATOR
# ============================================================

def generate_stats_image(
    *,
    player_name: str,
    avatar_bytes: bytes | None,
    rank: str,
    position: int,
    total_players: int,
    matches_played: int,
    last10_wins: int,
    last10_losses: int,
    peak_mmr: int,
    current_mmr: int,
    season_name: str,
    last10_mmr_delta: int = 0,
    country_code: str | None = None,
    country_name: str | None = None,
    is_season_placement: bool = False,
) -> io.BytesIO:
    """Genera la imagen de /stats y /stats_user con UI sci-fi/gamer."""

    if not STATS_BACKGROUND_PATH.exists():
        raise FileNotFoundError(f"Missing stats background: {STATS_BACKGROUND_PATH}")

    rank = "Season Placement" if is_season_placement else normalize_rank_name(rank, current_mmr, False)
    rank_display = "SEASON PLACEMENT" if is_season_placement else _rank_display_text(rank).upper()
    start_rgb, end_rgb = _rank_theme(rank)
    # Mantiene el feeling gamer/sci-fi, pero los elementos pedidos por el usuario
    # usan el color/degradado real del rango del jugador.
    blue_rgb = (70, 185, 255)
    deep_blue = (12, 43, 104)
    accent_rgb = tuple(int((start_rgb[i] + end_rgb[i]) / 2) for i in range(3))
    gold_rgb = end_rgb if rank.lower().startswith("gold") else (245, 175, 61)
    rank_stroke = (*accent_rgb, 255)
    white = (255, 255, 255, 255)
    green = (58, 231, 125, 255)
    red = (255, 92, 92, 255)

    img = Image.open(STATS_BACKGROUND_PATH).convert("RGBA")
    draw = ImageDraw.Draw(img)
    w, h = img.size

    # Diseño basado en la imagen generada: 1672 x 941.
    def sx(x: float) -> int:
        return int(x * w / 1672)

    def sy(y: float) -> int:
        return int(y * h / 941)

    title_font = _stats_font(sy(58))
    username_font = _fit_font_size_for_text(player_name or "Unknown", sx(610), sy(72), min_size=sy(38))
    country_font = _stats_font(sy(34))
    country_small_font = _stats_font(sy(18))
    label_font = _stats_font(sy(24))
    top_value_font = _stats_font(sy(38))
    top_small_value_font = _stats_font(sy(34))
    bottom_label_font = _stats_font(sy(25))
    value_font = _stats_font(sy(46))
    mmr_font = _stats_font(sy(56))
    rank_label_font = _stats_font(sy(25))
    rank_font = _fit_font_size_for_text(rank_display, sx(205), sy(58), min_size=sy(30))

    # subtle dark glass overlay so text always reads well on the generated background
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle((0, 0, w, h), fill=(0, 0, 8, 30))
    # thin external sci-fi rails; background already has the outer crystals
    outer = (sx(55), sy(63), sx(1615), sy(908))
    od.rounded_rectangle(outer, radius=sy(32), outline=(*blue_rgb, 70), width=max(1, sy(2)))
    # top gold rails around title
    od.line((sx(425), sy(85), sx(642), sy(85)), fill=(*gold_rgb, 180), width=max(2, sy(3)))
    od.line((sx(1030), sy(85), sx(1247), sy(85)), fill=(*gold_rgb, 180), width=max(2, sy(3)))
    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    # Header
    _center_text(
        draw,
        (sx(836), sy(78)),
        "PJSK LOUNGE STATS",
        title_font,
        fill=(245, 247, 255, 255),
        stroke_width=max(2, sy(3)),
        stroke_fill=(30, 48, 88, 255),
    )

    # Avatar ring
    avatar_size = sy(250)
    avatar_x = sx(225)
    avatar_y = sy(126)
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse(
        (avatar_x - sy(24), avatar_y - sy(24), avatar_x + avatar_size + sy(24), avatar_y + avatar_size + sy(24)),
        fill=(*accent_rgb, 42),
    )
    gd.ellipse(
        (avatar_x - sy(14), avatar_y - sy(14), avatar_x + avatar_size + sy(14), avatar_y + sy(14) + avatar_size),
        outline=(*accent_rgb, 155),
        width=max(2, sy(4)),
    )
    img = Image.alpha_composite(img, glow)

    ring_outer = _make_ellipse_ring(avatar_size + sy(24), max(7, sy(10)), start_rgb, end_rgb)
    img.paste(ring_outer, (avatar_x - sy(12), avatar_y - sy(12)), ring_outer)
    ring_inner = _make_ellipse_ring(avatar_size + sy(4), max(3, sy(4)), end_rgb, start_rgb)
    img.paste(ring_inner, (avatar_x - sy(2), avatar_y - sy(2)), ring_inner)

    avatar_drawn = False
    if avatar_bytes:
        try:
            avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
            avatar = ImageOps.fit(avatar, (avatar_size, avatar_size), method=Image.Resampling.LANCZOS)
            mask = Image.new("L", (avatar_size, avatar_size), 0)
            md = ImageDraw.Draw(mask)
            md.ellipse((0, 0, avatar_size, avatar_size), fill=255)
            img.paste(avatar, (avatar_x, avatar_y), mask)
            avatar_drawn = True
        except Exception:
            avatar_drawn = False

    draw = ImageDraw.Draw(img)
    if not avatar_drawn:
        draw.ellipse((avatar_x + sy(82), avatar_y + sy(58), avatar_x + sy(168), avatar_y + sy(144)), fill=(74, 93, 146, 255))
        draw.pieslice((avatar_x + sy(60), avatar_y + sy(142), avatar_x + sy(190), avatar_y + sy(275)), start=180, end=360, fill=(74, 93, 146, 255))

    # Username and country badge
    safe_name = str(player_name or "Unknown").strip() or "Unknown"
    username_img = make_gradient_text_image(
        safe_name,
        username_font,
        start_rgb,
        end_rgb,
        padding=max(4, sy(4)),
        direction="right",
    )
    username_img = add_stroke(username_img, max(2, sy(3)), (8, 14, 28, 255))
    # soft glow in the player's rank color behind the username
    username_glow = Image.new("RGBA", username_img.size, (0, 0, 0, 0))
    ugd = ImageDraw.Draw(username_glow)
    ugd.rounded_rectangle((0, sy(8), username_img.size[0], username_img.size[1] - sy(8)), radius=max(6, sy(8)), fill=(*accent_rgb, 22))
    username_glow = username_glow.filter(ImageFilter.GaussianBlur(radius=max(3, sy(4))))
    img.paste(username_glow, (sx(570), sy(198)), username_glow)
    img.paste(username_img, (sx(570), sy(198)), username_img)
    draw = ImageDraw.Draw(img)
    draw.line((sx(565), sy(292), sx(1138), sy(292)), fill=(*end_rgb, 210), width=max(2, sy(3)))
    _draw_country_badge(
        img,
        (sx(545), sy(318), sx(935), sy(382)),
        country_code=country_code,
        country_name=country_name,
        font=country_font,
        small_font=country_small_font,
        outline_rgb=blue_rgb,
    )
    draw = ImageDraw.Draw(img)

    # Rank box on the right
    rank_box = (sx(1210), sy(128), sx(1460), sy(398))
    _draw_sci_fi_card(draw, rank_box, accent_rgb, fill=(5, 7, 19, 218), radius=sy(22), width=max(2, sy(3)), cut=sy(18))
    _paste_gradient_rounded_border(img, rank_box, start_rgb, end_rgb, thickness=max(2, sy(3)), radius=sy(22), alpha=250)
    inner_rank = (rank_box[0] + sx(14), rank_box[1] + sy(15), rank_box[2] - sx(14), rank_box[3] - sy(15))
    _paste_gradient_rounded_border(img, inner_rank, end_rgb, start_rgb, thickness=max(1, sy(2)), radius=sy(18), alpha=165)
    _center_text(draw, (sx(1335), sy(170)), "RANK", rank_label_font, fill=white, stroke_width=1, stroke_fill=(0, 0, 0, 255))

    rank_logo = _load_rank_logo(rank)
    if rank_logo:
        rank_logo = ImageOps.contain(rank_logo, (sx(162), sy(124)), method=Image.Resampling.LANCZOS)
        _paste_centered(img, rank_logo, (sx(1335), sy(258)))
    else:
        _draw_shield_icon(draw, sx(1335), sy(258), sy(108), gold_rgb, star=True)

    _draw_gradient_text_centered(
        img,
        (sx(1335), sy(355)),
        rank_display,
        rank_font,
        start_rgb,
        end_rgb,
        stroke_fill=(30, 25, 10, 255),
        stroke_width=max(2, sy(3)),
    )
    draw = ImageDraw.Draw(img)

    # Top stat cards with icons
    cards = [
        (sx(155), sy(463), sx(468), sy(585)),
        (sx(500), sy(463), sx(813), sy(585)),
        (sx(845), sy(463), sx(1158), sy(585)),
        (sx(1190), sy(463), sx(1503), sy(585)),
    ]
    for rect in cards:
        _draw_sci_fi_card(draw, rect, blue_rgb, fill=(4, 10, 30, 188), radius=sy(16), width=max(1, sy(2)), cut=sy(16))

    icon_y = sy(525)
    _draw_position_icon(draw, sx(222), icon_y, sy(56), blue_rgb)
    _draw_swords_icon(draw, sx(568), icon_y, sy(56), blue_rgb)
    _draw_target_icon(draw, sx(912), icon_y, sy(56), blue_rgb)
    _draw_chart_icon(draw, sx(1258), icon_y, sy(56), blue_rgb)

    _center_text(draw, (sx(378), sy(505)), "POSITION", label_font, fill=(220, 232, 255, 245))
    _center_text(draw, (sx(378), sy(552)), f"{int(position)} / {int(total_players)}", top_value_font, fill=white, stroke_width=1, stroke_fill=(0, 0, 0, 255))

    _center_text(draw, (sx(708), sy(505)), "MATCHES PLAYED", label_font, fill=(220, 232, 255, 245))
    _center_text(draw, (sx(708), sy(552)), str(int(matches_played)), top_value_font, fill=white, stroke_width=1, stroke_fill=(0, 0, 0, 255))

    _center_text(draw, (sx(1053), sy(505)), "LAST 10", label_font, fill=(220, 232, 255, 245))
    _draw_centered_segments(
        draw,
        (sx(1053), sy(552)),
        [(f"{int(last10_wins)}W", green), (" - ", white), (f"{int(last10_losses)}L", red)],
        top_value_font,
        gap=0,
        stroke_width=max(1, sy(2)),
        stroke_fill=(0, 0, 0, 255),
    )

    _center_text(draw, (sx(1400), sy(505)), "LAST 10 ± MMR", label_font, fill=(220, 232, 255, 245))
    delta_value = int(last10_mmr_delta or 0)
    if delta_value > 0:
        delta_segs = [(f"+{delta_value}", green), (" MMR", green)]
    elif delta_value < 0:
        delta_segs = [(str(delta_value), red), (" MMR", red)]
    else:
        delta_segs = [("0", white), (" MMR", white)]
    _draw_centered_segments(
        draw,
        (sx(1400), sy(552)),
        delta_segs,
        top_small_value_font,
        gap=0,
        stroke_width=max(1, sy(2)),
        stroke_fill=(0, 0, 0, 255),
    )

    # MMR progress panel/bar
    bar_panel = (sx(130), sy(615), sx(1542), sy(742))
    _draw_sci_fi_card(draw, bar_panel, accent_rgb, fill=(5, 8, 20, 202), radius=sy(20), width=max(2, sy(3)), cut=sy(22))
    _paste_gradient_rounded_border(img, bar_panel, start_rgb, end_rgb, thickness=max(2, sy(3)), radius=sy(20), alpha=245)
    _center_text(draw, (sx(836), sy(638)), "STARTING MMR" if is_season_placement else "MMR PROGRESS", label_font, fill=white, stroke_width=1, stroke_fill=(0, 0, 0, 255))

    bar_x1, bar_y1, bar_x2, bar_y2 = sx(280), sy(663), sx(1392), sy(720)
    draw.rounded_rectangle((bar_x1, bar_y1, bar_x2, bar_y2), radius=sy(22), fill=(2, 9, 30, 245), outline=(*accent_rgb, 140), width=1)
    _paste_gradient_rounded_border(img, (bar_x1, bar_y1, bar_x2, bar_y2), start_rgb, end_rgb, thickness=max(2, sy(3)), radius=sy(22), alpha=245)
    progress_text, progress = _rank_progress(rank, current_mmr)
    if is_season_placement:
        progress_text = f"{int(current_mmr)} Starting MMR"
    fill_x2 = bar_x1 + int((bar_x2 - bar_x1) * progress)
    if fill_x2 > bar_x1 + sy(12):
        pad = sy(6)
        fill_w = max(1, fill_x2 - (bar_x1 + pad))
        fill_h = max(1, (bar_y2 - pad) - (bar_y1 + pad))
        fill_grad = _make_horizontal_gradient((fill_w, fill_h), start_rgb, end_rgb)
        fill_mask = Image.new("L", (fill_w, fill_h), 0)
        fmd = ImageDraw.Draw(fill_mask)
        fmd.rounded_rectangle((0, 0, fill_w - 1, fill_h - 1), radius=sy(18), fill=255)
        img.paste(fill_grad, (bar_x1 + pad, bar_y1 + pad), fill_mask)
        # shine and end glow
        shine = Image.new("RGBA", (fill_w, max(1, sy(16))), (255, 255, 255, 0))
        sd = ImageDraw.Draw(shine)
        sd.rounded_rectangle((0, 0, fill_w, max(1, sy(16))), radius=sy(8), fill=(255, 255, 255, 70))
        img.paste(shine, (bar_x1 + pad, bar_y1 + sy(8)), shine)
        end_glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        eg = ImageDraw.Draw(end_glow)
        eg.ellipse((fill_x2 - sy(38), bar_y1 - sy(14), fill_x2 + sy(42), bar_y2 + sy(14)), fill=(*end_rgb, 32))
        img = Image.alpha_composite(img, end_glow)

    draw = ImageDraw.Draw(img)
    _center_text(
        draw,
        (sx(836), sy(691)),
        progress_text,
        mmr_font,
        fill=(255, 255, 255, 255),
        stroke_width=max(2, sy(3)),
        stroke_fill=rank_stroke,
    )

    # Bottom cards
    bottom_cards = [
        (sx(150), sy(770), sx(570), sy(888)),
        (sx(625), sy(770), sx(1047), sy(888)),
        (sx(1098), sy(770), sx(1518), sy(888)),
    ]
    for rect in bottom_cards:
        _draw_sci_fi_card(draw, rect, blue_rgb, fill=(4, 10, 30, 188), radius=sy(16), width=max(1, sy(2)), cut=sy(16))

    bottom_specs = [
        {
            "rect": bottom_cards[0],
            "label": "PEAK MMR",
            "value": str(int(peak_mmr)),
            "icon": lambda d, cx, cy, sz: _draw_crown_icon(d, cx, cy, sz, blue_rgb),
        },
        {
            "rect": bottom_cards[1],
            "label": "STARTING MMR" if is_season_placement else "CURRENT MMR",
            "value": str(int(current_mmr)),
            "icon": lambda d, cx, cy, sz: _draw_shield_icon(d, cx, cy, sz, blue_rgb, star=True),
        },
        {
            "rect": bottom_cards[2],
            "label": "CURRENT SEASON",
            "value": str(season_name or "").upper(),
            "icon": lambda d, cx, cy, sz: _draw_shield_icon(d, cx, cy, sz, blue_rgb, star=True),
        },
    ]

    for spec in bottom_specs:
        x1, y1, x2, y2 = spec["rect"]
        card_w = x2 - x1
        card_h = y2 - y1

        icon_cx = x1 + int(card_w * 0.19)
        icon_cy = y1 + int(card_h * 0.50)
        spec["icon"](draw, icon_cx, icon_cy, sy(56))

        text_center_x = x1 + int(card_w * 0.62)
        label_y = y1 + int(card_h * 0.30)
        value_y = y1 + int(card_h * 0.68)
        text_max_w = int(card_w * 0.58)

        label_font_fit = _fit_font_size_for_text(spec["label"], text_max_w, sy(25), min_size=sy(18))
        value_font_fit = _fit_font_size_for_text(spec["value"], text_max_w, sy(48), min_size=sy(28))

        _center_text(draw, (text_center_x, label_y), spec["label"], label_font_fit, fill=(220, 232, 255, 245))
        _center_text(
            draw,
            (text_center_x, value_y),
            spec["value"],
            value_font_fit,
            fill=white,
            stroke_width=max(1, sy(2)),
            stroke_fill=(0, 0, 0, 255),
        )

    out = io.BytesIO()
    img.save(out, format="PNG")
    out.seek(0)
    return out
