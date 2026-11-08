import asyncio
import asyncpg
from dotenv import dotenv_values

creds = dotenv_values(r"C:\Users\Usuario\.pjsk_secure_backups\lounge_credentials.env")

async def main():
    conn = await asyncpg.connect(creds["SUPABASE_POOLER_URL"], statement_cache_size=0)
    cols = await conn.fetch("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'lounge_season_policy'")
    print("Columns:", [(c["column_name"], c["data_type"]) for c in cols])
    rows = await conn.fetch("SELECT * FROM lounge_season_policy")
    print("Rows:", [dict(r) for r in rows])
    await conn.close()

if __name__ == "__main__":
    asyncio.run(main())

