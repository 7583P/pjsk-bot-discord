import asyncio
import time
import asyncpg
from dotenv import dotenv_values

creds = dotenv_values(r"C:\Users\Usuario\.pjsk_secure_backups\lounge_credentials.env")

async def main():
    conn = await asyncpg.connect(creds["SUPABASE_POOLER_URL"], statement_cache_size=0)
    
    # Measure the current query in _build_visible_leaderboard_rows
    t0 = time.perf_counter()
    rows = await conn.fetch("""
        SELECT
            sp.user_id,
            sp.mmr,
            sp.role,
            tie.first_reached_current_mmr,
            COALESCE(mh.matches_played, 0) AS matches_played,
            COALESCE(sp.is_season_placement, FALSE) AS is_season_placement_db,
            sp.starting_mmr AS starting_mmr_db,
            prev.prev_mmr,
            prev.prev_pos
        FROM seasonplayers sp
        LEFT JOIN (
            SELECT user_id, COUNT(*)::int AS matches_played
            FROM match_history
            WHERE season_id = $1
            GROUP BY user_id
        ) mh
            ON mh.user_id = sp.user_id
        LEFT JOIN (
            SELECT
                sp2.user_id,
                MIN(mhist.played_at) AS first_reached_current_mmr
            FROM seasonplayers sp2
            LEFT JOIN match_history mhist
              ON mhist.season_id = sp2.season_id
             AND mhist.user_id = sp2.user_id
             AND mhist.mmr_after = sp2.mmr
            WHERE sp2.season_id = $1
            GROUP BY sp2.user_id
        ) tie
            ON tie.user_id = sp.user_id
        LEFT JOIN (
            SELECT
                sp2.user_id,
                sp2.mmr AS prev_mmr,
                RANK() OVER (ORDER BY sp2.mmr DESC, sp2.user_id ASC) AS prev_pos
            FROM seasonplayers sp2
            WHERE sp2.season_id = $2
        ) prev
            ON prev.user_id = sp.user_id
        WHERE sp.season_id = $1
    """, 8, 7)
    t1 = time.perf_counter()
    print(f"Current query duration: {t1 - t0:.3f}s for {len(rows)} rows")

    # Measure 1403 calls to get_current_season
    print("Testing what happens with N+1 get_current_season calls:")
    t_start = time.perf_counter()
    for _ in range(50): # just 50 instead of 1403 to avoid waiting 10s
        await conn.fetchrow("SELECT * FROM seasons WHERE active = true")
    t_end = time.perf_counter()
    avg_per_query = (t_end - t_start) / 50
    print(f"Average time per single query: {avg_per_query*1000:.1f}ms")
    print(f"Projected time for 1403 queries: {avg_per_query * 1403:.2f}s!")

    await conn.close()

if __name__ == "__main__":
    asyncio.run(main())

