import asyncio
from dotenv import load_dotenv
from lounge.config import Settings
from lounge.database import Database


async def inspect():
    db = Database(Settings.load())
    await db.open()
    try:
        async with db.pool.acquire() as c:
            for row in await c.fetch(
                "SELECT table_name,column_name,data_type FROM information_schema.columns WHERE table_schema='public' ORDER BY table_name,ordinal_position"
            ):
                print(*row)
    finally:
        await db.close()


if __name__ == "__main__":
    load_dotenv()
    asyncio.run(inspect())
