# cogs/rooms.py

import time
import asyncio
import discord
from discord.ext import commands, tasks

# Mapea cada category_id al canal “rooms” correspondiente
CATEGORY_TO_ROOM_CHANNEL: dict[int, int] = {
    1371306302671687710: 1371307831176728706,  # pjsk-queue → rooms pjsk-queue
    1371951461612912802: 1388515368934309978,  # jp-pjsk    → rooms jp-pjsk
}


class Rooms(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Será un dict: { category_id: discord.Message }
        self.posted_messages: dict[int, discord.Message] = {}

        # Listeners para refrescar al vuelo
        bot.add_listener(self.on_room_updated, 'room_updated')
        bot.add_listener(self.on_room_finished, 'room_finished')

        # Loop de respaldo cada 15s
        self.update_rooms.start()

    def cog_unload(self):
        self.update_rooms.cancel()
        self.bot.remove_listener(self.on_room_updated, 'room_updated')
        self.bot.remove_listener(self.on_room_finished, 'room_finished')

    @commands.Cog.listener()
    async def on_ready(self):
        print("✅ Cog cargado: cogs.rooms")

    @tasks.loop(seconds=15.0)
    async def update_rooms(self):
        await self._do_update()

    @update_rooms.before_loop
    async def before_update(self):
        await self.bot.wait_until_ready()

    async def on_room_updated(self, room_id: int | None = None):
        # Se dispara desde Matchmaking tras /start, /join, /d…
        await self._do_update()

    async def on_room_finished(self, room_id: int):
        # Tras eliminar una sala vacía
        await asyncio.sleep(1)
        await self._do_update()

    async def _do_update(self):
        try:
            # 1) Obtener estado actual de Matchmaking
            mm = self.bot.get_cog("Matchmaking")
            if mm is None:
                return
            all_rooms = getattr(mm, "rooms", {}) or {}

            # 2) Agrupar por categoría
            #    Cada item: (room_id, players_data[(member, mmr)], avg_mmr, started_bool, started_ts|None)
            grouped: dict[int, list[tuple[int, list[tuple[discord.Member, int]], int, bool, int | None]]] = {}

            for rid, info in all_rooms.items():
                cat = info.get("category_id")
                if cat not in CATEGORY_TO_ROOM_CHANNEL:
                    continue

                pdata: list[tuple[discord.Member, int]] = []
                mmr_vals: list[int] = []

                # Construimos listado de jugadores con MMR actual
                for member in info.get("players", []):
                    if not isinstance(member, discord.Member):
                        continue
                    # fetch_player devuelve (mmr, role)
                    mmr, _ = await mm.fetch_player(member.id)
                    pdata.append((member, mmr))
                    mmr_vals.append(mmr)

                avg = sum(mmr_vals) // len(mmr_vals) if mmr_vals else 0

                # Estado de inicio y timestamp
                count = len(pdata)
                started_flag = bool(info.get("started")) or (count >= 5)
                # Aceptamos varias claves posibles por compatibilidad
                started_ts = (
                    info.get("started_ts")
                    or info.get("start_ts")
                    or info.get("started_at")
                    or info.get("reached5_ts")
                )
                # Si sabemos que está iniciada pero no tenemos ts, lo dejamos en None
                grouped.setdefault(cat, []).append((rid, pdata, avg, started_flag, started_ts))

            # 3) Para cada categoría, editar o enviar mensaje en su canal rooms
            now_ts = int(time.time())
            for cat_id, room_chan_id in CATEGORY_TO_ROOM_CHANNEL.items():
                channel = self.bot.get_channel(room_chan_id)
                if not isinstance(channel, discord.TextChannel):
                    continue

                lines: list[str] = []
                rooms_list = grouped.get(cat_id, [])

                if not rooms_list:
                    lines.append("**No active rooms**")
                else:
                    # Orden: por Average MMR desc, si empatan por room_id asc
                    rooms_list.sort(key=lambda x: (-x[2], x[0]))

                    for rid, pdata, avg, started_flag, started_ts in rooms_list:
                        count = len(pdata)

                        # Formateo de estado
                        if started_flag:
                            # Si hay timestamp, lo mostramos a la derecha de [STARTED]
                            if isinstance(started_ts, int) and started_ts > 0:
                                status = f"[STARTED] <t:{started_ts}:t>"
                            else:
                                status = "[STARTED]"
                        else:
                            status = "[NOT STARTED]"

                        # Encabezado de la sala
                        lines.append(
                            f"**Room {rid} · {count}/5 Players · Average MMR {avg} · {status}**"
                        )

                        # Lista de jugadores
                        for member, mmr in pdata:
                            lines.append(f"- {member.display_name} ({mmr})")
                        lines.append("")  # separación

                # Pie con timestamp relativo de actualización
                lines.append(f"Last Update <t:{now_ts}:R>")
                content = "\n".join(lines)

                prev_msg = self.posted_messages.get(cat_id)
                if prev_msg is None or prev_msg.channel.id != room_chan_id:
                    msg = await channel.send(content)
                    self.posted_messages[cat_id] = msg
                else:
                    if prev_msg.content != content:
                        await prev_msg.edit(content=content)

        except Exception as e:
            print(f"[Rooms] Error en _do_update: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Rooms(bot))
