import os
import re
import asyncpg
import datetime as dt
import calendar
import discord
import pycountry
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from lounge.connections import get_bot_pool, player_connection

# Cargar variables de entorno
load_dotenv()
GUILD_ID = int(os.getenv("GUILD_ID", "1371306026917298267"))
DATABASE_URL = os.getenv("DB_HOST")  # Formato: postgresql://user:pass@host:port/dbname

# --- NEW: Season Placement / Season reset config ---
SEASON_PLACEMENT_ROLE_ID = 1470417525635551314
SEASON_RESET_PENALTY = 50  # -100 MMR (aplicado como seed cuando jueguen, no aquí)

# Diccionario con todos los roles de rango (Discord Role IDs)
RANK_ROLE_IDS = {
    "Placement": 1371321594068336811,
    "Iron": 1394444407536881845,
    "Iron ⬆": 1446954780915273778,
    "Bronze": 1371324225838645339,
    "Bronze ⬆": 1433827677713858732,
    "Silver": 1389343997100560514,
    "Silver ⬆": 1433827565222494383,
    "Gold": 1371324328708149328,
    "Gold ⬆": 1433827466744561720,
    "Platinum": 1389343805521789098,
    "Platinum ⬆": 1434564540619554960,
    "Diamond": 1371324561542484108,
    "Diamond ⬆": 1440477553151184916,
    "Crystal": 1394445724703527012,
    "Crystal ⬆": 1440477788501966858,
    "Master": 1394635883466199110,
    "Master ⬆": 1440478019012530267,
    "Champion": 1371323543501144115,
    "Grand Champion": 1449495589153935421,
    "Star": 1394444744892874832,
    "Divine Star": 1449495651535552729,
    "Legend": 1371323380510363749,
}


# Crea una conexión temporal a PostgreSQL (usa un pool en producción)
def get_conn():
    return player_connection()


async def get_current_season():
    sql = """
    SELECT ID, Name, Active, StartDate, EndDate
    FROM seasons
    WHERE active = true
    """
    async with player_connection() as conn:
        row = await conn.fetchrow(sql)
        return row  # asyncpg.Record (dict-like object)


async def get_all_seasons():
    sql = """
    SELECT id, name, active, startdate, enddate
    FROM seasons
    ORDER BY startdate DESC
    """
    async with player_connection() as conn:
        rows = await conn.fetch(sql)
        return rows  # list of asyncpg.Record


# Inserta o actualiza un jugador
async def upsert_player(user_id: int, name: str, country: str | None):
    # Upsert player and get players.id (surrogate PK)
    sql_players = """
        INSERT INTO players (user_id, name, country)
        VALUES ($1, $2, coalesce($3,''))
        ON CONFLICT (user_id) DO UPDATE SET
          name = EXCLUDED.name,
          country = coalesce($3, players.country)
        RETURNING user_id
    """

    # seasonplayers has (user_id, season_id, ...) with a unique constraint on that pair
    sql_seasonplayers = """
        INSERT INTO seasonplayers (user_id, season_id, points, mmr, role)
        VALUES ($1, $2, 0, 0, $3)
        ON CONFLICT (user_id, season_id) DO UPDATE SET
          season_id = EXCLUDED.season_id
    """

    async with player_connection() as conn:
        async with conn.transaction():
            current=await conn.fetchrow('SELECT id FROM seasons WHERE active FOR SHARE')
            if not current:raise RuntimeError('No active season found')
            season_id=current['id']
            policy=await conn.fetchval('SELECT placement_enabled FROM lounge_season_policy WHERE season_id=$1',season_id)
            initial_role='Iron' if policy is False else 'Placement'
            player_row = await conn.fetchrow(sql_players, user_id, name, country)
            player_id = player_row["user_id"]
            await conn.execute(sql_seasonplayers, player_id, season_id,initial_role)


# Actualiza solo el nombre y temporada, sin tocar country
async def update_player_name(user_id: int, name: str):
    sql = """
    INSERT INTO players(user_id, name)
    VALUES ($1, $2)
    ON CONFLICT (user_id) DO UPDATE SET
      name = EXCLUDED.name
    """
    async with player_connection() as conn:
        await conn.execute(sql, user_id, name)


# Obtiene el nombre a mostrar con prioridad: nickname > show name > username global
def get_member_name(member: discord.Member) -> str:
    if member.nick:
        return member.nick
    if getattr(member, "global_name", None):
        return member.global_name
    return member.name


