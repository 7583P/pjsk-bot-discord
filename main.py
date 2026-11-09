import os
from pathlib import Path
import asyncio
import logging
import signal

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
from lounge.connections import create_pool, bind_bot_pool
from lounge.config import Settings, resolve_ca_file
from lounge.lease import RuntimeLease
from lounge.logging import configure
from lounge.health import BotHealth
from lounge.topology import staging_topology
from lounge import perf

# -------------------------
# Environment
# -------------------------
load_dotenv()

HERE = Path(__file__).resolve().parent
ca_candidate = HERE / "certs" / "supabase_ca_bundle.crt"
if ca_candidate.exists() and not os.environ.get("DB_CA_FILE"):
    os.environ["DB_CA_FILE"] = str(ca_candidate.resolve())

TOKEN = os.getenv("DISCORD_TOKEN")
APP_ID = int(os.getenv("APPLICATION_ID", "0") or "0")
GUILD_ID = os.getenv("GUILD_ID", "")
LOG_CHANNEL_ID = int(
    os.getenv("LOG_CHANNEL_ID", "1451649796971692104") or "1451649796971692104"
)

configure()
logger = logging.getLogger("bot")

# -------------------------
# Discord Bot
# -------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True


class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
            application_id=APP_ID,
            help_command=None,
        )
        self.db_pool = None
        self.lease = None
        self.lease_task = None
        self.lease_owner = None
        self.runtime_failure = None
        self.health = None

    async def _send_log(self, message: str):
        try:
            channel = await self.fetch_channel(LOG_CHANNEL_ID)
            if channel:
                await channel.send(message)
        except Exception as e:
            logger.warning("No se pudo enviar log a Discord: %s", e)

    async def setup_hook(self):
        staging_topology()
        self.db_pool = await create_pool()
        self.lease = RuntimeLease(self.db_pool)
        await self.lease.claim()
        self.lease_owner = self.lease.owner
        self.lease_task = asyncio.create_task(self._heartbeat(), name="runtime-lease")
        bind_bot_pool(self.db_pool)
        cogs = [
            "cogs.matchmaking",
            "cogs.rooms",
            "cogs.reroll",
            "cogs.autoroles",
            "cogs.players",
        ]

        loaded = []
        failed = []

        for cog in cogs:
            try:
                await self.load_extension(cog)
                loaded.append(cog)
                logger.info("✅ Cog cargado: %s", cog)
            except Exception as e:
                failed.append((cog, e))
                logger.exception("❌ Error cargando %s: %s", cog, e)

        if loaded:
            await self._send_log("✅ Cogs loaded: " + ", ".join(loaded))

        if failed:
            msg = "\n".join([f"❌ Error cargando {cog}: {err}" for cog, err in failed])
            await self._send_log(msg)
            raise RuntimeError("Required cogs failed to load; refusing partial startup")
        perf.install_discord_hooks(self)
        try:
            self.health = BotHealth(self)
            await self.health.start(int(os.getenv("BOT_HEALTH_PORT", "8081")))
            logger.info("BotHealth iniciado en 127.0.0.1:%s", os.getenv("BOT_HEALTH_PORT", "8081"))
        except Exception as e:
            logger.warning("No se pudo iniciar BotHealth en puerto %s: %s (no crítico, continuando)", os.getenv("BOT_HEALTH_PORT", "8081"), e)
            self.health = None

    async def close(self):
        if self.lease_task:
            self.lease_task.cancel()
            await asyncio.gather(self.lease_task, return_exceptions=True)
            self.lease_task = None
        try:
            if self.health:
                try:
                    await self.health.close()
                except Exception:
                    logger.exception("health_shutdown_failed")
            for extension in list(self.extensions):
                try:
                    await self.unload_extension(extension)
                except Exception:
                    logger.exception("cog_unload_failed")
            await super().close()
        finally:
            bind_bot_pool(None)
            if self.db_pool is not None:
                try:
                    if self.lease:
                        await self.lease.release()
                finally:
                    await self.db_pool.close()
                    self.db_pool = None

    async def _heartbeat(self):
        try:
            while True:
                await asyncio.sleep(10)
                await self.lease.heartbeat()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.runtime_failure = RuntimeError(
                f"Database lease lost; supervisor restart required ({e})"
            )
            logger.exception("runtime_lease_lost")
            try:
                await self._send_log(f"⚠️ Lease de base de datos perdida: `{e}`. Reiniciando...")
            except Exception:
                pass
            asyncio.create_task(self.close())

    async def on_ready(self):
        logger.info(
            "🤖 Conectado como %s (ID: %s)",
            self.user,
            self.user.id if self.user else "?",
        )
        try:
            await self._send_log(f"🤖 **Bot Conectado y Online** como `{self.user}` (ID: `{self.user.id if self.user else '?'}`)")
        except Exception as e:
            logger.warning("No se pudo enviar log de on_ready: %s", e)

        if GUILD_ID:
            try:
                guild = discord.Object(id=int(GUILD_ID))
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                logger.info("Slash commands sincronizados en guild %s (%d): %s", GUILD_ID, len(synced), [c.name for c in synced])
                try:
                    await self._send_log(f"📋 Slash commands sincronizados en guild {GUILD_ID} ({len(synced)}): {', '.join([c.name for c in synced])}")
                except Exception:
                    pass
            except Exception as e:
                logger.warning("Aviso sincronizando slash commands: %s", e)
                try:
                    await self._send_log(f"⚠️ Aviso sincronizando slash commands: `{e}`")
                except Exception:
                    pass

