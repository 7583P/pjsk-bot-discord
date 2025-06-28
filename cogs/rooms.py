# cogs/rooms.py

import discord
import asyncio
import time
from discord.ext import commands, tasks

# Ajusta estos mapeos a los IDs de tus categorías y canales rooms
CATEGORY_TO_ROOM_CHANNEL: dict[int, int] = {
    1371306302671687710: 1371307831176728706,  # pjsk queue → rooms pjsk queue
    1371951461612912802: 1388515368934309978,  # jp-pjsk    → rooms jp-pjsk
}

class Rooms(commands.Cog):
    """Lee Matchmaking.rooms y publica/actualiza cada canal de rooms."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Guardamos el mensaje enviado en cada canal para editarlo después
        self.posted_messages: dict[int, discord.Message] = {}

        # Lanzamos el loop y escuchamos los eventos de Matchmaking
        self.update_rooms.start()
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
        # Refresca cada 15s por seguridad
        await self._do_update()

    @update_rooms.before_loop
    async def before_update(self):
        await self.bot.wait_until_ready()

    async def on_room_updated(self, room_id: int | None = None):
        # Disparado desde matchmaking.py tras /c o /d
        await self._do_update()

    async def on_room_finished(self, room_id: int):
        # Disparado desde matchmaking.py al vaciar y cerrar una sala
        await asyncio.sleep(1)
        await self._do_update()

    async def _do_update(self):
        try:
            # 1) Obtiene el cog de Matchmaking y su dict de rooms
            mm = self.bot.get_cog("Matchmaking")
            all_rooms = getattr(mm, "rooms", {})

            # 2) Agrupa por categoría
            grouped: dict[int, list[tuple[int,int,list[tuple[discord.Member,int]]]]] = {}
            for rid, info in all_rooms.items():
                cat = info.get("category_id")
                if cat not in CATEGORY_TO_ROOM_CHANNEL:
                    continue

                pdata, mmr_vals = [], []
                for member in info.get("players", []):
                    if not isinstance(member, discord.Member):
                        continue
                    # Llama a tu función real de fetch_player en matchmaking
                    try:
                        mmr, _ = await mm.fetch_player(member.id)
                    except:
                        mmr = 0
                    pdata.append((member, mmr))
                    mmr_vals.append(mmr)

                avg = sum(mmr_vals) // len(mmr_vals) if mmr_vals else 0
                grouped.setdefault(cat, []).append((rid, avg, pdata))

            now = int(time.time())

            # 3) Para cada categoría, envía o edita en su canal rooms
            for cat_id, room_chan_id in CATEGORY_TO_ROOM_CHANNEL.items():
                channel = self.bot.get_channel(room_chan_id)
                if not channel:
                    continue

                lines: list[str] = []
                rooms_list = grouped.get(cat_id, [])
                if not rooms_list:
                    lines.append("**No active rooms**")
                else:
                    # Orden descendiente por MMR promedio
                    rooms_list.sort(key=lambda x: x[1], reverse=True)
                    for rid, avg, pdata in rooms_list:
                        lines.append(f"**Room {rid}** – Average MMR: **{avg}**")
                        for mem, mmr in pdata:
                            lines.append(f"{mem.display_name} ({mmr})")
                        lines.append("")  # línea en blanco entre salas

                lines.append(f"**Last Updated:** <t:{now}:R>")
                content = "\n".join(lines)

                prev = self.posted_messages.get(cat_id)
                if prev is None or prev.channel.id != room_chan_id:
                    msg = await channel.send(content)
                    self.posted_messages[cat_id] = msg
                else:
                    await prev.edit(content=content)

        except Exception as e:
            print(f"›› [Rooms] Error en _do_update: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(Rooms(bot))
