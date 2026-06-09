from pathlib import Path
import sys

# Permite importar helpers.image desde scripts/
ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from helpers.image import generate_stats_image


def main():
    output_path = ROOT_DIR / "scripts" / "stats_preview.png"

    image_buffer = generate_stats_image(
        player_name="Desconocido",
        avatar_bytes=None,
        rank="Gold",
        position=12,
        total_players=350,
        matches_played=47,
        last10_wins=7,
        last10_losses=3,
        peak_mmr=412,
        current_mmr=325,
        season_name="Season 3",
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "wb") as f:
        f.write(image_buffer.getvalue())

    print(f"Stats preview generated: {output_path}")


if __name__ == "__main__":
    main()
