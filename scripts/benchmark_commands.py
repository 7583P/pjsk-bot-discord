import asyncio
import time
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock
from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

creds = dotenv_values(r"C:\Users\Usuario\.pjsk_secure_backups\lounge_credentials.env")
for k, v in creds.items():
    if v and k not in os.environ:
        os.environ[k] = v

os.environ["DATABASE_URL"] = creds["SUPABASE_POOLER_URL"]
os.environ["DB_HOST"] = creds["SUPABASE_POOLER_URL"]

import asyncpg
from cogs.matchmaking import Matchmaking

async def main():
    pool = await asyncpg.create_pool(creds["SUPABASE_POOLER_URL"], min_size=1, max_size=2, statement_cache_size=0)
    bot = MagicMock()
    bot.db_pool = pool
    cog = Matchmaking(bot)
    cog.db_pool = pool

    # Benchmark _build_visible_leaderboard_rows (/top100)
    print("--- Benchmark /top100 ---")
    guild = MagicMock()
    guild.get_member.return_value = None
    t0 = time.perf_counter()
    rows = await cog._build_visible_leaderboard_rows(guild, 8)
    t1 = time.perf_counter()
    print(f"/top100 duration (optimized): {t1 - t0:.3f}s for {len(rows)} players")

    # Benchmark /mmr
    print("\n--- Benchmark /mmr ---")
    user = MagicMock()
    user.id = 686865396359364633
    user.roles = []
    t0 = time.perf_counter()
    stats = await cog._fetch_user_stats(guild, user, 8)
    t1 = time.perf_counter()
    print(f"/mmr duration (optimized): {t1 - t0:.3f}s (Stats: {stats})")

    # Benchmark /cutoffs
    print("\n--- Benchmark /cutoffs ---")
    t0 = time.perf_counter()
    tiers = (1, 10, 20, 30, 40, 50, 100, 200, 300, 400, 500, 1000)
    data = await cog._build_cutoffs_data(8, tiers)
    t1 = time.perf_counter()
    print(f"/cutoffs duration: {t1 - t0:.3f}s (Rows: {len(data)})")

    # Benchmark /c
    print("\n--- Benchmark /c ---")
    t0 = time.perf_counter()
    async with pool.acquire() as conn:
        await conn.fetchval("SELECT payload FROM lounge_queue_state WHERE queue_id = 1371307353437110282")
    t1 = time.perf_counter()
    print(f"/c duration: {t1 - t0:.3f}s")

    # Benchmark /d
    print("\n--- Benchmark /d ---")
    t0 = time.perf_counter()
    async with pool.acquire() as conn:
        await conn.fetchval("SELECT payload FROM lounge_queue_state WHERE queue_id = 1371307353437110282")
    t1 = time.perf_counter()
    print(f"/d duration: {t1 - t0:.3f}s")

    cog.cog_unload()
    await pool.close()

if __name__ == "__main__":
    asyncio.run(main())
