import asyncio
import json
from dotenv import load_dotenv
from lounge.config import Settings
from lounge.database import Database


async def check():
    db = Database(Settings.load())
    await db.open()
    try:
        async with db.pool.acquire() as c:
            row = await c.fetchrow("""SELECT s.id,s.name,count(sp.user_id) AS players,
                count(sp.user_id) FILTER(WHERE sp.mmr=0) AS zero_mmr,
                count(sp.user_id) FILTER(WHERE sp.placement_pending) AS placement_pending
                FROM seasons s LEFT JOIN season_players sp ON sp.season_id=s.id
                WHERE s.status='active' GROUP BY s.id""")
            print(json.dumps(dict(row) if row else {}))
    finally:
        await db.close()


if __name__ == "__main__":
    load_dotenv()
    asyncio.run(check())
