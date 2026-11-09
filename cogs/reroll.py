"""/reroll — staff recovery of a failed song selection, without losing the room.

Only Discord user 878310498720940102 may run it (checked on the backend by ID,
never by permissions/roles). All durable state lives in PostgreSQL
(see lounge/reroll.py); this cog only (1) authorizes, (2) serializes against
other work on the same room, (3) picks songs with the normal matchmaking rules,
(4) publishes to Discord and (5) resumes unfinished publications after a
restart/outage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re

import discord
from discord import app_commands
from discord.ext import commands, tasks

from lounge.reroll import AUTHORIZED_USER_ID, RerollError, RerollService, Selection
from lounge.room_store import RoomStore

log = logging.getLogger("reroll")

GUILD_ID = int(os.getenv("GUILD_ID") or "1371306026917298267")
COMMAND_TIMEOUT_SEC = float(os.getenv("REROLL_TIMEOUT_SEC", "25"))
SONGS_PER_ROOM = 5
MARKER = "Reroll #{id}"


def _tuples(songs):
    return [tuple(s) for s in songs or []]


def render_selection_message(result) -> str:
    lines = "\n".join(
        f"{i}. **{t}** (Lv {lvl} {diff}) [{notes} notes]" for i, (t, lvl, diff, notes) in enumerate(result_songs(result), 1)
    )
    return (
        f"🔄 **Song selection recovered by staff** ({MARKER.format(id=result_id(result))})\n"
        f"Same room, same players. No need to use /c or /d again; continue as normal.\n\n"
        f"Selected songs (Lv {result_low(result)}-{result_high(result)})\n"
        f"(Total {result_total(result)} notes)\n"
        f"{lines}"
    )


# Small adapters so the same renderer works for a fresh result and a pending DB row.
def result_songs(r):
    return r["songs"] if isinstance(r, dict) else r.new


def result_id(r):
    return r["audit_id"] if isinstance(r, dict) else r.audit_id


def result_low(r):
    return r["low"] if isinstance(r, dict) else r.low


def result_high(r):
    return r["high"] if isinstance(r, dict) else r.high


def result_total(r):
    return r["total"] if isinstance(r, dict) else r.total_notes


class RerollCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.service: RerollService | None = None
        self._publish_guard: set[int] = set()

    async def cog_load(self):
        self.service = RerollService(self.bot.db_pool)
        self.resume_pending.start()

    async def cog_unload(self):
        self.resume_pending.cancel()

    # ------------------------------------------------------------ helpers
    @property
    def mm(self):
        return self.bot.get_cog("Matchmaking")

    async def _members_for(self, guild: discord.Guild, payload: dict) -> list[discord.Member]:
        members = []
        for uid in payload.get("player_ids", []):
            m = guild.get_member(int(uid))
            if m is None:
                try:
                    m = await guild.fetch_member(int(uid))
                except discord.HTTPException:
                    m = None
            if m is None:
                raise RerollError(
                    "member_missing",
                    f"Player <@{int(uid)}> could not be resolved in the server, so the room was left untouched "
                    "(nobody was removed). Resolve it and run /reroll again.",
                )
            members.append(m)
        return members

    async def _parse_manual(self, spec: str, server: str) -> list[tuple]:
        mm = self.mm
        picks, seen = [], set()
        tokens = [t for t in re.split(r"[,\s;]+", spec.strip()) if t]
        if not tokens or len(tokens) > SONGS_PER_ROOM:
            raise RerollError("bad_manual", f"Give 1-{SONGS_PER_ROOM} song ids like `123` or `123:master`.")
        banned = mm._banned_song_filter_sql("s")
        async with self.bot.db_pool.acquire() as conn:
            for tok in tokens:
                sid, _, diff = tok.partition(":")
                if not sid.isdigit():
                    raise RerollError("bad_manual", f"`{tok}` is not a valid song id.")
                rows = await conn.fetch(
                    f"""SELECT s.title,s.level,s.diff,s.notes_count FROM songs s
                        WHERE s.id=$1 AND s.server=$2 AND ($3::text='' OR lower(s.diff)=lower($3)) {banned}""",
                    int(sid), server, diff.strip(),
                )
                if not rows:
                    raise RerollError("bad_manual", f"Song id `{sid}`{':' + diff if diff else ''} does not exist for this room's server ({server.upper()}) or is banned.")
                if len(rows) > 1:
                    opts = ", ".join(sorted(r["diff"] for r in rows))
                    raise RerollError("bad_manual", f"Song id `{sid}` has several difficulties ({opts}); use `{sid}:<difficulty>`.")
                r = rows[0]
                key = mm._song_title_key(r["title"])
                if key in seen:
                    raise RerollError("bad_manual", "The same song was listed twice.")
                seen.add(key)
                picks.append((r["title"], int(r["level"]), r["diff"].capitalize(), int(r["notes_count"])))
        return picks

    async def _verify_catalog(self, server: str, picks: list) -> None:
        """Every pick must exist for this server and not be banned (valid for all participants)."""
        mm = self.mm
        banned = mm._banned_song_filter_sql("s")
        async with self.bot.db_pool.acquire() as conn:
            n = await conn.fetchval(
                f"""SELECT count(*) FROM unnest($2::text[],$3::text[],$4::int[]) AS x(title,diff,level)
                    WHERE EXISTS(SELECT 1 FROM songs s WHERE s.server=$1 AND s.title=x.title
                                 AND lower(s.diff)=lower(x.diff) AND s.level=x.level {banned})""",
                server, [p[0] for p in picks], [p[2] for p in picks], [int(p[1]) for p in picks],
            )
        if int(n) != len(picks):
            raise RerollError("invalid_catalog", "The generated selection contained a song that is not valid/available for this server; nothing was changed. Retry /reroll.")

    def _make_selector(self, members, manual_spec: str | None):
        mm = self.mm

        async def selector(payload: dict, exclude: list) -> Selection:
            server = mm._normalize_song_server(payload.get("server", "en"))
            if server == "all":
                server = "en"
            room_info = {"server": server, "players": members}
            notes: list[str] = []
            manual: list = []
            if manual_spec:
                manual = await self._parse_manual(manual_spec, server)
            picks = list(manual)
            low = high = None
            if len(picks) < SONGS_PER_ROOM:
                taken = list(exclude) + [p[0] for p in picks]
                auto, low, high = await mm._select_room_songs(room_info, players=members, exclude_titles=taken)
                picks.extend(auto[: SONGS_PER_ROOM - len(picks)])
                if len(picks) < SONGS_PER_ROOM and exclude:
                    # Alternatives exhausted: only now allow previously failed titles again.
                    have = {mm._song_title_key(p[0]) for p in picks}
                    again, low2, high2 = await mm._select_room_songs(
                        room_info, players=members, exclude_titles=[p[0] for p in picks]
                    )
                    refill = [p for p in again if mm._song_title_key(p[0]) not in have]
                    if refill:
                        notes.append("Not enough fresh songs; some previously used songs were allowed again.")
                    picks.extend(refill[: SONGS_PER_ROOM - len(picks)])
                    low, high = (low if low is not None else low2), (high if high is not None else high2)
                if len(picks) < SONGS_PER_ROOM and picks:
                    notes.append(f"Only {len(picks)} valid songs were available.")
            if low is None:
                lv = [int(p[1]) for p in picks]
                low, high = (min(lv), max(lv)) if lv else (0, 0)
            elif manual:
                lv = [int(p[1]) for p in picks]
                low, high = min(low, min(lv)), max(high, max(lv))
            if picks:
                await self._verify_catalog(server, picks)
            return Selection(picks=picks, low=int(low), high=int(high), mode="manual" if manual else "auto", notes=notes)

        return selector

    async def _state_to_memory(self, row, fields: dict | None = None):
        """Put the (now active) durable room back in the matchmaking cog's memory."""
        mm = self.mm
        guild = self.bot.get_guild(int(row["guild_id"]))
        state = json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"])
        state["players"] = await self._members_for(guild, state)
        state["left_server_user_ids"] = set(map(int, state.get("left_server_user_ids", [])))
        state["player_snapshots"] = {int(k): v for k, v in state.get("player_snapshots", {}).items()}
        state["songs"] = _tuples(state.get("songs"))
        mm.rooms[int(row["room_id"])] = state
        return state

    def _find_memory(self, thread_id: int):
        for rid, info in self.mm.rooms.items():
            if int(info.get("thread") or 0) == int(thread_id):
                return rid, info
        return None, None

    # ------------------------------------------------------------- command
    @app_commands.command(name="reroll", description="STAFF: recover a failed song selection and let this room keep playing.")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.describe(
        reason="Why the selection failed (kept in the audit log).",
        room_id="Optional room id, when running this outside the room's thread.",
        songs="Optional manual songs: song ids like `123 456:master` (rest filled automatically).",
    )
    async def reroll(
        self,
        interaction: discord.Interaction,
        reason: str | None = None,
        room_id: int | None = None,
        songs: str | None = None,
    ):
        # 1) Authorization strictly by Discord user id.
        if interaction.user.id != AUTHORIZED_USER_ID:
            log.warning("reroll_denied user=%s channel=%s", interaction.user.id, interaction.channel_id)
            return await interaction.response.send_message("⛔ You are not allowed to use this command.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        mm = self.mm
        if mm is None or not mm._runtime_recovered.is_set():
            return await interaction.followup.send("⏳ The bot is still recovering its rooms. Try again in a few seconds.", ephemeral=True)
        try:
            ready = await asyncio.wait_for(self.service.table_ready(), timeout=10)
        except Exception:
            log.exception("reroll_table_check_failed")
            return await interaction.followup.send("⚠️ Database temporarily unavailable; nothing was changed. Retry shortly.", ephemeral=True)
        if not ready:
            return await interaction.followup.send("⚠️ Migration 008 (lounge_room_reroll) is not applied yet; nothing was changed.", ephemeral=True)

        # 2) Which room?
        try:
            thread_id = None
            if room_id is not None:
                for rid, info in mm.rooms.items():
                    if rid == room_id or int(info.get("display_num") or -1) == room_id:
                        thread_id = int(info["thread"])
                        break
                if thread_id is None:
                    thread_id = await self.service.resolve_thread(room_id=room_id)
            else:
                thread_id = await self.service.resolve_thread(thread_id=interaction.channel_id)
        except Exception:
            log.exception("reroll_resolve_failed")
            return await interaction.followup.send("⚠️ Database temporarily unavailable; nothing was changed. Retry shortly.", ephemeral=True)
        if not thread_id:
            return await interaction.followup.send("❌ No recoverable room found here. Run it inside the room thread or pass `room_id`.", ephemeral=True)

        # 3) One operation per room at a time (shares the lock used by song selection).
        lock = mm._room_locks.setdefault(thread_id, asyncio.Lock())
        if lock.locked():
            return await interaction.followup.send("⏳ Another operation (song selection or reroll) is running on this room. Retry in a moment.", ephemeral=True)
        async with lock:
            try:
                msg = await asyncio.wait_for(
                    self._run(interaction, thread_id, reason, songs), timeout=COMMAND_TIMEOUT_SEC
                )
            except RerollError as e:
                log.info("reroll_refused thread=%s code=%s", thread_id, e.code)
                msg = f"❌ {e.message}"
            except asyncio.TimeoutError:
                log.exception("reroll_timeout thread=%s", thread_id)
                msg = "⏱️ Timed out. The room was not lost; if a selection was saved it will be published automatically, otherwise retry /reroll."
            except Exception:
                log.exception("reroll_failed thread=%s", thread_id)
                msg = "⚠️ Unexpected/temporary database error. The room and results are untouched; retry /reroll."
        await interaction.followup.send(msg, ephemeral=True)

    async def _run(self, interaction, thread_id: int, reason: str | None, songs: str | None) -> str:
        mm = self.mm
        rid, info = self._find_memory(thread_id)
        pool = self.bot.db_pool
        live = {}
        if info is not None:
            live = {k: bool(info.get(k)) for k in ("vote_in_progress", "forfeit_in_progress")}
            if info.get("finished") or info.get("closed"):
                raise RerollError("finished", "This room is finished/closed; results are final and untouched.")
            await RoomStore(pool).save(rid, info)  # make the durable row equal to memory before locking it
        async with pool.acquire() as c:
            row = await c.fetchrow("SELECT * FROM lounge_room_state WHERE thread_id=$1", thread_id)
        if row is None:
            raise RerollError("no_room", "No persisted room for this channel.")
        guild = self.bot.get_guild(int(row["guild_id"]))
        payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"])
        members = (info or {}).get("players") if info is not None else None
        members = [m for m in (members or []) if m is not None] or await self._members_for(guild, payload)

        result = await self.service.prepare(
            thread_id=thread_id,
            admin_id=interaction.user.id,
            reason=reason or "song selection failure",
            operation_id=f"interaction:{interaction.id}",
            selector=self._make_selector(members, songs),
            live_flags=live,
        )

        # Durable state is committed. Now mirror it into memory, then publish.
        async with pool.acquire() as c:
            row = await c.fetchrow("SELECT * FROM lounge_room_state WHERE thread_id=$1", thread_id)
        rid, info = self._find_memory(thread_id)
        if info is not None:
            f = dict(result.fields)
            f["songs"] = _tuples(f.get("songs"))
            info.update(f)
        elif not result.replayed:
            await self._state_to_memory(row)
            rid, info = self._find_memory(thread_id)

        published = False
        if info is not None:
            published = await self._publish(rid, info, result)
        lines = [
            f"✅ **Reroll #{result.audit_id} saved** for room {result.room_id}"
            + (" (replayed: same request, no new selection)" if result.replayed else "")
            + f" — {len(result.new)} songs, {result.total_notes} notes, mode `{result.mode}`.",
            "Players, order, host, results and MMR were not touched.",
        ]
        if result.recovered_from_attention:
            lines.append("The room was in `needs_attention` and is active again.")
        lines += [f"ℹ️ {n}" for n in result.notes]
        lines.append("📣 Published in the room thread." if published else "📣 Saved; publication to Discord is pending and will be retried automatically.")
        return "\n".join(lines)

    # ----------------------------------------------------------- publishing
    async def _publish(self, rid, info: dict, result) -> bool:
        """Post (or find) the new selection message. Idempotent per audit id."""
        audit_id = result_id(result)
        if audit_id in self._publish_guard:
            return False
        self._publish_guard.add(audit_id)
        try:
            mm = self.mm
            thread = await mm._get_thread_obj(info)
            if thread is None:
                return False
            marker = MARKER.format(id=audit_id)
            msg = None
            try:
                async for m in thread.history(limit=40):
                    if m.author.id == self.bot.user.id and marker in (m.content or ""):
                        msg = m
                        break
                if msg is None:
                    msg = await thread.send(render_selection_message(result))
                    await msg.pin()
                    # Unpin earlier song messages so players see only the valid one.
                    for pinned in await thread.pins():
                        if pinned.id != msg.id and pinned.author.id == self.bot.user.id and (
                            (pinned.content or "").startswith("Selected songs") or "Song selection recovered" in (pinned.content or "")
                        ):
                            try:
                                await pinned.unpin()
                            except discord.HTTPException:
                                pass
            except discord.HTTPException:
                log.exception("reroll_publish_failed audit=%s", audit_id)
                return False
            info["songs_msg_id"] = msg.id
            await self.service.mark_published(audit_id, msg.id)
            if rid is not None:
                await RoomStore(self.bot.db_pool).save(rid, info)
                try:
                    await mm.update_room_board(thread.guild, rid)
                except Exception:
                    log.exception("reroll_board_refresh_failed")
                self.bot.dispatch("room_updated", rid)
            return True
        finally:
            self._publish_guard.discard(audit_id)

    @tasks.loop(seconds=20)
    async def resume_pending(self):
        """After a restart/outage: publish saved-but-unpublished selections (never re-generate)."""
        mm = self.mm
        if mm is None or not mm._runtime_recovered.is_set() or self.service is None:
            return
        try:
            if not await self.service.table_ready():
                return
            for row in await self.service.pending():
                thread_id = int(row["thread_id"])
                lock = mm._room_locks.setdefault(thread_id, asyncio.Lock())
                if lock.locked():
                    continue
                async with lock:
                    rid, info = self._find_memory(thread_id)
                    if info is None:
                        async with self.bot.db_pool.acquire() as c:
                            full = await c.fetchrow("SELECT * FROM lounge_room_state WHERE thread_id=$1", thread_id)
                        try:
                            await self._state_to_memory(full)
                        except RerollError:
                            continue
                        rid, info = self._find_memory(thread_id)
                    sel = json.loads(row["new_selection"]) if isinstance(row["new_selection"], str) else row["new_selection"]
                    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
                    pend = {
                        "audit_id": row["id"], "songs": [tuple(s) for s in sel],
                        "low": int(payload.get("gap_low") or 0), "high": int(payload.get("gap_high") or 0),
                        "total": sum(int(s[3]) for s in sel),
                    }
                    if await self._publish(rid, info, pend):
                        log.info("reroll_resumed audit=%s thread=%s", row["id"], thread_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("reroll_resume_failed")

    @resume_pending.before_loop
    async def _before_resume(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(RerollCog(bot))
