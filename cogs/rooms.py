# cogs/rooms.py

import time, json
import asyncio
import discord
from discord.ext import commands, tasks

# Mapea cada category_id al canal “rooms” correspondiente
CATEGORY_TO_ROOM_CHANNEL: dict[int, int] = {
    1371306302671687710: 1371307831176728706,  # pjsk-queue → rooms pjsk-queue
    1371951461612912802: 1388515368934309978,  # jp-pjsk    → rooms jp-pjsk
}

ROOM_CAPACITY = 5

# Marcador simple para identificar el “mensaje tablero” del bot
BOARD_MARKER = "Last Update"

# Fases (para que rooms.py pueda renderizar FUTURE correctamente)
PHASE_FUTURE = "FUTURE"
PHASE_PREPARED = "PREPARED"
PHASE_STARTED = "STARTED"
PHASE_FINISHED = "FINISHED"


class Rooms(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # { category_id: discord.Message }
        self.posted_messages: dict[int, discord.Message] = {}

        # Evita condiciones de carrera
        self._update_lock = asyncio.Lock()

        # Coalesce: si llegan muchos eventos, hacemos como máximo 1 update extra al final
        self._update_pending: bool = False

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
        await self._request_update()

    @update_rooms.before_loop
    async def before_update(self):
        await self.bot.wait_until_ready()

    async def on_room_updated(self, room_id: int | None = None):
        # Se dispara desde Matchmaking tras /start, /join, /d…
        await self._request_update()

    async def on_room_finished(self, room_id: int):
        # Tras eliminar una sala vacía
        await asyncio.sleep(1)
        await self._request_update()

    async def _request_update(self):
        """
        Evita que se encolen muchos updates. Si ya hay uno corriendo, marca pending y sale.
        El update en curso ejecutará uno extra al finalizar.
        """
        if self._update_lock.locked():
            self._update_pending = True
            return

        async with self._update_lock:
            while True:
                self._update_pending = False
                await self._do_update_core()
                if not self._update_pending:
                    break

    async def _get_text_channel(
        self,
        channel_id: int,
        cache: dict[int, discord.abc.GuildChannel | None] | None = None,
    ) -> discord.TextChannel | None:
        """
        get_channel() depende de caché; fetch_channel() es fallback.
        Usa cache local por update para no repetir fetches.
        """
        if not isinstance(channel_id, int) or channel_id <= 0:
            return None

        if cache is not None and channel_id in cache:
            ch = cache[channel_id]
            return ch if isinstance(ch, discord.TextChannel) else None

        ch = self.bot.get_channel(channel_id)
        if isinstance(ch, discord.TextChannel):
            if cache is not None:
                cache[channel_id] = ch
            return ch

        try:
            fetched = await self.bot.fetch_channel(channel_id)
            if cache is not None:
                cache[channel_id] = fetched
            return fetched if isinstance(fetched, discord.TextChannel) else None
        except Exception:
            if cache is not None:
                cache[channel_id] = None
            return None

    async def _find_last_board_message(
        self, channel: discord.TextChannel
    ) -> discord.Message | None:
        """
        Reutiliza el último mensaje del bot en el canal (para no duplicar mensajes tras reinicios).
        Requiere permiso de Read Message History.
        """
        try:
            me = channel.guild.me or self.bot.user
            if me is None:
                return None
            async for msg in channel.history(limit=30):
                if msg.author and msg.author.id == me.id:
                    # Opcional: filtrar por marcador para evitar editar mensajes del bot no relacionados
                    if msg.content and BOARD_MARKER in msg.content:
                        return msg
            # Si no encontró con marcador, igual intentamos con el último mensaje del bot
            async for msg in channel.history(limit=30):
                if msg.author and msg.author.id == me.id:
                    return msg
        except Exception:
            return None
        return None

    async def _ensure_prev_message(self, cat_id: int, channel: discord.TextChannel):
        """
        Si no existe referencia en memoria (reinicio del bot), intenta recuperar un mensaje previo del bot.
        """
        if cat_id in self.posted_messages:
            return
        prev = await self._find_last_board_message(channel)
        if prev is not None:
            self.posted_messages[cat_id] = prev

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

    async def _fetch_mmrs_batch(
        self,
        mm,
        user_ids: list[int],
        season_id: int | None,
        has_pool: bool,
    ) -> dict[int, int]:
        """
        Devuelve {user_id: mmr} usando UNA consulta por lote si hay pool+season.
        """
        if not user_ids:
            return {}

        # Dedup conservando orden
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

    def _user_label(self, guild: discord.Guild, uid: int) -> str:
        """
        Mejor esfuerzo: Member -> User cacheado -> mention por ID.
        Así NO perdemos jugadores aunque no estén en caché.
        """
        m = guild.get_member(uid)
        if m is not None:
            return m.display_name
        u = self.bot.get_user(uid)
        if u is not None:
            return u.display_name
        return f"<@{uid}>"

    async def _do_update_core(self):
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
            #    (distinguimos EN vs JP sin hardcodear JOIN IDs)
            ch_cache: dict[int, discord.abc.GuildChannel | None] = {}
            cat_to_join: dict[int, int] = {}

            for join_id in list(queues.keys()):
                ch = await self._get_text_channel(join_id, cache=ch_cache)
                if ch is None or ch.category is None:
                    continue
                cat_id = ch.category.id
                if cat_id in CATEGORY_TO_ROOM_CHANNEL:
                    cat_to_join[cat_id] = join_id

            # 4) Agrupar rooms por categoría
            # tuple:
            # (rid, pdata_ids, avg, started_flag, started_ts, reached5_ts, thread_id, phase, slot_start_ts, songs_posted)
            grouped: dict[int, list[tuple]] = {}

            for rid, info in all_rooms.items():
                cat = info.get("category_id")
                if cat not in CATEGORY_TO_ROOM_CHANNEL:
                    continue

                raw_players = info.get("players", []) or []
                player_ids: list[int] = []

                for p in raw_players:
                    if isinstance(p, (discord.Member, discord.User)):
                        player_ids.append(p.id)
                    elif isinstance(p, int):
                        player_ids.append(p)

                # dedup conservando orden
                player_ids = list(dict.fromkeys(player_ids))

                mmr_map: dict[int, int] = {}
                if player_ids:
                    mmr_map = await self._fetch_mmrs_batch(
                        mm, player_ids, season_id, has_pool
                    )

                pdata_ids: list[tuple[int, int]] = [
                    (uid, mmr_map.get(uid, 0)) for uid in player_ids
                ]

                # Orden jugadores: MMR desc, y en empate user_id desc (estable)
                pdata_ids.sort(key=lambda t: (int(t[1]), int(t[0])), reverse=True)

                mmr_vals = [mmr for _, mmr in pdata_ids]
                avg = (sum(mmr_vals) // len(mmr_vals)) if mmr_vals else 0

                # Fase / scheduling
                phase = info.get("phase")
                if isinstance(phase, str):
                    phase = phase.upper()
                else:
                    phase = None

                slot_start_ts = self._coerce_started_ts(info.get("slot_start_ts"))
                songs_posted = bool(info.get("songs_posted", False))

                # STARTED solo si matchmaking lo marcó explícitamente
                state = info.get("state")  # opcional: "STARTED"
                started_flag = (
                    phase == PHASE_STARTED
                    or bool(info.get("started"))
                    or (isinstance(state, str) and state.upper() == PHASE_STARTED)
                )

                started_ts = self._coerce_started_ts(
                    info.get("started_ts")
                    or info.get("start_ts")
                    or info.get("started_at")
                )

                reached5_ts = self._coerce_started_ts(
                    info.get("reached5_ts")
                    or info.get("ready_ts")
                    or info.get("full_ts")
                )

                thread_id = info["thread"]

                grouped.setdefault(cat, []).append(
                    (
                        rid,
                        pdata_ids,
                        avg,
                        started_flag,
                        started_ts,
                        reached5_ts,
                        thread_id,
                        phase,
                        slot_start_ts,
                        songs_posted,
                    )
                )

            # helper: truncar si excede 2000 chars
            def build_content(lines: list[str], now_ts: int) -> str:
                footer = f"{BOARD_MARKER} <t:{now_ts}:R>"
                if not lines or not lines[-1].startswith(BOARD_MARKER):
                    lines = lines + [footer]

                content = "\n".join(lines)
                if len(content) <= 1990:
                    return content

                body = lines[:-1]
                while body and len("\n".join(body + ["…(truncated)", footer])) > 1990:
                    body.pop()

                return "\n".join(body + ["…(truncated)", footer])

            # 5) Por cada categoría, editar o enviar mensaje en su canal rooms
            now_ts = int(time.time())

            for cat_id, room_chan_id in CATEGORY_TO_ROOM_CHANNEL.items():
                channel = await self._get_text_channel(room_chan_id, cache=ch_cache)
                if not isinstance(channel, discord.TextChannel):
                    continue

                await self._ensure_prev_message(cat_id, channel)

                guild = channel.guild
                lines: list[str] = []

                # ---- A) Active rooms ----
                rooms_list = grouped.get(cat_id, [])
                if rooms_list:
                    # Prioridad: STARTED primero, luego FUTURE/SCHEDULED, luego lo demás; dentro por avg desc
                    def prio(t: tuple) -> int:
                        started_flag = bool(t[3])
                        phase = t[7]
                        if started_flag or phase == PHASE_STARTED:
                            return 0
                        if phase == PHASE_FUTURE:
                            return 1
                        return 2

                    rooms_list.sort(key=lambda t: (prio(t), -int(t[2]), int(t[0])))

                    for (
                        rid,
                        pdata_ids,
                        avg,
                        started_flag,
                        started_ts,
                        reached5_ts,
                        thread_id,
                        phase,
                        slot_start_ts,
                        songs_posted,
                    ) in rooms_list:
                        count = len(pdata_ids)

                        # Status
                        if started_flag:
                            status = (
                                f"[STARTED] <t:{started_ts}:t>"
                                if started_ts
                                else "[STARTED]"
                            )
                        else:
                            if phase == PHASE_FUTURE:
                                if slot_start_ts:
                                    if songs_posted:
                                        status = f"[SCHEDULED] songs posted · <t:{slot_start_ts}:t>"
                                    else:
                                        status = (
                                            f"[SCHEDULED] songs at <t:{slot_start_ts}:t> "
                                            f"(<t:{slot_start_ts}:R>)"
                                        )
                                else:
                                    status = "[SCHEDULED]"
                            elif phase == PHASE_PREPARED:
                                status = "[PREPARED]"
                            else:
                                # Compat con flujos antiguos
                                if count == ROOM_CAPACITY:
                                    status = (
                                        f"[READY 5/5] <t:{reached5_ts}:t>"
                                        if reached5_ts
                                        else "[READY 5/5]"
                                    )
                                else:
                                    status = f"[WAITING {count}/{ROOM_CAPACITY}]"

                        ref = (
                            f"<#{thread_id}>"
                            if isinstance(thread_id, int) and thread_id > 0
                            else None
                        )

                        if ref:
                            lines.append(
                                f"**Room {rid} · {ref} · {count}/{ROOM_CAPACITY} Players · Average MMR {avg} · {status}**"
                            )
                        else:
                            lines.append(
                                f"**Room {rid} · {count}/{ROOM_CAPACITY} Players · Average MMR {avg} · {status}**"
                            )

                        # Jugadores ya vienen ordenados desc por MMR
                        for uid, mmr in pdata_ids:
                            lines.append(f"- {self._user_label(guild, uid)} ({mmr})")
                        lines.append("")

                # ---- B) Queue ----
                join_id = cat_to_join.get(cat_id)
                queue_raw = queues.get(join_id, []) if isinstance(join_id, int) else []

                queue_ids: list[int] = []
                for item in queue_raw or []:
                    if isinstance(item, int):
                        queue_ids.append(item)
                    elif isinstance(item, (discord.Member, discord.User)):
                        queue_ids.append(item.id)

                queue_ids = list(dict.fromkeys(queue_ids))

                if queue_ids:
                    mmr_map_q = await self._fetch_mmrs_batch(
                        mm, queue_ids, season_id, has_pool
                    )

                    # Separador visual si venimos de rooms activos
                    if lines and lines[-1].strip() != "":
                        lines.append("")

                    lines.append("**Queue**")

                    # Orden SOLO para display (no altera la cola real en mm.queues)
                    queue_ids_sorted = sorted(
                        queue_ids,
                        key=lambda uid: (int(mmr_map_q.get(uid, 0)), int(uid)),
                        reverse=True,
                    )

                    for idx in range(0, len(queue_ids_sorted), ROOM_CAPACITY):
                        grp_ids = queue_ids_sorted[idx : idx + ROOM_CAPACITY]
                        grp_mmrs = [mmr_map_q.get(uid, 0) for uid in grp_ids]
                        avg_q = (sum(grp_mmrs) // len(grp_mmrs)) if grp_mmrs else 0
                        count_q = len(grp_ids)

                        q_status = (
                            "[READY 5/5]"
                            if count_q == ROOM_CAPACITY
                            else f"[WAITING {count_q}/{ROOM_CAPACITY}]"
                        )
                        group_n = (idx // ROOM_CAPACITY) + 1

                        lines.append(
                            f"**Queue Group {group_n} · {count_q}/{ROOM_CAPACITY} Players · Average MMR {avg_q} · {q_status}**"
                        )
                        for uid in grp_ids:
                            lines.append(
                                f"- {self._user_label(guild, uid)} ({mmr_map_q.get(uid, 0)})"
                            )
                        lines.append("")
                else:
                    if not rooms_list:
                        lines.append("**No active rooms**")

                content = build_content(lines, now_ts)

                prev_msg = self.posted_messages.get(cat_id)

                # Si no existe mensaje previo o el canal cambió, mandamos uno nuevo
                if prev_msg is None or prev_msg.channel.id != channel.id:
                    try:
                        msg = await channel.send(content)
                        self.posted_messages[cat_id] = msg
                    except discord.Forbidden:
                        continue
                    except discord.HTTPException:
                        continue
                    continue

                # Si existe, intentamos editar; si fue borrado, re-crear
                if prev_msg.content != content:
                    try:
                        await prev_msg.edit(content=content)
                    except (discord.NotFound, discord.HTTPException, discord.Forbidden):
                        try:
                            msg = await channel.send(content)
                            self.posted_messages[cat_id] = msg
                        except discord.Forbidden:
                            pass
                        except discord.HTTPException:
                            pass

            with open("assets/cached_rooms.json", "w+", encoding="utf-8") as f:
                json.dump(all_rooms, f)

        except Exception as e:
            print(f"[Rooms] Error en _do_update: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Rooms(bot))

