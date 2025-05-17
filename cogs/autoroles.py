import discord
from discord.ext import commands
import aiosqlite
import os

DB_PATH = 'matchmaking.db'

class AutoRoles(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        # Conectar a la base de datos al cargar el cog
        self.db = await aiosqlite.connect(DB_PATH)

    async def cog_unload(self):
        # Cerrar la conexión al descargar el cog
        await self.db.close()

    async def fetch_player(self, user_id: int):
        async with self.db.execute(
            "SELECT mmr, role FROM players WHERE user_id=?", (user_id,)
        ) as cursor:
            data = await cursor.fetchone()
        if data:
            return data
        # Si no existe, insertar con Placement
        await self.db.execute(
            "INSERT INTO players (user_id, mmr, role) VALUES (?, ?, ?)",
            (user_id, 0, "Placement")
        )
        await self.db.commit()
        return (0, "Placement")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        # Asignar rol basado en la base de datos al unirse
        mmr, role = await self.fetch_player(member.id)
        role_name = role if mmr > 0 else "Placement"
        guild_role = discord.utils.get(member.guild.roles, name=role_name)
        if guild_role:
            await member.add_roles(guild_role, reason="Asignación automática de rol al unirse")

async def setup(bot: commands.Bot):
    await bot.add_cog(AutoRoles(bot))