bot = MyBot()


@bot.command(name="sync")
async def sync_commands(ctx: commands.Context):
    if ctx.author.id not in [878310498720940102, 1364401080661708892]:
        return await ctx.reply("❌ No tienes permiso para ejecutar este comando.")

    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            synced = await bot.tree.sync(guild=guild)
            await ctx.send(
                f"📋 Slash commands sincronizados en guild {GUILD_ID}: "
                f"{[c.name for c in synced]}"
            )
        else:
            synced = await bot.tree.sync()
            await ctx.send(f"📋 Slash commands globales: {[c.name for c in synced]}")
    except Exception as e:
        logger.exception("Error al sincronizar comandos: %s", e)
        await ctx.send(f"❗ Error al sincronizar comandos: {e}")


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    perf.count("cmd_errors")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ Error: {error}", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"❌ Error: {error}", ephemeral=True
            )
    except Exception as e:
        logger.warning("⚠️ Falló el envío del mensaje de error: %s", e)


async def run_bot_async():
    """
    Esta función es importada por main.py/run.py.
    No levanta FastAPI. El servidor HTTP vive en main.py.
    """
    Settings.load()
    if not TOKEN:
        raise ValueError("DISCORD_TOKEN is required for the bot process")

    loop = asyncio.get_running_loop()
    handles_term = False
    try:
        try:
            loop.add_signal_handler(
                signal.SIGTERM, lambda: asyncio.create_task(bot.close())
            )
            handles_term = True
        except NotImplementedError:
            pass  # Windows console uses asyncio.run's interrupt handling.
        async with bot:
            await bot.start(TOKEN, reconnect=True)
    except BaseException as e:
        logger.exception("FATAL ERROR EN BOT START: %s", e)
        import datetime, traceback
        tb = traceback.format_exc()
        try:
            with open("crash.log", "a", encoding="utf-8") as f:
                f.write(f"\n--- CRASH {datetime.datetime.now(datetime.timezone.utc).isoformat()} ---\n")
                f.write(tb)
        except Exception:
            pass
        try:
            import httpx
            log_url = f"https://discord.com/api/v10/channels/{LOG_CHANNEL_ID}/messages"
            content = f"💥 **CRASH FATAL**: `{type(e).__name__}: {e}`\n```\n{tb[-1500:]}\n```"
            httpx.post(log_url, headers={"Authorization": f"Bot {TOKEN}"}, json={"content": content}, timeout=5.0)
        except Exception:
            pass
        raise
    finally:
        if handles_term:
            loop.remove_signal_handler(signal.SIGTERM)
    if bot.runtime_failure:
        raise bot.runtime_failure


if __name__ == "__main__":
    asyncio.run(run_bot_async())
