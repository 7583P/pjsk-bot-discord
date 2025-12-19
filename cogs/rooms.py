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

ROOM_CAPACITY = 5


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

    async def _fetch_mmrs_batch(self, mm, user_ids: list[int], season_id: int | None, has_pool: bool) -> dict[int, int]:
        """
        Devuelve {user_id: mmr} usando UNA consulta por lote si hay pool+season.
        """
        if not user_ids:
            return {}

        # Dedup para evitar unnest gigante
        user_ids = list(dict.fromkeys(int(x) for x in user_ids if isinstance(x, int)))

        mmr_map: dict[int, int] = {}
        try:
            if has_pool and season_id:
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
                # Fallback: una a una
                for uid in user_ids:
                    _mmr, _ = await mm.fetch_player(uid)
                    mmr_map[int(uid)] = int(_mmr)
        except Exception:
            # Fallback robusto
            for uid in user_ids:
                try:
                    _mmr, _ = await mm.fetch_player(uid)
                    mmr_map[int(uid)] = int(_mmr)
                except Exception:
                    mmr_map[int(uid)] = 0

        return mmr_map

    def _resolve_users_from_ids(self, guild: discord.Guild, ids: list[int]) -> list[discord.abc.User]:
        """
        Mejor esfuerzo: Member si existe, si no User cacheado.
        """
        out: list[discord.abc.User] = []
        for uid in ids:
            m = guild.get_member(uid)
            if m is not None:
                out.append(m)
                continue
            u = self.bot.get_user(uid)
            if u is not None:
                out.append(u)
        return out

    async def _do_update(self):
        # Serializa updates para evitar condiciones de carrera
        async with self._update_lock:
            try:
                # 1) Obtener estado actual de Matchmaking
                mm = self.bot.get_cog("Matchmaking")
                if mm is None:
                    return

                all_rooms = getattr(mm, "rooms", {}) or {}
                queues = getattr(mm, "queues", {}) or {}

                # 2) Resolver season una sola vez por update
                season_id = await self._get_active_season_id(mm)
                has_pool = getattr(mm, "db_pool", None) is not None and bool(season_id)

                # 3) Construir mapping category_id -> join_channel_id a partir de mm.queues
                #    (así distinguimos EN vs JP sin hardcodear IDs)
                cat_to_join: dict[int, int] = {}
                for join_id in list(queues.keys()):
                    ch = self.bot.get_channel(join_id)
                    if isinstance(ch, discord.TextChannel) and ch.category is not None:
                        cat_id = ch.category.id
                        if cat_id in CATEGORY_TO_ROOM_CHANNEL:
                            cat_to_join[cat_id] = join_id

                # 4) Agrupar rooms por categoría
                # Cada item: (room_id, pdata[(user, mmr)], avg_mmr, started_bool, started_ts|None, thread_id|None)
                grouped: dict[
                    int,
                    list[tuple[int, list[tuple[discord.abc.User, int]], int, bool, int | None, int | None]],
                ] = {}

                # Para minimizar I/O, por cada room seguimos tu estrategia (1 consulta por sala).
                for rid, info in all_rooms.items():
                    cat = info.get("category_id")
                    if cat not in CATEGORY_TO_ROOM_CHANNEL:
                        continue

                    # ---- players puede venir como Member/User o como IDs (int) ----
                    raw_players = info.get("players", []) or []
                    users: list[discord.abc.User] = []
                    user_ids: list[int] = []

                    # OJO: aquí no tenemos guild directo; lo obtendremos luego por canal rooms.
                    # Guardamos IDs y resolvemos users por cache/best effort:
                    for p in raw_players:
                        if isinstance(p, (discord.Member, discord.User)):
                            users.append(p)
                            user_ids.append(p.id)
                        elif isinstance(p, int):
                            u = self.bot.get_user(p)
                            if u is not None:
                                users.append(u)
                            user_ids.append(p)

                    # ---- Obtener MMRs con UNA consulta por sala (menos I/O) ----
                    mmr_map: dict[int, int] = {}
                    if user_ids:
                        mmr_map = await self._fetch_mmrs_batch(mm, user_ids, season_id, has_pool)

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
                    if not lines or not lines[-1].startswith("Last Update"):
                        lines = lines + [footer]

                    content = "\n".join(lines)
                    if len(content) <= 1990:
                        return content

                    body = lines[:-1]
                    while body and len("\n".join(body + ["…(truncated)", footer])) > 1990:
                        body.pop()

                    return "\n".join(body + ["…(truncated)", footer])

                # 5) Para cada categoría, editar o enviar mensaje en su canal rooms
                now_ts = int(time.time())
                for cat_id, room_chan_id in CATEGORY_TO_ROOM_CHANNEL.items():
                    channel = self.bot.get_channel(room_chan_id)
                    if not isinstance(channel, discord.TextChannel):
                        continue

                    guild = channel.guild

                    lines: list[str] = []

                    # ---- A) Active rooms (lo que ya tenías) ----
                    rooms_list = grouped.get(cat_id, [])
                    if rooms_list:
                        rooms_list.sort(key=lambda x: (-x[2], x[0]))  # avg desc, rid asc

                        for rid, pdata, avg, started_flag, started_ts, thread_id in rooms_list:
                            count = len(pdata)

                            if started_flag:
                                if isinstance(started_ts, int) and started_ts > 0:
                                    status = f"[STARTED] <t:{started_ts}:t>"
                                else:
                                    status = "[STARTED]"
                            else:
                                status = "[NOT STARTED]"

                            ref = f"<#{thread_id}>" if isinstance(thread_id, int) and thread_id > 0 else None

                            if ref:
                                lines.append(
                                    f"**Room {rid} · {ref} · {count}/5 Players · Average MMR {avg} · {status}**"
                                )
                            else:
                                lines.append(f"**Room {rid} · {count}/5 Players · Average MMR {avg} · {status}**")

                            for user, mmr in pdata:
                                lines.append(f"- {user.display_name} ({mmr})")
                            lines.append("")
                    else:
                        # No imprimimos todavía "No active rooms" porque ahora habrá queue NO-STARTED
                        pass

                    # ---- B) Queue NO-STARTED (nuevo) ----
                    join_id = cat_to_join.get(cat_id)
                    queue_raw = queues.get(join_id, []) if isinstance(join_id, int) else []

                    # Normalizar a IDs
                    queue_ids: list[int] = []
                    for item in (queue_raw or []):
                        if isinstance(item, int):
                            queue_ids.append(item)
                        elif isinstance(item, (discord.Member, discord.User)):
                            queue_ids.append(item.id)

                    # Construir grupos de 5 (future rooms)
                    if queue_ids:
                        # Resolver usuarios best-effort en guild (para display_name)
                        users_queue = self._resolve_users_from_ids(guild, queue_ids)

                        # Orden: respetar orden de cola, pero necesitamos mmr para imprimir
                        ids_in_order = [u.id for u in users_queue]

                        mmr_map_q = await self._fetch_mmrs_batch(mm, ids_in_order, season_id, has_pool)

                        # Si hay rooms activos arriba, separador
                        if lines and not lines[-1].strip():
                            pass
                        if lines:
                            lines.append("**Queue (NO-STARTED)**")

                        # Chunks de 5
                        for idx in range(0, len(users_queue), ROOM_CAPACITY):
                            grp = users_queue[idx: idx + ROOM_CAPACITY]
                            grp_mmrs = [mmr_map_q.get(u.id, 0) for u in grp]
                            avg_q = (sum(grp_mmrs) // len(grp_mmrs)) if grp_mmrs else 0
                            count_q = len(grp)

                            room_n = (idx // ROOM_CAPACITY) + 1
                            lines.append(f"**Room Q{room_n} · {count_q}/5 Players · Average MMR {avg_q} · [NO-STARTED]**")
                            for u in grp:
                                lines.append(f"- {u.display_name} ({mmr_map_q.get(u.id, 0)})")
                            lines.append("")
                    else:
                        # Si no hay active rooms y no hay queue, ahora sí mostramos el mensaje
                        if not rooms_list:
                            lines.append("**No active rooms**")

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
