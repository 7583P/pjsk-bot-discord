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

                # Miembros válidos
                players_list = [m for m in info.get("players", []) if isinstance(m, discord.Member)]
                ids = [m.id for m in players_list]

                # ----- Obtener MMRs con UNA consulta por sala (menos I/O) -----
                mmr_map: dict[int, int] = {}
                if ids:
                    try:
                        # Usar el pool del Matchmaking si existe
                        if getattr(mm, "db_pool", None) is not None:
                            async with mm.db_pool.acquire() as con:
                                rows_db = await con.fetch(
                                    "SELECT p.user_id, COALESCE(sp.mmr,0) AS mmr FROM players p inner join seasonplayers sp on p.user_id = sp.user_id WHERE p.user_id = ANY($1::bigint[])",
                                    ids
                                )
                            mmr_map = {int(r["user_id"]): int(r["mmr"]) for r in rows_db}
                        else:
                            # Fallback: una a una (más I/O, pero seguro)
                            for m in players_list:
                                _mmr, _ = await mm.fetch_player(m.id)
                                mmr_map[m.id] = int(_mmr)
                    except Exception:
                        # Fallback robusto si falla la consulta por lote
                        for m in players_list:
                            _mmr, _ = await mm.fetch_player(m.id)
                            mmr_map[m.id] = int(_mmr)

                # pdata = [(member, mmr)] ORDENADO por MMR DESC (mayor arriba)
                pdata: list[tuple[discord.Member, int]] = [(m, mmr_map.get(m.id, 0)) for m in players_list]
                pdata.sort(key=lambda t: t[1], reverse=True)  # ← orden dentro de la sala por MMR

                mmr_vals = [mmr for _, mmr in pdata]
                avg = (sum(mmr_vals) // len(mmr_vals)) if mmr_vals else 0

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
                        lines.append(f"**Room {rid} · {count}/5 Players · Average MMR {avg} · {status}**")

                        # Lista de jugadores (ya viene ordenada por MMR DESC)
                        for member, mmr in pdata:
                            lines.append(f"- {member.display_name} ({mmr})")
                        lines.append("")  # separación

                # Pie con timestamp relativo de actualización
                lines.append(f"Last Update <t:{now_ts}:R>")
                content = "\n".join(lines)

                prev_msg = self.posted_messages.get(cat_id)

                # Si no existe mensaje previo o el canal cambió, mandamos uno nuevo
                if prev_msg is None or prev_msg.channel.id != room_chan_id:
                    try:
                        msg = await channel.send(content)
                    except discord.Forbidden:
                        continue
                    self.posted_messages[cat_id] = msg
                    continue

                # Si existe, intentamos editar; si fue borrado, re-crear
                if prev_msg.content != content:
                    try:
                        await prev_msg.edit(content=content)
                    except (discord.NotFound, discord.HTTPException):
                        try:
                            msg = await channel.send(content)
                            self.posted_messages[cat_id] = msg
                        except discord.Forbidden:
                            pass

        except Exception as e:
            print(f"[Rooms] Error en _do_update: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Rooms(bot))
