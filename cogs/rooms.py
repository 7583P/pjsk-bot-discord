import discord
import asyncio
import time
from discord.ext import commands, tasks

# Mapea category_id → canal "rooms" correspondiente
CATEGORY_TO_ROOM_CHANNEL: dict[int, int] = {
    1371306302671687710: 1371307831176728706,  # pjsk queue → rooms pjsk queue
    1371951461612912802: 1388515368934309978,  # jp-pjsk    → rooms jp-pjsk
}

async def setup(bot: commands.Bot):
    await bot.add_cog(Rooms(bot))
    print("✅ Cog cargado: cogs.rooms")

class Rooms(commands.Cog):
    """Cog para publicar y actualizar las salas en cada canal de rooms según su categoría."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # posted_messages: { category_id: discord.Message }
        self.posted_messages: dict[int, discord.Message] = {}
        self.update_rooms.start()
        bot.add_listener(self.on_room_updated, 'room_updated')
        bot.add_listener(self.on_room_finished, 'room_finished')

    def cog_unload(self):
        self.update_rooms.cancel()
        self.bot.remove_listener(self.on_room_updated)
        self.bot.remove_listener(self.on_room_finished)

    @tasks.loop(seconds=15.0)
    async def update_rooms(self):
        await self._do_update()

    @update_rooms.before_loop
    async def before_update(self):
        await self.bot.wait_until_ready()

    async def on_room_updated(self, room_id: int | None = None):
        await self._do_update()

    async def on_room_finished(self, room_id: int):
        # pequeño delay antes de refrescar
        await asyncio.sleep(10)
        mm_cog = self.bot.get_cog("Matchmaking")
        if mm_cog and room_id in getattr(mm_cog, 'rooms', {}):
            del mm_cog.rooms[room_id]
        await self._do_update()

    async def _do_update(self):
        try:
            mm_cog = self.bot.get_cog("Matchmaking")
            rooms = getattr(mm_cog, 'rooms', {}) if mm_cog else {}

            # Agrupar rooms por categoría
            grouped: dict[int, list[tuple[int,int,list[tuple[discord.Member,int]]]]] = {}
            for rid, info in rooms.items():
                cat = info.get('category_id')
                if cat not in CATEGORY_TO_ROOM_CHANNEL:
                    continue  # no tenemos canal configurado para esta categoría
                players = info.get('players', [])
                pdata: list[tuple[discord.Member,int]] = []
                mmr_vals: list[int] = []
                for mem in players:
                    if isinstance(mem, discord.Member):
                        try:
                            mmr_val, _ = await mm_cog.fetch_player(mem.id)
                        except:
                            mmr_val = 0
                        pdata.append((mem, mmr_val))
                        mmr_vals.append(mmr_val)
                avg = sum(mmr_vals)//len(mmr_vals) if mmr_vals else 0
                grouped.setdefault(cat, []).append((rid, avg, pdata))

            now = int(time.time())
            # Para cada categoría configurada, construir y enviar/editar el mensaje
            for cat_id, channel_id in CATEGORY_TO_ROOM_CHANNEL.items():
                channel = self.bot.get_channel(channel_id)
                if not channel:
                    continue

                lines: list[str] = []
                rooms_list = grouped.get(cat_id, [])
                if not rooms_list:
                    lines.append("**No active rooms**")
                else:
                    # ordenar por MMR promedio descendente
                    rooms_list.sort(key=lambda x: x[1], reverse=True)
                    for rid, avg, pdata in rooms_list:
                        lines.append(f"**Room {rid}** – Average MMR: **{avg}**")
                        for mem, mmr in pdata:
                            lines.append(f"{mem.display_name} ({mmr})")
                        lines.append("")

                lines.append(f"**Last Updated:** <t:{now}:R>")
                content = "\n".join(lines)

                # enviar o editar
                if cat_id not in self.posted_messages or self.posted_messages[cat_id].channel.id != channel_id:
                    msg = await channel.send(content)
                    self.posted_messages[cat_id] = msg
                else:
                    await self.posted_messages[cat_id].edit(content=content)
        except Exception as e:
            print(f"›› [Rooms] Error: {e}")
