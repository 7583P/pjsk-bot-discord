import logging
import random
import discord
from discord.ext import commands, tasks
from lounge.rules import RANKS

log = logging.getLogger(__name__)


class AutoRoles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.reconcile.start()

    async def cog_unload(self):
        self.reconcile.cancel()

    @tasks.loop(seconds=10)
    async def reconcile(self):
        try:
            async with self.bot.db.pool.acquire() as c:
                jobs = await c.fetch("""SELECT j.*,sp.role FROM role_jobs j JOIN season_players sp USING(user_id)
                    JOIN seasons s ON s.id=sp.season_id WHERE s.status='active' AND next_attempt<=now() ORDER BY next_attempt LIMIT 10""")
            for job in jobs:
                await self.apply(job)
        except Exception:
            log.exception("role_reconciliation_failed")

    async def apply(self, job):
        try:
            guild = self.bot.get_guild(job["guild_id"])
            if guild is None:
                raise ValueError("guild_missing")
            member = guild.get_member(job["user_id"]) or await guild.fetch_member(
                job["user_id"]
            )
            target = discord.utils.get(guild.roles, name=job["role"])
            if target is None:
                raise ValueError("rank_role_missing:" + job["role"])
            if (
                not guild.me.guild_permissions.manage_roles
                or target >= guild.me.top_role
                or target.managed
            ):
                raise ValueError("rank_role_hierarchy_or_permissions")
            remove = [r for r in member.roles if r.name in RANKS and r != target]
            if target not in member.roles:
                await member.add_roles(
                    target, reason="PJSK: reconciliar rango PostgreSQL"
                )
            if remove:
                await member.remove_roles(
                    *remove, reason="PJSK: retirar rangos anteriores"
                )
            async with self.bot.db.pool.acquire() as c:
                await c.execute(
                    """DELETE FROM role_jobs j WHERE guild_id=$1 AND user_id=$2 AND EXISTS(
                    SELECT 1 FROM season_players sp JOIN seasons s ON s.id=sp.season_id
                    WHERE sp.user_id=j.user_id AND s.status='active' AND sp.role=$3)""",
                    job["guild_id"],
                    job["user_id"],
                    job["role"],
                )
        except discord.NotFound:
            await self.failed(job, "member_missing", 3600)
        except (discord.HTTPException, ValueError) as exc:
            code = (
                str(exc)
                if isinstance(exc, ValueError)
                else f"discord_http_{exc.status}"
            )
            await self.failed(
                job,
                code,
                min(3600, 10 * 2 ** min(job["attempts"], 8)) + random.random() * 5,
            )
            log.warning("role_sync_failed user_id=%s reason=%s", job["user_id"], code)

    async def failed(self, job, code, delay):
        async with self.bot.db.pool.acquire() as c:
            await c.execute(
                "UPDATE role_jobs SET attempts=attempts+1,last_error=$3,next_attempt=now()+$4*interval '1 second' WHERE guild_id=$1 AND user_id=$2",
                job["guild_id"],
                job["user_id"],
                code,
                delay,
            )

    @reconcile.before_loop
    async def before_reconcile(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(AutoRoles(bot))
