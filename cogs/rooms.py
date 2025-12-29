# rooms.py
import os
import json
import asyncio
import datetime as dt
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks


GUILD_ID = int(os.getenv("GUILD_ID", "0"))

# Si tienes el ID exacto del canal #rooms, ponlo en env:
#   ROOMS_CHANNEL_ID=123...
ROOMS_CHANNEL_ID = int(os.getenv("ROOMS_CHANNEL_ID", "0"))
ROOMS_CHANNEL_NAME = os.getenv("ROOMS_CHANNEL_NAME", "rooms")

LIMA_TZ = ZoneInfo("America/Lima")

ROOM_CAPACITY = 5

# Deben matchear matchmaking.py
PHASE_FUTURE = "FUTURE"
PHASE_PREPARED = "PREPARED"
PHASE_STARTED = "STARTED"
PHASE_PENDING = "PENDING"
PHASE_FINISHED = "FINISHED"


def _safe_int(x, default=0) -> int:
    try:
        return int(x)
    except Exception:
        return default


class Rooms(commands.Cog):
    """
    Mantiene el tablero #rooms (un solo mensaje) mostrando:

    - ROOMS STARTED (con timestamp) + lista de jugadores (ordenados por MMR desc)
    - ROOMS NO STARTED pero ya lock-in (threads creados en PREP) con su start ts
    - FUTURE ROOMS (preview desde la queue /c; NO crea threads) con slots vacíos

    Se alimenta del cog Matchmaking:
      - match_cog.rooms
      - match_cog.queues
      - match_cog._get_ordered_queue_members(...)
      - match_cog._fetch_mmrs_batch(...)
      - match_cog._is_placement_member(...)
      - match_cog.START_WINDOWS (si existe)
    """

    CACHE_PATH = "assets/cached_rooms_board.json"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._refresh_lock = asyncio.Lock()
        self._board_message_id: int | None = None

        self._load_cache()

        self.auto_refresh.start()

    # -------------------------
    # Cache (message id)
    # -------------------------
    def _load_cache(self):
        try:
            if os.path.isfile(self.CACHE_PATH):
                with open(self.CACHE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._board_message_id = _safe_int(data.get("board_message_id"), None)
        except Exception:
            self._board_message_id = None

    def _save_cache(self):
        try:
            os.makedirs(os.path.dirname(self.CACHE_PATH), exist_ok=True)
            with open(self.CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump({"board_message_id": self._board_message_id}, f)
        except Exception:
            pass

    # -------------------------
    # Helpers
    # -------------------------
    def _now_lima(self) -> dt.datetime:
        return dt.datetime.now(tz=LIMA_TZ)

    def _get_matchmaking(self):
        return self.bot.get_cog("Matchmaking")

    async def _resolve_rooms_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        if ROOMS_CHANNEL_ID:
            ch = guild.get_channel(ROOMS_CHANNEL_ID)
            return ch if isinstance(ch, discord.TextChannel) else None

        # fallback: por nombre
        for ch in guild.text_channels:
            if ch.name == ROOMS_CHANNEL_NAME:
                return ch
        return None

    async def _get_or_create_board_message(
        self, rooms_channel: discord.TextChannel
    ) -> discord.Message | None:
        # Si tenemos ID cacheado, intentamos fetch
        if self._board_message_id:
            try:
                msg = await rooms_channel.fetch_message(self._board_message_id)
                return msg
            except Exception:
                self._board_message_id = None

        # Buscar uno existente reciente por “firma” (best effort)
        try:
            async for m in rooms_channel.history(limit=25):
                if m.author == self.bot.user and m.embeds:
                    emb = m.embeds[0]
                    if emb.title and "PJSK ROOMS" in emb.title:
                        self._board_message_id = m.id
                        self._save_cache()
                        return m
        except Exception:
            pass

        # Crear uno nuevo
        try:
            msg = await rooms_channel.send(embed=discord.Embed(title="PJSK ROOMS", description="Initializing..."))
            self._board_message_id = msg.id
            self._save_cache()
            return msg
        except Exception:
            return None

    def _starts_minutes(self, match_cog) -> list[int]:
        # match_cog.START_WINDOWS = ((0,4),(20,24),(40,44)) -> base: 0,20,40
        sw = getattr(match_cog, "START_WINDOWS", ((0, 4), (30, 34)))
        mins = []
        for a, _b in sw:
            mins.append(int(a))
        mins = sorted(set(mins))
        return mins if mins else [0, 30]

    def _next_slot_dt(self, now: dt.datetime, match_cog) -> dt.datetime:
        mins = self._starts_minutes(match_cog)
        base = now.replace(second=0, microsecond=0)
        for m in mins:
            if m > base.minute:
                return base.replace(minute=m)
        # siguiente hora, primer minuto base
        return (base + dt.timedelta(hours=1)).replace(minute=mins[0])

    async def _normalize_room_players(
        self, guild: discord.Guild, info: dict
    ) -> list[discord.Member]:
        """
        info["players"] puede venir como:
          - list[discord.Member]
          - list[int] (si alguien serializa)
          - list[str] de IDs
        Normalizamos a list[discord.Member] (sin bots).
        """
        raw = info.get("players", []) or []
        out: list[discord.Member] = []
        seen: set[int] = set()

        for p in raw:
            m = None
            if isinstance(p, discord.Member):
                m = p
            elif isinstance(p, int):
                m = guild.get_member(p)
            elif isinstance(p, str) and p.isdigit():
                m = guild.get_member(int(p))
            elif isinstance(p, dict):
                pid = p.get("id") or p.get("user_id")
                if isinstance(pid, int):
                    m = guild.get_member(pid)
                elif isinstance(pid, str) and pid.isdigit():
                    m = guild.get_member(int(pid))

            if m and not m.bot and m.id not in seen:
                seen.add(m.id)
                out.append(m)

        return out

    async def _mmr_map_for_members(self, match_cog, members: list[discord.Member]) -> dict[int, int]:
        # Placement: se mostrará como "Placement" (no depende de MMR)
        nonp = [m for m in members if not match_cog._is_placement_member(m)]
        mmr_map = await match_cog._fetch_mmrs_batch([m.id for m in nonp])
        return {int(k): int(v) for k, v in (mmr_map or {}).items()}

    def _sort_room_members(self, match_cog, members: list[discord.Member], mmr_map: dict[int, int]) -> list[discord.Member]:
        # No-placement primero por MMR desc, Placement al final (estable por nombre/id)
        def key(m: discord.Member):
            if match_cog._is_placement_member(m):
                return (1, 0, m.id)
            return (0, -int(mmr_map.get(m.id, 0)), m.id)

        return sorted(members, key=key)

    def _room_status_line(self, rid: int, info: dict) -> str:
        disp = _safe_int(info.get("display_num"), rid)
        phase = str(info.get("phase") or "")
        thread_id = _safe_int(info.get("thread"), 0)

        # STARTED: timestamp obligatorio en la cabecera
        if phase == PHASE_STARTED or bool(info.get("started", False)):
            started_at = _safe_int(info.get("started_at"), 0)
            ts = f"<t:{started_at}:T> (<t:{started_at}:R>)" if started_at else ""
            return f"ROOM {disp} — STARTED {ts}  ·  <#{thread_id}>" if thread_id else f"ROOM {disp} — STARTED {ts}"

        # FUTURE/PREPARED (lock-in para slot)
        slot_ts = info.get("slot_start_ts")
        slot_ts = _safe_int(slot_ts, 0) if slot_ts is not None else 0
        if phase in (PHASE_FUTURE, PHASE_PREPARED):
            if slot_ts:
                return f"ROOM {disp} — LOCKED IN (starts <t:{slot_ts}:T>, <t:{slot_ts}:R>)  ·  <#{thread_id}>" if thread_id else f"ROOM {disp} — LOCKED IN (starts <t:{slot_ts}:T>, <t:{slot_ts}:R>)"
            return f"ROOM {disp} — LOCKED IN  ·  <#{thread_id}>" if thread_id else f"ROOM {disp} — LOCKED IN"

        if phase == PHASE_PENDING:
            return f"ROOM {disp} — PENDING  ·  <#{thread_id}>" if thread_id else f"ROOM {disp} — PENDING"

        return f"ROOM {disp} — NOT STARTED  ·  <#{thread_id}>" if thread_id else f"ROOM {disp} — NOT STARTED"

    async def _render_join_section(self, guild: discord.Guild, join_id: int) -> str:
        match_cog = self._get_matchmaking()
        if not match_cog:
            return ""

        join_channel = guild.get_channel(join_id)
        join_name = f"#{join_channel.name}" if isinstance(join_channel, discord.TextChannel) else f"join:{join_id}"

        # Rooms activos de ese join_channel
        rooms = []
        for rid, info in (match_cog.rooms or {}).items():
            if info.get("finished", False):
                continue
            if _safe_int(info.get("join_channel_id"), 0) != int(join_id):
                continue
            rooms.append((int(rid), info))

        # Separamos STARTED primero (como tu ejemplo)
        started_rooms = []
        other_rooms = []
        for rid, info in rooms:
            phase = str(info.get("phase") or "")
            if phase == PHASE_STARTED or bool(info.get("started", False)):
                started_rooms.append((rid, info))
            else:
                other_rooms.append((rid, info))

        # Orden: por display_num (si existe)
        def disp_key(item):
            rid, info = item
            return (_safe_int(info.get("display_num"), 999999), rid)

        started_rooms.sort(key=disp_key)
        other_rooms.sort(key=disp_key)

        lines: list[str] = []
        lines.append(f"**{join_name}**")

        if not started_rooms and not other_rooms:
            lines.append("_No active rooms._")
        else:
            # Render rooms
            for rid, info in (started_rooms + other_rooms)[:12]:  # safety cap
                lines.append(self._room_status_line(rid, info))

                members = await self._normalize_room_players(guild, info)
                mmr_map = await self._mmr_map_for_members(match_cog, members)
                ordered = self._sort_room_members(match_cog, members, mmr_map)

                # Lista jugadores (ordenados por MMR desc)
                if ordered:
                    for idx, m in enumerate(ordered, start=1):
                        if match_cog._is_placement_member(m):
                            lines.append(f"{idx}) {m.display_name} — Placement")
                        else:
                            lines.append(f"{idx}) {m.display_name} — {int(mmr_map.get(m.id, 0))} MMR")
                else:
                    lines.append("_No players._")

                lines.append("")  # blank line entre rooms

            if len(started_rooms) + len(other_rooms) > 12:
                lines.append(f"_…and {len(started_rooms) + len(other_rooms) - 12} more rooms._")
                lines.append("")

        # FUTURE ROOMS (desde queue)
        now = self._now_lima()
        next_slot = self._next_slot_dt(now, match_cog)
        next_slot_ts = int(next_slot.timestamp())

        ordered_queue = await match_cog._get_ordered_queue_members(guild, join_id)
        # chunk de 5 (ROOM_CAPACITY)
        groups = [ordered_queue[i : i + ROOM_CAPACITY] for i in range(0, len(ordered_queue), ROOM_CAPACITY)]

        lines.append("—" * 32)
        lines.append(f"**FUTURE ROOMS** (next slot <t:{next_slot_ts}:T>, <t:{next_slot_ts}:R>)")

        if not groups:
            lines.append("_Queue empty._")
        else:
            # MMR map en batch para todos los non-placement del queue
            all_members = [m for g in groups for m in g]
            mmr_map_all = await self._mmr_map_for_members(match_cog, all_members)

            for i, g in enumerate(groups[:12], start=1):  # safety cap
                lines.append(f"ROOM {i}")
                # ya viene ordenado por MMR desc + placement al final por _get_ordered_queue_members
                for pos in range(1, ROOM_CAPACITY + 1):
                    if pos <= len(g):
                        m = g[pos - 1]
                        if match_cog._is_placement_member(m):
                            lines.append(f"{pos}) {m.display_name} — Placement")
                        else:
                            lines.append(f"{pos}) {m.display_name} — {int(mmr_map_all.get(m.id, 0))} MMR")
                    else:
                        lines.append(f"{pos}) [empty]")
                lines.append("")

            if len(groups) > 12:
                lines.append(f"_…and {len(groups) - 12} more future rooms._")

        return "\n".join(lines).rstrip()

    async def _render_board_embed(self, guild: discord.Guild) -> discord.Embed:
        match_cog = self._get_matchmaking()
        now = self._now_lima()
        ts = int(now.timestamp())

        emb = discord.Embed(
            title="PJSK ROOMS",
            description=f"Last update: <t:{ts}:T> (<t:{ts}:R>)",
        )

        if not match_cog:
            emb.add_field(name="Status", value="Matchmaking cog not loaded.", inline=False)
            return emb

        # join channels = keys de match_cog.queues (EN/JP)
        join_ids = list((match_cog.queues or {}).keys())
        join_ids = sorted(set(int(x) for x in join_ids if int(x) > 0))

        if not join_ids:
            emb.add_field(name="Status", value="No join channels configured.", inline=False)
            return emb

        for join_id in join_ids:
            block = await self._render_join_section(guild, join_id)
            # Discord embed field value limit ~1024; si te pasas, cortamos.
            if len(block) > 1024:
                block = block[:1000] + "\n…(truncated)"
            emb.add_field(name="\u200b", value=block, inline=False)

        return emb

    # -------------------------
    # Refresh pipeline
    # -------------------------
    async def refresh_board(self):
        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            return

        rooms_channel = await self._resolve_rooms_channel(guild)
        if not rooms_channel:
            return

        async with self._refresh_lock:
            msg = await self._get_or_create_board_message(rooms_channel)
            if not msg:
                return

            embed = await self._render_board_embed(guild)

            try:
                await msg.edit(embed=embed, content=None)
            except Exception:
                # si falló editar, intentamos crear otro
                try:
                    msg2 = await rooms_channel.send(embed=embed)
                    self._board_message_id = msg2.id
                    self._save_cache()
                except Exception:
                    pass

    # -------------------------
    # Events
    # -------------------------
    @commands.Cog.listener()
    async def on_room_updated(self, _rid):
        await self.refresh_board()

    @commands.Cog.listener()
    async def on_room_finished(self, _rid):
        await self.refresh_board()

    # -------------------------
    # Periodic refresh (safety)
    # -------------------------
    @tasks.loop(seconds=30.0)
    async def auto_refresh(self):
        await self.refresh_board()

    @auto_refresh.before_loop
    async def _before_auto_refresh(self):
        await self.bot.wait_until_ready()

    async def cog_unload(self):
        try:
            self.auto_refresh.cancel()
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Rooms(bot))
