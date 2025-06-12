import os
import re
import asyncpg
from datetime import datetime
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
DATABASE_URL = os.getenv("DB_HOST")  # Formato: postgresql://user:pass@host:port/dbname

# Calcula la temporada actual ('beta' o número cada 3 meses)
def get_current_season_label() -> str:
    start = datetime(2025, 5, 16)
    now = datetime.now()
    months = (now.year - start.year) * 12 + (now.month - start.month)
    period = months // 3
    return "beta" if period == 0 else str(period)

# Crea una conexión temporal a PostgreSQL (usa un pool en producción)
async def get_conn():
    return await asyncpg.connect(DATABASE_URL)

# Inserta o actualiza un jugador (comando /register)
async def upsert_player(user_id: int, name: str, country: str):
    season = get_current_season_label()
    sql = """
    INSERT INTO players(user_id, name, mmr, role, country, season)
    VALUES ($1, $2, 0, 'Placement', $3, $4)
    ON CONFLICT (user_id) DO UPDATE SET
      name    = EXCLUDED.name,
      country = EXCLUDED.country,
      season  = EXCLUDED.season
    """
    conn = await get_conn()
    try:
        await conn.execute(sql, user_id, name, country, season)
    finally:
        await conn.close()

# Sincronización inicial sin sobreescribir country
async def upsert_player_load(user_id: int, name: str):
    season = get_current_season_label()
    sql = """
    INSERT INTO players(user_id, name, mmr, role, country, season)
    VALUES ($1, $2, 0, 'Placement', '', $3)
    ON CONFLICT (user_id) DO UPDATE SET
      name    = EXCLUDED.name,
      season  = EXCLUDED.season
    """
    conn = await get_conn()
    try:
        await conn.execute(sql, user_id, name, season)
    finally:
        await conn.close()

class PlayersCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.synced = False

    async def cog_load(self):
        await self.bot.tree.sync(guild=discord.Object(id=GUILD_ID))

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.synced:
            self.synced = True
            total = 0
            for guild in self.bot.guilds:
                await guild.chunk()
                for member in guild.members:
                    if not member.bot:
                        await upsert_player_load(member.id, member.display_name)
                        total += 1
            print(f"⚙️ Tabla sincronizada con {total} miembros existentes.")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if not member.bot:
            await upsert_player(member.id, member.display_name, "")
            if member.guild.system_channel:
                await member.guild.system_channel.send(
                    f"Welcome {member.mention}Execute command `/register <ISO2>` into #register to see your country in the page"
                )

    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.command(name="register", description="Registra tu país (código ISO2)")
    @app_commands.describe(country="Código ISO2 de dos letras (p.ej., PE)")
    async def register(self, interaction: discord.Interaction, country: str):
        code = country.strip().upper()
        if not re.match(r'^[A-Z]{2}$', code):
            return await interaction.response.send_message(
                "❌ Debes indicar un código ISO de dos letras.", ephemeral=True
            )
        await upsert_player(interaction.user.id, interaction.user.display_name, code)
        await interaction.response.send_message(
            f"✅ {interaction.user.display_name}, país registrado **{code}**.",
            ephemeral=True
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(PlayersCog(bot))
