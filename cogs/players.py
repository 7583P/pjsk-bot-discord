import os
import re
import asyncpg
from datetime import datetime
import calendar
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

    def _month_bounds_utc(today: datetime.date) -> tuple[datetime.date, datetime.date]:
        start = today.replace(day=1)
        last_day = calendar.monthrange(today.year, today.month)[1]
        end = today.replace(day=last_day)
        return start, end

    async def _ensure_current_season(pool: asyncpg.Pool) -> tuple[int, int | None, bool]:
        """
        Returns (current_season_id, previous_active_season_id, created_new_flag).
        Creates a new active season for the *current month* if none active covers today.
        """
        today = datetime.date.today()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # find active season covering today
                cur = await conn.fetchrow("""
                    SELECT id FROM seasons
                    WHERE active = true
                    AND startdate <= CURRENT_DATE
                    AND enddate   >= CURRENT_DATE
                    ORDER BY startdate DESC
                    LIMIT 1
                """)
                if cur:
                    current_id = cur["id"]
                    # previous (most recent other) season
                    prev = await conn.fetchrow("""
                        SELECT id FROM seasons
                        WHERE id <> $1
                        ORDER BY enddate DESC NULLS LAST, startdate DESC
                        LIMIT 1
                    """, current_id)
                    return current_id, (prev["id"] if prev else None), False

                # need to create a new season for current month
                startdate, enddate = _month_bounds_utc(today)
                # deactivate any currently active seasons (if any)
                await conn.execute("UPDATE seasons SET active = false WHERE active = true")
                # create new active season (temporary name)
                row = await conn.fetchrow("""
                    INSERT INTO seasons (name, active, startdate, enddate)
                    VALUES ('', true, $1, $2)
                    RETURNING id
                """, startdate, enddate)
                new_id = row["id"]
                # set canonical name "Season {id}"
                await conn.execute("UPDATE seasons SET name = $1 WHERE id = $2",
                                f"Season {new_id}", new_id)
                # previous season = most recent by enddate (now all inactive)
                prev = await conn.fetchrow("""
                    SELECT id FROM seasons
                    WHERE id <> $1
                    ORDER BY enddate DESC NULLS LAST, startdate DESC
                    LIMIT 1
                """, new_id)
                prev_id = prev["id"] if prev else None
                return new_id, prev_id, True

    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.command(name="newseason", description="Setup new season (create if needed, migrate players, sync roles)")
    async def newseason(self, interaction: discord.Interaction):
        # 1) Ensure current season exists (create if needed)
        try:
            current_season_id, prev_season_id, created = await _ensure_current_season(self.db_pool)
        except Exception as e:
            return await interaction.response.send_message(f"❌ DB error ensuring season: {e}", ephemeral=True)

        # 2) If there is a previous season, copy players into current (only new ones)
        migrated = 0
        try:
            async with self.db_pool.acquire() as conn:
                async with conn.transaction():
                    if prev_season_id is not None:
                        # Insert missing players from previous season
                        migrated = await conn.execute("""
                            INSERT INTO seasonplayers (user_id, season_id, points, mmr, role, synced)
                            SELECT sp.user_id, $1, 0, 0, 'Placement', false
                            FROM seasonplayers sp
                            WHERE sp.season_id = $2
                            AND NOT EXISTS (
                                SELECT 1 FROM seasonplayers x
                                WHERE x.user_id = sp.user_id AND x.season_id = $1
                            )
                        """, current_season_id, prev_season_id)
                        # asyncpg returns "INSERT 0 N" → parse N
                        if isinstance(migrated, str) and migrated.startswith("INSERT"):
                            try:
                                migrated = int(migrated.split()[-1])
                            except Exception:
                                migrated = 0
                    else:
                        migrated = 0
        except Exception as e:
            return await interaction.response.send_message(f"❌ DB error migrating players: {e}", ephemeral=True)

        # 3) Sync Discord roles for players in current season where synced=false, then mark synced=true
        synced = 0
        try:
            guild = interaction.guild
            if guild is None:
                return await interaction.response.send_message("❌ This command must be used inside a server.", ephemeral=True)

            placement_role = guild.get_role(PLACEMENT_ROLE_ID)

            async with self.db_pool.acquire() as conn:
                # fetch batch to sync
                rows = await conn.fetch("""
                    SELECT sp.user_id, sp.role
                    FROM seasonplayers sp
                    WHERE sp.season_id = $1
                    AND sp.synced = false
                """, current_season_id)

                # assign roles in Discord
                rank_role_ids = set(RANK_ROLE_IDS.values()) | {PLACEMENT_ROLE_ID}
                for r in rows:
                    uid = int(r["user_id"])
                    role_name = r["role"] or "Placement"
                    member = guild.get_member(uid)
                    if member is None:
                        # member not in guild or not cached — skip silently
                        continue

                    # target role
                    if role_name == "Placement":
                        target_role = placement_role
                    else:
                        target_role = guild.get_role(RANK_ROLE_IDS.get(role_name, 0))

                    if not target_role:
                        # if role missing in guild, skip this user
                        continue

                    # keep non-rank roles; add target
                    keep = [rr for rr in member.roles if rr.id not in rank_role_ids]
                    new_roles = list(dict.fromkeys(keep + [target_role]))
                    try:
                        await member.edit(roles=new_roles, reason=f"Season {current_season_id} sync")
                        synced += 1
                    except (discord.Forbidden, discord.HTTPException):
                        # insufficient perms or hierarchy — skip
                        continue

                # mark synced=true for everyone we attempted where they now have the role
                # (to keep logic simple, set synced=true for all rows processed this run)
                await conn.execute("""
                    UPDATE seasonplayers
                    SET synced = true
                    WHERE season_id = $1 AND synced = false
                """, current_season_id)

        except Exception as e:
            return await interaction.response.send_message(f"⚠️ Partial success (season ok). Role sync error: {e}", ephemeral=True)

        # 4) Final message
        created_text = "created" if created else "already existed"
        await interaction.response.send_message(
            f"✅ Season {current_season_id} {created_text}. "
            f"Migrated **{migrated}** players from previous season. "
            f"Synchronized roles for **{synced}** players.",
            ephemeral=True
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(PlayersCog(bot))
