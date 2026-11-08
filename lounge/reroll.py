"""Administrative song-selection recovery (``/reroll``): durable, idempotent, audited.

The room keeps its identity (thread, room id, players, order, host, season,
EN/JP server, phase, started/…); only the *current song selection* is replaced.
Nothing here touches MMR, results or ``lounge_submissions``.

Persistence model
-----------------
``prepare`` runs ONE transaction that holds ``FOR UPDATE`` on the room row and:
  1. refuses finished/closed rooms and rooms that already have a recorded result,
  2. obtains a new selection (never re-picking titles that already failed),
  3. rewrites the room payload and inserts the audit row ``status='selected'``.
Publishing to Discord happens afterwards; the audit row flips to ``published``
only once a message exists. A crash in between leaves ``status='selected'``,
which ``pending()`` returns so the new selection is re-published (never
re-generated) after a restart.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

AUTHORIZED_USER_ID = 878310498720940102

# Phases/flags that mean "a result vote or forfeit is live right now".
_LIVE_FLAGS = ("vote_in_progress", "forfeit_in_progress")


class RerollError(Exception):
    """Business-rule refusal. ``code`` is stable for tests; ``message`` is shown to the admin."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Selection:
    picks: list  # [(title, level, Diff, notes), ...]
    low: int
    high: int
    mode: str = "auto"  # auto | manual
    notes: list[str] = field(default_factory=list)


Selector = Callable[[dict, list], Awaitable[Selection]]


@dataclass
class RerollResult:
    audit_id: int
    thread_id: int
    room_id: int
    previous: list | None
    new: list
    low: int
    high: int
    total_notes: int
    mode: str
    replayed: bool
    status: str
    fields: dict
    notes: list[str] = field(default_factory=list)
    recovered_from_attention: bool = False


def _norm_song(song) -> list:
    title, level, diff, notes = song[0], song[1], song[2], song[3]
    return [str(title), int(level), str(diff), int(notes or 0)]


def build_fields(payload: dict, picks: list, low: int, high: int, audit_id: int, previous: list | None, *, clear_dead_flags: bool) -> dict:
    """The only payload keys a reroll is allowed to change."""
    failed = list(payload.get("reroll_failed_titles") or [])
    for song in previous or []:
        if song and song[0] not in failed:
            failed.append(song[0])
    fields = {
        "songs": [list(p) for p in picks],
        "total_notes": sum(int(p[3] or 0) for p in picks),
        "gap_low": int(low),
        "gap_high": int(high),
        "songs_posted": True,
        "rerolling": False,
        "selection_in_progress": False,
        "reroll_failed_titles": failed,
        "reroll_count": int(payload.get("reroll_count") or 0) + 1,
        "last_reroll_id": int(audit_id),
    }
    if clear_dead_flags:
        # After a crash the interrupted vote/forfeit coroutine no longer exists.
        for flag in _LIVE_FLAGS:
            if payload.get(flag):
                fields[flag] = False
        fields["needs_attention"] = False
    return fields


