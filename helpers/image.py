from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops, ImageOps

import io
from pathlib import Path

from typing import Literal, List

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


def strip_unsupported_chars(text: str, font_path: str | Path) -> str:
    font = TTFont(font_path)

    # Collect all Unicode → glyph maps
    cmaps = [table.cmap for table in font["cmap"].tables]

    def supported(char):
        cp = ord(char)
        return any(cp in cmap for cmap in cmaps)

    # Filter characters
    filtered = "".join(ch for ch in text if supported(ch))

    return filtered.strip()


def add_stroke(img, stroke_width=4, stroke_color=(0, 0, 0, 255)):
    img = img.convert("RGBA")
    w, h = img.size
    pad = stroke_width

    # Expand canvas
    expanded = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    expanded.paste(img, (pad, pad))

    # Extract alpha
    alpha = expanded.getchannel("A")

    # Dilate alpha
    dilated = alpha
    for _ in range(stroke_width):
        dilated = dilated.filter(ImageFilter.MaxFilter(3))

    # Stroke mask = dilated - original alpha
    stroke_mask = ImageChops.subtract(dilated, alpha)

    # Create stroke layer
    stroke = Image.new("RGBA", expanded.size, stroke_color)
    stroke.putalpha(stroke_mask)

    # Composite
    out = Image.alpha_composite(stroke, expanded)

    return out


def hex_to_rgb(hex_color: str) -> tuple[int]:
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
        for y in range(0, img.height):
            color = tuple(
                int(start[i] + (end[i] - start[i]) * y / img.height) for i in range(3)
            )
            for x in range(0, img.width):
                px[x, y] = color
    elif direction == "right":
        for x in range(0, img.width):
            color = tuple(
                int(start[i] + (end[i] - start[i]) * x / img.width) for i in range(3)
            )
            for y in range(0, img.height):
                px[x, y] = color


def make_gradient_text_image(
    text: str,
    font: ImageFont.FreeTypeFont,
    gradient_start: tuple,
    gradient_end: tuple,
    padding: int = 2,
    direction: Literal["down", "right"] = "right",
) -> Image.Image:
    # --- text metrics ---
    x0, y0, x1, y1 = font.getbbox(text)
    text_w = x1 - x0
    text_h = y1 - y0

    ascent, descent = font.getmetrics()

    w = text_w
    h = text_h + descent + padding

    gradient = Image.new("RGB", (w, h))
    draw_gradient(gradient, gradient_start, gradient_end, direction=direction)

    mask = Image.new("RGBA", (w, h))
    d = ImageDraw.Draw(mask)

    d.text(
        (0, 0),
        text,
        font=font,
    )

    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(gradient, (0, 0), mask)

    return out


def generate_results_image(
    summary_rows: List[tuple],
    font_path: str,
    side_image: Image.Image,
) -> io.BytesIO:
    """
    Draws foreground results on top of background.png (1920x1080).
    ONLY draws:
      - song jacket
      - song titles (EN + JP, same style)
      - difficulty text
      - player rows (rank, name, results, points, MMR)

    summary_rows = [
        (pos, name_with_flag, total_points, stats_list, mmr_prev, mmr_delta, mmr_final, was_placement),
        ...
    ]
    """

    summary_rows = sorted(summary_rows, key=lambda r: r[0])  # r[0] = pos

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
    # AVERAGE MMR (from summary_rows)
    # ======================
    average_mmr = round(sum(row[4] for row in summary_rows) / len(summary_rows))

    # ======================
    # LOUNGE INFO (TOP RIGHT)
    # ======================
    side_x = 1650
    side_y = 40

    side = ImageOps.fit(
        side_image.convert("RGBA"), (249, 210), method=Image.Resampling.LANCZOS
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

    im.paste(invite_img, (text_anchor_x - invite_img.width, side_y + 120), invite_img)

    # ======================
    # PLAYER ROW LAYOUT
    # ======================
    ROW_START_Ys = [314, 461, 603, 742, 899]

    X_NAME = 241
    NAME_MAX_PX = 20 * 23
    X_RESULTS = 739
    X_POINTS = 1090
    X_MMR = 1360

    # ======================
    # DRAW PLAYER ROWS
    # ======================
    for i, (
        pos,
        name,
        points,
        results,
        mmr_old,
        mmr_delta,
        mmr_new,
        was_placement,
        rank_name,
    ) in enumerate(summary_rows):

        y = ROW_START_Ys[i]

        # Name (gradient)
        name_gradient_start, name_gradient_end = rank_colors[rank_name]
        name_img = add_stroke(
            make_gradient_text_image(
                strip_unsupported_chars(name, font_path),
                row_font,
                hex_to_rgb(name_gradient_start),
                hex_to_rgb(name_gradient_end),
            ),
            3,
        )

        t_y = y
        if name_img.width > NAME_MAX_PX:
            name_img = add_stroke(
                make_gradient_text_image(
                    name,
                    small_font,
                    hex_to_rgb(name_gradient_start),
                    hex_to_rgb(name_gradient_end),
                ),
                3,
            )
            t_y += 5

        im.paste(name_img, (X_NAME, t_y), name_img)

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

        # MMR
        if mmr_delta > 0:
            delta_color = (0, 150, 0)
            delta_str = f"+{mmr_delta}"
        elif mmr_delta < 0:
            delta_color = (220, 20, 60)
            delta_str = str(mmr_delta)
        else:
            delta_color = (220, 180, 30)
            delta_str = "+0"

        draw.text((X_MMR, y), f"{mmr_old} ", font=row_font, fill=(255, 255, 255))
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
