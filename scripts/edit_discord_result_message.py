"""Self-contained script to regenerate result image and edit Discord result message."""
import asyncio
import io
import json
import os
import ssl
import sys
from pathlib import Path
import asyncpg
import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from helpers.image import generate_results_image


def get_token():
    # 1. CLI arg
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()
    # 2. Environment
    tok = os.environ.get("DISCORD_TOKEN")
    if tok:
        return tok.strip('"\'')
    # 3. Secure backups
    p = Path(r"C:\Users\Usuario\.pjsk_secure_backups\lounge_credentials.env")
    if p.exists():
        for line in p.read_text().splitlines():
            if line.startswith("DISCORD_TOKEN="):
                return line.split("=", 1)[1].strip('"\'')
    return None


def get_db_url():
    env_path = r"C:\Users\Usuario\.pjsk_secure_backups\wispbyte_bot.env"
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("DATABASE_URL="):
                    return line.strip().split("=", 1)[1].strip('"\'')
    return os.environ.get("DATABASE_URL")


async def run_fix(token: str, result_id: int = 280):
    dsn = get_db_url()
    ca_path = ROOT / "certs" / "supabase_ca_bundle.crt"
    tls = ssl.create_default_context(cafile=str(ca_path)) if ca_path.exists() else None
    
    conn = await asyncpg.connect(dsn, ssl=tls, timeout=10)
    try:
        # 1. Fetch result and submission
        res_row = await conn.fetchrow("SELECT * FROM results WHERE result_id = $1", result_id)
        if not res_row:
            raise ValueError(f"Result {result_id} not found in database")
            
        sub_row = await conn.fetchrow("SELECT * FROM lounge_submissions WHERE result_id = $1", result_id)
        if not sub_row:
            raise ValueError(f"Submission {result_id} not found in database")
            
        payload = json.loads(sub_row["payload"])
        channel_id = res_row["channel_id"]
        message_id = res_row["message_id"]
        season_name = res_row["season_name"]
        
        print(f"Target Result #{result_id}: Channel={channel_id}, Message={message_id}")
        
        # 2. Build summary rows for image generator
        summary = [
            (
                r["position"],
                r["name"],
                r["total"],
                r["stats"],
                r["mmr_prev"],
                r["mmr_delta"],
                r["mmr_final"],
                r["was_placement"],
                r["final_role"],
                r["country_code"],
                bool(r["was_placement"] and r.get("matches_played_before", 0) == 0),
                r.get("is_season_placement", False),
            )
            for r in payload["players"]
        ]
        
        created_at = res_row["created_at"]
        import datetime
        lima_tz = datetime.timezone(datetime.timedelta(hours=-5))
        created_at_lima = created_at.astimezone(lima_tz)
        submitted_at_text = created_at_lima.strftime("%d/%m/%Y %H:%M") + " UTC-5"
        
        print(f"Rendering image with submitted_at: {submitted_at_text}...")
        logo_path = ROOT / "assets" / "logo.png"
        font_path = str(ROOT / "assets" / "rodinntlg_eb.otf")
        flag_path = str(ROOT / "assets" / "flags" / "unknown.jpg")
        
        with Image.open(logo_path) as logo_img:
            buf = generate_results_image(
                summary,
                font_path=font_path,
                side_image=logo_img.copy(),
                submitted_at_text=submitted_at_text,
                unknown_flag_path=flag_path,
                unknown_flag_code="__UNKNOWN_FLAG__",
            )
            
        buf.seek(0)
        image_bytes = buf.read()
        print(f"Rendered image ({len(image_bytes)} bytes)")
        
        # Save artifact
        artifact_path = ROOT / "artifacts" / f"result_{result_id}_updated.png"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_bytes(image_bytes)
        print(f"Saved artifact to {artifact_path}")
        
        # 3. Patch Discord message
        print(f"Patching Discord message {message_id} in channel {channel_id}...")
        headers = {
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordBot (https://github.com/7583P/pjsk-bot-discord, 1.0)",
        }
        
        gap_low = res_row["gap_low"]
        gap_high = res_row["gap_high"]
        room_mmr = res_row["room_mmr"]
        songs_block = res_row["songs_block"] or ""
        
        gap_text = ""
        if gap_low is not None and gap_high is not None:
            gap_text = f" · Lv {gap_low}-{gap_high}"
            if room_mmr is not None:
                gap_text += f" · Avg MMR {room_mmr}"
                
        if songs_block:
            songs_block = f"\n**Songs (Lv / Diff / Notes):**\n{songs_block}"
            
        header_line = f"ID: {result_id}{gap_text}{songs_block}"
        
        payload_json = {
            "attachments": [
                {
                    "id": 0,
                    "filename": "results.png",
                    "description": f"Result #{result_id}"
                }
            ],
            "embeds": [
                {
                    "title": f"RESULTS — {season_name}",
                    "description": header_line,
                    "color": 0x00F7FF,
                    "image": {
                        "url": "attachment://results.png"
                    }
                }
            ]
        }
        
        files = {
            "files[0]": ("results.png", image_bytes, "image/png"),
        }
        data_payload = {
            "payload_json": json.dumps(payload_json),
        }
        
        patch_url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.patch(patch_url, headers=headers, data=data_payload, files=files)
            if resp.status_code == 200:
                print("SUCCESS: Discord message and image updated successfully!")
                return True
            else:
                print(f"FAILED to update message ({resp.status_code}): {resp.text}")
                return False
                
    finally:
        await conn.close()


async def main():
    tok = get_token()
    if not tok:
        print("ERROR: DISCORD_TOKEN not provided.")
        print("Usage: python scripts/edit_discord_result_message.py <DISCORD_TOKEN>")
        sys.exit(1)
    ok = await run_fix(tok, 280)
    if not ok:
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
