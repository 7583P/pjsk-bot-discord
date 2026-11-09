"""Private loopback diagnostics. This does not contact Discord or change state."""

import asyncio
from aiohttp import web


class BotHealth:
    def __init__(self, bot):
        self.bot = bot
        self.runner = None

    async def state(self):
        db = False
        lease = False
        pending = None
        error = None
        try:
            async with self.bot.db_pool.acquire(timeout=2) as c:
                async with asyncio.timeout(3):
                    db = await c.fetchval("SELECT 1") == 1
                    lease = bool(
                        await c.fetchval(
                            "SELECT owner=$1 AND expires_at>clock_timestamp() FROM lounge_runtime_lease WHERE id=1",
                            self.bot.lease_owner,
                        )
                    )
                    pending = await c.fetchval(
                        "SELECT count(*) FROM lounge_submissions WHERE publication_status='pending'"
                    )
        except Exception as exc:
            error = type(exc).__name__  # Never include DSN or exception messages.
        cog = self.bot.get_cog("Matchmaking")
        recovered = bool(cog and cog._runtime_recovered.is_set())
        gateway = bool(self.bot.is_ready() and not self.bot.is_closed())
        return dict(
            gateway=gateway,
            database=db,
            lease=lease,
            recovered=recovered,
            database_error_type=error,
            pending_publications=pending,
            ready=gateway and db and lease and recovered,
        )

    def app(self):
        app = web.Application()

        async def live(_):
            return web.json_response({"live": True})

        async def ready(_):
            state = await self.state()
            return web.json_response(state, status=200 if state["ready"] else 503)

        app.router.add_get("/health/live", live)
        app.router.add_get("/health/ready", ready)
        app.router.add_get("/health/diagnostics", ready)

        async def metrics(_):
            from lounge import perf

            return web.json_response(perf.snapshot())

        app.router.add_get("/health/metrics", metrics)
        return app

    async def start(self, port=8081):
        try:
            self.runner = web.AppRunner(self.app(), access_log=None)
            await self.runner.setup()
            await web.TCPSite(self.runner, "127.0.0.1", port).start()
        except Exception:
            if self.runner:
                try:
                    await self.runner.cleanup()
                except Exception:
                    pass
                self.runner = None
            raise

    async def close(self):
        if self.runner:
            try:
                await self.runner.cleanup()
            finally:
                self.runner = None
