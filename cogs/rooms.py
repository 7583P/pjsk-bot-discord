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
    await bot.add_cog(Rooms(bot))
    print("✅ Cog cargado: cogs.rooms")

class Rooms(commands.Cog):
    """
    Cog que publica en un único mensaje el listado de salas activas y sus jugadores.
    Edita el mensaje usando Discord timestamps (<t:epoch:R>) para mostrar 'Last Updated'.
    Actualiza el mensaje cada 15 s y al recibir eventos 'room_updated' y 'room_finished'.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.posted_message: discord.Message | None = None
        self.update_rooms.start()
        bot.add_listener(self.on_room_updated, 'room_updated')
        bot.add_listener(self.on_room_finished, 'room_finished')

    def cog_unload(self):
        self.update_rooms.cancel()
        self.bot.remove_listener(self.on_room_updated)
        self.bot.remove_listener(self.on_room_finished)

    def _get_channel(self) -> discord.TextChannel | None:
        channel = self.bot.get_channel(ROOMS_CHANNEL_ID)
        if not channel:
            print(f"›› [Rooms] Canal ID {ROOMS_CHANNEL_ID} no encontrado.")
        return channel

    @tasks.loop(seconds=15.0)
    async def update_rooms(self):
        await self._do_update()

    @update_rooms.before_loop
    async def before_update(self):
        await self.bot.wait_until_ready()
        print("›› [Rooms] Loop de salas listo (15s interval).")

    @update_rooms.error
    async def update_error(self, error):
        print(f"›› [Rooms] Error en loop de actualización: {error}")

    async def on_room_updated(self, room_id: int | None = None):
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
            lines: list[str] = []

            if not rooms:
                lines.append("**No hay salas activas.**")
            else:
                rooms_list = []
                for room_id, players in rooms.items():
                    mmr_vals = []
                    valid_players: list[tuple[discord.Member, int]] = []
                    for entry in players:
                        # Resolver miembro si es ID o Member
                        member = None
                        raw_mmr = None
                        if isinstance(entry, discord.Member):
                            member = entry
                            raw_mmr = None  # fallback later
                        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                            raw_member, raw_mmr = entry[0], entry[1]
                            if isinstance(raw_member, discord.Member):
                                member = raw_member
                            else:
                                try:
                                    member_id = int(raw_member)
                                    member = channel.guild.get_member(member_id)
                                except Exception:
                                    member = None
                        if not member:
                            continue
                        # procesar MMR como entero
                        try:
                            mmr_value = int(raw_mmr) if raw_mmr is not None else 0
                        except Exception:
                            mmr_value = 0
                        mmr_vals.append(mmr_value)
                        valid_players.append((member, mmr_value))
                    avg_mmr = sum(mmr_vals) // len(mmr_vals) if mmr_vals else 0
                    rooms_list.append((room_id, avg_mmr, valid_players))

                # ordenar por MMR descendente
                rooms_list.sort(key=lambda x: x[1], reverse=True)
                for room_id, avg_mmr, players in rooms_list:
                    lines.append(f"**Sala {room_id}** - MMR promedio: **{avg_mmr}**")
                    for member, mmr_value in players:
                        name = member.display_name or member.name
                        lines.append(f"{name} ({mmr_value})")
                    lines.append("")

            now = int(time.time())
            lines.append(f"**Last Updated:** <t:{now}:R>")

            content = "\n".join(lines)
            if not self.posted_message:
                self.posted_message = await channel.send(content)
            else:
                try:
                    await self.posted_message.edit(content=content)
                except discord.NotFound:
                    pass

        except Exception as e:
            print(f"›› [Rooms] Excepción en _do_update: {e}")

    async def on_room_finished(self, room_id: int):
        await asyncio.sleep(10)
        matchmaking = self.bot.get_cog("Matchmaking")
        if matchmaking and hasattr(matchmaking, 'rooms') and room_id in matchmaking.rooms:
            del matchmaking.rooms[room_id]
            await self._do_update()
