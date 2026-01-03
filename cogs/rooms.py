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

# Marcadores
HUMAN_LAST_UPDATE_LABEL = "Last update"
BOARD_MARKER = "[PJSK_ROOMS_BOARD]"  # marcador único para localizar el mensaje correcto

# Fases (para renderizar FUTURE correctamente)
PHASE_FUTURE = "FUTURE"
PHASE_PREPARED = "PREPARED"
PHASE_STARTED = "STARTED"
PHASE_FINISHED = "FINISHED"

# Slot “next” (solo para display). Si tu Matchmaking ya maneja slots, esto solo es UI.
SLOT_INTERVAL_MINUTES = 30

# Estado persistente del mensaje tablero (para no depender del history)
STATE_PATH = "assets/rooms_board_state.json"

# Cache debug serializable (no revienta por objetos discord)
DEBUG_SNAPSHOT_PATH = "assets/cached_rooms.json"

# TTL de snapshots (fallback) para no acumular basura si nunca se linkean a rooms reales
ACTIVE_SNAPSHOT_TTL_SECONDS = 2 * 60 * 60  # 2 horas


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

        # Estado persistente: {str(cat_id): {"channel_id": int, "message_id": int, ...}}
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
                if msg.author and msg.author.id == me.id:
                    if msg.content and BOARD_MARKER in msg.content:
                        return msg

            async for msg in channel.history(limit=50):
                if msg.author and msg.author.id == me.id:
                    if msg.content and ("Last Update" in msg.content or "Last update" in msg.content):
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

        # Dedup
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
        # Mantengo mención para que sea clickeable, pero NO pingea por allowed_mentions=none
        return f"<@{uid}>"

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
        REQ 1: Ordena jugadores por MMR desc y, en base a ese orden, arma grupos (rooms) de 5.
        """
        # Orden global: MMR desc, tie-break por uid asc para estabilidad
        ordered = sorted(queue_ids, key=lambda uid: (-_safe_int(mmr_map.get(uid, 0)), _safe_int(uid)))
        groups: list[dict] = []

        for idx in range(0, len(ordered), ROOM_CAPACITY):
            grp_ids = ordered[idx: idx + ROOM_CAPACITY]
            # Asegurar orden dentro del grupo (MMR desc)
            grp_ids = sorted(grp_ids, key=lambda uid: (-_safe_int(mmr_map.get(uid, 0)), _safe_int(uid)))

            grp_mmrs = [_safe_int(mmr_map.get(uid, 0)) for uid in grp_ids]
            avg = (sum(grp_mmrs) // len(grp_mmrs)) if grp_mmrs else 0

            groups.append(
                {
                    "group_n": (idx // ROOM_CAPACITY) + 1,
                    "player_ids": [int(x) for x in grp_ids],
                    "mmrs": {str(int(uid)): int(_safe_int(mmr_map.get(uid, 0))) for uid in grp_ids},
                    "avg": int(avg),
                    "created_ts": int(time.time()),
                    "activated_ts": None,
                    "slot_ts": None,
                    "linked_room_id": None,
                }
            )
        return groups

    def _normalize_group_player_set(self, g: dict) -> frozenset[int]:
        try:
            return frozenset(int(x) for x in (g.get("player_ids") or []) if isinstance(x, int) or str(x).isdigit())
        except Exception:
            return frozenset()

    async def _do_update_core(self):
        mm = self.bot.get_cog("Matchmaking")
        if mm is None:
            return

        # Snapshot de estado
        all_rooms = getattr(mm, "rooms", {}) or {}
        queues = getattr(mm, "queues", {}) or {}

        # Resolver season 1 vez
        season_id = await self._get_active_season_id(mm)
        has_pool = getattr(mm, "db_pool", None) is not None and bool(season_id)

        # Cache de canales por update
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
                    elif isinstance(p, dict) and isinstance(p.get("id"), int):
                        player_ids.append(int(p["id"]))

                player_ids = list(dict.fromkeys(player_ids))

                mmr_map = await self._fetch_mmrs_batch(mm, player_ids, season_id, has_pool) if player_ids else {}
                pdata_ids = [(uid, mmr_map.get(uid, 0)) for uid in player_ids]
                # Orden dentro de la room: MMR desc
                pdata_ids.sort(key=lambda t: t[1], reverse=True)

                mmr_vals = [mmr for _, mmr in pdata_ids]
                avg = (sum(mmr_vals) // len(mmr_vals)) if mmr_vals else 0

                phase = info.get("phase")
                phase = phase.upper() if isinstance(phase, str) else None

                slot_start_ts = self._coerce_ts(info.get("slot_start_ts"))
                songs_posted = bool(info.get("songs_posted", False))

                state = info.get("state")
                started_flag = (
                    phase == PHASE_STARTED
                    or bool(info.get("started"))
                    or (isinstance(state, str) and state.upper() == PHASE_STARTED)
                )
                started_ts = self._coerce_ts(info.get("started_ts") or info.get("start_ts") or info.get("started_at"))
                reached5_ts = self._coerce_ts(info.get("reached5_ts") or info.get("ready_ts") or info.get("full_ts"))

                thread_id = info.get("thread")  # puede ser None
                grouped.setdefault(cat, []).append(
                    (rid, pdata_ids, avg, started_flag, started_ts, reached5_ts, thread_id, phase, slot_start_ts, songs_posted)
                )
            except Exception:
                continue

        # Debug snapshot serializable (no rompe update)
        try:
            _ensure_assets_dir()
            snap = {
                "rooms": {
                    str(rid): {
                        "category_id": (info.get("category_id") if isinstance(info, dict) else None),
                        "thread": (info.get("thread") if isinstance(info, dict) else None),
                        "phase": (info.get("phase") if isinstance(info, dict) else None),
                        "players": [
                            (p.id if isinstance(p, (discord.Member, discord.User)) else p)
                            for p in ((info.get("players") or []) if isinstance(info, dict) else [])
                            if isinstance(p, (int, discord.Member, discord.User))
                        ],
                    }
                    for rid, info in (all_rooms.items() if isinstance(all_rooms, dict) else [])
                },
                "queues": {str(k): list(v) if isinstance(v, list) else [] for k, v in (queues.items() if isinstance(queues, dict) else [])},
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

                # -----------------------------
                # Estado por categoría (persistente)
                # -----------------------------
                cat_key = str(cat_id)
                cat_state = self._state.get(cat_key) or {}
                state_dirty = False

                last_next_slot_ts = cat_state.get("last_next_slot_ts")
                pending_future_groups = cat_state.get("pending_future_groups") or []
                active_queue_groups = cat_state.get("active_queue_groups") or []

                # Cleanup TTL básico (fallback)
                cleaned_active = []
                for g in active_queue_groups:
                    try:
                        activated = _safe_int(g.get("activated_ts") or 0)
                        if activated and (now_ts - activated) > ACTIVE_SNAPSHOT_TTL_SECONDS:
                            # si ya expiró y no está linkeado a una room viva, se limpia
                            if not g.get("linked_room_id"):
                                continue
                        cleaned_active.append(g)
                    except Exception:
                        cleaned_active.append(g)
                if cleaned_active != active_queue_groups:
                    active_queue_groups = cleaned_active
                    state_dirty = True

                # REQ 2: Cuando el "next slot" cambie, lo que estaba en FUTURE se pasa a ACTIVE y NO se elimina.
                # Detecta rollover de slot:
                if isinstance(last_next_slot_ts, int) and last_next_slot_ts > 0 and last_next_slot_ts != next_slot_ts:
                    # Promueve pending_future_groups -> active_queue_groups
                    promoted: list[dict] = []
                    for g in (pending_future_groups or []):
                        try:
                            g2 = dict(g)
                            g2["activated_ts"] = now_ts
                            g2["slot_ts"] = int(last_next_slot_ts)
                            # No duplicar si ya existe el mismo set
                            gset = self._normalize_group_player_set(g2)
                            dup = False
                            for ex in active_queue_groups:
                                if self._normalize_group_player_set(ex) == gset and _safe_int(ex.get("slot_ts")) == _safe_int(g2.get("slot_ts")):
                                    dup = True
                                    break
                            if not dup and gset:
                                promoted.append(g2)
                        except Exception:
                            continue

                    if promoted:
                        active_queue_groups = list(active_queue_groups) + promoted
                        state_dirty = True

                    # Limpia pending (ya se movió)
                    pending_future_groups = []
                    state_dirty = True

                # Actualiza last_next_slot_ts
                if _safe_int(last_next_slot_ts) != _safe_int(next_slot_ts):
                    cat_state["last_next_slot_ts"] = int(next_slot_ts)
                    state_dirty = True

                # -----------------------------
                # Rooms reales del Matchmaking
                # -----------------------------
                rooms_list = grouped.get(cat_id, []) or []

                active_rooms: list[tuple] = []
                future_rooms: list[tuple] = []

                for t in rooms_list:
                    phase = t[7]
                    started_flag = bool(t[3])
                    if phase == PHASE_FUTURE and not started_flag:
                        future_rooms.append(t)
                    else:
                        active_rooms.append(t)

                def active_prio(t: tuple) -> int:
                    started_flag = bool(t[3])
                    phase = t[7]
                    return 0 if (started_flag or phase == PHASE_STARTED) else 1

                active_rooms.sort(key=lambda t: (active_prio(t), -int(t[2]), int(t[0])))

                def future_sort_key(t: tuple):
                    slot_ts = t[8] if isinstance(t[8], int) else 0
                    slot_ts = slot_ts if slot_ts > 0 else 10**12
                    return (slot_ts, -int(t[2]), int(t[0]))

                future_rooms.sort(key=future_sort_key)

                # Map de rooms por set de jugadores (para linkear snapshots y evitar duplicados)
                room_set_to_rid: dict[frozenset[int], int] = {}
                room_id_to_phase: dict[int, str | None] = {}
                for (rid, pdata_ids, avg, started_flag, started_ts, reached5_ts, thread_id, phase, slot_start_ts, songs_posted) in rooms_list:
                    try:
                        pset = frozenset(int(uid) for uid, _ in (pdata_ids or []) if isinstance(uid, int))
                        if pset:
                            room_set_to_rid[pset] = int(rid)
                        room_id_to_phase[int(rid)] = phase
                    except Exception:
                        continue

                # -----------------------------
                # Queue -> FUTURE groups (ordenados por MMR desc)
                # -----------------------------
                queue_raw = queues.get(join_id, []) if isinstance(join_id, int) else []
                queue_ids: list[int] = []
                for item in (queue_raw or []):
                    if isinstance(item, int):
                        queue_ids.append(item)
                    elif isinstance(item, (discord.Member, discord.User)):
                        queue_ids.append(item.id)
                queue_ids = list(dict.fromkeys(queue_ids))

                pending_groups_now: list[dict] = []
                if queue_ids:
                    mmr_map_q = await self._fetch_mmrs_batch(mm, queue_ids, season_id, has_pool)
                    pending_groups_now = self._queue_groups_sorted_by_mmr(queue_ids, mmr_map_q)

                # Guardar pending_future_groups para el next_slot actual (se promueve en el próximo rollover)
                # (Esto es lo que se ve en FUTURE ROOMS)
                cat_state["pending_future_groups"] = pending_groups_now
                pending_future_groups = pending_groups_now
                state_dirty = True

                # Persistir el active_queue_groups actualizado también
                cat_state["active_queue_groups"] = active_queue_groups
                state_dirty = True

                # Intento de link: si un snapshot coincide con una room real, lo linkeamos.
                # Además: si ya está linkeado pero ya no existe la room, se limpia (room finished).
                alive_room_ids = set(int(rid) for rid in (all_rooms.keys() if isinstance(all_rooms, dict) else []) if str(rid).isdigit() or isinstance(rid, int))

                new_active_groups: list[dict] = []
                for g in active_queue_groups:
                    try:
                        pset = self._normalize_group_player_set(g)
                        linked = g.get("linked_room_id")
                        if isinstance(linked, int) and linked > 0:
                            if linked not in alive_room_ids:
                                # room ya no existe -> limpiar snapshot (ya terminó)
                                continue
                            new_active_groups.append(g)
                            continue

                        # no linkeado: intentar link
                        if pset in room_set_to_rid:
                            g2 = dict(g)
                            g2["linked_room_id"] = int(room_set_to_rid[pset])
                            new_active_groups.append(g2)
                            state_dirty = True
                        else:
                            new_active_groups.append(g)
                    except Exception:
                        new_active_groups.append(g)

                if new_active_groups != active_queue_groups:
                    active_queue_groups = new_active_groups
                    cat_state["active_queue_groups"] = active_queue_groups
                    state_dirty = True

                # Lookup: snapshot por room_id (para REQ 2: no perder lista si el Matchmaking vacía players al pasar a ACTIVE)
                snapshot_by_room_id: dict[int, dict] = {}
                for g in active_queue_groups:
                    rid = g.get("linked_room_id")
                    if isinstance(rid, int) and rid > 0:
                        snapshot_by_room_id[rid] = g

                if state_dirty:
                    self._state[cat_key] = {
                        **cat_state,
                        # Mantener si ya existían
                        "channel_id": (cat_state.get("channel_id") if isinstance(cat_state.get("channel_id"), int) else None),
                        "message_id": (cat_state.get("message_id") if isinstance(cat_state.get("message_id"), int) else None),
                    }
                    _save_state(self._state)

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
                    # REQ 1: lista interna siempre MMR desc
                    pdata_ids_sorted = sorted(pdata_ids, key=lambda t: t[1], reverse=True)
                    for uid, mmr in pdata_ids_sorted:
                        lines.append(f"- {self._user_label(guild, uid)} ({mmr})")
                    lines.append("")

                # -----------------------------
                # ACTIVE ROOMS (reales)
                # -----------------------------
                if not active_rooms and not active_queue_groups:
                    lines.append("_No active rooms._")
                    lines.append("")
                else:
                    # Rooms reales
                    for (rid, pdata_ids, avg, started_flag, started_ts, reached5_ts, thread_id, phase, slot_start_ts, songs_posted) in active_rooms:
                        # Si la room real no trae players (o viene vacía), usamos snapshot si existe
                        if (not pdata_ids) and int(rid) in snapshot_by_room_id:
                            sg = snapshot_by_room_id[int(rid)]
                            pids = [int(x) for x in (sg.get("player_ids") or []) if isinstance(x, int) or str(x).isdigit()]
                            mmrs = sg.get("mmrs") or {}
                            pdata_ids = [(uid, _safe_int(mmrs.get(str(uid), 0))) for uid in pids]
                            avg = _safe_int(sg.get("avg"), 0)

                        count = len(pdata_ids)
                        if started_flag or phase == PHASE_STARTED:
                            status = f"[STARTED] <t:{started_ts}:t>" if started_ts else "[STARTED]"
                        else:
                            if count == ROOM_CAPACITY:
                                status = f"[READY 5/5] <t:{reached5_ts}:t>" if reached5_ts else "[READY 5/5]"
                            else:
                                status = f"[WAITING {count}/{ROOM_CAPACITY}]"
                        render_group_like_room(f"Room {rid}", pdata_ids, int(avg), status, thread_id)

                    # Snapshots activos (venían de FUTURE y ya pasaron a ACTIVE)
                    # Evitar duplicado si ya existe una room real con el mismo set de jugadores visible
                    active_room_player_sets = set(
                        frozenset(int(uid) for uid, _ in (pdata_ids or []) if isinstance(uid, int))
                        for (_, pdata_ids, _, _, _, _, _, _, _, _) in active_rooms
                    )

                    # Orden: grupos con mayor avg primero
                    def _snap_sort(g: dict):
                        return (-_safe_int(g.get("avg"), 0), _safe_int(g.get("slot_ts") or 0), _safe_int(g.get("group_n"), 0))

                    for g in sorted(active_queue_groups, key=_snap_sort):
                        pset = self._normalize_group_player_set(g)
                        if pset and pset in active_room_player_sets:
                            continue  # ya se muestra en room real
                        # si está linkeado a room real viva, normalmente se omitirá arriba; pero si la room real está vacía, esta sí sirve.
                        linked = g.get("linked_room_id")
                        if isinstance(linked, int) and linked > 0:
                            # si la room real existe y está en active_rooms, ya está cubierta (arriba)
                            # si no está, igual mostramos como snapshot activo
                            pass

                        pids = [int(x) for x in (g.get("player_ids") or []) if isinstance(x, int) or str(x).isdigit()]
                        mmrs = g.get("mmrs") or {}
                        pdata_ids = [(uid, _safe_int(mmrs.get(str(uid), 0))) for uid in pids]
                        avg = _safe_int(g.get("avg"), 0)
                        count = len(pdata_ids)

                        slot_ts = _safe_int(g.get("slot_ts") or 0)
                        suffix = f" · <t:{slot_ts}:t>" if slot_ts > 0 else ""
                        status = "[READY 5/5]" if count == ROOM_CAPACITY else f"[WAITING {count}/{ROOM_CAPACITY}]"
                        render_group_like_room(f"Active Group {g.get('group_n', '?')}{suffix}", pdata_ids, avg, status, None)

                lines.append("────────────────────────")

                # -----------------------------
                # FUTURE ROOMS (next slot ...)
                #   - aquí se muestran los grupos derivados de queue (ordenados por MMR desc)
                #   - al pasar el slot, se promueven a ACTIVE y NO se eliminan
                # -----------------------------
                lines.append(f"**FUTURE ROOMS** (next slot <t:{next_slot_ts}:T>, <t:{next_slot_ts}:R>)")

                # 1) FUTURE rooms reales (si existen en mm.rooms con phase FUTURE)
                if future_rooms:
                    lines.append("")
                    for (rid, pdata_ids, avg, started_flag, started_ts, reached5_ts, thread_id, phase, slot_start_ts, songs_posted) in future_rooms:
                        count = len(pdata_ids)
                        if slot_start_ts:
                            status = f"[WAITING {count}/{ROOM_CAPACITY}] · songs at <t:{slot_start_ts}:t> (<t:{slot_start_ts}:R>)"
                        else:
                            status = f"[WAITING {count}/{ROOM_CAPACITY}]"
                        render_group_like_room(f"Room {rid}", pdata_ids, int(avg), status, thread_id)

                # 2) Grupos de queue (pending) para el próximo slot (esto es lo que pediste ordenar por MMR y usar para crear rooms)
                if not pending_future_groups:
                    lines.append("_Queue empty._")
                else:
                    # Evitar duplicado si ya hay FUTURE rooms reales con el mismo set
                    future_room_sets = set(
                        frozenset(int(uid) for uid, _ in (pdata_ids or []) if isinstance(uid, int))
                        for (_, pdata_ids, _, _, _, _, _, _, _, _) in future_rooms
                    )

                    lines.append("")
                    # Orden: group_n asc (ya viene armado por MMR global desc)
                    for g in sorted(pending_future_groups, key=lambda x: _safe_int(x.get("group_n"), 0)):
                        pset = self._normalize_group_player_set(g)
                        if pset and pset in future_room_sets:
                            continue

                        pids = [int(x) for x in (g.get("player_ids") or []) if isinstance(x, int) or str(x).isdigit()]
                        mmrs = g.get("mmrs") or {}
                        pdata_ids = [(uid, _safe_int(mmrs.get(str(uid), 0))) for uid in pids]
                        avg = _safe_int(g.get("avg"), 0)
                        count = len(pdata_ids)
                        status = "[READY 5/5]" if count == ROOM_CAPACITY else f"[WAITING {count}/{ROOM_CAPACITY}]"
                        render_group_like_room(f"Queue Group {g.get('group_n', '?')}", pdata_ids, avg, status, None)

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
