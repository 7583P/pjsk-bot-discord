# rooms.py
# PJSK Lounge - Rooms status updater
#
# Fix: mantiene 1 mensaje por canal de status (global vs JP) y SOLO muestra el join correspondiente.
# - #rooms  -> muestra SOLO #join
# - #jp-rooms -> muestra SOLO #jp-join
#
# Requisitos:
# - discord.py 2.x
# - permisos del bot en ambos canales: View Channel, Read Message History, Send Messages, Embed Links

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence

import discord
from discord.ext import commands, tasks

try:
    from zoneinfo import ZoneInfo  # py>=3.9
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


log = logging.getLogger(__name__)


# ----------------------------
# Config (importa tu config.py si existe, si no usa ENV)
# ----------------------------

def _env_int(name: str, default: Optional[int] = None) -> int:
    v = os.getenv(name)
    if v is None:
        if default is None:
            raise RuntimeError(f"Missing required env var: {name}")
        return default
    try:
        return int(v)
    except ValueError as e:
        raise RuntimeError(f"Invalid int env var {name}={v!r}") from e


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


# Intenta leer desde config.py (recomendado)
try:
    from config import (  # type: ignore
        ROOMS_CHANNEL_ID,
        JOIN_CHANNEL_ID,
        JP_ROOMS_CHANNEL_ID,
        JP_JOIN_CHANNEL_ID,
        ROOMS_UPDATE_INTERVAL_SECONDS,
        ROOMS_SLOT_INTERVAL_MINUTES,
        ROOMS_STATUS_SCAN_LIMIT,
        ROOMS_TIMEZONE,
        ROOMS_PIN_STATUS_MESSAGE,
        SQLITE_PATH,
    )
except Exception:
    # Fallback a env vars
    ROOMS_CHANNEL_ID = _env_int("ROOMS_CHANNEL_ID")
    JOIN_CHANNEL_ID = _env_int("JOIN_CHANNEL_ID")
    JP_ROOMS_CHANNEL_ID = _env_int("JP_ROOMS_CHANNEL_ID")
    JP_JOIN_CHANNEL_ID = _env_int("JP_JOIN_CHANNEL_ID")

    ROOMS_UPDATE_INTERVAL_SECONDS = _env_int("ROOMS_UPDATE_INTERVAL_SECONDS", 30)
    ROOMS_SLOT_INTERVAL_MINUTES = _env_int("ROOMS_SLOT_INTERVAL_MINUTES", 30)
    ROOMS_STATUS_SCAN_LIMIT = _env_int("ROOMS_STATUS_SCAN_LIMIT", 50)

    ROOMS_TIMEZONE = _env_str("ROOMS_TIMEZONE", "America/Lima")
    ROOMS_PIN_STATUS_MESSAGE = _env_bool("ROOMS_PIN_STATUS_MESSAGE", False)
    SQLITE_PATH = _env_str("SQLITE_PATH", "pjsk_lounge.db")


# ----------------------------
# SQLite state store (solo para recordar el message_id por canal)
# ----------------------------

class BotStateStore:
    def __init__(self, path: str) -> None:
        self.path = path

    def _ensure(self) -> None:
        conn = sqlite3.connect(self.path)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _get(self, key: str) -> Optional[str]:
        conn = sqlite3.connect(self.path)
        try:
            cur = conn.cursor()
            cur.execute("SELECT value FROM bot_state WHERE key = ?", (key,))
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def _set(self, key: str, value: str) -> None:
        conn = sqlite3.connect(self.path)
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO bot_state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            conn.commit()
        finally:
            conn.close()

    async def init(self) -> None:
        await asyncio.to_thread(self._ensure)

    async def get(self, key: str) -> Optional[str]:
        return await asyncio.to_thread(self._get, key)

    async def set(self, key: str, value: str) -> None:
        await asyncio.to_thread(self._set, key, value)


# ----------------------------
# Rooms status Cog
# ----------------------------

@dataclass(frozen=True)
class RegionConfig:
    key: str
    status_channel_id: int
    join_channel_id: int
    state_message_id_key: str


class RoomsStatusCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = BotStateStore(SQLITE_PATH)

        self.regions: Sequence[RegionConfig] = (
            RegionConfig(
                key="global",
                status_channel_id=ROOMS_CHANNEL_ID,
                join_channel_id=JOIN_CHANNEL_ID,
                state_message_id_key="rooms_status_message_id_global",
            ),
            RegionConfig(
                key="jp",
                status_channel_id=JP_ROOMS_CHANNEL_ID,
                join_channel_id=JP_JOIN_CHANNEL_ID,
                state_message_id_key="rooms_status_message_id_jp",
            ),
        )

        self._tz = None
        if ZoneInfo is not None:
            try:
                self._tz = ZoneInfo(ROOMS_TIMEZONE)
            except Exception:
                self._tz = None

        self.update_rooms_status.start()

    def cog_unload(self) -> None:
        self.update_rooms_status.cancel()

    def _now(self) -> datetime:
        now = datetime.utcnow()
        if self._tz is not None:
            return now.replace(tzinfo=discord.utils.UTC).astimezone(self._tz)
        return now

    @tasks.loop(seconds=ROOMS_UPDATE_INTERVAL_SECONDS)
    async def update_rooms_status(self) -> None:
        for cfg in self.regions:
            try:
                await self._update_one_region(cfg)
            except Exception:
                log.exception("rooms status update failed for region=%s", cfg.key)

    @update_rooms_status.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()
        await self.store.init()

    async def _update_one_region(self, cfg: RegionConfig) -> None:
        status_ch = await self._get_text_channel(cfg.status_channel_id)
        join_ch = await self._get_text_channel(cfg.join_channel_id)

        if status_ch is None:
            log.warning("Status channel not found: %s (%s)", cfg.status_channel_id, cfg.key)
            return
        if join_ch is None:
            log.warning("Join channel not found: %s (%s)", cfg.join_channel_id, cfg.key)
            return

        embed = await self._build_embed(join_ch, cfg_key=cfg.key)

        msg = await self._get_or_discover_status_message(status_ch, cfg.state_message_id_key)
        if msg is None:
            # Crea uno nuevo
            msg = await status_ch.send(embed=embed)
            if ROOMS_PIN_STATUS_MESSAGE:
                try:
                    await msg.pin(reason="Rooms status message")
                except Exception:
                    pass
            await self.store.set(cfg.state_message_id_key, str(msg.id))
            return

        # Edita el mensaje existente (respeta canal + mensaje)
        await msg.edit(content="", embed=embed)

    async def _get_text_channel(self, channel_id: int) -> Optional[discord.TextChannel]:
        ch = self.bot.get_channel(channel_id)
        if isinstance(ch, discord.TextChannel):
            return ch
        try:
            fetched = await self.bot.fetch_channel(channel_id)
            return fetched if isinstance(fetched, discord.TextChannel) else None
        except Exception:
            return None

    async def _get_or_discover_status_message(
        self,
        status_channel: discord.TextChannel,
        state_key: str,
    ) -> Optional[discord.Message]:
        # 1) Intenta por ID persistido (lo más seguro)
        stored = await self.store.get(state_key)
        if stored:
            try:
                mid = int(stored)
                return await status_channel.fetch_message(mid)
            except Exception:
                # si ya no existe / no se puede leer, cae a discovery
                pass

        # 2) Discovery: busca el último mensaje del bot en ese canal
        me_id = self.bot.user.id if self.bot.user else None
        if me_id is None:
            return None

        async for m in status_channel.history(limit=ROOMS_STATUS_SCAN_LIMIT, oldest_first=False):
            if m.author and m.author.id == me_id:
                # Preferimos el que ya es "PJSK ROOMS" embed, pero también aceptamos el viejo "No active rooms"
                if m.embeds:
                    e = m.embeds[0]
                    if (e.title or "").strip().upper() == "PJSK ROOMS":
                        await self.store.set(state_key, str(m.id))
                        return m
                # fallback por contenido legacy
                if m.content and ("No active rooms" in m.content or "Last Update" in m.content or "PJSK" in m.content):
                    await self.store.set(state_key, str(m.id))
                    return m

        return None

    def _compute_next_slot(self, now: datetime, interval_minutes: int) -> datetime:
        # Redondea hacia arriba al próximo múltiplo del intervalo
        base = now.replace(second=0, microsecond=0)
        minutes = base.minute
        remainder = minutes % interval_minutes
        if remainder == 0:
            # si ya está exacto, toma el siguiente slot (evita que "next slot" sea ahora)
            return base + timedelta(minutes=interval_minutes)
        delta = interval_minutes - remainder
        return base + timedelta(minutes=delta)

    async def _build_embed(self, join_channel: discord.TextChannel, cfg_key: str) -> discord.Embed:
        now = self._now()
        now_ts = int(now.timestamp())
        next_slot = self._compute_next_slot(now, ROOMS_SLOT_INTERVAL_MINUTES)
        next_ts = int(next_slot.timestamp())

        active_threads = await self._get_active_threads_for(join_channel)

        # Render de rooms
        if not active_threads:
            rooms_lines = ["_No active rooms._"]
        else:
            rooms_lines = []
            for t in active_threads[:25]:
                # member_count puede ser None; lo mostramos sólo si existe
                mc = getattr(t, "member_count", None)
                suffix = f" — **{mc}/5**" if isinstance(mc, int) and mc > 0 else ""
                rooms_lines.append(f"• <#{t.id}>{suffix}")

        # Queue (opcional: intenta leer desde un provider si existe; si no, muestra vacío)
        queue_lines = await self._render_queue(cfg_key)
        if not queue_lines:
            queue_lines = ["_Queue empty._"]

        desc = []
        desc.append(f"**Last update:** <t:{now_ts}:T> (<t:{now_ts}:R>)")
        desc.append("")
        desc.append(f"**#{join_channel.name}**")
        desc.extend(rooms_lines)
        desc.append("────────────────────────")
        desc.append(f"**FUTURE ROOMS** (next slot <t:{next_ts}:T>, <t:{next_ts}:R>)")
        desc.extend(queue_lines)

        embed = discord.Embed(
            title="PJSK ROOMS",
            description="\n".join(desc),
        )
        return embed

    async def _get_active_threads_for(self, join_channel: discord.TextChannel) -> list[discord.Thread]:
        # Asegura lista completa (sin depender solo de cache)
        threads: list[discord.Thread] = []

        # 1) cache local del canal
        try:
            threads.extend([t for t in join_channel.threads if not t.archived])
        except Exception:
            pass

        # 2) fetch global de threads activos del guild (más confiable)
        try:
            active = await join_channel.guild.active_threads()
            # discord.py devuelve objeto con .threads en algunas versiones; en otras, lista directa
            active_threads = getattr(active, "threads", active)
            for t in active_threads:
                if isinstance(t, discord.Thread) and t.parent_id == join_channel.id and not t.archived:
                    threads.append(t)
        except Exception:
            pass

        # Dedup por id
        uniq: dict[int, discord.Thread] = {}
        for t in threads:
            uniq[t.id] = t

        # Orden: más nuevos primero
        out = list(uniq.values())
        out.sort(key=lambda x: x.created_at or discord.utils.snowflake_time(x.id), reverse=True)
        return out

    async def _render_queue(self, cfg_key: str) -> list[str]:
        """
        Intenta obtener la cola desde distintos providers sin acoplar rooms.py a tu matchmaking.
        Si no encuentra nada, devuelve [] y el embed mostrará Queue empty.
        """
        # 1) Si existe un cog con método get_queue_snapshot(region_key) -> list[int]
        for cog_name in ("Matchmaking", "MatchmakingCog", "Queue", "QueueCog"):
            cog = self.bot.get_cog(cog_name)
            if cog is None:
                continue
            fn = getattr(cog, "get_queue_snapshot", None)
            if callable(fn):
                try:
                    user_ids = await fn(cfg_key)  # type: ignore[misc]
                    return self._format_queue_user_ids(user_ids)
                except Exception:
                    pass

        # 2) Si el bot expone un atributo queue_by_region / queues
        for attr in ("queue_by_region", "queues", "queue"):
            q = getattr(self.bot, attr, None)
            if isinstance(q, dict):
                user_ids = q.get(cfg_key) or q.get(cfg_key.upper()) or q.get(cfg_key.lower())
                if isinstance(user_ids, (list, tuple)):
                    return self._format_queue_user_ids(user_ids)

        return []

    def _format_queue_user_ids(self, user_ids: Sequence[int]) -> list[str]:
        ids = [int(x) for x in user_ids if isinstance(x, int) or (isinstance(x, str) and str(x).isdigit())]
        if not ids:
            return []
        # Muestra máximo 10 para no hacer embed gigante
        shown = ids[:10]
        mentions = " ".join(f"<@{uid}>" for uid in shown)
        more = f" (+{len(ids) - len(shown)} more)" if len(ids) > len(shown) else ""
        return [f"**{len(ids)} queued:** {mentions}{more}"]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RoomsStatusCog(bot))
