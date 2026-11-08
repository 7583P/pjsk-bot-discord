import asyncio
import asyncpg
from dotenv import dotenv_values

creds = dotenv_values(r"C:\Users\Usuario\.pjsk_secure_backups\lounge_credentials.env")
pooler_url = creds.get("SUPABASE_POOLER_URL")

async def main():
    conn = await asyncpg.connect(pooler_url, statement_cache_size=0)
    try:
        seasons = await conn.fetch("SELECT * FROM seasons ORDER BY id")
        print("Seasons:")
        for s in seasons:
            print(f"  id={s['id']}, name='{s['name']}', active={s['active']}")
        
        lease = await conn.fetch("SELECT * FROM lounge_runtime_lease")
        print(f"Runtime lease rows: {len(lease)}")
        for l in lease:
            print(f"  {dict(l)}")
        
        rooms = await conn.fetch("SELECT * FROM lounge_room_state WHERE status IN ('active', 'needs_attention')")
        print(f"Active rooms: {len(rooms)}")
        
        subs = await conn.fetch("SELECT * FROM lounge_submissions WHERE publication_status = 'pending'")
        print(f"Pending submissions: {len(subs)}")
        
        players_count = await conn.fetchval("SELECT count(*) FROM players")
        print(f"Total players in players table: {players_count}")
        
        splayers_count = await conn.fetchval("SELECT count(*) FROM seasonplayers")
        print(f"Total rows in seasonplayers: {splayers_count}")
        
        cur_season = [s for s in seasons if s["active"]]
        print(f"Current active season: {[(s['id'], s['name']) for s in cur_season]}")
        
        tables = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
        print(f"Tables ({len(tables)}): {[t['tablename'] for t in tables]}")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
