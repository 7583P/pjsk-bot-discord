# cogs/rooms.py

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta
from typing import Optional

import discord
from discord.ext import commands, tasks

# -----------------------------
# CONFIG
# -----------------------------

# Mapea cada category_id al canal “rooms” correspondiente
CATEGORY_TO_ROOM_CHANNEL: dict[int, int] = {
    1371306302671687710: 1371307831176728706,  # pjsk-queue → #rooms
    1371951461612912802: 1388515368934309978,  # jp-pjsk    → #jp-rooms
}

ROOM_CAPACITY = 5

# Si quieres que se vean 5 líneas aunque falten jugadores (slots vacíos), pon True
SHOW_EMPTY_SLOTS = False

# Marcadores
HUMAN_LAST_UPDATE_LABEL = "Last update"
BOARD_MARKER = "[PJSK_ROOMS_BOARD]"  # marcador único para localizar el mensaje correcto

# Fases (para FUTURE/STARTED si vienen del matchmaking)
PHASE_FUTURE = "FUTURE"
PHASE_PREPARED = "PREPARED"
PHASE_STARTED = "STARTED"
PHASE_FINISHED = "FINISHED"

# Slot “next” (solo para display). Si tu Matchmaking ya maneja slots, esto solo es UI.
SLOT_INTERVAL_MINUTES = 30

# Estado persistente del mensaje tablero (para no depender del history)
STATE_PATH = "assets/rooms_board_state.json"

# Cache debug serializable
DEBUG_SNAPSHOT_PATH = "assets/cached_rooms.json"


# -----------------------------
# Helpers de estado persistente
# -----------------------------

def _ensure_assets_dir() -> None:
    os.makedirs("assets", exist_ok=True)


def _load_state() -> dict:
    _ensure_assets_dir()
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    _ensure_assets_dir()
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def _round_up_to_next_slot(now: datetime, interval_minutes: int) -> datetime:
    base = now.replace(second=0, microsecond=0)
    remainder = base.minute % interval_minutes
    if remainder == 0:
        return base + timedelta(minutes=interval_minutes)
    return base + timedelta(minutes=(interval_minutes - remainder))


