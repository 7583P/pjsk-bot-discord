"""Peak/steady RSS of the combined bot+API process footprint (no Discord connection)."""
import asyncio, os, sys, time
from pathlib import Path
import psutil
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
p = psutil.Process()
def rss(): return p.memory_info().rss / 2**20
async def main():
    print(f"python baseline      {rss():6.0f} MB")
    from scripts.perf_baseline import load_from_railway; load_from_railway(); os.environ["DISCORD_TOKEN"] = "dummy-not-used"
    import discord, app as api_app, main as bot_main  # FastAPI app + bot module
    print(f"after imports        {rss():6.0f} MB")
    from lounge.connections import create_pool, bind_bot_pool
    pool = await create_pool(); bind_bot_pool(pool)
    for cog in ("cogs.matchmaking", "cogs.rooms", "cogs.reroll", "cogs.autoroles", "cogs.players"):
        __import__(cog)
    print(f"cogs imported+pool   {rss():6.0f} MB")
    from helpers.leaderboard import generate_top100_image
    rows = [dict(user_id=i, member_name=f"Player {i}", player_name="x", role="Diamond", visible_mmr=9000 - i, title_label="T", title_style="top-white") for i in range(100)]
    peak = 0
    for pg in range(10):
        generate_top100_image(rows, "Season 5 (New Era)", pg); peak = max(peak, rss())
    print(f"after 10 renders     {rss():6.0f} MB (peak {peak:.0f})")
    # simulate guild member cache: 1500 members x ~2.5KB
    blob = [bytearray(2500) for _ in range(1500)]
    print(f"+~members cache est  {rss():6.0f} MB")
    t=time.process_time(); await asyncio.sleep(0)
    await pool.close()
asyncio.run(main())
