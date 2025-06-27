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
    Cog que publica en un único mensaje el listado de salas activas y sus jugadores.
    Edita el mensaje cada 5 s usando Discord timestamps (<t:...:R>) para el "Last Updated".
    Además proporciona un listener para actualizar inmediatamente al recibir 'room_updated'.
    Elimina salas 10 s después de recibir evento 'room_finished'.
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.posted_message: discord.Message | None = None
        # Iniciar loop cada 5 s
        self.update_rooms.start()
        # Listeners de eventos personalizados
        bot.add_listener(self.on_room_finished, 'room_finished')
        bot.add_listener(self.on_room_updated, 'room_updated')

    def cog_unload(self):
        self.update_rooms.cancel()
        self.bot.remove_listener(self.on_room_finished)
        self.bot.remove_listener(self.on_room_updated)

    def _get_channel(self) -> discord.TextChannel | None:
        channel = self.bot.get_channel(ROOMS_CHANNEL_ID)
        if not channel:
            print(f"›› [Rooms] Canal ID {ROOMS_CHANNEL_ID} no encontrado.")
        return channel

    @tasks.loop(seconds=5.0)
    async def update_rooms(self):
        """
        Loop que actualiza el mensaje cada 5 segundos.
        """
        await self._do_update()

    @update_rooms.error
    async def update_error(self, error):
        print(f"›› [Rooms] Error en loop: {error}")

    async def on_room_updated(self, room_id: int | None = None):
        """
        Listener que actualiza inmediatamente cuando se despacha 'room_updated'.
        """
        await self._do_update()

    async def _do_update(self):
        try:
            channel = self._get_channel()
            if not channel:
                return

            matchmaking = self.bot.get_cog("Matchmaking")
            if not matchmaking or not hasattr(matchmaking, 'rooms'):
                return

            rooms = matchmaking.rooms
            if not rooms:
                lines = ["**No hay salas activas.**"]
            else:
                sorted_rooms = sorted(
                    ((rid, sum(m for (_u,m) in players)//len(players), players)
                     for rid, players in rooms.items()),
                    key=lambda x: x[1], reverse=True
                )
                lines = []
                for room_id, avg_mmr, players in sorted_rooms:
                    lines.append(f"**Sala {room_id}** - MMR promedio: **{avg_mmr}**")
                    for member, mmr in players:
                        lines.append(f"{member.mention} ({mmr})")
                    lines.append("")

            now = int(asyncio.get_event_loop().time())
            # Discord relative timestamp
            lines.append(f"**Last Updated:** <t:{now}:R>")

            content = "\n".join(lines)
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
            # Forzar actualización al eliminar sala
            await self._do_update()
