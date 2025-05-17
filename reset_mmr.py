# reset_mmr.py

import aiosqlite
import asyncio

DB_PATH = 'matchmaking.db'

async def reset_all():
    # Conecta a la base de datos
    db = await aiosqlite.connect(DB_PATH)
    # Resetea todos los jugadores a MMR=0 y rol Placement
    await db.execute("UPDATE players SET mmr = 0, role = 'Placement'")
    await db.commit()
    await db.close()
    print("✅ Todos los jugadores han sido reseteados a Placement con MMR=0")

if __name__ == "__main__":
    asyncio.run(reset_all())
