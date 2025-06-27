import os
import sys
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# Cargar variables de entorno\load_dotenv()
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
APP_ID = int(os.getenv("APPLICATION_ID", "0"))
GUILD_ID = os.getenv("GUILD_ID", "")

# Configurar intents
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True  # Necesario para on_member_join

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
            application_id=APP_ID,
            help_command=None
        )

    async def setup_hook(self):
        # Cargar Matchmaking Cog
        try:
            await self.load_extension("cogs.matchmaking")
            print("✅ Cog cargado: cogs.matchmaking")
        except Exception as e:
            print(f"❌ Error cargando cogs.matchmaking: {e}")
             # Cargar Rooms Cog
        try:
            await self.load_extension("cogs.rooms")
            print("✅ Cog cargado: cogs.rooms")
        except Exception as e:
            print(f"❌ Error cargando cogs.rooms: {e}")


        # Cargar AutoRoles Cog
        try:
            await self.load_extension("cogs.autoroles")
            print("✅ Cog cargado: cogs.autoroles")
        except Exception as e:
            print(f"❌ Error cargando cogs.autoroles: {e}")

        # Cargar Players Cog (on_member_join + /register)
        try:
            await self.load_extension("cogs.players")
            print("✅ Cog cargado: cogs.players")
        except Exception as e:
            print(f"❌ Error cargando cogs.players: {e}")

        # Sincronizar comandos slash
        try:
            if GUILD_ID:
                guild = discord.Object(id=int(GUILD_ID))
                synced = await self.tree.sync(guild=guild)
                print(f"📋 Slash commands sincronizados en guild {GUILD_ID}: {[c.name for c in synced]}")
            else:
                synced = await self.tree.sync()
                print(f"📋 Slash commands globales: {[c.name for c in synced]}")
        except Exception as e:
            print(f"❗ Error al sincronizar comandos: {e}")

    async def on_ready(self):
        print(f"🤖 Conectado como {self.user} (ID: {self.user.id})")

# Instanciar bot
bot = MyBot()

# Manejo de errores globales para comandos slash
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

async def run_bot_async():
    if not TOKEN:
        print("❌ DISCORD_TOKEN no definido.")
        return
    await bot.start(TOKEN)