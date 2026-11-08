"""Integration tests for /reroll against a DISPOSABLE PostgreSQL (never production).

    $env:TEST_REROLL_DATABASE_URL='postgresql://postgres@127.0.0.1:55432/pjsk_test_reroll'
    python -m unittest tests.test_reroll -v

The database must be a copy of the modern schema (migrations 001-007 applied)
with seasons/songs data. The URL host must be local and the db name must start
with pjsk_test_. Migration 008 is applied here (idempotent).
"""

import asyncio
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

os.environ.setdefault("GUILD_ID", "1371306026917298267")

URL = os.getenv("TEST_REROLL_DATABASE_URL")
SKIP = not URL
if URL:
    parts = urlsplit(URL)
    assert parts.hostname in ("127.0.0.1", "localhost"), "tests only run against a local database"
    assert parts.path.strip("/").startswith("pjsk_test_"), "database name must start with pjsk_test_"

if not SKIP:
    import asyncpg
    from lounge.reroll import AUTHORIZED_USER_ID, RerollError, RerollService, Selection
    from lounge.room_store import RoomStore

if SKIP:
    AUTHORIZED_USER_ID = 878310498720940102

ROOT = Path(__file__).resolve().parents[1]
THREAD = 910_000_001
PLAYERS = [1001, 1002, 1003, 1004, 1005]


def song(i, level=26, diff="Master", notes=1000):
    return (f"Song {i}", level, diff, notes + i)


def room_payload(**over):
    p = {
        "season_id": 8, "guild_id": 1371306026917298267, "thread": THREAD, "join_channel_id": 1371307353437110282,
        "server": "en", "player_ids": list(PLAYERS), "phase": "STARTED", "started": True, "finished": False,
        "closed": False, "host": 1001, "left_server_user_ids": [], "player_snapshots": {},
        "selection_in_progress": True, "vote_in_progress": False, "forfeit_in_progress": False,
        "display_num": 77, "board_msg_id": 555, "created_ts": 1700000000,
    }
    p.update(over)
    return p


