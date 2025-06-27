import discord
import asyncio
from discord.ext import commands, tasks

# Configuración: ID fijo del canal #rooms (hardcode)
ROOMS_CHANNEL_ID = 1371307831176728706

async def setup(bot: commands.Bot):
    """
    Setup asíncrono para discord.py v2: registra el cog Rooms.
    """
    bot.add_cog(Rooms(bot))
    print("✅ Cog cargado: cogs.rooms")

class Rooms(commands.Cog):
    """
    Cog que publica periódicamente en #rooms el listado de salas activas y sus jugadores.
    Elimina salas 10 s después de recibir evento 'room_finished'.
    Se apoya en matchmaking.rooms para uniones, abandonos e inactividad.
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Loop de actualización cada 5 s
        self.update_rooms.start()
        # Listener para limpieza de sala tras finalizar
        bot.add_listener(self.on_room_finished)

    def cog_unload(self):
        self.update_rooms.cancel()
        self.bot.remove_listener(self.on_room_finished)

    def _get_rooms_channel(self) -> discord.TextChannel | None:
        """
        Obtiene el canal de texto donde publicar por ID fijo.
        """
        channel = self.bot.get_channel(ROOMS_CHANNEL_ID)
        if not channel:
            print(f"›› [Rooms] Canal con ID {ROOMS_CHANNEL_ID} no encontrado. Asegúrate de usar el ID correcto.")
        return channel

    @tasks.loop(seconds=5.0)
    async def update_rooms(self):
        print("›› [Rooms] Ejecutando update_rooms")
        channel = self._get_rooms_channel()
        if not channel:
            return

        # Obtener cog matchmaking
        matchmaking = self.bot.get_cog("Matchmaking")
        if not matchmaking or not hasattr(matchmaking, 'rooms'):
            print("›› [Rooms] Cog Matchmaking o atributo rooms no disponible")
            return

        rooms = matchmaking.rooms
        if not rooms:
            # Sin salas
            try:
                await channel.purge(limit=100)
                await channel.send("No hay salas activas en este momento.")
            except Exception as e:
                print(f"›› [Rooms] Error al enviar mensaje: {e}")
            return

        # Ordenar por MMR promedio descendente
        sorted_rooms = []
        for room_id, players in rooms.items():
            mmrs = [m for (_u, m) in players]
            avg = sum(mmrs)//len(mmrs) if mmrs else 0
            sorted_rooms.append((room_id, avg, players))
        sorted_rooms.sort(key=lambda x: x[1], reverse=True)

        # Construir mensaje
        lines: list[str] = []
        for room_id, avg_mmr, players in sorted_rooms:
            lines.append(f"**Sala {room_id}** - MMR promedio: **{avg_mmr}**")
            for member, mmr in players:
                lines.append(f"{member.mention} ({mmr})")
            lines.append("")

        # Publicar
        try:
            await channel.purge(limit=100)
            await channel.send("\n".join(lines))
            print(f"›› [Rooms] Publicadas {len(sorted_rooms)} salas en canal {ROOMS_CHANNEL_ID}")
        except Exception as e:
            print(f"›› [Rooms] Error al publicar listado: {e}")

    @update_rooms.before_loop
    async def before_update(self):
        await self.bot.wait_until_ready()
        print("›› [Rooms] Bot listo, arrancando loop de salas")

    async def on_room_finished(self, room_id: int):
        """
        Listener para evento 'room_finished'. Elimina sala tras 10 s.
        """
        await asyncio.sleep(10)
        matchmaking = self.bot.get_cog("Matchmaking")
        if matchmaking and hasattr(matchmaking, 'rooms') and room_id in matchmaking.rooms:
            del matchmaking.rooms[room_id]
            print(f"›› [Rooms] Sala {room_id} eliminada tras finish")
