import aiosqlite
import asyncio

async def remove_user(user_id: int):
    async with aiosqlite.connect("matchmaking.db") as db:
        await db.execute("DELETE FROM players WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM placements WHERE user_id = ?", (user_id,))
        await db.commit()
        print(f"✅ Jugador {user_id} eliminado.")

# Reemplaza tu ID real aquí
asyncio.run(remove_user(1878310498720940102))
