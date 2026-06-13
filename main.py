import os
import asyncio
import logging

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# -------------------------
# Environment
# -------------------------
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
APP_ID = int(os.getenv("APPLICATION_ID", "0") or "0")
GUILD_ID = os.getenv("GUILD_ID", "")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "1451649796971692104") or "1451649796971692104")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
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

    async def _send_log(self, message: str):
        try:
            channel = await self.fetch_channel(LOG_CHANNEL_ID)
            if channel:
                await channel.send(message)
        except Exception as e:
            logger.warning("No se pudo enviar log a Discord: %s", e)

    async def setup_hook(self):
        cogs = [
            "cogs.matchmaking",
            "cogs.rooms",
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

    async def on_ready(self):
        logger.info("🤖 Conectado como %s (ID: %s)", self.user, self.user.id if self.user else "?")


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
    try:
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ Error: {error}", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ Error: {error}", ephemeral=True)
    except Exception as e:
        logger.warning("⚠️ Falló el envío del mensaje de error: %s", e)


async def run_bot_async():
    """
    Esta función es importada por main.py/run.py.
    No levanta FastAPI. El servidor HTTP vive en main.py.
    """
    if not TOKEN:
        logger.error("❌ DISCORD_TOKEN no definido.")
        # No matamos el proceso completo para que HTTP pueda seguir vivo.
        while True:
            await asyncio.sleep(3600)

    await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(run_bot_async())
