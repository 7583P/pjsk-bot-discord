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

        # Evita que loop + eventos ejecuten _do_update a la vez
        self._update_lock = asyncio.Lock()

        # Listeners para refrescar al vuelo
        bot.add_listener(self.on_room_updated, "room_updated")
        bot.add_listener(self.on_room_finished, "room_finished")

        # Loop de respaldo cada 15s
        self.update_rooms.start()

    def cog_unload(self):
        self.update_rooms.cancel()
        self.bot.remove_listener(self.on_room_updated, "room_updated")
        self.bot.remove_listener(self.on_room_finished, "room_finished")

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

    async def _get_active_season_id(self, mm) -> int | None:
        # intenta reutilizar info del cog Matchmaking si la expone
        sid = getattr(mm, "current_season_id", None)
        if isinstance(sid, int) and sid > 0:
            return sid

        # fallback: leer de la BD
        if getattr(mm, "db_pool", None) is None:
            return None

        try:
            async with mm.db_pool.acquire() as con:
                row = await con.fetchrow(
                    "SELECT id FROM seasons WHERE active = true ORDER BY startdate DESC LIMIT 1"
                )
                return int(row["id"]) if row else None
        except Exception:
            return None

    def _coerce_started_ts(self, raw):
        """Acepta int/float o datetime-like (con .timestamp())."""
        if isinstance(raw, (int, float)):
            ts = int(raw)
            return ts if ts > 0 else None
        if raw is not None and hasattr(raw, "timestamp"):
            try:
                ts = int(raw.timestamp())
                return ts if ts > 0 else None
            except Exception:
                return None
        return None

    async def _do_update(self):
        # Serializa updates para evitar condiciones de carrera
        async with self._update_lock:
            try:
                # 1) Obtener estado actual de Matchmaking
                mm = self.bot.get_cog("Matchmaking")
                if mm is None:
                    return

                all_rooms = getattr(mm, "rooms", {}) or {}

                # 2) Resolver season una sola vez por update
                season_id = await self._get_active_season_id(mm)
                has_pool = getattr(mm, "db_pool", None) is not None and bool(season_id)

                # 3) Agrupar por categoría
                # Cada item: (room_id, pdata[(user, mmr)], avg_mmr, started_bool, started_ts|None, thread_id|None)
                grouped: dict[
                    int,
                    list[tuple[int, list[tuple[discord.abc.User, int]], int, bool, int | None, int | None]],
                ] = {}

                for rid, info in all_rooms.items():
                    cat = info.get("category_id")
                    if cat not in CATEGORY_TO_ROOM_CHANNEL:
                        continue

                    # ---- players puede venir como Member/User o como IDs (int) ----
                    raw_players = info.get("players", []) or []
                    users: list[discord.abc.User] = []
                    user_ids: list[int] = []

                    for p in raw_players:
                        if isinstance(p, (discord.Member, discord.User)):
                            users.append(p)
                            user_ids.append(p.id)
                        elif isinstance(p, int):
                            # mejor esfuerzo: User cacheado (no hacemos fetch masivo para no golpear rate limits)
                            u = self.bot.get_user(p)
                            if u is not None:
                                users.append(u)
                            user_ids.append(p)

                    # ---- Obtener MMRs con UNA consulta por sala (menos I/O) ----
                    mmr_map: dict[int, int] = {}
                    if user_ids:
                        try:
                            if has_pool:
                                async with mm.db_pool.acquire() as con:
                                    rows_db = await con.fetch(
                                        """
                                        WITH u AS (SELECT unnest($1::bigint[]) AS user_id)
                                        SELECT u.user_id,
                                            COALESCE(sp.mmr, 0) AS mmr
                                        FROM u
                                        LEFT JOIN seasonplayers sp
                                            ON sp.user_id = u.user_id
                                            AND sp.season_id = $2
                                        """,
                                        user_ids,
                                        season_id,
                                    )
                                mmr_map = {int(r["user_id"]): int(r["mmr"]) for r in rows_db}
                            else:
                                # Fallback: una a una (si no hay pool o no se pudo resolver season)
                                for uid in user_ids:
                                    _mmr, _ = await mm.fetch_player(uid)  # debe leer current season
                                    mmr_map[uid] = int(_mmr)
                        except Exception:
                            # Fallback robusto si falla la consulta por lote
                            for uid in user_ids:
                                _mmr, _ = await mm.fetch_player(uid)
                                mmr_map[uid] = int(_mmr)

                    # pdata = [(user, mmr)] ORDENADO por MMR DESC
                    pdata: list[tuple[discord.abc.User, int]] = [(u, mmr_map.get(u.id, 0)) for u in users]
                    pdata.sort(key=lambda t: t[1], reverse=True)

                    mmr_vals = [mmr for _, mmr in pdata]
                    avg = (sum(mmr_vals) // len(mmr_vals)) if mmr_vals else 0

                    count = len(pdata)
                    started_flag = bool(info.get("started")) or (count >= 5)

                    started_ts = self._coerce_started_ts(
                        info.get("started_ts")
                        or info.get("start_ts")
                        or info.get("started_at")
                        or info.get("reached5_ts")
                    )

                    # Opcional: si Matchmaking guarda thread/canal por sala, lo mostramos
                    thread_id = None
                    raw_thread = info.get("thread")
                    if isinstance(raw_thread, discord.Thread):
                        thread_id = raw_thread.id
                    else:
                        tid = info.get("thread_id") or info.get("thread_channel_id")
                        if isinstance(tid, int) and tid > 0:
                            thread_id = tid

                    grouped.setdefault(cat, []).append((rid, pdata, avg, started_flag, started_ts, thread_id))

                # helper: truncar si excede 2000 chars
                def build_content(lines: list[str], now_ts: int) -> str:
                    footer = f"Last Update <t:{now_ts}:R>"
                    # Asegura footer presente
                    if not lines or not lines[-1].startswith("Last Update"):
                        lines = lines + [footer]

                    # Truncado conservador
                    content = "\n".join(lines)
                    if len(content) <= 1990:
                        return content

                    body = lines[:-1]
                    while body and len("\n".join(body + ["…(truncated)", footer])) > 1990:
                        body.pop()

                    return "\n".join(body + ["…(truncated)", footer])

                # 4) Para cada categoría, editar o enviar mensaje en su canal rooms
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

                        for rid, pdata, avg, started_flag, started_ts, thread_id in rooms_list:
                            count = len(pdata)

                            # Formateo de estado
                            if started_flag:
                                if isinstance(started_ts, int) and started_ts > 0:
                                    status = f"[STARTED] <t:{started_ts}:t>"
                                else:
                                    status = "[STARTED]"
                            else:
                                status = "[NOT STARTED]"

                            # Referencia al thread/canal si existe
                            ref = f"<#{thread_id}>" if isinstance(thread_id, int) and thread_id > 0 else None

                            # Encabezado de la sala
                            if ref:
                                lines.append(
                                    f"**Room {rid} · {ref} · {count}/5 Players · Average MMR {avg} · {status}**"
                                )
                            else:
                                lines.append(f"**Room {rid} · {count}/5 Players · Average MMR {avg} · {status}**")

                            # Lista de jugadores (ordenada por MMR DESC)
                            for user, mmr in pdata:
                                # user puede ser User (cache) o Member; display_name existe en ambos en discord.py 2.x
                                lines.append(f"- {user.display_name} ({mmr})")
                            lines.append("")

                    content = build_content(lines, now_ts)

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
