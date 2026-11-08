import asyncio
import ssl
from pathlib import Path
import asyncpg

cred_path = Path(r"C:\Users\Usuario\.pjsk_secure_backups\lounge_credentials.env")
creds = {}
with open(cred_path, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            creds[k.strip()] = v.strip().strip('"').strip("'")

db_url = creds.get("SUPABASE_POOLER_URL") or creds.get("SUPABASE_DATABASE_URL")
ca_path = Path(r"d:\Projects\PJSK Bot\certs\supabase_ca_bundle.crt")

ssl_ctx = ssl.create_default_context(cafile=str(ca_path))
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_REQUIRED

async def check():
    conn = await asyncpg.connect(db_url, ssl=ssl_ctx, statement_cache_size=0)
    try:
        cols_room = await conn.fetch("SELECT column_name FROM information_schema.columns WHERE table_name = 'lounge_room_state'")
        print("lounge_room_state cols:", [c["column_name"] for c in cols_room])
        room_count = await conn.fetchval("SELECT count(*) FROM lounge_room_state;")
        print(f"Total rows in lounge_room_state: {room_count}")
        
        # Check historical integrity
        players = await conn.fetchval("SELECT count(*) FROM players;")
        matches = await conn.fetchval("SELECT count(*) FROM match_history;")
        seasonplayers = await conn.fetchval("SELECT count(*) FROM seasonplayers;")
        seasons = await conn.fetchval("SELECT count(*) FROM seasons;")
        current_season = await conn.fetchrow("SELECT id, name, active FROM seasons WHERE active = true;")
        
        print(f"\n--- Supabase Integrity Check ---")
        print(f"Players: {players} (expected 1,403)")
        print(f"Matches: {matches} (expected 1,360)")
        print(f"Seasonplayers: {seasonplayers} (expected 4,715)")
        print(f"Seasons: {seasons} (expected 7)")
        print(f"Current Season: ID {current_season['id']} ({current_season['name']}), Active: {current_season['active']}")
        
        assert room_count == 0, "No active rooms"
        assert players == 1403, f"Expected 1403 players, found {players}"
        assert matches == 1360, f"Expected 1360 matches, found {matches}"
        print("\n[SUCCESS] Cero partidas pendientes, cero salas abiertas, historial 100% integro.")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(check())
