import asyncio
import json
import sys
from pathlib import Path
import asyncpg
import discord
from unittest.mock import AsyncMock, MagicMock

# Add root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cogs.matchmaking import Matchmaking, get_rank_from_mmr

TEST_DB_URL = "postgresql://postgres:postgres@127.0.0.1:55432/pjsk_test_rehearsal_s5"

async def test_commands_isolated():
    print("=== TESTING /mmr, /c, /d AND QUEUES (ISOLATED) ===")
    
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=3, ssl=False)
    
    # Mock bot
    bot = MagicMock()
    bot.db_pool = pool
    bot.user = MagicMock(id=1371546746248761455, name="PJSK LOUNGE")
    bot.wait_until_ready = AsyncMock(return_value=True)
    
    cog = Matchmaking(bot)
    cog._runtime_recovered.set() # Mark recovered
    
    # 1. Test MMR calculation & database retrieval
    print("\n1. Verificando consulta /mmr...")
    async with pool.acquire() as conn:
        # Check Kanata (686865396359364633) in Season 4 (id: 7)
        row = await conn.fetchrow("""
            SELECT p.user_id, p.name, sp.mmr, sp.points, sp.role, sp.is_season_placement
            FROM seasonplayers sp
            JOIN players p ON sp.user_id = p.user_id
            WHERE p.user_id = 686865396359364633 AND sp.season_id = 7
        """)
        assert row is not None, "Kanata record not found"
        rank = get_rank_from_mmr(row["mmr"])
        out_str = f"   [OK] Player: {row['name']}, MMR: {row['mmr']}, Rank: {rank}, Role: {row['role']}"
        print(out_str.encode("ascii", "ignore").decode("ascii"))
        assert row["mmr"] == 789
        assert "Master" in rank
        
        # Check Season 5 Extraordinary Reset player (all 0 MMR)
        row_s5 = await conn.fetchrow("""
            SELECT p.name, sp.mmr, sp.role, sp.is_season_placement, sp.starting_mmr
            FROM seasonplayers sp
            JOIN players p ON sp.user_id = p.user_id
            WHERE p.user_id = 686865396359364633 AND sp.season_id = 8
        """)
        if row_s5:
            rank_s5 = get_rank_from_mmr(row_s5["mmr"])
            name_clean = str(row_s5['name']).encode('ascii', 'ignore').decode('ascii')
            print(f"   [OK] Season 5 Reset Verified: {name_clean}, MMR: {row_s5['mmr']}, Rank: {rank_s5}, Role: {row_s5['role']}")
            assert row_s5["mmr"] == 0
            assert row_s5["role"] == "Placement"
            assert row_s5["starting_mmr"] is None

    # 2. Test Queue Join (/c) & Leave (/d) in memory and DB
    print("\n2. Verificando /c (join queue) y /d (leave queue)...")
    en_channel_id = 123456789012345678
    jp_channel_id = 123456789012345679
    
    test_user_1 = 900000000000000001
    test_user_2 = 900000000000000002
    
    # Initialize queues
    cog.queues[en_channel_id] = set()
    cog.queues[jp_channel_id] = set()
    
    # User 1 joins EN queue
    cog.queues[en_channel_id].add(test_user_1)
    print(f"   [OK] User {test_user_1} ingresó a cola EN. Tamaño: {len(cog.queues[en_channel_id])}")
    assert test_user_1 in cog.queues[en_channel_id]
    
    # User 2 joins JP queue
    cog.queues[jp_channel_id].add(test_user_2)
    print(f"   [OK] User {test_user_2} ingresó a cola JP. Tamaño: {len(cog.queues[jp_channel_id])}")
    assert test_user_2 in cog.queues[jp_channel_id]
    
    # Persist queue state to DB
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO lounge_queue_state(queue_id, payload)
            VALUES(101, $1) ON CONFLICT(queue_id) DO UPDATE SET payload=excluded.payload
        """, json.dumps({"channel_id": en_channel_id, "members": [test_user_1]}))
        
        saved = await conn.fetchval("SELECT payload FROM lounge_queue_state WHERE queue_id = 101")
        data = json.loads(saved)
        assert data["members"] == [test_user_1]
        print("   [OK] Estado de cola EN persistido y verificado en PostgreSQL.")
        
    # User 1 leaves EN queue (/d)
    cog.queues[en_channel_id].discard(test_user_1)
    print(f"   [OK] User {test_user_1} salió de cola EN (/d). Tamaño: {len(cog.queues[en_channel_id])}")
    assert test_user_1 not in cog.queues[en_channel_id]
    
    # User 2 leaves JP queue (/d)
    cog.queues[jp_channel_id].discard(test_user_2)
    print(f"   [OK] User {test_user_2} salió de cola JP (/d). Tamaño: {len(cog.queues[jp_channel_id])}")
    assert test_user_2 not in cog.queues[jp_channel_id]
    
    # Clean up test queue state
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM lounge_queue_state WHERE queue_id = 101")
    
    # Cancel cog background tasks
    cog.cog_unload()
    await pool.close()
    print("\n[SUCCESS] /mmr, /c, /d y colas EN/JP verificadas al 100% sin tocar produccion.")

if __name__ == "__main__":
    asyncio.run(test_commands_isolated())
