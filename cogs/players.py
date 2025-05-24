import os
import aiosqlite
from datetime import datetime
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# Cargar GUILD_ID para comando manual
load_dotenv()
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

# Ruta a la base de datos
DB_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "matchmaking.db")

# Calcula la temporada actual ('beta' o número cada 3 meses)
def get_current_season_label() -> str:
    start = datetime(2025, 5, 16)
    now = datetime.now()
    months = (now.year - start.year) * 12 + (now.month - start.month)
    period = months // 3
    return "beta" if period == 0 else str(period)

# Inserta o actualiza un jugador en la tabla
async def upsert_player(user_id: int, name: str, country: str):
    season = get_current_season_label()
    sql = """
    INSERT INTO players(user_id, name, mmr, role, country, season)
    VALUES (?, ?, 0, 'Placement', ?, ?)
    ON CONFLICT(user_id) DO UPDATE SET
      name    = excluded.name,
      country = excluded.country,
      season  = excluded.season
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(sql, (user_id, name, country, season))
        await db.commit()

async def upsert_player_load(user_id: int, name: str, country: str):
    season = get_current_season_label()
    sql = """
    INSERT INTO players(user_id, name, mmr, role, country, season)
    VALUES (?, ?, 0, 'Placement', ?, ?)
    ON CONFLICT(user_id) DO UPDATE SET
      name    = excluded.name,      
      season  = excluded.season
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(sql, (user_id, name, country, season))
        await db.commit()

class PlayersCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._synced = False

    @commands.Cog.listener()
    async def on_ready(self):
        if not self._synced:
            self._synced = True
            total = 0
            for guild in self.bot.guilds:
                await guild.chunk()
                for member in guild.members:
                    if not member.bot:                        
                        await upsert_player_load(member.id, member.display_name, "")
                        total += 1
            print(f"⚙️ Tabla sincronizada con {total} miembros existentes.")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if not member.bot:
            await upsert_player(member.id, member.display_name, "")
            canal = member.guild.system_channel
            if canal:
                await canal.send(
                    f"👋 Bienvenido {member.mention}! Ejecuta `/register <ISO2>` para guardar tu país."
                )

    @app_commands.command(
        name="register",
        description="Registra tu país (código ISO2) para aparecer en PJSK LOUNGE"
    )
    @app_commands.describe(country="Código ISO2 de tu país (por ejemplo, PE, US, FR)")
    async def register(self, interaction: discord.Interaction, country: str):
        code = country.strip().upper()
        if len(code) != 2 or not code.isalpha():
            return await interaction.response.send_message(
                "❌ Debes indicar un código ISO de dos letras.", ephemeral=True
            )
        await upsert_player(
            user_id=interaction.user.id,
            name=interaction.user.display_name,
            country=code
        )
        await interaction.response.send_message(
            f"✅ {interaction.user.display_name}, país registrado **{code}**.",
            ephemeral=True
        )

async def setup(bot: commands.Bot):
    cog = PlayersCog(bot)
    await bot.add_cog(cog)
    # Registrar manualmente el comando en el árbol de comandos
    bot.tree.add_command(cog.register, guild=discord.Object(id=GUILD_ID))

