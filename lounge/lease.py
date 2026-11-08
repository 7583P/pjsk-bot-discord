"""Database-backed lease fences accidental duplicate bot processes."""

import uuid


class RuntimeLease:
    def __init__(self, pool):
        self.pool = pool
        self.owner = uuid.uuid4()

    async def claim(self):
        async with self.pool.acquire() as c:
            owner = await c.fetchval(
                "INSERT INTO lounge_runtime_lease(id,owner,expires_at) VALUES(1,$1,now()+interval '45 seconds') ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at WHERE lounge_runtime_lease.expires_at<now() RETURNING owner",
                self.owner,
            )
        if owner != self.owner:
            raise RuntimeError("Another bot process has a live database lease")

    async def heartbeat(self):
        async with self.pool.acquire() as c:
            valid = await c.fetchval(
                "UPDATE lounge_runtime_lease SET expires_at=now()+interval '45 seconds' WHERE id=1 AND owner=$1 AND expires_at>clock_timestamp() RETURNING true",
                self.owner,
            )
        if not valid:
            raise RuntimeError("Bot lease lost")

    async def release(self):
        async with self.pool.acquire() as c:
            await c.execute(
                "DELETE FROM lounge_runtime_lease WHERE id=1 AND owner=$1", self.owner
            )
