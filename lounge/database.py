import asyncio
from contextlib import asynccontextmanager
import logging
import random
import ssl
import time
import uuid
import asyncpg
from lounge.config import resolve_ca_file

log = logging.getLogger(__name__)


class Database:
    def __init__(self, settings):
        self.settings = settings
        self.pool = None
        self.owner = None

    async def open(self):
        ca_file = self.settings.ca_file or resolve_ca_file()
        tls = (
            ssl.create_default_context(cafile=ca_file)
            if self.settings.ssl
            else False
        )
        for attempt in range(5):
            try:
                self.pool = await asyncpg.create_pool(
                    self.settings.database_url,
                    min_size=1,
                    max_size=self.settings.pool_max,
                    ssl=tls,
                    timeout=10,
                    command_timeout=15,
                    max_inactive_connection_lifetime=120,
                    statement_cache_size=0
                    if self.settings.pool_mode == "transaction"
                    else 100,
                )
                break
            except (OSError, asyncio.TimeoutError, asyncpg.PostgresConnectionError):
                if attempt == 4:
                    raise
                log.warning("database_connect_retry attempt=%s", attempt + 1)
                await asyncio.sleep(min(15, 2**attempt) + random.random())
        async with self.pool.acquire() as c:
            version = await c.fetchval("SELECT max(version) FROM schema_migrations")
            if version != "001_initial.sql":
                raise RuntimeError(
                    "Migraciones pendientes; ejecuta python -m scripts.migrate"
                )

    async def close(self):
        if self.pool:
            await asyncio.wait_for(self.pool.close(), timeout=20)

    async def claim(self):
        self.owner = uuid.uuid4()
        async with self.pool.acquire() as c:
            claimed = await c.fetchval(
                """INSERT INTO runtime_lease(id,owner,expires_at) VALUES(1,$1,now()+interval '45 seconds')
                ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at
                WHERE runtime_lease.expires_at<now() RETURNING owner""",
                self.owner,
            )
            if claimed != self.owner:
                self.owner = None
                raise RuntimeError("Otra instancia del bot tiene la lease activa")

    async def heartbeat(self):
        async with self.pool.acquire() as c:
            ok = await c.fetchval(
                "UPDATE runtime_lease SET expires_at=now()+interval '45 seconds' WHERE id=1 AND owner=$1 AND expires_at>now() RETURNING true",
                self.owner,
            )
            if not ok:
                raise RuntimeError(
                    "Lease perdida; detener instancia para evitar eventos duplicados"
                )

    async def release(self):
        if self.owner:
            async with self.pool.acquire() as c:
                await c.execute(
                    "DELETE FROM runtime_lease WHERE id=1 AND owner=$1", self.owner
                )

    @asynccontextmanager
    async def competitive(self, expected_season=None):
        start = time.perf_counter()
        async with self.pool.acquire() as c, c.transaction():
            valid = await c.fetchval(
                "SELECT owner=$1 AND expires_at>clock_timestamp() FROM runtime_lease WHERE id=1 FOR SHARE",
                self.owner,
            )
            if not valid:
                raise RuntimeError("Instancia sin lease válida")
            season = await c.fetchrow(
                "SELECT * FROM seasons WHERE status='active' FOR SHARE"
            )
            if not season or (
                expected_season is not None and season["id"] != expected_season
            ):
                raise ValueError(
                    "La operación pertenece a otra temporada o no hay temporada activa"
                )
            yield c, season
        log.info(
            "competitive_transaction duration_ms=%.2f",
            (time.perf_counter() - start) * 1000,
        )

    async def health(self):
        async with self.pool.acquire(timeout=3) as c:
            return await c.fetchval("SELECT 1") == 1
