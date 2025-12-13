import os
import re
import asyncpg
import datetime as dt
import calendar
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
DATABASE_URL = os.getenv("DB_HOST")  # Formato: postgresql://user:pass@host:port/dbname

# Diccionario con todos los roles de rango (Discord Role IDs)
RANK_ROLE_IDS = {
    "Placement":    1371321594068336811,
    "Iron":        1394444407536881845,   
    "Iron ⬆":       1446954780915273778,
    "Bronze":        1371324225838645339,
    "Bronze ⬆":        1433827677713858732,
    "Silver":        1389343997100560514,
    "Silver ⬆":        1433827565222494383,
    "Gold":          1371324328708149328,
    "Gold ⬆":          1433827466744561720,
    "Platinum":      1389343805521789098,
    "Platinum ⬆":    1434564540619554960,
    "Diamond":      1371324561542484108,
    "Diamond ⬆":    1440477553151184916,
    "Crystal":       1394445724703527012,
    "Crystal ⬆":   1440477788501966858,
    "Master":       1394635883466199110,
    "Master ⬆":   1440478019012530267,
    "Champion":      1371323543501144115,
    "Champion ⬆":      1449495589153935421,
    "Grand Champion": 1394444744892874832,
    "Grand Champion ⬆": 1449495651535552729,
    "Legend":        1371323380510363749,
}



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
        self.db_pool: asyncpg.Pool | None = None

    async def cog_load(self):
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL env var not set")
        self.db_pool = await asyncpg.create_pool(DATABASE_URL)
        await self.bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        print("[PlayersCog] asyncpg pool ready.")        
    
    async def cog_unload(self):
        if self.db_pool:
            await self.db_pool.close()
            self.db_pool = None
        print("[PlayersCog] asyncpg pool closed.")
        
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
                    f"Welcome {member.mention}! Excecute `/register <COUNTRY IN 2 LETTERS>` in #set_your_country to see your country in the page"
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

    @staticmethod
    def _month_bounds_utc(today: dt.date) -> tuple[dt.date, dt.date]:
        start = today.replace(day=1)
        last_day = calendar.monthrange(today.year, today.month)[1]
        end = today.replace(day=last_day)
        return start, end
    
    async def _ensure_current_season(self) -> tuple[int, int | None, bool]:
        """
        Returns (current_season_id, previous_active_season_id, created_new_flag).
        Creates a new active season for the *current month* if none active covers today.
        """
        pool = self.db_pool        
        today = dt.date.today()
        startdate, enddate = self._month_bounds_utc(today)
        async with pool.acquire() as conn:
            async with conn.transaction():
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
                    prev = await conn.fetchrow("""
                        SELECT id FROM seasons
                        WHERE id <> $1
                        ORDER BY enddate DESC NULLS LAST, startdate DESC
                        LIMIT 1
                    """, current_id)
                    return current_id, (prev["id"] if prev else None), False

                # create a new season spanning the current month
                startdate, enddate = self._month_bounds_utc(today)
                await conn.execute("UPDATE seasons SET active = false WHERE active = true")
                row = await conn.fetchrow("""
                    INSERT INTO seasons (name, active, startdate, enddate)
                    VALUES ('', true, $1, $2)
                    RETURNING id
                """, startdate, enddate)
                new_id = row["id"]
                await conn.execute("UPDATE seasons SET name = $1 WHERE id = $2",
                                   f"Season {new_id}", new_id)
                prev = await conn.fetchrow("""
                    SELECT id FROM seasons
                    WHERE id <> $1
                    ORDER BY enddate DESC NULLS LAST, startdate DESC
                    LIMIT 1
                """, new_id)
                prev_id = prev["id"] if prev else None
                return new_id, prev_id, True
            
    async def _apply_rank_role(self, guild: discord.Guild, member_or_id, role_name: str, reason: str = "") -> bool:
        rank_role_ids = set(RANK_ROLE_IDS.values())

        # Resolver member
        member = member_or_id if isinstance(member_or_id, discord.Member) else guild.get_member(int(member_or_id))
        if member is None:
            # Como fallback, intenta fetch_member (puede fallar si no está en el guild)
            try:
                member = await guild.fetch_member(int(member_or_id))
            except Exception:
                return False

        # Normaliza y resuelve rol objetivo (fallback a Placement)
        role_name = (role_name or "Placement").strip()
        if role_name not in RANK_ROLE_IDS:
            role_name = "Placement"

        target_role = guild.get_role(RANK_ROLE_IDS[role_name])
        if not target_role:
            # Si el rol no existe en el servidor, cae a Placement si existe; si no, no se puede asignar.
            target_role = guild.get_role(RANK_ROLE_IDS["Placement"])
            if not target_role:
                return False

        # Mantener roles NO-rango y agregar el nuevo rango
        keep = [r for r in member.roles if r.id not in rank_role_ids]
        new_roles = list(dict.fromkeys(keep + [target_role]))

        try:
            await member.edit(roles=new_roles, reason=reason or f"Apply rank {role_name}")
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False


    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.command(name="newseason", description="Setup new season (create if needed, migrate players, sync roles)")
    async def newseason(self, interaction: discord.Interaction):
        # Permitir solo a un usuario específico
        if interaction.user.id != 878310498720940102:
            return await interaction.response.send_message(
                "❌ No tienes permiso para ejecutar este comando.", ephemeral=True
            )

        # 1) Ensure current season exists (create if needed)
        try:
            current_season_id, prev_season_id, created = await self._ensure_current_season()
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


            async with self.db_pool.acquire() as conn:
                # fetch batch to sync
                rows = await conn.fetch("""
                    SELECT sp.user_id, sp.role
                    FROM seasonplayers sp
                    WHERE sp.season_id = $1
                    AND sp.synced = false
                """, current_season_id)

                synced = 0
                for r in rows:
                    uid = int(r["user_id"])
                    role_name = (r["role"] or "Placement").strip()
                    ok = await self._apply_rank_role(
                        guild,
                        uid,
                        role_name,
                        reason=f"Season {current_season_id} sync"
                    )
                    if ok:
                        synced += 1

                # marcar synced=true para todos los procesados
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
