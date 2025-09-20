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

async def get_current_season():
    sql = """ 
    SELECT ID, Name, Active, StartDate, EndDate from seasons where active = true
    """;
    conn = await get_conn()
    try:
        row = await conn.fetchrow(sql)
        return row  # asyncpg.Record (dict-like object)
    finally:
        await conn.close()

async def get_all_seasons():
    sql = """ 
    SELECT id, name, active, startdate, enddate 
    FROM seasons
    ORDER BY startdate DESC
    """
    conn = await get_conn()
    try:
        rows = await conn.fetch(sql)
        return rows  # list of asyncpg.Record
    finally:
        await conn.close()

# Inserta o actualiza un jugador (comando /register)
async def upsert_player(user_id: int, name: str, country: str):
    season = await get_current_season()
    if not season:
        raise RuntimeError("No active season found")
    season_id = season["id"]

    # Upsert player and get players.id (surrogate PK)
    sql_players = """
        INSERT INTO players (user_id, name, country)
        VALUES ($1, $2, $3)
        ON CONFLICT (user_id) DO UPDATE SET
          name = EXCLUDED.name,
          country = EXCLUDED.country
        RETURNING user_id
    """

    # seasonplayers has (player_id, season_id, ...) with a unique constraint on that pair
    sql_seasonplayers = """
        INSERT INTO seasonplayers (user_id, season_id, points, mmr, role)
        VALUES ($1, $2, 0, 0, 'Placement')
        ON CONFLICT (user_id, season_id) DO UPDATE SET
          season_id = EXCLUDED.season_id
    """

    conn = await get_conn()
    try:
        async with conn.transaction():
            player_row = await conn.fetchrow(sql_players, user_id, name, country)
            player_id = player_row["user_id"]
            await conn.execute(sql_seasonplayers, player_id, season_id)
    finally:
        await conn.close()

# Actualiza solo el nombre y temporada, sin tocar country
async def update_player_name(user_id: int, name: str):    
    sql = """
    INSERT INTO players(user_id, name)
    VALUES ($1, $2)
    ON CONFLICT (user_id) DO UPDATE SET
      name   = EXCLUDED.name
    """
    conn = await get_conn()
    try:
        await conn.execute(sql, user_id, name)
    finally:
        await conn.close()

# Obtiene el nombre a mostrar con prioridad: nickname > show name > username global

def get_member_name(member: discord.Member) -> str:
    if member.nick:
        return member.nick
    if getattr(member, 'global_name', None):
        return member.global_name
    return member.name

# Sincronización inicial
async def upsert_player_load(user_id: int, name: str):
    await update_player_name(user_id, name)

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
                        name = get_member_name(member)
                        await upsert_player_load(member.id, name)
                        total += 1
            print(f"⚙️ Tabla sincronizada con {total} miembros existentes.")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if not member.bot:
            name = get_member_name(member)
            await upsert_player(member.id, name, "")
            if member.guild.system_channel:
                await member.guild.system_channel.send(
                    f"Welcome {member.mention}! Excecute `/register <ISO2>` in #register to see your country in the page"
                )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        prev = get_member_name(before)
        curr = get_member_name(after)
        if prev != curr:
            await update_player_name(after.id, curr)

    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.command(name="register", description="Register your country (código ISO2)")
    @app_commands.describe(country="ISO2 2 letters code (p.ej., PE)")
    async def register(self, interaction: discord.Interaction, country: str):
        code = country.strip().upper()
        if not re.match(r'^[A-Z]{2}$', code):
            return await interaction.response.send_message(
                "You should especify a 2 letter ISO2 code counrty", ephemeral=True
            )
        name = get_member_name(interaction.user)
        await upsert_player(interaction.user.id, name, code)
        await interaction.response.send_message(
            f"✅ {name}, Country registered **{code}**.",
            ephemeral=True
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(PlayersCog(bot))
