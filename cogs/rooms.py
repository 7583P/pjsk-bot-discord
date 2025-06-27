import discord
import asyncio
import time
from discord.ext import commands, tasks

# ID fijo del canal #rooms (hardcode)
ROOMS_CHANNEL_ID = 1371307831176728706

async def setup(bot: commands.Bot):
    await bot.add_cog(Rooms(bot))
    print("✅ Cog cargado: cogs.rooms")

class Rooms(commands.Cog):
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

            mm = self.bot.get_cog("Matchmaking")
            if not mm or not hasattr(mm, 'rooms'):
                return
            rooms = mm.rooms

            lines: list[str] = []
            if not rooms:
                lines.append("**No hay salas activas.**")
            else:
                rooms_list = []
                for room_id, players in rooms.items():
                    mmr_vals = []
                    valid = []
                    for entry in players:
                        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                            member, raw = entry
                            try:
                                val = int(raw)
                            except:
                                val = 0
                            mmr_vals.append(val)
                            valid.append((member, val))
                    avg = sum(mmr_vals) // len(mmr_vals) if mmr_vals else 0
                    rooms_list.append((room_id, avg, valid))
                # ordenar desc
                rooms_list.sort(key=lambda x: x[1], reverse=True)

                for room_id, avg, players in rooms_list:
                    lines.append(f"**Sala {room_id}** - MMR promedio: **{avg}**")
                    for member, val in players:
                        # obtiene display_name o name
                        name = getattr(member, 'display_name', None) or getattr(member, 'name', None) or str(member)
                        lines.append(f"{name} ({val})")
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
                    # mensaje original perdido: no volvemos a enviar
                    pass

        except Exception as e:
            print(f"›› [Rooms] Excepción en _do_update: {e}")

    async def on_room_finished(self, room_id: int):
        await asyncio.sleep(10)
        mm = self.bot.get_cog("Matchmaking")
        if mm and hasattr(mm, 'rooms') and room_id in mm.rooms:
            del mm.rooms[room_id]
            await self._do_update()
