import asyncio
import logging
import aiohttp
from lounge.rules import pick_songs

log = logging.getLogger(__name__)
RAW = "https://raw.githubusercontent.com/Sekai-World/sekai-master-db-en-diff/main"


class SongCatalog:
    def __init__(self, db):
        self.db = db
        self.rows = []

    async def load(self):
        async with self.db.pool.acquire() as c:
            self.rows = [
                dict(r) for r in await c.fetch("SELECT * FROM songs ORDER BY id,diff")
            ]

    async def refresh(self):
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as session:

            async def get(name):
                async with session.get(f"{RAW}/{name}.json") as response:
                    response.raise_for_status()
                    return await response.json(content_type=None)

            musics, diffs = await asyncio.gather(
                get("musics"), get("musicDifficulties")
            )
        titles = {m["id"]: m["title"] for m in musics}
        rows = [
            dict(
                id=d["musicId"],
                title=titles[d["musicId"]],
                diff=d["musicDifficulty"],
                level=int(d["playLevel"]),
            )
            for d in diffs
            if d["musicId"] in titles
            and d["musicDifficulty"] in ("append", "master", "expert")
        ]
        if not rows:
            raise ValueError("Catálogo remoto vacío; se conserva el último válido")
        async with self.db.competitive() as (c, s):
            await c.execute("DELETE FROM songs")
            await c.executemany(
                "INSERT INTO songs VALUES($1,$2,$3,$4)",
                [(r["id"], r["title"], r["diff"], r["level"]) for r in rows],
            )
        self.rows = rows
        log.info("catalog_updated charts=%s", len(rows))

    def pick(self, low, high):
        return pick_songs(self.rows, low, high)
