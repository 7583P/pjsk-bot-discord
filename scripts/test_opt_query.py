import asyncio
import time
import asyncpg
from dotenv import dotenv_values

creds = dotenv_values(r"C:\Users\Usuario\.pjsk_secure_backups\lounge_credentials.env")

async def main():
    conn = await asyncpg.connect(creds["SUPABASE_POOLER_URL"], statement_cache_size=0)
    
    t0 = time.perf_counter()
    rows = await conn.fetch("""
        SELECT
            sp.user_id,
            p.name AS member_name,
            sp.mmr,
            sp.role,
            tie.first_reached_current_mmr,
            COALESCE(mh.matches_played, 0) AS matches_played,
            COALESCE(sp.is_season_placement, FALSE) AS is_season_placement_db,
            sp.starting_mmr AS starting_mmr_db,
            prev.prev_mmr,
            prev.prev_pos
        FROM seasonplayers sp
        JOIN players p ON p.user_id = sp.user_id
        LEFT JOIN (
            SELECT user_id, COUNT(*)::int AS matches_played
            FROM match_history
            WHERE season_id = $1
            GROUP BY user_id
        ) mh ON mh.user_id = sp.user_id
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
        ) tie ON tie.user_id = sp.user_id
        LEFT JOIN (
            SELECT
                sp2.user_id,
                sp2.mmr AS prev_mmr,
                RANK() OVER (ORDER BY sp2.mmr DESC, sp2.user_id ASC) AS prev_pos
            FROM seasonplayers sp2
            WHERE sp2.season_id = $2
        ) prev ON prev.user_id = sp.user_id
        WHERE sp.season_id = $1
        ORDER BY
            CASE WHEN COALESCE(mh.matches_played, 0) > 0 THEN 0
                 WHEN COALESCE(sp.is_season_placement, FALSE) AND $3 = FALSE THEN 1
                 ELSE 2 END ASC,
            sp.mmr DESC,
            tie.first_reached_current_mmr ASC NULLS LAST,
            sp.user_id ASC
        LIMIT 100
    """, 8, 7, True)
    t1 = time.perf_counter()
    print(f"Optimized top100 query duration: {t1 - t0:.3f}s for {len(rows)} rows!")
    print(f"First row: {dict(rows[0])}")

    await conn.close()

if __name__ == "__main__":
    asyncio.run(main())

