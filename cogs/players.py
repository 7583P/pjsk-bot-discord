import os
import re
import asyncpg
import datetime as dt
import calendar
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# -----------------------------
# Config / Environment
# -----------------------------
load_dotenv()

GUILD_ID = int(os.getenv("GUILD_ID", "0"))
if GUILD_ID == 0:
    raise RuntimeError("GUILD_ID env var not set (it cannot be 0).")

# Soporta ambos nombres de variable, por compatibilidad
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("DB_HOST")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL (or DB_HOST) env var not set.")

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


def get_member_name(member: discord.Member) -> str:
    """Prioridad: nickname > global_name > username."""
    if member.nick:
        return member.nick
    if getattr(member, "global_name", None):
        return member.global_name
    return member.name


class PlayersCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.synced = False
        self.db_pool: asyncpg.Pool | None = None

    # -----------------------------
    # Lifecycle
    # -----------------------------
    async def cog_load(self):
        # Pool DB (evita abrir conexiones por comando)
        self.db_pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=10,
            command_timeout=30,
        )

        # Forzar limpieza + sync en el GUILD para que el comando aparezca al instante
        guild = discord.Object(id=GUILD_ID)
        self.bot.tree.clear_commands(guild=guild)
        synced = await self.bot.tree.sync(guild=guild)

        print(f"[PlayersCog] Synced {len(synced)} commands to guild {GUILD_ID}:")
        for c in synced:
            print(" -", c.name)

        print("[PlayersCog] asyncpg pool ready and commands synced.")

    async def cog_unload(self):
        if self.db_pool:
            await self.db_pool.close()
            self.db_pool = None
        print("[PlayersCog] asyncpg pool closed.")

    # -----------------------------
    # DB helpers (POOL)
    # -----------------------------
    async def _get_current_season(self) -> asyncpg.Record | None:
        sql = """
        SELECT id, name, active, startdate, enddate
        FROM seasons
        WHERE active = true
        ORDER BY startdate DESC
        LIMIT 1
        """
        if not self.db_pool:
            raise RuntimeError("DB pool not initialized.")
        async with self.db_pool.acquire() as conn:
            return await conn.fetchrow(sql)

    async def _get_all_seasons(self) -> list[asyncpg.Record]:
        sql = """
        SELECT id, name, active, startdate, enddate
        FROM seasons
        ORDER BY startdate DESC
        """
        if not self.db_pool:
            raise RuntimeError("DB pool not initialized.")
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(sql)
            return list(rows)

    async def _update_player_name(self, user_id: int, name: str):
        sql = """
        INSERT INTO players(user_id, name)
        VALUES ($1, $2)
        ON CONFLICT (user_id) DO UPDATE SET
          name = EXCLUDED.name
        """
        if not self.db_pool:
            raise RuntimeError("DB pool not initialized.")
        async with self.db_pool.acquire() as conn:
            await conn.execute(sql, user_id, name)

    async def _upsert_player(self, user_id: int, name: str, country: str):
        season = await self._get_current_season()
        if not season:
            raise RuntimeError("No active season found (seasons.active=true).")

        season_id = int(season["id"])

        sql_players = """
            INSERT INTO players (user_id, name, country)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE SET
              name = EXCLUDED.name,
              country = EXCLUDED.country
            RETURNING user_id
        """

        sql_seasonplayers = """
            INSERT INTO seasonplayers (user_id, season_id, points, mmr, role)
            VALUES ($1, $2, 0, 0, 'Placement')
            ON CONFLICT (user_id, season_id) DO UPDATE SET
              season_id = EXCLUDED.season_id
        """

        if not self.db_pool:
            raise RuntimeError("DB pool not initialized.")

        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                player_row = await conn.fetchrow(sql_players, user_id, name, country)
                player_id = int(player_row["user_id"])
                await conn.execute(sql_seasonplayers, player_id, season_id)

    # -----------------------------
    # Discord listeners
    # -----------------------------
    @commands.Cog.listener()
    async def on_ready(self):
        # Sincroniza tabla de players con miembros existentes (solo names)
        if self.synced:
            return

        if not self.db_pool:
            print("[PlayersCog] on_ready: db_pool not ready yet.")
            return

        self.synced = True
        total = 0

        for guild in self.bot.guilds:
            await guild.chunk()
            for member in guild.members:
                if member.bot:
                    continue
                name = get_member_name(member)
                try:
                    await self._update_player_name(member.id, name)
                    total += 1
                except Exception as e:
                    # No abortar toda la sync por un error puntual
                    print(f"[PlayersCog] on_ready sync error for {member.id}: {e}")

        print(f"[PlayersCog] Tabla sincronizada con {total} miembros existentes.")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return

        name = get_member_name(member)
        try:
            # Inserta player con country vacío + seasonplayers Placement
            await self._upsert_player(member.id, name, "")
        except Exception as e:
            print(f"[PlayersCog] on_member_join upsert error for {member.id}: {e}")
            return

        if member.guild.system_channel:
            await member.guild.system_channel.send(
                f"Welcome {member.mention}! Execute `/set_your_country <ISO2>` in #set_your_country "
                f"to register your country (example: `PE`)."
            )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        prev = get_member_name(before)
        curr = get_member_name(after)
        if prev == curr:
            return

        try:
            await self._update_player_name(after.id, curr)
        except Exception as e:
            print(f"[PlayersCog] on_member_update name update error for {after.id}: {e}")

    # -----------------------------
    # Command: /set_your_country
    # -----------------------------
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.command(
        name="set_your_country",
        description="Register your country (ISO2 code, 2 letters)",
    )
    @app_commands.describe(country="ISO2 2-letter code (e.g., PE)")
    async def set_your_country(self, interaction: discord.Interaction, country: str):
        code = (country or "").strip().upper()

        if not re.fullmatch(r"[A-Z]{2}", code):
            return await interaction.response.send_message(
                "You should specify a 2-letter ISO2 country code (e.g., `PE`, `US`, `JP`).",
                ephemeral=True,
            )

        name = get_member_name(interaction.user)

        try:
            await self._upsert_player(interaction.user.id, name, code)
        except Exception as e:
            return await interaction.response.send_message(
                f"❌ Could not register country due to DB/season error: `{type(e).__name__}: {e}`",
                ephemeral=True,
            )

        await interaction.response.send_message(
            f"✅ {name}, Country registered **{code}**.", ephemeral=True
        )

    # -----------------------------
    # Season utilities
    # -----------------------------
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
        if not self.db_pool:
            raise RuntimeError("DB pool not initialized.")

        today = dt.date.today()

        async with self.db_pool.acquire() as conn:
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
                startdate, enddate = self._month_bounds_utc(today)
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

        keep = [r for r in member.roles if r.id not in rank_role_ids]
        new_roles = list(dict.fromkeys(keep + [target_role]))

        try:
            await member.edit(roles=new_roles, reason=reason or f"Apply rank {role_name}")
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
        if interaction.user.id != 878310498720940102:
            return await interaction.response.send_message(
                "❌ No tienes permiso para ejecutar este comando.", ephemeral=True
            )

        if not self.db_pool:
            return await interaction.response.send_message(
                "❌ DB pool not ready yet.", ephemeral=True
            )

        # 1) Ensure current season exists (create if needed)
        try:
            current_season_id, prev_season_id, created = await self._ensure_current_season()
        except Exception as e:
            return await interaction.response.send_message(
                f"❌ DB error ensuring season: {e}", ephemeral=True
            )

        # 2) If there is a previous season, copy players into current (only new ones)
        migrated = 0
        try:
            async with self.db_pool.acquire() as conn:
                async with conn.transaction():
                    if prev_season_id is not None:
                        migrated_str = await conn.execute(
                            """
                            INSERT INTO seasonplayers (user_id, season_id, points, mmr, role, synced)
                            SELECT sp.user_id, $1, 0, 0, 'Placement', false
                            FROM seasonplayers sp
                            WHERE sp.season_id = $2
                            AND NOT EXISTS (
                                SELECT 1 FROM seasonplayers x
                                WHERE x.user_id = sp.user_id AND x.season_id = $1
                            )
                            """,
                            current_season_id,
                            prev_season_id,
                        )
                        # asyncpg returns "INSERT 0 N"
                        if isinstance(migrated_str, str) and migrated_str.startswith("INSERT"):
                            try:
                                migrated = int(migrated_str.split()[-1])
                            except Exception:
                                migrated = 0
                    else:
                        migrated = 0
        except Exception as e:
            return await interaction.response.send_message(
                f"❌ DB error migrating players: {e}", ephemeral=True
            )

        # 3) Sync Discord roles for players in current season where synced=false, then mark synced=true
        synced = 0
        try:
            guild = interaction.guild
            if guild is None:
                return await interaction.response.send_message(
                    "❌ This command must be used inside a server.", ephemeral=True
                )

            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT sp.user_id, sp.role
                    FROM seasonplayers sp
                    WHERE sp.season_id = $1
                      AND sp.synced = false
                    """,
                    current_season_id,
                )

                synced = 0
                for r in rows:
                    uid = int(r["user_id"])
                    role_name = (r["role"] or "Placement").strip()
                    ok = await self._apply_rank_role(
                        guild, uid, role_name, reason=f"Season {current_season_id} sync"
                    )
                    if ok:
                        synced += 1

                await conn.execute(
                    """
                    UPDATE seasonplayers
                    SET synced = true
                    WHERE season_id = $1 AND synced = false
                    """,
                    current_season_id,
                )

        except Exception as e:
            return await interaction.response.send_message(
                f"⚠️ Partial success (season ok). Role sync error: {e}", ephemeral=True
            )

        created_text = "created" if created else "already existed"
        await interaction.response.send_message(
            f"✅ Season {current_season_id} {created_text}. "
            f"Migrated **{migrated}** players from previous season. "
            f"Synchronized roles for **{synced}** players.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(PlayersCog(bot))


