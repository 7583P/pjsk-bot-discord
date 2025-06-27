import discord
import asyncio
from discord.ext import commands, tasks

async def setup(bot: commands.Bot):
    # Registrar el cog usando la nueva API asíncrona de discord.py v2
    await bot.add_cog(Rooms(bot))

class Rooms(commands.Cog):
    """
    Cog que publica periódicamente en #rooms el listado de salas activas y sus jugadores,
    elimina salas 10s después de que se despache el evento 'room_finished'.
    Asume que el cog Matchmaking mantiene un atributo `rooms`:
      rooms: Dict[int, List[Tuple[discord.Member, int]]]
    donde cada tupla es (member, mmr).
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Loop de actualización cada 5 segundos
        self.update_rooms.start()
        # Escuchar evento personalizado para finalización de sala
        bot.add_listener(self.on_room_finished)

    def cog_unload(self):
        self.update_rooms.cancel()
        self.bot.remove_listener(self.on_room_finished)

    @tasks.loop(seconds=5.0)
    async def update_rooms(self):
        # Intento de publicar en #rooms
        channel = discord.utils.get(
            self.bot.get_all_channels(),
            name="rooms",
            type=discord.TextChannel
        )
        if not channel:
            return

        matchmaking = self.bot.get_cog("Matchmaking")
        if not matchmaking or not hasattr(matchmaking, 'rooms'):
            return

        rooms = matchmaking.rooms
        if not rooms:
            await channel.purge(limit=100)
            await channel.send("No hay salas activas en este momento.")
            return

        # Ordenar por MMR promedio
        sorted_rooms = [
            (rid, sum(m for (_u,m) in players)//len(players), players)
            for rid, players in rooms.items()
        ]
        sorted_rooms.sort(key=lambda x: x[1], reverse=True)

        lines = []
        for room_id, avg_mmr, players in sorted_rooms:
            lines.append(f"**Sala {room_id}** - MMR promedio: **{avg_mmr}**")
            for member, mmr in players:
                lines.append(f"{member.mention} ({mmr})")
            lines.append("")

        await channel.purge(limit=100)
        await channel.send("\n".join(lines))

    @update_rooms.before_loop
    async def before_update(self):
        await self.bot.wait_until_ready()

    async def on_room_finished(self, room_id: int):
        await asyncio.sleep(10)
        matchmaking = self.bot.get_cog("Matchmaking")
        if matchmaking and hasattr(matchmaking, 'rooms') and room_id in matchmaking.rooms:
            del matchmaking.rooms[room_id]