# English-only aliases.
# Users must write the country name in English.
# Internally, we still save ISO2 so flags/page keep working.
COUNTRY_ALIASES = {
    "usa": "US",
    "us": "US",
    "u.s.": "US",
    "u.s.a.": "US",
    "united states": "US",
    "united states of america": "US",

    "uk": "GB",
    "u.k.": "GB",
    "united kingdom": "GB",
    "england": "GB",

    "south korea": "KR",
    "north korea": "KP",
    "russia": "RU",
    "vietnam": "VN",
    "taiwan": "TW",
    "turkey": "TR",
    "united arab emirates": "AE",
    "saudi arabia": "SA",
    "dominican republic": "DO",
    "south africa": "ZA",
    "new zealand": "NZ",
    "czech republic": "CZ",
    "czechia": "CZ",
    "ivory coast": "CI",
    "cote d'ivoire": "CI",
    "côte d'ivoire": "CI",
}


def normalize_country_text(value: str) -> str:
    return " ".join((value or "").strip().split()).lower()


def country_name_to_iso2(value: str) -> tuple[str | None, str | None]:
    raw = " ".join((value or "").strip().split())
    key = normalize_country_text(raw)

    if not key:
        return None, None

    # Do NOT accept ISO2 directly anymore.
    # User must write the full country name in English.
    if len(key) == 2 and key.isalpha():
        return None, None

    # English aliases
    if key in COUNTRY_ALIASES:
        code = COUNTRY_ALIASES[key]
        country = pycountry.countries.get(alpha_2=code)
        return code, country.name if country else raw

    # Exact match against official English country names from pycountry.
    # This is case-insensitive but does not translate Spanish names.
    for country in pycountry.countries:
        possible_names = [
            getattr(country, "name", ""),
            getattr(country, "official_name", ""),
            getattr(country, "common_name", ""),
        ]

        for name in possible_names:
            if name and normalize_country_text(name) == key:
                return country.alpha_2, country.name

    # Fuzzy match only for normal English names.
    # If user used accents not present in the official English name, it will not auto-convert Spanish.
    try:
        matches = pycountry.countries.search_fuzzy(raw)
        if matches:
            country = matches[0]
            return country.alpha_2, country.name
    except LookupError:
        pass

    return None, None


# Sincronización inicial
async def upsert_player_load(user_id: int, name: str):
    await update_player_name(user_id, name)


from lounge.topology import apply_staging
apply_staging(globals(),'players')


class PlayersCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.synced = False
        self.db_pool: asyncpg.Pool | None = None

    async def cog_load(self):
        self.db_pool = get_bot_pool()
        print("[PlayersCog] asyncpg pool ready.")

    async def cog_unload(self):
        if self.db_pool:
            self.db_pool = None
        print("[PlayersCog] asyncpg pool closed.")

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.synced:
            self.synced = True
            member_tuples = []
            for guild in self.bot.guilds:
                try:
                    await guild.chunk()
                except Exception:
                    pass
                for member in guild.members:
                    if not member.bot:
                        name = get_member_name(member)
                        member_tuples.append((member.id, name))
            if member_tuples and self.db_pool:
                try:
                    sql = """
                    INSERT INTO players(user_id, name)
                    VALUES ($1, $2)
                    ON CONFLICT (user_id) DO UPDATE SET
                      name = EXCLUDED.name
                    """
                    async with self.db_pool.acquire() as conn:
                        await conn.executemany(sql, member_tuples)
                    print(f"⚙️ Tabla sincronizada con {len(member_tuples)} miembros existentes en batch.")
                except Exception as e:
                    print(f"Aviso en sincronización inicial de miembros: {e}")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if not member.bot:
            name = get_member_name(member)
            await upsert_player(member.id, name, None)

            if member.guild.system_channel:
                await member.guild.system_channel.send(
                    f"Welcome {member.mention}! make sure to have a look to <#1441176972964270375> before playing."
                )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        prev = get_member_name(before)
        curr = get_member_name(after)
        if prev != curr:
            await update_player_name(after.id, curr)

    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.command(
        name="set_country",
        description="Register your country using its English name"
    )
    @app_commands.describe(
        country="Country name in English, for example: Peru, United States, Japan"
    )
    async def set_country(self, interaction: discord.Interaction, country: str):
        code, country_display_name = country_name_to_iso2(country)

        if not code:
            return await interaction.response.send_message(
                "❌ Country not found. Please write the country name in English, for example: `Peru`, `United States`, `Japan`.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True, thinking=True)
        name = get_member_name(interaction.user)  # type: ignore
        await upsert_player(interaction.user.id, name, code)  # type: ignore

        await interaction.followup.send(
            f"✅ {name}, country registered as **{country_display_name}** (`{code}`).",
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
        assert pool is not None, "DB pool not initialized"

        today = dt.date.today()
        startdate, enddate = self._month_bounds_utc(today)
        async with pool.acquire() as conn:
            async with conn.transaction():
                cur = await conn.fetchrow(
                    """
                    SELECT id FROM seasons
                    WHERE active = true
                      AND startdate <= CURRENT_DATE
                      AND enddate   >= CURRENT_DATE
                    ORDER BY startdate DESC
                    LIMIT 1
                    """
                )
                if cur:
                    current_id = int(cur["id"])
                    prev = await conn.fetchrow(
                        """
                        SELECT id FROM seasons
                        WHERE id <> $1
                        ORDER BY enddate DESC NULLS LAST, startdate DESC
                        LIMIT 1
                        """,
                        current_id,
                    )
                    return current_id, (int(prev["id"]) if prev else None), False

                # create a new season spanning the current month
                await conn.execute("UPDATE seasons SET active = false WHERE active = true")
                row = await conn.fetchrow(
                    """
                    INSERT INTO seasons (name, active, startdate, enddate)
                    VALUES ('', true, $1, $2)
                    RETURNING id
                    """,
                    startdate,
                    enddate,
                )
                new_id = int(row["id"])
                from lounge.ttlcache import invalidate_seasons

                invalidate_seasons()
                await conn.execute(
                    "UPDATE seasons SET name = $1 WHERE id = $2",
                    f"Season {new_id}",
                    new_id,
                )
                prev = await conn.fetchrow(
                    """
                    SELECT id FROM seasons
                    WHERE id <> $1
                    ORDER BY enddate DESC NULLS LAST, startdate DESC
                    LIMIT 1
                    """,
                    new_id,
                )
                prev_id = int(prev["id"]) if prev else None
                return new_id, prev_id, True

    async def _apply_rank_role(
        self, guild: discord.Guild, member_or_id, role_name: str, reason: str = ""
    ) -> bool:
        rank_role_ids = set(RANK_ROLE_IDS.values())

        # Resolver member
        member = (
            member_or_id
            if isinstance(member_or_id, discord.Member)
            else guild.get_member(int(member_or_id))
        )
        if member is None:
            try:
                member = await guild.fetch_member(int(member_or_id))
            except Exception:
                return False

        role_name = (role_name or "Placement").strip()
        if role_name not in RANK_ROLE_IDS:
            role_name = "Placement"

        target_role = guild.get_role(RANK_ROLE_IDS[role_name])
        if not target_role:
            target_role = guild.get_role(RANK_ROLE_IDS["Placement"])
            if not target_role:
                return False

        # Mantener roles NO-rango y agregar el nuevo rango
        keep = [
            r for r in member.roles
            if r.id not in rank_role_ids and r.id != SEASON_PLACEMENT_ROLE_ID
        ]
        new_roles = list(dict.fromkeys(keep + [target_role]))

        try:
            await member.edit(roles=new_roles, reason=reason or f"Apply rank {role_name}")
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def _apply_season_placement_role(
        self, guild: discord.Guild, member_or_id, reason: str = ""
    ) -> bool:
        """
        Season reset: aplicar rol Season Placement y remover roles de rango.
        Mantiene todos los roles que no sean de rango.
        """
        rank_role_ids = set(RANK_ROLE_IDS.values())

        member = (
            member_or_id
            if isinstance(member_or_id, discord.Member)
            else guild.get_member(int(member_or_id))
        )
        if member is None:
            try:
                member = await guild.fetch_member(int(member_or_id))
            except Exception:
                return False

        sp_role = guild.get_role(SEASON_PLACEMENT_ROLE_ID)
        if sp_role is None:
            return False

        keep = [
            r for r in member.roles
            if r.id not in rank_role_ids and r.id != SEASON_PLACEMENT_ROLE_ID
        ]
        new_roles = list(dict.fromkeys(keep + [sp_role]))

        try:
            await member.edit(
                roles=new_roles,
                reason=reason or "Season reset: Season Placement applied",
            )
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.command(
        name="newseason",
        description="Setup new season (create if needed, migrate players, sync roles)",
    )
    async def newseason(self, interaction: discord.Interaction):
        # Permitir solo a un usuario específico
        await interaction.response.send_message(
            "Season changes are paused. Use the verified backup and audited Season 5 procedure; production reset is disabled.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(PlayersCog(bot))