class RerollService:
    def __init__(self, pool):
        self.pool = pool

    async def table_ready(self) -> bool:
        async with self.pool.acquire() as c:
            return bool(await c.fetchval("SELECT to_regclass('public.lounge_room_reroll')"))

    async def resolve_thread(self, *, thread_id: int | None = None, room_id: int | None = None) -> int | None:
        """Map a room id (or display number) to its thread id from durable state."""
        async with self.pool.acquire() as c:
            if thread_id:
                return await c.fetchval("SELECT thread_id FROM lounge_room_state WHERE thread_id=$1", int(thread_id))
            if room_id is not None:
                row = await c.fetchrow(
                    """SELECT thread_id FROM lounge_room_state
                       WHERE status IN ('active','needs_attention') AND (room_id=$1 OR (payload->>'display_num')::int=$1)
                       ORDER BY updated_at DESC LIMIT 1""",
                    int(room_id),
                )
                return row["thread_id"] if row else None
        return None

    async def prepare(
        self,
        *,
        thread_id: int,
        admin_id: int,
        reason: str,
        operation_id: str,
        selector: Selector,
        live_flags: dict | None = None,
    ) -> RerollResult:
        if int(admin_id) != AUTHORIZED_USER_ID:  # defence in depth; the command checks too
            raise RerollError("forbidden", "Not authorized.")
        reason = (reason or "song selection failure").strip()[:500]
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                done = await conn.fetchrow("SELECT * FROM lounge_room_reroll WHERE operation_id=$1", operation_id)
                if done:  # same interaction delivered twice -> same outcome, no new selection
                    return await self._replay(conn, done)

                row = await conn.fetchrow(
                    "SELECT * FROM lounge_room_state WHERE thread_id=$1 FOR UPDATE", int(thread_id)
                )
                if row is None:
                    raise RerollError("no_room", "No persisted room exists for this channel.")
                if row["status"] in ("finished", "closed"):
                    raise RerollError("finished", "This room is already finished/closed; its results are final and stay untouched.")
                payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"])
                if payload.get("finished") or payload.get("closed"):
                    raise RerollError("finished", "This room is already finished/closed; its results are final and stay untouched.")
                if await conn.fetchval("SELECT 1 FROM lounge_submissions WHERE thread_id=$1", int(thread_id)):
                    raise RerollError("result_recorded", "A definitive result is already recorded for this room; nothing was changed.")
                current = await conn.fetchval(
                    "SELECT id FROM seasons WHERE active=true ORDER BY startdate DESC LIMIT 1"
                )
                if current is None or int(current) != int(row["season_id"]):
                    raise RerollError("closed_season", "This room belongs to a season that is no longer active.")

                attention = row["status"] == "needs_attention"
                flags = {**{k: bool(payload.get(k)) for k in _LIVE_FLAGS}, **(live_flags or {})}
                if not attention and any(flags.values()):
                    raise RerollError(
                        "vote_live",
                        "A result vote/forfeit is in progress in this room. Let it finish (or time out), then retry /reroll.",
                    )
                if payload.get("phase") == "FUTURE" and not payload.get("started") and not attention:
                    raise RerollError("not_started", "The room has not reached its start slot; there is no failed selection to recover yet.")
                if not payload.get("player_ids"):
                    raise RerollError("no_players", "The persisted room has no players; refusing to guess.")

                previous = [_norm_song(s) for s in payload.get("songs") or []] or None
                failed = list(payload.get("reroll_failed_titles") or [])
                exclude = failed + [s[0] for s in previous or [] if s[0] not in failed]

                selection = await selector(payload, exclude)
                picks = [_norm_song(p) for p in selection.picks]
                if not picks:
                    raise RerollError(
                        "no_songs",
                        "No valid songs are available for this room's server/difficulty range. The room was kept as is; fix the catalog or ban list and run /reroll again.",
                    )
                if len({p[0].casefold() for p in picks}) != len(picks):
                    raise RerollError("duplicate_songs", "Selection contained duplicate songs; nothing was changed.")

                audit_id = await conn.fetchval(
                    """INSERT INTO lounge_room_reroll(operation_id,thread_id,room_id,season_id,admin_user_id,reason,mode,
                                                      previous_selection,new_selection,status)
                       VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,'selected') RETURNING id""",
                    operation_id,
                    int(thread_id),
                    int(row["room_id"]),
                    int(row["season_id"]),
                    int(admin_id),
                    reason,
                    selection.mode,
                    json.dumps(previous) if previous is not None else None,
                    json.dumps(picks),
                )
                fields = build_fields(payload, picks, selection.low, selection.high, audit_id, previous, clear_dead_flags=attention)
                payload.update(fields)
                await conn.execute(
                    "UPDATE lounge_room_state SET payload=$2::jsonb,status='active',issue=NULL,updated_at=now() WHERE thread_id=$1",
                    int(thread_id),
                    json.dumps(payload, ensure_ascii=False),
                )
                log.info(
                    "reroll_selected audit=%s room=%s thread=%s admin=%s mode=%s songs=%d",
                    audit_id, row["room_id"], thread_id, admin_id, selection.mode, len(picks),
                )
                return RerollResult(
                    audit_id=audit_id, thread_id=int(thread_id), room_id=int(row["room_id"]), previous=previous,
                    new=picks, low=int(selection.low), high=int(selection.high), total_notes=fields["total_notes"],
                    mode=selection.mode, replayed=False, status="selected", fields=fields, notes=list(selection.notes),
                    recovered_from_attention=attention,
                )

    async def _replay(self, conn, done) -> RerollResult:
        new = json.loads(done["new_selection"]) if isinstance(done["new_selection"], str) else done["new_selection"]
        prev = done["previous_selection"]
        prev = json.loads(prev) if isinstance(prev, str) else prev
        state = await conn.fetchrow("SELECT payload FROM lounge_room_state WHERE thread_id=$1", done["thread_id"])
        payload = json.loads(state["payload"]) if state and isinstance(state["payload"], str) else (dict(state["payload"]) if state else {})
        return RerollResult(
            audit_id=done["id"], thread_id=done["thread_id"], room_id=done["room_id"], previous=prev, new=new,
            low=int(payload.get("gap_low") or 0), high=int(payload.get("gap_high") or 0),
            total_notes=sum(int(p[3]) for p in new), mode=done["mode"], replayed=True, status=done["status"], fields={},
        )

    async def mark_published(self, audit_id: int, message_id: int | None) -> None:
        async with self.pool.acquire() as c:
            await c.execute(
                "UPDATE lounge_room_reroll SET status='published',published_message_id=$2,published_at=now() WHERE id=$1 AND status='selected'",
                int(audit_id),
                int(message_id) if message_id else None,
            )

    async def pending(self):
        """Selections saved but not yet visible in Discord (resume after crash/outage)."""
        async with self.pool.acquire() as c:
            return await c.fetch(
                """SELECT r.id,r.thread_id,r.room_id,r.new_selection,r.created_at,s.payload,s.status AS room_status
                   FROM lounge_room_reroll r JOIN lounge_room_state s USING(thread_id)
                   WHERE r.status='selected' AND s.status IN ('active','needs_attention')
                     AND r.id=(SELECT max(id) FROM lounge_room_reroll x WHERE x.thread_id=r.thread_id)
                   ORDER BY r.id"""
            )
