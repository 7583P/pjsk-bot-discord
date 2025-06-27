import discord
import asyncio
from discord.ext import commands, tasks

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
    Se apoya en matchmaking.rooms para cambios de unirse/abandonar/inactividad.
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Estado previo para detectar cambios (puede usarse si queremos diffs)
        self.previous_rooms = {}
        # Loop cada 5 s
        self.update_rooms.start()
        # Evento para limpieza de sala finalizada
        bot.add_listener(self.on_room_finished)

    def cog_unload(self):
        self.update_rooms.cancel()
        self.bot.remove_listener(self.on_room_finished)

    @tasks.loop(seconds=5.0)
    async def update_rooms(self):
        # Debug
        print("›› [Rooms] Ejecutando update_rooms")
        # Obtener canal #rooms
        channel = discord.utils.get(
            self.bot.get_all_channels(),
            name="rooms",
            type=discord.TextChannel
        )
        if not channel:
            print("›› [Rooms] Canal 'rooms' no encontrado")
            return

        # Obtener cog Matchmaking
        matchmaking = self.bot.get_cog("Matchmaking")
        if not matchmaking or not hasattr(matchmaking, 'rooms'):
            print("›› [Rooms] Cog Matchmaking o atributo rooms no disponible")
            return

        rooms = matchmaking.rooms  # dict {room_id: [(member, mmr), ...]}
        # Preparar mensaje
        if not rooms:
            # No hay salas
            await channel.purge(limit=100)
            await channel.send("No hay salas activas en este momento.")
            return

        # Ordenar las salas por MMR promedio descendente
        sorted_rooms = []
        for room_id, players in rooms.items():
            mmrs = [m for (_u, m) in players]
            avg = sum(mmrs)//len(mmrs) if mmrs else 0
            sorted_rooms.append((room_id, avg, players))
        sorted_rooms.sort(key=lambda x: x[1], reverse=True)

        # Construir líneas
        lines = []
        for room_id, avg_mmr, players in sorted_rooms:
            lines.append(f"**Sala {room_id}** - MMR promedio: **{avg_mmr}**")
            for member, mmr in players:
                lines.append(f"{member.mention} ({mmr})")
            lines.append("")

        # Publicar
        try:
            await channel.purge(limit=100)
            await channel.send("\n".join(lines))
            print(f"›› [Rooms] Mensaje enviado con {len(sorted_rooms)} salas")
        except Exception as e:
            print(f"›› [Rooms] Error al publicar: {e}")

    @update_rooms.before_loop
    async def before_update(self):
        await self.bot.wait_until_ready()
        print("›› [Rooms] Bot listo, arrancando loop de salas")

    async def on_room_finished(self, room_id: int):
        """
        listener: limpia la sala 10 s tras finalizar (submit).  
        matchmaking.rooms gestiona la remoción.
        """
        await asyncio.sleep(10)
        matchmaking = self.bot.get_cog("Matchmaking")
        if matchmaking and hasattr(matchmaking, 'rooms'):
            if room_id in matchmaking.rooms:
                del matchmaking.rooms[room_id]
                print(f"›› [Rooms] Sala {room_id} eliminada tras finish")
