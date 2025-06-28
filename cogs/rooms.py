# cogs/rooms.py

import discord
import asyncio
import time
from discord.ext import commands, tasks

# Mapea cada category_id al canal “rooms” correspondiente
CATEGORY_TO_ROOM_CHANNEL: dict[int, int] = {
    1371306302671687710: 1371307831176728706,  # pjsk queue → rooms pjsk queue
    1371951461612912802: 1388515368934309978,  # jp-pjsk    → rooms jp-pjsk
}

class Rooms(commands.Cog):
    """Cog que lee Matchmaking.rooms y publica/actualiza cada canal #rooms."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Guarda el mensaje enviado en cada canal rooms para editarlo luego
        self.posted_messages: dict[int, discord.Message] = {}

        # Inicia el loop de respaldo
        self.update_rooms.start()
        # Se suscribe a los eventos disparados por matchmaking.py
        bot.add_listener(self.on_room_updated, 'room_updated')
        bot.add_listener(self.on_room_finished, 'room_finished')

    def cog_unload(self):
        self.update_rooms.cancel()
        self.bot.remove_listener(self.on_room_updated)
        self.bot.remove_listener(self.on_room_finished)

    @commands.Cog.listener()
    async def on_ready(self):
        print("✅ Cog cargado: cogs.rooms")

    @tasks.loop(seconds=15.0)
    async def update_rooms(self):
        # Refresco periódico por si se pierde algún evento
        await self._do_update()

    @update_rooms.before_loop
    async def before_update(self):
        await self.bot.wait_until_ready()

    async def on_room_updated(self, room_id: int | None = None):
        # Disparado justo después de /c o /d en matchmaking
        await self._do_update()

    async def on_room_finished(self, room_id: int):
        # Disparado justo después de eliminar sala vacía
        await asyncio.sleep(1)
        await self._do_update()

    async def _do_update(self):
        try:
            # 1) Leer el estado de las salas desde el cog Matchmaking
            mm = self.bot.get_cog("Matchmaking")
            all_rooms = getattr(mm, "rooms", {})

            # 2) Agrupar salas por categoría
            grouped: dict[int, list[tuple[int,int,list[tuple[discord.Member,int]]]]] = {}
            for rid, info in all_rooms.items():
                cat = info.get("category_id")
                if cat not in CATEGORY_TO_ROOM_CHANNEL:
                    continue

                pdata, mmr_vals = [], []
                for member in info.get("players", []):
                    if not isinstance(member, discord.Member):
                        continue
                    try:
                        mmr, _ = await mm.fetch_player(member.id)
                    except:
                        mmr = 0
                    pdata.append((member, mmr))
                    mmr_vals.append(mmr)

                avg = sum(mmr_vals) // len(mmr_vals) if mmr_vals else 0
                grouped.setdefault(cat, []).append((rid, avg, pdata))

            now = int(time.time())

            # 3) Para cada categoría, publica o edita en su canal de rooms
            for cat_id, room_chan_id in CATEGORY_TO_ROOM_CHANNEL.items():
                channel = self.bot.get_channel(room_chan_id)
                if not channel:
                    continue

                lines: list[str] = []
                rooms_list = grouped.get(cat_id, [])
                if not rooms_list:
                    lines.append("**No active rooms**")
                else:
                    rooms_list.sort(key=lambda x: x[1], reverse=True)
                    for rid, avg, pdata in rooms_list:
                        lines.append(f"**Room {rid}** – Average MMR: **{avg}**")
                        for mem, mmr in pdata:
                            lines.append(f"{mem.display_name} ({mmr})")
                        lines.append("")

                lines.append(f"**Last Updated:** <t:{now}:R>")
                content = "\n".join(lines)

                prev_msg = self.posted_messages.get(cat_id)
                if not prev_msg or prev_msg.channel.id != room_chan_id:
                    msg = await channel.send(content)
                    self.posted_messages[cat_id] = msg
                else:
                    await prev_msg.edit(content=content)

        except Exception as e:
            print(f"›› [Rooms] Error en _do_update: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(Rooms(bot))
