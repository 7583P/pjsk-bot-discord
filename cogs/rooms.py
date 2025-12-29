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
                    # Primero intentamos con marker nuevo
                    if msg.content and BOARD_MARKER in msg.content:
                        return msg

            async for msg in channel.history(limit=50):
                if msg.author and msg.author.id == me.id:
                    # Compatibilidad con tu marker viejo
                    if msg.content and ("Last Update" in msg.content or "Last update" in msg.content):
                        return msg

            # Último fallback: último mensaje del bot, aunque sea embed (lo vamos a limpiar)
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
            self._state[str(cat_id)] = {"channel_id": channel.id, "message_id": prev.id}
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
                # No dejes que una sala rota rompa todo el update
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

        # IMPORTANTE: procesa cada categoría con try/except para no bloquear JP
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

                lines: list[str] = []
                lines.append("**PJSK ROOMS**")
                lines.append(f"**{join_label}**")
                lines.append("")

                def render_room(rid: int, pdata_ids: list[tuple[int, int]], avg: int, status: str, thread_id: Optional[int]):
                    count = len(pdata_ids)
                    ref = f"<#{thread_id}>" if isinstance(thread_id, int) and thread_id > 0 else ""
                    if ref:
                        lines.append(f"**Room {rid} · {ref} · {count}/{ROOM_CAPACITY} Players · Average MMR {avg} · {status}**")
                    else:
                        lines.append(f"**Room {rid} · {count}/{ROOM_CAPACITY} Players · Average MMR {avg} · {status}**")
                    for uid, mmr in pdata_ids:
                        lines.append(f"- {self._user_label(guild, uid)} ({mmr})")
                    lines.append("")

                if not active_rooms:
                    lines.append("_No active rooms._")
                    lines.append("")
                else:
                    for (rid, pdata_ids, avg, started_flag, started_ts, reached5_ts, thread_id, phase, slot_start_ts, songs_posted) in active_rooms:
                        count = len(pdata_ids)
                        if started_flag or phase == PHASE_STARTED:
                            status = f"[STARTED] <t:{started_ts}:t>" if started_ts else "[STARTED]"
                        else:
                            if count == ROOM_CAPACITY:
                                status = f"[READY 5/5] <t:{reached5_ts}:t>" if reached5_ts else "[READY 5/5]"
                            else:
                                status = f"[WAITING {count}/{ROOM_CAPACITY}]"
                        render_room(rid, pdata_ids, avg, status, thread_id)

                lines.append("────────────────────────")

                # FUTURE header siempre (como tu screenshot)
                lines.append(f"**FUTURE ROOMS** (next slot <t:{next_slot_ts}:T>, <t:{next_slot_ts}:R>)")

                # Si hay rooms FUTURE explícitas, muéstralas
                if future_rooms:
                    lines.append("")
                    for (rid, pdata_ids, avg, started_flag, started_ts, reached5_ts, thread_id, phase, slot_start_ts, songs_posted) in future_rooms:
                        count = len(pdata_ids)
                        if slot_start_ts:
                            status = f"[WAITING {count}/{ROOM_CAPACITY}] · songs at <t:{slot_start_ts}:t> (<t:{slot_start_ts}:R>)"
                        else:
                            status = f"[WAITING {count}/{ROOM_CAPACITY}]"
                        render_room(rid, pdata_ids, avg, status, thread_id)

                # Queue (derivada del join correspondiente)
                queue_raw = queues.get(join_id, []) if isinstance(join_id, int) else []
                queue_ids: list[int] = []
                for item in (queue_raw or []):
                    if isinstance(item, int):
                        queue_ids.append(item)
                    elif isinstance(item, (discord.Member, discord.User)):
                        queue_ids.append(item.id)
                queue_ids = list(dict.fromkeys(queue_ids))

                if not queue_ids:
                    lines.append("_Queue empty._")
                else:
                    mmr_map_q = await self._fetch_mmrs_batch(mm, queue_ids, season_id, has_pool)
                    lines.append("")
                    for idx in range(0, len(queue_ids), ROOM_CAPACITY):
                        grp_ids = queue_ids[idx: idx + ROOM_CAPACITY]
                        grp_mmrs = [mmr_map_q.get(uid, 0) for uid in grp_ids]
                        avg_q = (sum(grp_mmrs) // len(grp_mmrs)) if grp_mmrs else 0
                        count_q = len(grp_ids)
                        q_status = "[READY 5/5]" if count_q == ROOM_CAPACITY else f"[WAITING {count_q}/{ROOM_CAPACITY}]"
                        group_n = (idx // ROOM_CAPACITY) + 1
                        lines.append(f"**Queue Group {group_n} · {count_q}/{ROOM_CAPACITY} Players · Average MMR {avg_q} · {q_status}**")
                        for uid in grp_ids:
                            lines.append(f"- {self._user_label(guild, uid)} ({mmr_map_q.get(uid, 0)})")
                        lines.append("")

                content = self._build_content(lines, now_ts)

                prev_msg = self.posted_messages.get(cat_id)

                # Si no existe mensaje previo o el canal cambió, mandamos uno nuevo
                if prev_msg is None or prev_msg.channel.id != channel.id:
                    msg = await channel.send(
                        content,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    self.posted_messages[cat_id] = msg
                    self._state[str(cat_id)] = {"channel_id": channel.id, "message_id": msg.id}
                    _save_state(self._state)
                    continue

                # Edit seguro: LIMPIA embeds (esto arregla tu caso donde quedó el embed “PJSK ROOMS” mezclado)
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
                        self._state[str(cat_id)] = {"channel_id": channel.id, "message_id": msg.id}
                        _save_state(self._state)

            except Exception as e:
                print(f"[Rooms] Error actualizando categoría {cat_id}: {e}")
                continue


async def setup(bot: commands.Bot):
    await bot.add_cog(Rooms(bot))