@unittest.skipIf(SKIP, "TEST_REROLL_DATABASE_URL not set")
class RerollServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(URL, min_size=1, max_size=4)
        async with self.pool.acquire() as c:
            await c.execute((ROOT / "migrations" / "008_room_reroll.sql").read_text(encoding="utf-8"))
            await c.execute("DELETE FROM lounge_room_reroll WHERE thread_id=$1", THREAD)
            await c.execute("DELETE FROM lounge_submissions WHERE thread_id=$1", THREAD)
            await c.execute("DELETE FROM lounge_room_members WHERE thread_id=$1", THREAD)
            await c.execute("DELETE FROM lounge_room_state WHERE thread_id=$1", THREAD)
        self.svc = RerollService(self.pool)
        self.calls = []

    async def asyncTearDown(self):
        async with self.pool.acquire() as c:
            await c.execute("DELETE FROM lounge_room_reroll WHERE thread_id=$1", THREAD)
            await c.execute("DELETE FROM lounge_submissions WHERE thread_id=$1", THREAD)
            await c.execute("DELETE FROM lounge_room_members WHERE thread_id=$1", THREAD)
            await c.execute("DELETE FROM lounge_room_state WHERE thread_id=$1", THREAD)
        await self.pool.close()

    # -- helpers
    async def make_room(self, status="needs_attention", **over):
        payload = room_payload(**over)
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO lounge_room_state(thread_id,room_id,season_id,guild_id,queue_id,payload,status,issue) "
                "VALUES($1,$2,8,$3,$4,$5::jsonb,$6,$7)",
                THREAD, 901, payload["guild_id"], payload["join_channel_id"], json.dumps(payload), status,
                "interrupted_vote_or_song_delivery" if status == "needs_attention" else None,
            )
            await c.executemany(
                "INSERT INTO lounge_room_members(thread_id,guild_id,user_id,position) VALUES($1,$2,$3,$4)",
                [(THREAD, payload["guild_id"], str(u), i) for i, u in enumerate(PLAYERS, 1)],
            )
        return payload

    async def row(self):
        async with self.pool.acquire() as c:
            r = await c.fetchrow("SELECT * FROM lounge_room_state WHERE thread_id=$1", THREAD)
        return r, json.loads(r["payload"])

    async def audits(self):
        async with self.pool.acquire() as c:
            return await c.fetch("SELECT * FROM lounge_room_reroll WHERE thread_id=$1 ORDER BY id", THREAD)

    async def competitive_fingerprint(self):
        async with self.pool.acquire() as c:
            return await c.fetchval(
                """SELECT md5(string_agg(x,',' ORDER BY x)) FROM (
                     SELECT 'sp'||user_id||':'||season_id||':'||mmr AS x FROM seasonplayers
                     UNION ALL SELECT 'mh'||id||':'||mmr_after FROM match_history
                     UNION ALL SELECT 'r'||result_id::text FROM lounge_submissions WHERE thread_id<>$1) t""",
                THREAD,
            )

    def selector(self, first=1, count=5, mode="auto", fail=None):
        async def sel(payload, exclude):
            self.calls.append(list(exclude))
            if fail:
                raise fail
            picks = [song(i) for i in range(first, first + count)]
            return Selection(picks=picks, low=24, high=28, mode=mode)

        return sel

    async def prepare(self, op="op-1", sel=None, admin=AUTHORIZED_USER_ID, **kw):
        return await self.svc.prepare(
            thread_id=THREAD, admin_id=admin, reason="bad song", operation_id=op, selector=sel or self.selector(), **kw
        )

    # -- authorization
    async def test_only_the_authorized_id_can_reroll(self):
        await self.make_room()
        for who in (1, 123456789012345678, AUTHORIZED_USER_ID + 1):
            with self.assertRaises(RerollError) as cm:
                await self.prepare(admin=who)
            self.assertEqual(cm.exception.code, "forbidden")
        self.assertEqual(await self.audits(), [])
        self.assertEqual((await self.row())[0]["status"], "needs_attention")

    # -- recovery keeps identity, replaces only the selection
    async def test_recovers_interrupted_room_and_preserves_everything_else(self):
        before_payload = await self.make_room()
        fp = await self.competitive_fingerprint()
        res = await self.prepare()
        r, after = await self.row()
        self.assertEqual(r["status"], "active")
        self.assertIsNone(r["issue"])
        self.assertEqual((r["room_id"], r["thread_id"], r["season_id"]), (901, THREAD, 8))
        changed = {"songs", "total_notes", "gap_low", "gap_high", "songs_posted", "rerolling", "selection_in_progress",
                   "reroll_failed_titles", "reroll_count", "last_reroll_id", "needs_attention", "vote_in_progress",
                   "forfeit_in_progress"}
        for k, v in before_payload.items():
            if k not in changed:
                self.assertEqual(after[k], v, f"{k} must be preserved")
        self.assertEqual(after["player_ids"], PLAYERS)  # members and their order
        self.assertEqual(after["board_msg_id"], 555)
        self.assertEqual(after["phase"], "STARTED")
        self.assertTrue(after["songs_posted"])
        self.assertFalse(after["selection_in_progress"])
        self.assertEqual(len(after["songs"]), 5)
        self.assertEqual(after["total_notes"], sum(s[3] for s in after["songs"]))
        self.assertEqual(res.status, "selected")
        self.assertTrue(res.recovered_from_attention)
        # reservations of the players are still in place
        async with self.pool.acquire() as c:
            self.assertEqual(await c.fetchval("SELECT count(*) FROM lounge_room_members WHERE thread_id=$1", THREAD), 5)
        # audit
        (a,) = await self.audits()
        self.assertEqual((a["admin_user_id"], a["reason"], a["status"], a["mode"]), (AUTHORIZED_USER_ID, "bad song", "selected", "auto"))
        self.assertEqual(json.loads(a["new_selection"]), [list(song(i)) for i in range(1, 6)])
        # no competitive data touched
        self.assertEqual(await self.competitive_fingerprint(), fp)

    async def test_previous_selection_is_audited_and_not_reselected(self):
        await self.make_room(status="active", selection_in_progress=False, songs=[list(song(i)) for i in range(1, 6)],
                             songs_posted=True, total_notes=5010)
        res = await self.prepare(sel=self.selector(first=6))
        self.assertEqual([s[0] for s in res.previous], [f"Song {i}" for i in range(1, 6)])
        self.assertEqual(set(self.calls[0]), {f"Song {i}" for i in range(1, 6)})  # excluded from the next pick
        (a,) = await self.audits()
        self.assertEqual(json.loads(a["previous_selection"])[0][0], "Song 1")
        self.assertEqual((await self.row())[1]["reroll_failed_titles"], [f"Song {i}" for i in range(1, 6)])

    # -- idempotency / repetition
    async def test_replay_of_same_operation_does_not_generate_a_second_selection(self):
        await self.make_room()
        first = await self.prepare(op="interaction:42")
        again = await self.prepare(op="interaction:42", sel=self.selector(first=50))
        self.assertTrue(again.replayed)
        self.assertEqual(again.audit_id, first.audit_id)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(await self.audits()), 1)
        self.assertEqual((await self.row())[1]["songs"][0][0], "Song 1")

    async def test_sequential_rerolls_accumulate_failed_titles(self):
        await self.make_room()
        await self.prepare(op="a", sel=self.selector(first=1))
        await self.prepare(op="b", sel=self.selector(first=6))
        _, p = await self.row()
        self.assertEqual(p["reroll_count"], 2)
        self.assertEqual(len(p["reroll_failed_titles"]), 5)  # titles of the previous selection(s)
        self.assertEqual(set(self.calls[1]), {f"Song {i}" for i in range(1, 6)})
        self.assertEqual(len(await self.audits()), 2)

    async def test_two_simultaneous_rerolls_are_serialized_and_consistent(self):
        await self.make_room()

        async def slow(payload, exclude):
            self.calls.append(list(exclude))
            await asyncio.sleep(0.2)
            base = 1 if not exclude else 10
            return Selection(picks=[song(i) for i in range(base, base + 5)], low=24, high=28)

        results = await asyncio.gather(
            self.prepare(op="x1", sel=slow), self.prepare(op="x2", sel=slow), return_exceptions=True
        )
        self.assertTrue(all(not isinstance(r, Exception) for r in results), results)
        _, p = await self.row()
        audits = await self.audits()
        self.assertEqual(len(audits), 2)
        self.assertEqual(p["reroll_count"], 2)
        self.assertEqual(p["last_reroll_id"], audits[-1]["id"])
        # the second one saw the first one's songs and avoided them
        self.assertTrue(any(self.calls[i] for i in range(2)))
        self.assertEqual([s[0] for s in p["songs"]], [s[0] for s in json.loads(audits[-1]["new_selection"])])

    # -- refusal paths: the room must be left exactly as it was
    async def assert_untouched(self, expected_status, before):
        r, p = await self.row()
        self.assertEqual(r["status"], expected_status)
        self.assertEqual(p, before)
        self.assertEqual(await self.audits(), [])

    async def test_no_valid_songs_keeps_the_room_and_allows_retry(self):
        before = await self.make_room()
        with self.assertRaises(RerollError) as cm:
            await self.prepare(sel=self.selector(count=0))
        self.assertEqual(cm.exception.code, "no_songs")
        await self.assert_untouched("needs_attention", before)
        res = await self.prepare(op="retry")  # retry works
        self.assertEqual(res.status, "selected")

    async def test_selector_or_database_error_rolls_back_everything(self):
        before = await self.make_room()
        with self.assertRaises(ConnectionError):
            await self.prepare(sel=self.selector(fail=ConnectionError("supabase down")))
        await self.assert_untouched("needs_attention", before)

    async def test_unreachable_database_changes_nothing(self):
        before = await self.make_room()
        broken = RerollService(await asyncpg.create_pool(URL, min_size=1, max_size=1))
        await broken.pool.close()
        with self.assertRaises(Exception):
            await broken.prepare(thread_id=THREAD, admin_id=AUTHORIZED_USER_ID, reason="x", operation_id="o", selector=self.selector())
        await self.assert_untouched("needs_attention", before)

    async def test_result_already_recorded_is_never_touched(self):
        before = await self.make_room(status="active", selection_in_progress=False)
        fp = await self.competitive_fingerprint()
        async with self.pool.acquire() as c:
            rid = 987_654_321
            await c.execute(
                "INSERT INTO lounge_submissions(thread_id,result_id,season_id,payload) VALUES($1,$2,8,'{}'::jsonb)", THREAD, rid
            )
        with self.assertRaises(RerollError) as cm:
            await self.prepare()
        self.assertEqual(cm.exception.code, "result_recorded")
        r, p = await self.row()
        self.assertEqual(p, before)
        self.assertEqual(await self.audits(), [])
        async with self.pool.acquire() as c:
            self.assertEqual(await c.fetchval("SELECT count(*) FROM lounge_submissions WHERE thread_id=$1", THREAD), 1)
        self.assertNotEqual(await self.competitive_fingerprint(), None)
        del fp

    async def test_finished_and_closed_rooms_are_refused(self):
        for status in ("finished", "closed"):
            await self.make_room(status=status)
            with self.assertRaises(RerollError) as cm:
                await self.prepare(op=f"op-{status}")
            self.assertEqual(cm.exception.code, "finished")
            async with self.pool.acquire() as c:
                await c.execute("DELETE FROM lounge_room_members WHERE thread_id=$1", THREAD)
                await c.execute("DELETE FROM lounge_room_state WHERE thread_id=$1", THREAD)

    async def test_live_vote_blocks_reroll_but_dead_vote_after_crash_is_cleared(self):
        before = await self.make_room(status="active", vote_in_progress=True, selection_in_progress=False)
        with self.assertRaises(RerollError) as cm:
            await self.prepare()
        self.assertEqual(cm.exception.code, "vote_live")
        await self.assert_untouched("active", before)
        # in-memory flag also blocks even if the durable copy is stale
        async with self.pool.acquire() as c:
            await c.execute("UPDATE lounge_room_state SET payload=jsonb_set(payload,'{vote_in_progress}','false')")
        with self.assertRaises(RerollError) as cm:
            await self.prepare(live_flags={"vote_in_progress": True})
        self.assertEqual(cm.exception.code, "vote_live")
        # crash case: needs_attention + stale vote flag -> recoverable, flag cleared
        async with self.pool.acquire() as c:
            await c.execute(
                "UPDATE lounge_room_state SET status='needs_attention',payload=jsonb_set(payload,'{vote_in_progress}','true') WHERE thread_id=$1",
                THREAD,
            )
        await self.prepare(op="after-crash")
        self.assertFalse((await self.row())[1]["vote_in_progress"])

    async def test_room_that_never_reached_its_slot_is_not_rerolled(self):
        await self.make_room(status="active", phase="FUTURE", started=False, selection_in_progress=False)
        with self.assertRaises(RerollError) as cm:
            await self.prepare()
        self.assertEqual(cm.exception.code, "not_started")

    # -- restart recovery
    async def test_saved_selection_is_published_after_restart_without_regenerating(self):
        await self.make_room()
        res = await self.prepare()
        # "process dies" before publishing; a new process builds a new service
        svc2 = RerollService(self.pool)
        pend = await svc2.pending()
        self.assertEqual([p["id"] for p in pend], [res.audit_id])
        self.assertEqual(json.loads(pend[0]["new_selection"]), [list(song(i)) for i in range(1, 6)])
        await svc2.mark_published(res.audit_id, 123)
        self.assertEqual(await svc2.pending(), [])
        self.assertEqual((await self.audits())[0]["published_message_id"], 123)
        self.assertEqual(len(await self.audits()), 1)  # nothing regenerated

    async def test_stale_in_memory_snapshot_cannot_overwrite_a_newer_selection(self):
        await self.make_room(status="active", selection_in_progress=False)
        stale = dict(room_payload(selection_in_progress=False))
        await self.prepare()
        _, newer = await self.row()
        members = [SimpleNamespace(id=u) for u in PLAYERS]
        room = {k: v for k, v in stale.items() if k != "player_ids"}
        room.update(players=members, thread=THREAD, songs=[song(99)], reroll_count=0)
        await RoomStore(self.pool).save(901, room)  # what the 15 s reconcile loop would do with old memory
        _, now = await self.row()
        self.assertEqual(now["songs"], newer["songs"])
        self.assertEqual(now["reroll_count"], 1)


if __name__ == "__main__":
    unittest.main()
