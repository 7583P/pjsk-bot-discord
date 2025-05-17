import discord
from discord.ext import commands
from discord import app_commands
import os

GUILD_ID = int(os.getenv("GUILD_ID", "0"))

class Ping(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.command(name="ping", description="Verifica si el bot responde")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message("🏓 Pong")

async def setup(bot: commands.Bot):
    await bot.add_cog(Ping(bot))
