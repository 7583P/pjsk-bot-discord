import asyncio
import logging
import signal
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
from aiohttp import web
from lounge.config import Settings
from lounge.database import Database
from lounge.repository import Repository
from lounge.logging import configure

log = logging.getLogger(__name__)


class MyBot(commands.Bot):
    def __init__(self, settings):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            application_id=settings.application_id,
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.settings = settings
        self.db = Database(settings)
        self.repo = Repository(self.db)
        self.heartbeat_task = None
        self.health_runner = None
        self.failure = None

    async def setup_hook(self):
        await self.db.open()
        await self.db.claim()
        self.heartbeat_task = asyncio.create_task(
            self.keep_lease(), name="runtime-lease"
        )
        for extension in ("cogs.matchmaking", "cogs.autoroles", "cogs.players"):
            await self.load_extension(extension)
        if self.settings.guild_id:
            synced = []
            for guild_id in sorted(self.settings.guild_ids):
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                synced.extend(await self.tree.sync(guild=guild))
        else:
            synced = await self.tree.sync()
        log.info("commands_synced count=%s", len(synced))
        app = web.Application()
        app.router.add_get("/health/live", self.live)
        app.router.add_get("/health/ready", self.ready)
        app.router.add_get("/health/diagnostics", self.diagnostics)
        self.health_runner = web.AppRunner(app)
        await self.health_runner.setup()
        await web.TCPSite(self.health_runner, "127.0.0.1", 8081).start()

    async def keep_lease(self):
        try:
            while True:
                await asyncio.sleep(10)
                await self.db.heartbeat()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failure = RuntimeError("Lease perdida; reinicio supervisado requerido")
            log.exception("runtime_lease_lost_shutting_down")
            # close from another task so it never awaits/cancels itself.
            asyncio.create_task(self.close())

    async def live(self, request):
        return web.json_response({"process": True})

    async def ready(self, request):
        try:
            database = await self.db.health()
        except Exception:
            database = False
        gateway = self.is_ready() and not self.is_closed()
        return web.json_response(
            {"gateway": gateway, "database": database},
            status=200 if gateway and database else 503,
        )

    async def diagnostics(self, request):
        async with self.db.pool.acquire(timeout=3) as c:
            row = await c.fetchrow("""SELECT
                (SELECT id FROM seasons WHERE status='active') AS season,
                (SELECT count(*) FROM rooms WHERE status NOT IN('closed','finished')) AS rooms,
                (SELECT count(*) FROM rooms WHERE status='needs_attention') AS rooms_needing_attention,
                (SELECT count(*) FROM polls WHERE status='active') AS polls,
                (SELECT count(*) FROM matches) AS matches,
                (SELECT count(*) FROM role_jobs) AS pending_roles,
                (SELECT count(*) FROM thread_jobs) AS pending_memberships""")
        return web.json_response(
            {
                **dict(row),
                "pool_size": self.db.pool.get_size(),
                "pool_idle": self.db.pool.get_idle_size(),
            }
        )

    async def close(self):
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
            await asyncio.gather(self.heartbeat_task, return_exceptions=True)
            self.heartbeat_task = None
        for name in list(self.extensions):
            await self.unload_extension(name)
        await super().close()
        if self.health_runner:
            await self.health_runner.cleanup()
            self.health_runner = None
        if self.db.pool:
            try:
                await self.db.release()
            finally:
                await self.db.close()
                self.db.pool = None

    async def on_ready(self):
        log.info(
            "gateway_ready user_id=%s latency_ms=%.2f",
            self.user.id,
            self.latency * 1000,
        )

    async def on_disconnect(self):
        log.warning("gateway_disconnected")

    async def on_resumed(self):
        log.info("gateway_resumed")

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        cause = getattr(error, "original", error)
        log.error(
            "text_command_error command=%s type=%s sqlstate=%s",
            getattr(ctx.command, "name", "unknown"),
            type(cause).__name__,
            getattr(cause, "sqlstate", ""),
            exc_info=(type(cause), cause, cause.__traceback__),
        )
        await ctx.send(
            str(cause)
            if isinstance(
                cause, (ValueError, commands.CheckFailure, commands.UserInputError)
            )
            else "No se pudo completar la operación. Consulta los logs del servicio."
        )


def create_bot(settings):
    bot = MyBot(settings)

    @bot.tree.error
    async def slash_error(interaction, error):
        cause = getattr(error, "original", error)
        log.error(
            "slash_command_error command=%s type=%s sqlstate=%s",
            getattr(interaction.command, "name", "unknown"),
            type(cause).__name__,
            getattr(cause, "sqlstate", ""),
            exc_info=(type(cause), cause, cause.__traceback__),
        )
        message = (
            str(cause)
            if isinstance(cause, (ValueError, app_commands.CheckFailure))
            else "No se pudo completar la operación. Consulta los logs del servicio."
        )
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    return bot


async def run():
    load_dotenv()
    configure()
    settings = Settings.load()
    if not settings.token or not settings.guild_id:
        raise ValueError("DISCORD_TOKEN y GUILD_ID obligatorios")
    bot = create_bot(settings)
    loop = asyncio.get_running_loop()
    # POSIX systemd/container SIGTERM; asyncio.run handles SIGINT on Windows.
    try:
        loop.add_signal_handler(
            signal.SIGTERM, lambda: asyncio.create_task(bot.close())
        )
    except NotImplementedError:
        pass
    async with bot:
        await bot.start(settings.token, reconnect=True)
    if bot.failure:
        raise bot.failure


if __name__ == "__main__":
    asyncio.run(run())