def _safe_int(x, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _coerce_int_or_none(x) -> int | None:
    try:
        if x is None:
            return None
        if isinstance(x, bool):
            return None
        if isinstance(x, int):
            return x if x > 0 else None
        if isinstance(x, float):
            v = int(x)
            return v if v > 0 else None
        if isinstance(x, str):
            s = x.strip()
            if s.isdigit():
                v = int(s)
                return v if v > 0 else None
            # intentar extraer dígitos (p.ej. "r1")
            digits = "".join(ch for ch in s if ch.isdigit())
            if digits:
                v = int(digits)
                return v if v > 0 else None
        return None
    except Exception:
        return None


def _room_sort_id(rid) -> int:
    """
    Soporta rid tipo int, "1", "r1", etc.
    Devuelve un entero para sorting estable.
    """
    v = _coerce_int_or_none(rid)
    return v if v is not None else 0


def _extract_uid(item) -> int | None:
    """
    Extrae user_id desde distintas formas:
      - int
      - str numérico
      - discord.Member / discord.User
      - dict con id/user_id/discord_id
      - objeto con atributo .id / .user_id / .discord_id
    """
    try:
        if isinstance(item, int):
            return item if item > 0 else None

        if isinstance(item, str):
            s = item.strip()
            return int(s) if s.isdigit() else None

        if isinstance(item, (discord.Member, discord.User)):
            return int(item.id)

        if isinstance(item, dict):
            for k in ("id", "user_id", "discord_id"):
                v = item.get(k)
                if isinstance(v, int) and v > 0:
                    return int(v)
                if isinstance(v, str) and v.strip().isdigit():
                    return int(v.strip())

        for attr in ("id", "user_id", "discord_id"):
            if hasattr(item, attr):
                v = getattr(item, attr, None)
                if isinstance(v, int) and v > 0:
                    return int(v)
                if isinstance(v, str) and v.strip().isdigit():
                    return int(v.strip())
    except Exception:
        return None

    return None


def _debug_serialize_queue(q) -> list:
    out = []
    try:
        for it in (q or []):
            uid = _extract_uid(it)
            if uid is not None:
                out.append(uid)
            else:
                out.append(f"<unparsed:{type(it).__name__}>")
    except Exception:
        pass
    return out


# -----------------------------
# Cog
# -----------------------------

class Rooms(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # { category_id: discord.Message }
        self.posted_messages: dict[int, discord.Message] = {}

        # Evita condiciones de carrera
        self._update_lock = asyncio.Lock()

        # Coalesce: si llegan muchos eventos, hacemos como máximo 1 update extra al final
        self._update_pending: bool = False

        # Estado persistente: {str(cat_id): {"channel_id": int, "message_id": int}}
        self._state = _load_state()

        # Listeners para refrescar al vuelo
        bot.add_listener(self.on_room_updated, "room_updated")
        bot.add_listener(self.on_room_finished, "room_finished")

        # Loop de respaldo
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
        await self._request_update()

    async def on_room_finished(self, room_id: int):
        await asyncio.sleep(1)
        await self._request_update()

    async def _request_update(self):
        """
        Evita que se encolen muchos updates.
        Si ya hay uno corriendo, marca pending y sale.
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

    async def _infer_category_id_from_thread(
        self,
        thread_id: int | None,
        cache: dict[int, discord.abc.GuildChannel | None] | None = None,
    ) -> int | None:
        """
        Si Matchmaking no guarda category_id o lo guarda raro, inferimos:
          Thread -> parent channel -> parent.category.id
        """
        if not isinstance(thread_id, int) or thread_id <= 0:
            return None

        # cache de canales (incluye threads) por id
        if cache is not None and thread_id in cache:
            ch = cache[thread_id]
        else:
            ch = self.bot.get_channel(thread_id)
            if ch is None:
                try:
                    ch = await self.bot.fetch_channel(thread_id)
                except Exception:
                    ch = None
            if cache is not None:
                cache[thread_id] = ch

        # discord.Thread tiene .parent
        if isinstance(ch, discord.Thread):
            parent = ch.parent
            if parent and parent.category:
                return parent.category.id

        # Algunos bots guardan "thread" como el parent channel directamente
        if isinstance(ch, discord.TextChannel) and ch.category:
            return ch.category.id

        return None

    async def _find_last_board_message(self, channel: discord.TextChannel) -> discord.Message | None:
        """
        Reutiliza el último mensaje tablero del bot en el canal (para no duplicar mensajes tras reinicios).
        Requiere Read Message History.
        """
        try:
            me = self.bot.user
            if me is None:
                return None

            async for msg in channel.history(limit=50):
                if msg.author and msg.author.id == me.id and msg.content:
                    if BOARD_MARKER in msg.content:
                        return msg

            async for msg in channel.history(limit=50):
                if msg.author and msg.author.id == me.id and msg.content:
                    if ("Last Update" in msg.content) or ("Last update" in msg.content):
                        return msg

            async for msg in channel.history(limit=50):
                if msg.author and msg.author.id == me.id:
                    return msg

        except Exception:
            return None

        return None

    async def _ensure_prev_message(self, cat_id: int, channel: discord.TextChannel) -> None:
        """
        1) Si ya está en memoria, ok.
        2) Si hay message_id persistido, fetch.
        3) Si no, busca en history.
        """
        if cat_id in self.posted_messages:
            return

        # 2) persistido
        try:
            st = self._state.get(str(cat_id)) or {}
            mid = st.get("message_id")
            ch_id = st.get("channel_id")
            if isinstance(mid, int) and isinstance(ch_id, int) and ch_id == channel.id:
                try:
                    msg = await channel.fetch_message(mid)
                    self.posted_messages[cat_id] = msg
                    return
                except Exception:
                    pass
        except Exception:
            pass

        # 3) history
        prev = await self._find_last_board_message(channel)
        if prev is not None:
            self.posted_messages[cat_id] = prev
            self._state[str(cat_id)] = {
                **(self._state.get(str(cat_id)) or {}),
                "channel_id": channel.id,
                "message_id": prev.id,
            }
            _save_state(self._state)

    async def _get_active_season_id(self, mm) -> int | None:
        sid = getattr(mm, "current_season_id", None)
        if isinstance(sid, int) and sid > 0:
            return sid

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

    def _coerce_ts(self, raw) -> int | None:
        if isinstance(raw, (int, float)):
            ts = int(raw)
            return ts if ts > 0 else None
        if raw is not None and hasattr(raw, "timestamp"):
            try:
                ts = int(raw.timestamp())
                return ts if ts > 0 else None
            except Exception:
                return None
        if isinstance(raw, str):
            s = raw.strip()
            if s.isdigit():
                ts = int(s)
                return ts if ts > 0 else None
        return None

    async def _fetch_mmrs_batch(
        self,
        mm,
        user_ids: list[int],
        season_id: int | None,
        has_pool: bool,
    ) -> dict[int, int]:
        if not user_ids:
            return {}

        user_ids = list(dict.fromkeys(int(x) for x in user_ids if isinstance(x, int)))

        mmr_map: dict[int, int] = {}
        try:
            if has_pool and season_id:
                async with mm.db_pool.acquire() as con:
                    rows_db = await con.fetch(
                        """
                        WITH u AS (SELECT unnest($1::bigint[]) AS user_id)
                        SELECT u.user_id, COALESCE(sp.mmr, 0) AS mmr
                        FROM u
                        LEFT JOIN seasonplayers sp
                          ON sp.user_id = u.user_id AND sp.season_id = $2
                        """,
                        user_ids,
                        season_id,
                    )
                    mmr_map = {int(r["user_id"]): int(r["mmr"]) for r in rows_db}
            else:
                for uid in user_ids:
                    _mmr, _ = await mm.fetch_player(uid)
                    mmr_map[int(uid)] = int(_mmr)
        except Exception:
            for uid in user_ids:
                try:
                    _mmr, _ = await mm.fetch_player(uid)
                    mmr_map[int(uid)] = int(_mmr)
                except Exception:
                    mmr_map[int(uid)] = 0

        return mmr_map

    def _user_label(self, guild: discord.Guild, uid: int) -> str:
        m = guild.get_member(uid)
        if m is not None:
            return m.display_name
        u = self.bot.get_user(uid)
        if u is not None:
            return u.display_name
        # Mención clickeable, pero NO pingea (allowed_mentions=none)
        return f"<@{uid}>"

    def _display_mmr_label(self, mm, guild: discord.Guild, uid: int, mmr: int) -> str:
        member = guild.get_member(int(uid)) if guild else None
        try:
            if member is not None and hasattr(mm, "_has_season_placement_role") and mm._has_season_placement_role(member):
                return "Season Placement" if int(mmr or 0) <= 0 else f"{int(mmr)} Starting MMR"
            if member is not None and hasattr(mm, "_is_placement_member") and mm._is_placement_member(member):
                return "Placement"
        except Exception:
            pass
        return str(int(mmr or 0))

    def _build_content(self, lines: list[str], now_ts: int) -> str:
        footer = f"{HUMAN_LAST_UPDATE_LABEL}: <t:{now_ts}:T> (<t:{now_ts}:R>) {BOARD_MARKER}"
        if not lines or BOARD_MARKER not in (lines[-1] if lines else ""):
            lines = lines + [footer]

        content = "\n".join(lines)
        if len(content) <= 1990:
            return content

        body = lines[:-1]
        while body and len("\n".join(body + ["…(truncated)", footer])) > 1990:
            body.pop()
        return "\n".join(body + ["…(truncated)", footer])

    def _queue_groups_sorted_by_mmr(self, queue_ids: list[int], mmr_map: dict[int, int]) -> list[dict]:
        """
        Orden global: MMR desc, tie-break uid asc. Luego arma grupos de 5.
        """
        ordered = sorted(queue_ids, key=lambda uid: (-_safe_int(mmr_map.get(uid, 0)), _safe_int(uid)))
        groups: list[dict] = []

        for idx in range(0, len(ordered), ROOM_CAPACITY):
            grp_ids = ordered[idx: idx + ROOM_CAPACITY]
            grp_ids = sorted(grp_ids, key=lambda uid: (-_safe_int(mmr_map.get(uid, 0)), _safe_int(uid)))

            grp_mmrs = [_safe_int(mmr_map.get(uid, 0)) for uid in grp_ids]
            avg = (sum(grp_mmrs) // len(grp_mmrs)) if grp_mmrs else 0

            groups.append(
                {
                    "group_n": (idx // ROOM_CAPACITY) + 1,
                    "player_ids": [int(x) for x in grp_ids],
                    "mmrs": {str(int(uid)): int(_safe_int(mmr_map.get(uid, 0))) for uid in grp_ids},
                    "avg": int(avg),
                }
            )
        return groups

    def _is_songs_posted(self, info: dict) -> bool:
        """
        Tu definición de STARTED:
        Sala iniciada = songs desplegadas.
        Soporta distintas claves y señales.
        """
        try:
            if bool(info.get("songs_posted", False)):
                return True
            if bool(info.get("songsPosted", False)):
                return True
            # si hay lista/estructura de songs no vacía
            for k in ("songs", "song_ids", "songIds", "current_songs", "currentSongs"):
                v = info.get(k)
                if isinstance(v, (list, tuple, set, dict)) and len(v) > 0:
                    return True
            # si existe id del mensaje de songs
            for k in ("songs_message_id", "songsMessageId"):
                if _coerce_int_or_none(info.get(k)) is not None:
                    return True
        except Exception:
            return False
        return False

    async def _do_update_core(self):
        mm = self.bot.get_cog("Matchmaking")
        if mm is None:
            return

        all_rooms = getattr(mm, "rooms", {}) or {}
        queues = getattr(mm, "queues", {}) or {}

        season_id = await self._get_active_season_id(mm)
        has_pool = getattr(mm, "db_pool", None) is not None and bool(season_id)

        ch_cache: dict[int, discord.abc.GuildChannel | None] = {}

        # category_id -> join_channel_id (derivado de queues)
        cat_to_join: dict[int, int] = {}
        for join_id in list(queues.keys()):
            ch = await self._get_text_channel(join_id, cache=ch_cache)
            if ch is None or ch.category is None:
                continue
            cat_id = ch.category.id
            if cat_id in CATEGORY_TO_ROOM_CHANNEL:
                cat_to_join[cat_id] = join_id

        # Agrupar rooms por categoría
        grouped: dict[int, list[tuple]] = {}
        for rid, info in (all_rooms.items() if isinstance(all_rooms, dict) else []):
            try:
                if not isinstance(info, dict):
                    continue

                # thread id (puede ayudar a inferir category)
                thread_id = _coerce_int_or_none(info.get("thread") or info.get("thread_id") or info.get("threadId"))

                # category id (robusto)
                cat_raw = info.get("category_id", None)
                if cat_raw is None:
                    cat_raw = info.get("category") or info.get("cat_id") or info.get("catId")
                cat_id = _coerce_int_or_none(cat_raw)

                if cat_id is None:
                    cat_id = await self._infer_category_id_from_thread(thread_id, cache=ch_cache)

                if cat_id not in CATEGORY_TO_ROOM_CHANNEL:
                    continue

                raw_players = info.get("players", []) or []
                player_ids: list[int] = []
                for p in raw_players:
                    uid = _extract_uid(p)
                    if uid is not None:
                        player_ids.append(uid)

                player_ids = list(dict.fromkeys(player_ids))

                mmr_map = await self._fetch_mmrs_batch(mm, player_ids, season_id, has_pool) if player_ids else {}
                pdata_ids = [(uid, mmr_map.get(uid, 0)) for uid in player_ids]
                pdata_ids.sort(key=lambda t: t[1], reverse=True)

                mmr_vals = [mmr for _, mmr in pdata_ids]
                avg = (sum(mmr_vals) // len(mmr_vals)) if mmr_vals else 0

                phase = info.get("phase")
                phase = phase.upper() if isinstance(phase, str) else None

                slot_start_ts = self._coerce_ts(info.get("slot_start_ts") or info.get("slot_ts") or info.get("slotStartTs"))

                songs_posted = self._is_songs_posted(info)
                songs_posted_ts = self._coerce_ts(
                    info.get("songs_posted_ts")
                    or info.get("songs_ts")
                    or info.get("songs_posted_at")
                    or info.get("songsPostedTs")
                )

                state = info.get("state")
                state_up = state.upper() if isinstance(state, str) else None

                started_ts = self._coerce_ts(
                    info.get("started_ts")
                    or info.get("start_ts")
                    or info.get("started_at")
                    or info.get("startedAt")
                )
                # si tu inicio real es "songs posted", usa ese ts si no hay otro
                if started_ts is None and songs_posted_ts is not None:
                    started_ts = songs_posted_ts

                reached5_ts = self._coerce_ts(info.get("reached5_ts") or info.get("ready_ts") or info.get("full_ts"))

                # STARTED robusto: (tu definición) songs_posted manda
                started_flag = (
                    songs_posted
                    or phase == PHASE_STARTED
                    or bool(info.get("started"))
                    or (state_up == PHASE_STARTED)
                    or (started_ts is not None)
                )

                # FINISHED robusto (si existe)
                finished_flag = (
                    phase == PHASE_FINISHED
                    or bool(info.get("finished"))
                    or (state_up == PHASE_FINISHED)
                    or bool(info.get("ended"))
                )

                grouped.setdefault(cat_id, []).append(
                    (
                        rid,
                        pdata_ids,
                        int(avg),
                        bool(started_flag),
                        started_ts,
                        reached5_ts,
                        thread_id,
                        phase,
                        slot_start_ts,
                        bool(songs_posted),
                        bool(finished_flag),
                    )
                )
            except Exception:
                continue

        # Debug snapshot serializable (siempre)
        try:
            _ensure_assets_dir()
            snap = {
                "rooms": {
                    str(rid): {
                        "raw_category_id": (info.get("category_id") if isinstance(info, dict) else None),
                        "thread": (info.get("thread") if isinstance(info, dict) else None),
                        "phase": (info.get("phase") if isinstance(info, dict) else None),
                        "state": (info.get("state") if isinstance(info, dict) else None),
                        "started": (info.get("started") if isinstance(info, dict) else None),
                        "songs_posted": (info.get("songs_posted") if isinstance(info, dict) else None),
                        "players": [
                            _extract_uid(p)
                            for p in ((info.get("players") or []) if isinstance(info, dict) else [])
                        ],
                    }
                    for rid, info in (all_rooms.items() if isinstance(all_rooms, dict) else [])
                },
                "queues": {
                    str(k): _debug_serialize_queue(v) if isinstance(v, list) else []
                    for k, v in (queues.items() if isinstance(queues, dict) else [])
                },
                "cat_to_join": {str(k): v for k, v in cat_to_join.items()},
            }
            with open(DEBUG_SNAPSHOT_PATH, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        now_ts = int(time.time())
        now_dt = datetime.utcnow()
        next_slot_dt = _round_up_to_next_slot(now_dt, SLOT_INTERVAL_MINUTES)
        next_slot_ts = int(next_slot_dt.timestamp())

        # Procesa cada categoría con try/except para no bloquear JP
        for cat_id, room_chan_id in CATEGORY_TO_ROOM_CHANNEL.items():
            try:
                channel = await self._get_text_channel(room_chan_id, cache=ch_cache)
                if not isinstance(channel, discord.TextChannel):
                    continue

                await self._ensure_prev_message(cat_id, channel)
                guild = channel.guild

                join_id = cat_to_join.get(cat_id)
                join_ch = await self._get_text_channel(join_id, cache=ch_cache) if isinstance(join_id, int) else None
                join_label = f"#{join_ch.name}" if isinstance(join_ch, discord.TextChannel) else "join"

                rooms_list = grouped.get(cat_id, []) or []

                # ACTIVE = rooms reales que NO son FUTURE(no-started) y NO están FINISHED
                active_rooms: list[tuple] = []
                future_rooms_real: list[tuple] = []

                for t in rooms_list:
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
                        finished_flag,
                    ) = t

                    if finished_flag:
                        continue

                    # FUTURE real: solo si explícitamente FUTURE y todavía NO started (según tu definición)
                    if phase == PHASE_FUTURE and not started_flag and not songs_posted:
                        future_rooms_real.append(t)
                    else:
                        active_rooms.append(t)

                # Orden ACTIVE: STARTED primero, luego READY(5/5) si no started, luego por avg desc, rid
                def _active_sort_key(t: tuple):
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
                        finished_flag,
                    ) = t
                    started_prio = 0 if (started_flag or songs_posted or phase == PHASE_STARTED) else 1
                    ready_prio = 0 if (len(pdata_ids) == ROOM_CAPACITY and started_prio == 1) else 1
                    return (started_prio, ready_prio, -int(avg), _room_sort_id(rid))

                active_rooms.sort(key=_active_sort_key)

                # FUTURE reales: slot asc, avg desc
                def _future_real_sort_key(t: tuple):
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
                        finished_flag,
                    ) = t
                    slot_ts = slot_start_ts if isinstance(slot_start_ts, int) and slot_start_ts > 0 else 10**12
                    return (slot_ts, -int(avg), _room_sort_id(rid))

                future_rooms_real.sort(key=_future_real_sort_key)

                # -----------------------------
                # Queue -> FUTURE groups (ordenados por MMR desc)
                # -----------------------------
                queue_raw = queues.get(join_id, []) if isinstance(join_id, int) else []
                queue_ids: list[int] = []
                for item in (queue_raw or []):
                    uid = _extract_uid(item)
                    if uid is not None:
                        queue_ids.append(uid)
                queue_ids = list(dict.fromkeys(queue_ids))

                pending_groups_now: list[dict] = []
                if queue_ids:
                    mmr_map_q = await self._fetch_mmrs_batch(mm, queue_ids, season_id, has_pool)
                    pending_groups_now = self._queue_groups_sorted_by_mmr(queue_ids, mmr_map_q)

                # -----------------------------
                # Render
                # -----------------------------
                lines: list[str] = []
                lines.append("**PJSK ROOMS**")
                lines.append(f"**{join_label}**")
                lines.append("")

                def render_group_like_room(
                    title: str,
                    pdata_ids: list[tuple[int, int]],
                    avg: int,
                    status: str,
                    thread_id: Optional[int] = None,
                ):
                    count = len(pdata_ids)
                    ref = f"<#{thread_id}>" if isinstance(thread_id, int) and thread_id > 0 else ""
                    if ref:
                        lines.append(f"**{title} · {ref} · {count}/{ROOM_CAPACITY} Players · Average MMR {avg} · {status}**")
                    else:
                        lines.append(f"**{title} · {count}/{ROOM_CAPACITY} Players · Average MMR {avg} · {status}**")

                    pdata_ids_sorted = sorted(pdata_ids, key=lambda t: t[1], reverse=True)
                    for uid, mmr in pdata_ids_sorted:
                        label = self._display_mmr_label(mm, guild, uid, mmr)
                        lines.append(f"- {self._user_label(guild, uid)} ({label})")

                    if SHOW_EMPTY_SLOTS and count < ROOM_CAPACITY:
                        for _ in range(ROOM_CAPACITY - count):
                            lines.append("- _(empty slot)_")

                    lines.append("")

                # -----------------------------
                # ACTIVE ROOMS (solo rooms reales)
                # -----------------------------
                if not active_rooms:
                    lines.append("_No active rooms._")
                    lines.append("")
                else:
                    for t in active_rooms:
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
                            finished_flag,
                        ) = t
                        count = len(pdata_ids)

                        # STARTED (tu definición): songs_posted => STARTED aunque phase diga FUTURE/PREPARED
                        if songs_posted or started_flag or phase == PHASE_STARTED:
                            ts = started_ts or slot_start_ts
                            status = f"[STARTED] <t:{ts}:t>" if ts else "[STARTED]"
                        else:
                            if count == ROOM_CAPACITY:
                                status = f"[READY 5/5] <t:{reached5_ts}:t>" if reached5_ts else "[READY 5/5]"
                            else:
                                status = f"[WAITING {count}/{ROOM_CAPACITY}]"

                        render_group_like_room(f"Room {rid}", pdata_ids, int(avg), status, thread_id)

                lines.append("────────────────────────")

                # -----------------------------
                # FUTURE ROOMS (next slot ...)
                #   - Unificado y ordenado: slot asc, avg desc
                # -----------------------------
                lines.append(f"**FUTURE ROOMS** (next slot <t:{next_slot_ts}:T>, <t:{next_slot_ts}:R>)")

                future_entries: list[dict] = []

                # 1) grupos de queue (lo que matchmaking usaría en el next slot)
                for g in (pending_groups_now or []):
                    pids = [int(x) for x in (g.get("player_ids") or []) if isinstance(x, int) or str(x).isdigit()]
                    mmrs = g.get("mmrs") or {}
                    pdata_ids = [(uid, _safe_int(mmrs.get(str(uid), 0))) for uid in pids]
                    avg = _safe_int(g.get("avg"), 0)
                    count = len(pdata_ids)
                    status = "[READY 5/5]" if count == ROOM_CAPACITY else f"[WAITING {count}/{ROOM_CAPACITY}]"

                    future_entries.append(
                        {
                            "kind": "queue",
                            "slot_ts": next_slot_ts,
                            "avg": avg,
                            "tie": _safe_int(g.get("group_n"), 0),
                            "title": f"Queue Group {g.get('group_n', '?')}",
                            "pdata_ids": pdata_ids,
                            "status": status,
                            "thread_id": None,
                        }
                    )

                # 2) future rooms reales (si tu matchmaking crea rooms con phase=FUTURE)
                for t in future_rooms_real:
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
                        finished_flag,
                    ) = t

                    count = len(pdata_ids)
                    if slot_start_ts:
                        status = f"[WAITING {count}/{ROOM_CAPACITY}] · songs at <t:{slot_start_ts}:t> (<t:{slot_start_ts}:R>)"
                        slot_ts_for_sort = slot_start_ts
                    else:
                        status = f"[WAITING {count}/{ROOM_CAPACITY}]"
                        slot_ts_for_sort = next_slot_ts

                    future_entries.append(
                        {
                            "kind": "room",
                            "slot_ts": _safe_int(slot_ts_for_sort, next_slot_ts),
                            "avg": int(avg),
                            "tie": _room_sort_id(rid),
                            "title": f"Room {rid}",
                            "pdata_ids": pdata_ids,
                            "status": status,
                            "thread_id": thread_id,
                        }
                    )

                if not future_entries:
                    lines.append("_Queue empty._")
                else:
                    lines.append("")

                    # Orden: slot asc, avg desc, queue antes que room si empate, tie asc
                    def _future_sort(e: dict):
                        kind_prio = 0 if e.get("kind") == "queue" else 1
                        return (
                            _safe_int(e.get("slot_ts"), 10**12),
                            -_safe_int(e.get("avg"), 0),
                            kind_prio,
                            _safe_int(e.get("tie"), 0),
                        )

                    for e in sorted(future_entries, key=_future_sort):
                        render_group_like_room(
                            e["title"],
                            e["pdata_ids"],
                            int(e["avg"]),
                            e["status"],
                            e.get("thread_id"),
                        )

                content = self._build_content(lines, now_ts)

                prev_msg = self.posted_messages.get(cat_id)

                # Si no existe mensaje previo o el canal cambió, mandamos uno nuevo
                if prev_msg is None or prev_msg.channel.id != channel.id:
                    msg = await channel.send(
                        content,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    self.posted_messages[cat_id] = msg
                    self._state[str(cat_id)] = {
                        **(self._state.get(str(cat_id)) or {}),
                        "channel_id": channel.id,
                        "message_id": msg.id,
                    }
                    _save_state(self._state)
                    continue

                # Edit seguro: LIMPIA embeds
                if prev_msg.content != content:
                    try:
                        await prev_msg.edit(
                            content=content,
                            embeds=[],
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        msg = await channel.send(
                            content,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        self.posted_messages[cat_id] = msg
                        self._state[str(cat_id)] = {
                            **(self._state.get(str(cat_id)) or {}),
                            "channel_id": channel.id,
                            "message_id": msg.id,
                        }
                        _save_state(self._state)

            except Exception as e:
                print(f"[Rooms] Error actualizando categoría {cat_id}: {e}")
                continue


async def setup(bot: commands.Bot):
    await bot.add_cog(Rooms(bot))
