"""The no-reset pool must never leak session state between operations (disposable DB only)."""
import os, unittest
URL = os.getenv("TEST_REROLL_DATABASE_URL")

@unittest.skipIf(not URL, "TEST_REROLL_DATABASE_URL not set")
class PoolSessionState(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        from lounge import perf
        self.pool = await asyncpg.create_pool(URL, min_size=1, max_size=1, connection_class=perf.timed_connection_class(asyncpg.Connection))

    async def asyncTearDown(self):
        await self.pool.close()

    async def timeout(self):
        async with self.pool.acquire() as c:
            return await c.fetchval("SHOW statement_timeout")

    async def test_set_does_not_leak(self):
        base = await self.timeout()
        for stmt in ("SET statement_timeout='7s'", "set  statement_timeout = '7s'", "SELECT set_config('statement_timeout','7s',false)"):
            async with self.pool.acquire() as c:
                await c.execute(stmt) if stmt.lower().startswith("set") else await c.fetchval(stmt)
                self.assertEqual(await c.fetchval("SHOW statement_timeout"), "7s")
            self.assertEqual(await self.timeout(), base, stmt)

    async def test_set_local_and_open_transaction_do_not_leak(self):
        base = await self.timeout()
        async with self.pool.acquire() as c:
            async with c.transaction():
                await c.execute("SET LOCAL statement_timeout='7s'")
        self.assertEqual(await self.timeout(), base)
        async with self.pool.acquire() as c:
            tr = c.transaction(); await tr.start()
            await c.execute("CREATE TEMP TABLE leak_t(x int)")
            await c.execute("INSERT INTO leak_t VALUES(1)")
            # released inside an open transaction: asyncpg rolls it back
        async with self.pool.acquire() as c:
            self.assertFalse(await c.fetchval("SELECT to_regclass('pg_temp.leak_t') IS NOT NULL"))

    async def test_advisory_lock_is_released(self):
        async with self.pool.acquire() as c:
            await c.fetchval("SELECT pg_advisory_lock(424242)")
        async with self.pool.acquire() as c:
            self.assertEqual(await c.fetchval("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid()"), 0)
