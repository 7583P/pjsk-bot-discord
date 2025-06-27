import discord
import asyncio
import time
from discord.ext import commands, tasks

# Configuración: ID fijo del canal #rooms (hardcode)
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
        return self.bot.get_channel(ROOMS_CHANNEL_ID)

    @tasks.loop(seconds=15.0)
    async def update_rooms(self):
        await self._do_update()

    @update_rooms.before_loop
    async def before_update(self):
        await self.bot.wait_until_ready()

    async def on_room_updated(self, room_id: int | None = None):
        await self._do_update()

    async def _do_update(self):
        try:
            channel = self._get_channel()
            if not channel:
                return

            mm_cog = self.bot.get_cog("Matchmaking")
            rooms = getattr(mm_cog, 'rooms', {}) if mm_cog else {}
            lines: list[str] = []

            if not rooms:
                lines.append("**No hay salas activas.**")
            else:
                rooms_list = []
                # Recolectar datos de cada sala
                for rid, info in rooms.items():
                    players = info.get('players', [])
                    player_data = []
                    mmr_vals = []
                    # Para cada miembro, obtener mmr desde DB y nombre
                    for mem in players:
                        if isinstance(mem, discord.Member):
                            # obtener mmr (sincrónico en loop async)
                            try:
                                mmr_val, _ = await mm_cog.fetch_player(mem.id)
                            except:
                                mmr_val = 0
                            player_data.append((mem, mmr_val))
                            mmr_vals.append(mmr_val)
                    avg_mmr = sum(mmr_vals)//len(mmr_vals) if mmr_vals else 0
                    rooms_list.append((rid, avg_mmr, player_data))

                # ordenar por MMR promedio descendente
                rooms_list.sort(key=lambda x: x[1], reverse=True)

                # Construir líneas
                for rid, avg, pdata in rooms_list:
                    lines.append(f"**Sala {rid}** - MMR promedio: **{avg}**")
                    for mem, mmr in pdata:
                        lines.append(f"{mem.display_name} ({mmr})")
                    lines.append("")

            now = int(time.time())
            lines.append(f"**Last Updated:** <t:{now}:R>")
            content = "\n".join(lines)

            if not self.posted_message:
                self.posted_message = await channel.send(content)
            else:
                await self.posted_message.edit(content=content)
        except Exception as e:
            print(f"›› [Rooms] Error: {e}")

    async def on_room_finished(self, room_id: int):
        await asyncio.sleep(10)
        mm_cog = self.bot.get_cog("Matchmaking")
        if mm_cog and room_id in getattr(mm_cog, 'rooms', {}):
            del mm_cog.rooms[room_id]
            await self._do_update()
