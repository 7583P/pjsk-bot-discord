import discord
import asyncio
import time
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
    Cog que publica en un único mensaje el listado de salas activas y sus jugadores.
    Edita el mensaje cada 5 s e incluye un indicador de "Last Updated".
    Elimina salas 10 s después de recibir evento 'room_finished'.
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.posted_message: discord.Message | None = None
        self.last_update = 0
        # Iniciar loop cada 5 s
        self.update_rooms.start()
        bot.add_listener(self.on_room_finished)

    def cog_unload(self):
        self.update_rooms.cancel()
        self.bot.remove_listener(self.on_room_finished)

    def _get_channel(self) -> discord.TextChannel | None:
        channel = self.bot.get_channel(ROOMS_CHANNEL_ID)
        if not channel:
            print(f"›› [Rooms] Canal ID {ROOMS_CHANNEL_ID} no encontrado.")
        return channel

    @tasks.loop(seconds=5.0)
    async def update_rooms(self):
        try:
            channel = self._get_channel()
            if not channel:
                return

            matchmaking = self.bot.get_cog("Matchmaking")
            if not matchmaking or not hasattr(matchmaking, 'rooms'):
                return

            rooms = matchmaking.rooms
            # Construir líneas de estado
            if not rooms:
                lines = ["**No hay salas activas.**"]
            else:
                sorted_rooms = []
                for room_id, players in rooms.items():
                    mmrs = [m for (_u, m) in players]
                    avg = sum(mmrs)//len(mmrs) if mmrs else 0
                    sorted_rooms.append((room_id, avg, players))
                sorted_rooms.sort(key=lambda x: x[1], reverse=True)
                lines = []
                for room_id, avg_mmr, players in sorted_rooms:
                    lines.append(f"**Sala {room_id}** - MMR promedio: **{avg_mmr}**")
                    for member, mmr in players:
                        lines.append(f"{member.mention} ({mmr})")
                    lines.append("")

            # Calcular tiempo transcurrido
            now = int(time.time())
            elapsed = now - self.last_update if self.last_update else 0
            self.last_update = now
            # Indicador de última actualización
            lines.append(f"**Last Updated:** hace {elapsed} segundos. (editado)")

            content = "\n".join(lines)
            # Enviar o editar mensaje
            if not self.posted_message:
                self.posted_message = await channel.send(content)
            else:
                try:
                    await self.posted_message.edit(content=content)
                except discord.NotFound:
                    self.posted_message = await channel.send(content)

        except Exception as e:
            print(f"›› [Rooms] Error en actualización: {e}")

    @update_rooms.before_loop
    async def before_update(self):
        await self.bot.wait_until_ready()
        print("›› [Rooms] Loop de salas listo.")

    async def on_room_finished(self, room_id: int):
        await asyncio.sleep(10)
        matchmaking = self.bot.get_cog("Matchmaking")
        if matchmaking and hasattr(matchmaking, 'rooms') and room_id in matchmaking.rooms:
            del matchmaking.rooms[room_id]
