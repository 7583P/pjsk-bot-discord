import os
import aiosqlite
import discord
from discord.ext import commands

DB_PATH = "matchmaking.db"

class AutoRoles(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        # Conectar y asegurar que exista la tabla players
        self.db = await aiosqlite.connect(DB_PATH)
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS players (
                user_id INTEGER PRIMARY KEY,
                mmr     INTEGER DEFAULT 0,
                role    TEXT    DEFAULT 'Placement'
            );
        """)
        await self.db.commit()
        print("[AutoRoles] Tabla 'players' creada o ya existía.")

    async def cog_unload(self):
        await self.db.close()

    async def fetch_player(self, user_id: int):
        # Recupera mmr y role; si no existe inserta como Placement
        async with self.db.execute(
            "SELECT mmr, role FROM players WHERE user_id=?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            return row  # (mmr, role)
        await self.db.execute(
            "INSERT INTO players (user_id, mmr, role) VALUES (?, 0, 'Placement')",
            (user_id,)
        )
        await self.db.commit()
        return (0, "Placement")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        print(f"[AutoRoles] on_member_join disparado para {member} ({member.id})")
        try:
            mmr, role_name = await self.fetch_player(member.id)
            # forzamos "Placement" si no tiene MMR asignado
            if mmr == 0:
                role_name = "Placement"

            guild_role = discord.utils.get(member.guild.roles, name=role_name)
            if guild_role is None:
                print(f"[AutoRoles] ❌ No existe el rol '{role_name}' en {member.guild.name}")
                return

            await member.add_roles(guild_role, reason="Asignación automática al unirse")
            print(f"[AutoRoles] ✅ Asigné rol '{role_name}' a {member.name}")
        except Exception as e:
            print(f"[AutoRoles] ❌ Excepción en on_member_join: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(AutoRoles(bot))