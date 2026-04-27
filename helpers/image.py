from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops, ImageOps

import io
import re
import urllib.request
from pathlib import Path
from typing import Literal, List, Any


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


def normalize_country_code(value: Any) -> str | None:
    if value is None:
        return None

    code = str(value).strip().upper()
    if not ISO2_RE.match(code):
        return None

    return code


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

    try:
        font = TTFont(font_path)
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


def get_flag_image(country_code: str, target_size=(42, 30)) -> Image.Image | None:
    """
    Intenta cargar assets/flags/{iso2}.png.
    Si no existe, intenta descargar desde flagcdn y cachearlo.
    Si falla, devuelve None y se dibuja un badge ISO2.
    """
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


def normalize_summary_row(row: tuple) -> dict:
    """
    Soporta 3 formatos:

    Nuevo recomendado:
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

    Compatible con tu formato actual:
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

    if len(row) >= 10:
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
        ) = row[:10]

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
            "summary row must have 8, 9 or 10 values: "
            "(pos, name, points, results, mmr_old, mmr_delta, mmr_new, was_placement, rank_name?, country_code?)"
        )

    name = str(name or "").strip()

    # Si el nombre ya venía como "Player 🇵🇪", capturamos esa bandera
    # y la convertimos a ISO2 para dibujarla como PNG.
    clean_name, iso_from_emoji = flag_emoji_to_iso2(name)
    name = clean_name

    if not country_code and iso_from_emoji:
        country_code = iso_from_emoji

    country_code = normalize_country_code(country_code)

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
    }


def generate_results_image(
    summary_rows: List[tuple],
    font_path: str,
    side_image: Image.Image,
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
            country_code
        ),
        ...
    ]

    Importante:
    - country_code debe venir del ISO2 de !submit: [PE], [US], [JP], etc.
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

    TITLE_COLOR = (255, 255, 255)

    # ======================
    # CANVAS / BACKGROUND
    # ======================
    im = Image.open("assets/background.png").convert("RGBA")
    draw = ImageDraw.Draw(im)

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

    # Espacio máximo para que nombre + bandera no choque con results
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

        im.paste(name_img, (X_NAME, t_y), name_img)

        # Bandera a la derecha del nombre
        if country_code:
            flag_img = get_flag_image(country_code, target_size=FLAG_SIZE)

            if flag_img is None:
                flag_img = make_iso_badge(country_code, smallist_font)

            flag_x = X_NAME + name_img.width + FLAG_GAP
            flag_y = t_y + max(0, (name_img.height - flag_img.height) // 2)

            im.paste(flag_img, (flag_x, flag_y), flag_img)

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
