import os
import asyncpg
import discord
from discord.ext import commands
from lounge.connections import get_bot_pool

DATABASE_URL = os.getenv("DB_HOST")  # e.g. "postgresql://user:pass@host:5432/dbname"
GUILD_ID = int(os.getenv("GUILD_ID", "0"))


class AutoRoles(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_pool: asyncpg.Pool | None = None

    # ─────────── SETUP / TEARDOWN ───────────
    async def cog_load(self):
        """Reuse the single pool owned by the bot."""
        self.db_pool = get_bot_pool()
        print("[AutoRoles] asyncpg pool ready.")

    async def cog_unload(self):
        """Release this cog's reference; the bot closes the pool."""
        if self.db_pool:
            self.db_pool = None
        print("[AutoRoles] shared pool reference released.")

    # ─────────── HELPERS (asyncpg) ───────────
    async def get_current_season(self) -> asyncpg.Record | None:
        """
        Returns the active season row or None.
        """
        assert self.db_pool is not None, "DB pool not initialized"
        sql = """
            SELECT id, name, active, startdate, enddate
            FROM seasons
            WHERE active = true
            ORDER BY startdate DESC
            LIMIT 1
        """
        async with self.db_pool.acquire() as conn:
            return await conn.fetchrow(sql)

    async def fetch_player(self, user_id: int) -> tuple[int, str]:
        """
        Returns (mmr, role) for the current season.
        - Ensures a players row exists (by user_id).
        - Ensures a seasonplayers row exists for current season.
        """
        assert self.db_pool is not None, "DB pool not initialized"

        season = await self.get_current_season()
        if not season:
            # No active season; default to 0 / Placement
            return (0, "Placement")
        season_id = season["id"]

        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                # Ensure players row (natural key user_id)
                await conn.execute(
                    """
                    INSERT INTO players (user_id, name, country)
                    VALUES ($1, '', '')
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    user_id,
                )

                # Try to read existing season row
                placement_enabled=await conn.fetchval('SELECT placement_enabled FROM lounge_season_policy WHERE season_id=$1',season_id)
                initial_role='Iron' if placement_enabled is False else 'Placement'
                row = await conn.fetchrow(
                    """
                    SELECT sp.mmr, sp.role, sp.is_season_placement,
                        (SELECT count(*) FROM match_history mh WHERE mh.season_id=sp.season_id AND mh.user_id=sp.user_id) AS matches_played
                    FROM seasonplayers sp
                    WHERE sp.user_id = $1 AND sp.season_id = $2
                    """,
                    user_id,
                    season_id,
                )

                if row:
                    role='Season Placement' if row['is_season_placement'] and row['matches_played']==0 else (row['role'] or 'Placement')
                    return (row['mmr'] or 0,role)

                # Create season row if missing
                row = await conn.fetchrow(
                    """
                    INSERT INTO seasonplayers (user_id, season_id, points, mmr, role)
                    VALUES ($1, $2, 0, 0, $3)
                    ON CONFLICT (user_id, season_id) DO NOTHING
                    RETURNING mmr, role
                    """,
                    user_id,
                    season_id,
                    initial_role,
                )

                if row is None:
                    # Race: someone else created it; fetch again
                    row = await conn.fetchrow(
                        """
                        SELECT sp.mmr, sp.role
                        FROM seasonplayers sp
                        WHERE sp.user_id = $1 AND sp.season_id = $2
                        """,
                        user_id,
                        season_id,
                    )

                return ((row["mmr"] or 0), (row["role"] or "Placement"))

    # ─────────── LISTENER on_member_join ───────────
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Assign role based on MMR when a user joins."""
        if member.guild.id != GUILD_ID:
            return

        try:
            mmr, role_name = await self.fetch_player(member.id)

            role = discord.utils.get(member.guild.roles, name=role_name)
            if not role:
                print(
                    f"[AutoRoles] ❌ Role '{role_name}' not found in {member.guild.name}"
                )
                return

            await member.add_roles(role, reason="Asignación automática al unirse")
            print(f"[AutoRoles] ✅ Assigned '{role_name}' to {member} ({member.id})")

        except Exception as e:
            print(f"[AutoRoles] ❌ Excepción en on_member_join: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoRoles(bot))
