import os
import sys
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
APP_ID = int(os.getenv("APPLICATION_ID", "0"))
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

# Intents
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True  # Necesario para on_member_join

# Bot
class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
            application_id=APP_ID,
            help_command=None
        )

    async def setup_hook(self):
        # Cargar Matchmaking
        try:
            await self.load_extension("cogs.matchmaking")
            print("✅ Cog cargado: cogs.matchmaking")
        except Exception as e:
            print(f"❌ Error cargando cogs.matchmaking: {e}")

        # Cargar AutoRoles
        try:
            await self.load_extension("cogs.autoroles")
            print("✅ Cog cargado: cogs.autoroles")
        except Exception as e:
            print(f"❌ Error cargando cogs.autoroles: {e}")

        # Sincronizar comandos slash al guild
        try:
            guild = discord.Object(id=GUILD_ID)
            synced = await self.tree.sync(guild=guild)
            print(f"📋 Comandos registrados: {[cmd.name for cmd in synced]}")
        except Exception as e:
            print(f"❗ Error al sincronizar comandos: {e}")

    async def on_ready(self):
        print(f"🤖 Conectado como {self.user} (ID: {self.user.id})")

bot = MyBot()

# Manejo de errores globales
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ Error: {error}", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ Error: {error}", ephemeral=True)
    except Exception as e:
        print(f"⚠️ Falló el envío del mensaje de error: {e}")

# Arrancar bot
if __name__ == "__main__":
    if not TOKEN:
        print("❌ DISCORD_TOKEN no definido.")
        sys.exit(1)
    bot.run(TOKEN)
