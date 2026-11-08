import aiosqlite
import asyncio


async def check_user(user_id: int):
    async with aiosqlite.connect("matchmaking.db") as db:
        async with db.execute(
            "SELECT * FROM players WHERE user_id = ?", (user_id,)
        ) as cursor:
            result = await cursor.fetchone()
            if result:
                print("🚨 Aún existe:", result)
            else:
                print("✅ Usuario eliminado correctamente.")


asyncio.run(check_user(878310498720940102))
