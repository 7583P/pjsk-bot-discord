"""Command-level /reroll tests: fake Discord objects + REAL PostgreSQL catalog (disposable DB only).

    $env:TEST_REROLL_DATABASE_URL='postgresql://postgres@127.0.0.1:55432/pjsk_test_reroll'
    python -m unittest tests.test_reroll_cog -v
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
    p = urlsplit(URL)
    assert p.hostname in ("127.0.0.1", "localhost") and p.path.strip("/").startswith("pjsk_test_")

if not SKIP:
    import asyncpg
    from cogs.matchmaking import Matchmaking
    from cogs.reroll import RerollCog, MARKER
    from lounge.reroll import AUTHORIZED_USER_ID, RerollService
    from lounge.room_store import RoomStore

if SKIP:
    AUTHORIZED_USER_ID = 878310498720940102

ROOT = Path(__file__).resolve().parents[1]
THREAD = 910_000_002
BOT_ID = 4242
GUILD_ID = 1371306026917298267
PLAYER_IDS = [2001, 2002, 2003, 2004, 2005]


class FakeMsg:
    _next = 10_000

    def __init__(self, thread, content):
        FakeMsg._next += 1
        self.id, self.content, self.thread, self.pinned = FakeMsg._next, content, thread, False
        self.author = SimpleNamespace(id=BOT_ID)

    async def pin(self):
        self.pinned = True

    async def unpin(self):
        self.pinned = False


class FakeThread:
    def __init__(self, guild):
        self.id, self.guild, self.messages = THREAD, guild, []

    async def send(self, content):
        m = FakeMsg(self, content)
        self.messages.append(m)
        return m

    async def history(self, limit=40):
        for m in reversed(self.messages[-limit:]):
            yield m

    async def pins(self):
        return [m for m in self.messages if m.pinned]


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.members = {
            uid: SimpleNamespace(id=uid, bot=False, display_name=f"p{uid}", roles=[SimpleNamespace(name="Diamond")])
            for uid in PLAYER_IDS
        }

    def get_member(self, uid):
        return self.members.get(uid)

    async def fetch_member(self, uid):
        return self.members.get(uid)


if not SKIP:

    class FakeMM(Matchmaking):
        """Real selection/rules code; only Discord I/O is replaced."""

        def __init__(self, pool, thread):
            self.db_pool = pool
            self._songs_lock = asyncio.Lock()
            self._room_locks = {}
            self._runtime_recovered = asyncio.Event()
            self._runtime_recovered.set()
            self.rooms = {}
            self.thread = thread
            self.board_updates = []

        async def _get_thread_obj(self, info):
            return self.thread

        async def update_room_board(self, guild, rid):
            self.board_updates.append(rid)


class FakeBot:
    def __init__(self, pool, mm, guild):
        self.db_pool, self.mm, self.guild = pool, mm, guild
        self.user = SimpleNamespace(id=BOT_ID)
        self.dispatched = []

    def get_cog(self, name):
        return self.mm if name == "Matchmaking" else None

    def get_guild(self, gid):
        return self.guild

    def dispatch(self, *a):
        self.dispatched.append(a)


def interaction(user_id, iid, channel_id=THREAD, admin_perms=False):
    sent = []

    async def send_message(content=None, **kw):
        sent.append(("response", content, kw))

    async def defer(**kw):
        sent.append(("defer", None, kw))

    async def followup(content=None, **kw):
        sent.append(("followup", content, kw))

    return SimpleNamespace(
        id=iid, channel_id=channel_id,
        user=SimpleNamespace(id=user_id, guild_permissions=SimpleNamespace(administrator=admin_perms, manage_guild=admin_perms, manage_channels=admin_perms)),
        response=SimpleNamespace(send_message=send_message, defer=defer),
        followup=SimpleNamespace(send=followup), sent=sent,
    )


@unittest.skipIf(SKIP, "TEST_REROLL_DATABASE_URL not set")
class RerollCogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(URL, min_size=1, max_size=4)
        async with self.pool.acquire() as c:
            await c.execute((ROOT / "migrations" / "008_room_reroll.sql").read_text(encoding="utf-8"))
        await self._cleanup()
        self.guild = FakeGuild()
        self.thread = FakeThread(self.guild)
        self.mm = FakeMM(self.pool, self.thread)
        self.bot = FakeBot(self.pool, self.mm, self.guild)
        self.cog = RerollCog(self.bot)
        self.cog.service = RerollService(self.pool)
        self.fp0 = await self.fingerprint()

    async def asyncTearDown(self):
        if not self.pool._closed:
            await self._cleanup()
            await self.pool.close()

    async def _cleanup(self):
        async with self.pool.acquire() as c:
            for t in ("lounge_room_reroll", "lounge_submissions", "lounge_room_members", "lounge_room_state"):
                await c.execute(f"DELETE FROM {t} WHERE thread_id=$1", THREAD)

    async def fingerprint(self):
        async with self.pool.acquire() as c:
            return await c.fetchval(
                "SELECT md5(string_agg(user_id||':'||season_id||':'||mmr,',' ORDER BY user_id,season_id)) FROM seasonplayers"
            ) , await c.fetchval("SELECT count(*) FROM match_history")

    async def room(self, server="en", status="needs_attention", in_memory=False, **over):
        payload = {
            "season_id": 8, "guild_id": GUILD_ID, "thread": THREAD, "join_channel_id": 1371307353437110282,
            "server": server, "player_ids": list(PLAYER_IDS), "phase": "STARTED", "started": True,
            "finished": False, "closed": False, "selection_in_progress": status == "needs_attention",
            "left_server_user_ids": [], "player_snapshots": {}, "display_num": 55,
        }
        payload.update(over)
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO lounge_room_state(thread_id,room_id,season_id,guild_id,queue_id,payload,status,issue) VALUES($1,902,8,$2,$3,$4::jsonb,$5,$6)",
                THREAD, GUILD_ID, payload["join_channel_id"], json.dumps(payload), status, "interrupted_vote_or_song_delivery" if status == "needs_attention" else None,
            )
            await c.executemany(
                "INSERT INTO lounge_room_members(thread_id,guild_id,user_id,position) VALUES($1,$2,$3,$4)",
                [(THREAD, GUILD_ID, str(u), i) for i, u in enumerate(PLAYER_IDS, 1)],
            )
        if in_memory:
            info = dict(payload)
            info["players"] = [self.guild.members[u] for u in PLAYER_IDS]
            info["left_server_user_ids"] = set()
            self.mm.rooms[902] = info
            return info
        return payload

    async def invoke(self, user_id=AUTHORIZED_USER_ID, iid=1, **kw):
        it = interaction(user_id, iid)
        await RerollCog.reroll.callback(self.cog, it, kw.get("reason"), kw.get("room_id"), kw.get("songs"))
        return it

    def text(self, it):
        return "\n".join(str(c) for _, c, _ in it.sent if c)

    async def db_room(self):
        async with self.pool.acquire() as c:
            r = await c.fetchrow("SELECT * FROM lounge_room_state WHERE thread_id=$1", THREAD)
            a = await c.fetch("SELECT * FROM lounge_room_reroll WHERE thread_id=$1 ORDER BY id", THREAD)
        return r, json.loads(r["payload"]), a

    # ---------------------------------------------------------------- tests
    async def test_non_authorized_users_get_a_private_denial_even_with_admin_permissions(self):
        await self.room(in_memory=True)
        for uid, admin in ((1, False), (AUTHORIZED_USER_ID + 1, True), (123, True)):
            it = interaction(uid, 5, admin_perms=admin)
            await RerollCog.reroll.callback(self.cog, it, None, None, None)
            kind, content, kw = it.sent[0]
            self.assertEqual(kind, "response")
            self.assertTrue(kw.get("ephemeral"))
            self.assertIn("not allowed", content)
        _, _, audits = await self.db_room()
        self.assertEqual(audits, [])
        self.assertEqual(self.thread.messages, [])

    async def _recover(self, server, **extra):
        info = await self.room(server=server, in_memory=True, selection_in_progress=True)
        it = await self.invoke(iid=100 + len(server))
        r, payload, audits = await self.db_room()
        self.assertIn("saved", self.text(it), self.text(it))
        self.assertEqual(r["status"], "active")
        songs = payload["songs"]
        self.assertEqual(len(songs), 5)
        self.assertEqual(len({s[0] for s in songs}), 5)
        # every song exists in THIS server's catalog, within the Diamond range, and is not banned
        banned = self.mm._banned_song_filter_sql("s")
        async with self.pool.acquire() as c:
            ok = await c.fetchval(
                f"""SELECT count(*) FROM unnest($2::text[],$3::text[],$4::int[]) AS x(t,d,l)
                    WHERE EXISTS(SELECT 1 FROM songs s WHERE s.server=$1 AND s.title=x.t AND lower(s.diff)=lower(x.d) AND s.level=x.l {banned})""",
                server, [s[0] for s in songs], [s[2] for s in songs], [s[1] for s in songs],
            )
        self.assertEqual(ok, 5)
        print(server, [(x[1], x[2]) for x in songs], payload['gap_low'], payload['gap_high'])
        self.assertTrue(all(payload['gap_low'] <= s[1] <= payload['gap_high'] for s in songs))
        # Same room object, same players/order; published and pinned
        self.assertIs(self.mm.rooms[902], info)
        self.assertEqual([m.id for m in info["players"]], PLAYER_IDS)
        self.assertEqual(info["total_notes"], sum(s[3] for s in songs))
        (msg,) = self.thread.messages
        self.assertTrue(msg.pinned)
        self.assertIn(MARKER.format(id=audits[0]["id"]), msg.content)
        self.assertEqual(audits[0]["status"], "published")
        self.assertEqual(audits[0]["published_message_id"], msg.id)
        self.assertEqual(await self.fingerprint(), self.fp0)
        return payload

    async def test_recovers_an_english_room(self):
        await self._recover("en")

    async def test_recovers_a_japanese_room(self):
        await self._recover("jp")

    async def test_room_only_in_database_is_restored_into_memory_and_keeps_playing(self):
        await self.room(server="en")  # not in memory: as after a restart
        it = await self.invoke()
        self.assertIn("saved", self.text(it))
        info = self.mm.rooms[902]
        self.assertEqual([m.id for m in info["players"]], PLAYER_IDS)
        self.assertTrue(info["songs_posted"] and info["started"])
        self.assertEqual(len(info["songs"]), 5)
        self.assertIsInstance(info["songs"][0], tuple)

    async def test_publishing_twice_does_not_duplicate_the_message(self):
        info = await self.room(in_memory=True)
        await self.invoke()
        _, _, audits = await self.db_room()
        pend = {"audit_id": audits[0]["id"], "songs": [tuple(s) for s in json.loads(audits[0]["new_selection"])], "low": 24, "high": 28, "total": 1}
        await self.cog._publish(902, info, pend)
        await self.cog._publish(902, info, pend)
        self.assertEqual(len(self.thread.messages), 1)

    async def test_restart_between_save_and_publish_resumes_without_a_new_selection(self):
        info = await self.room(in_memory=True)
        # first process saves the selection, then dies before sending to Discord
        from lounge.reroll import Selection

        async def sel(payload, exclude):
            return Selection(picks=[("Resume Song A", 26, "Master", 1111), ("Resume Song B", 27, "Expert", 999)], low=24, high=28)

        res = await self.cog.service.prepare(thread_id=THREAD, admin_id=AUTHORIZED_USER_ID, reason="crash test", operation_id="op-crash", selector=sel)
        self.assertEqual(self.thread.messages, [])
        # new process: fresh cog, memory empty (room restored from DB)
        self.mm.rooms.clear()
        cog2 = RerollCog(self.bot)
        cog2.service = RerollService(self.pool)
        await RerollCog.resume_pending.coro(cog2)
        _, payload, audits = await self.db_room()
        self.assertEqual(len(audits), 1)  # no second selection
        self.assertEqual(audits[0]["status"], "published")
        (msg,) = self.thread.messages
        self.assertIn("Resume Song A", msg.content)
        self.assertIn(MARKER.format(id=res.audit_id), msg.content)
        self.assertEqual(payload["songs"][0][0], "Resume Song A")
        self.assertIn(902, self.mm.rooms)
        # a second loop iteration is a no-op
        await RerollCog.resume_pending.coro(cog2)
        self.assertEqual(len(self.thread.messages), 1)

    async def test_concurrent_commands_never_corrupt_the_room(self):
        await self.room(in_memory=True)
        a, b = await asyncio.gather(self.invoke(iid=201), self.invoke(iid=202))
        texts = [self.text(a), self.text(b)]
        _, payload, audits = await self.db_room()
        ok = sum("saved" in t for t in texts)
        self.assertGreaterEqual(ok, 1)
        self.assertEqual(len(audits), ok)
        self.assertEqual(payload["reroll_count"], ok)
        self.assertEqual(len(payload["songs"]), 5)
        self.assertEqual(len(self.thread.messages), ok)
        self.assertEqual([m.id for m in self.mm.rooms[902]["players"]], PLAYER_IDS)

    async def test_no_valid_songs_keeps_room_and_can_retry(self):
        await self.room(in_memory=True)
        real = self.mm._select_room_songs

        async def none(*a, **k):
            return [], 24, 28

        self.mm._select_room_songs = none
        it = await self.invoke(iid=301)
        self.assertIn("No valid songs", self.text(it))
        r, payload, audits = await self.db_room()
        self.assertEqual((r["status"], audits), ("needs_attention", []))
        self.assertTrue(set(PLAYER_IDS) == {int(x) for x in payload["player_ids"]})
        self.mm._select_room_songs = real
        it = await self.invoke(iid=302)
        self.assertIn("saved", self.text(it))

    async def test_database_outage_leaves_the_room_untouched(self):
        await self.room(in_memory=True)
        broken = await asyncpg.create_pool(URL, min_size=1, max_size=1)
        await broken.close()
        self.bot.db_pool = broken
        self.cog.service = RerollService(broken)
        it = await self.invoke(iid=401)
        self.assertRegex(self.text(it), "temporarily unavailable|Unexpected")
        self.bot.db_pool = self.pool
        self.cog.service = RerollService(self.pool)
        r, _, audits = await self.db_room()
        self.assertEqual((r["status"], audits), ("needs_attention", []))

    async def test_manual_song_by_id_and_validation(self):
        await self.room(in_memory=True)
        async with self.pool.acquire() as c:
            s = await c.fetchrow("SELECT id,title,diff FROM songs WHERE server='en' AND level BETWEEN 24 AND 28 AND diff='master' LIMIT 1")
        it = await self.invoke(iid=501, songs=f"{s['id']}:master", reason="manual fix")
        self.assertIn("manual", self.text(it))
        _, payload, audits = await self.db_room()
        self.assertEqual(payload["songs"][0][0], s["title"])
        self.assertEqual(len(payload["songs"]), 5)
        self.assertEqual(audits[0]["mode"], "manual")
        self.assertEqual(audits[0]["reason"], "manual fix")
        # invalid id: refused, nothing more is written
        it = await self.invoke(iid=502, songs="999999999")
        self.assertIn("does not exist", self.text(it))
        _, _, audits = await self.db_room()
        self.assertEqual(len(audits), 1)

    async def test_result_already_recorded_is_refused_at_command_level(self):
        await self.room(in_memory=True, selection_in_progress=False, status="active")
        async with self.pool.acquire() as c:
            await c.execute("INSERT INTO lounge_submissions(thread_id,result_id,season_id,payload) VALUES($1,987654322,8,'{}'::jsonb)", THREAD)
        it = await self.invoke(iid=601)
        self.assertIn("definitive result", self.text(it))
        _, _, audits = await self.db_room()
        self.assertEqual(audits, [])
        self.assertEqual(await self.fingerprint(), self.fp0)

    async def test_live_vote_blocks_command(self):
        info = await self.room(in_memory=True, selection_in_progress=False, status="active")
        info["vote_in_progress"] = True
        it = await self.invoke(iid=701)
        self.assertIn("vote", self.text(it).lower())
        _, _, audits = await self.db_room()
        self.assertEqual(audits, [])


if __name__ == "__main__":
    unittest.main()
