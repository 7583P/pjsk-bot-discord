import re
import logging
import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)


class PlayersCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.synced = False

    @commands.Cog.listener()
    async def on_ready(self):
        if self.synced:
            return
        self.synced = True
        try:
            for guild in self.bot.guilds:
                if guild.id not in self.bot.settings.guild_ids:
                    continue
                await guild.chunk()
                members = [m for m in guild.members if not m.bot]
                for offset in range(0, len(members), 100):
                    async with self.bot.db.competitive() as (c, s):
                        batch = members[offset : offset + 100]
                        ids = [m.id for m in batch]
                        await c.executemany(
                            "INSERT INTO players(user_id,name) VALUES($1,$2) ON CONFLICT(user_id) DO UPDATE SET name=excluded.name",
                            [(m.id, m.display_name) for m in batch],
                        )
                        await c.execute(
                            "INSERT INTO season_players(season_id,user_id,placement_pending,peak_mmr) SELECT $1,unnest($2::bigint[]),$3,0 ON CONFLICT DO NOTHING",
                            s["id"],
                            ids,
                            s["legacy_placement"],
                        )
                        await c.execute(
                            "INSERT INTO role_jobs(guild_id,user_id) SELECT $1,unnest($2::bigint[]) ON CONFLICT DO NOTHING",
                            guild.id,
                            ids,
                        )
                log.info("guild_players_synced count=%s", len(members))
        except Exception:
            self.synced = False
            log.exception("guild_player_sync_failed")

    @commands.Cog.listener()
    async def on_member_join(self, member):
        if member.bot or member.guild.id not in self.bot.settings.guild_ids:
            return
        await self.bot.repo.register(member.id, member.display_name)
        async with self.bot.db.pool.acquire() as c:
            await c.execute(
                "INSERT INTO role_jobs(guild_id,user_id) VALUES($1,$2) ON CONFLICT(guild_id,user_id) DO UPDATE SET next_attempt=now()",
                member.guild.id,
                member.id,
            )
        if member.guild.system_channel:
            try:
                await member.guild.system_channel.send(
                    f"👋 Bienvenido {member.mention}! Ejecuta `/register <ISO2>` para guardar tu país."
                )
            except discord.HTTPException:
                log.exception("welcome_send_failed guild=%s", member.guild.id)

    @app_commands.command(name="register", description="Registra tu país (código ISO2)")
    @app_commands.describe(country="Código ISO2 de dos letras (p.ej., PE)")
    async def register(self, interaction: discord.Interaction, country: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        code = country.strip().upper()
        if not re.fullmatch("[A-Z]{2}", code):
            return await interaction.followup.send(
                "❌ Debes indicar un código ISO de dos letras.", ephemeral=True
            )
        await self.bot.repo.register(
            interaction.user.id, interaction.user.display_name, code
        )
        await interaction.followup.send(
            f"✅ {interaction.user.display_name}, país registrado **{code}**.",
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(PlayersCog(bot))
