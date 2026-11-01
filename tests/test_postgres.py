import asyncio
from pathlib import Path
import os
import tempfile
from types import SimpleNamespace
import random
import unittest
from unittest.mock import AsyncMock
from urllib.parse import urlsplit, urlunsplit
import uuid
import asyncpg
import httpx
from lounge.config import Settings
from lounge.database import Database
from lounge.repository import Repository
from scripts.migrate import migrate
from scripts.activate_season_5 import activate
from app import create_app
from scripts.backup_postgres import backup
from cogs.autoroles import AutoRoles
import discord

ADMIN = os.getenv("TEST_DATABASE_ADMIN_URL", "")


@unittest.skipUnless(
    ADMIN, "Set TEST_DATABASE_ADMIN_URL to an isolated local PostgreSQL server"
)
class PostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        p = urlsplit(ADMIN)
        if p.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Integration tests refuse non-local databases")
        self.name = "pjsk_test_" + uuid.uuid4().hex
        self.admin = await asyncpg.connect(ADMIN, ssl=False)
        await self.admin.execute(f"CREATE DATABASE {self.name}")
        self.url = urlunsplit(p._replace(path="/" + self.name))
        self.settings = Settings(self.url, ssl=False, guild_id=1)
        await migrate(self.settings)
        # Repeat migration must be harmless.
        await migrate(self.settings)
        self.db = Database(self.settings)
        await self.db.open()
        self.repo = Repository(self.db)
        async with self.db.pool.acquire() as c:
            await c.execute(
                "INSERT INTO seasons(id,name,status) VALUES('beta','beta','active')"
            )
        await self.db.claim()

    async def asyncTearDown(self):
        try:
            await self.db.release()
        finally:
            await self.db.close()
            await self.admin.execute(f"DROP DATABASE {self.name} WITH (FORCE)")
            await self.admin.close()

    async def filled_room(self, queue="10"):
        for i in range(1, 6):
            await self.repo.join(1, queue, i, f"Player {i}")
        room = (await self.repo.rooms())[0]
        await self.repo.attach_thread(room["id"], 100)
        return room

    def block(self):
        return "\n".join(f"<@{i}> [PE] {1000 - i},0,0,0,0" for i in range(1, 6))

    async def test_50_concurrent_joins_and_leaves(self):
        await asyncio.gather(
            *(self.repo.join(1, "10", i, f"P{i}") for i in range(1, 51))
        )
        rooms = await self.repo.rooms()
        self.assertEqual(len(rooms), 10)
        self.assertTrue(all(len(r["players"]) == 5 for r in rooms))
        self.assertEqual(len({u for r in rooms for u in r["players"]}), 50)
        await asyncio.gather(*(self.repo.leave(1, i) for i in range(1, 51)))
        self.assertEqual(await self.repo.rooms(), [])

    async def test_10_duplicate_joins(self):
        results = await asyncio.gather(
            *(self.repo.join(1, "10", 1, "P1") for _ in range(10)),
            return_exceptions=True,
        )
        self.assertEqual(sum(not isinstance(r, Exception) for r in results), 1)
        self.assertEqual((await self.repo.rooms())[0]["players"], [1])

    async def test_queues_separate_and_user_unique(self):
        await self.repo.join(1, "10", 1, "P1")
        await self.repo.join(1, "20", 2, "P2")
        rooms = await self.repo.rooms()
        self.assertEqual(len(rooms), 2)
        self.assertEqual([r["display_number"] for r in rooms], [1, 1])
        with self.assertRaises(ValueError):
            await self.repo.join(1, "20", 1, "P1")

    async def test_capacity_constraint_and_rollback(self):
        room = await self.filled_room()
        await self.repo.register(6, "P6")
        async with self.db.pool.acquire() as c:
            with self.assertRaises(asyncpg.RaiseError):
                await c.execute(
                    "INSERT INTO room_members VALUES($1,1,6,now())", room["id"]
                )
            self.assertEqual(await c.fetchval("SELECT count(*) FROM room_members"), 5)

    async def test_submit_duplicate_atomic(self):
        await self.filled_room()
        results = await asyncio.gather(
            *(
                self.repo.submit(100, 1000 + i, self.block(), 1, False)
                for i in range(10)
            )
        )
        self.assertEqual(sum(created for _, created in results), 1)
        async with self.db.pool.acquire() as c:
            self.assertEqual(await c.fetchval("SELECT count(*) FROM matches"), 1)
            self.assertEqual(
                await c.fetchval("SELECT count(*) FROM match_participants"), 5
            )
            self.assertEqual(
                await c.fetchval("SELECT sum(matches_played) FROM season_players"), 5
            )
            self.assertEqual(
                await c.fetchval("SELECT sum(wins) FROM season_players"), 1
            )
            self.assertEqual(await c.fetchval("SELECT count(*) FROM role_jobs"), 5)
        again, created = await self.repo.submit(100, 2000, self.block(), 1, False)
        self.assertFalse(created)
        self.assertEqual(again, results[0][0])

    async def test_invalid_or_unauthorized_submit_no_writes(self):
        await self.filled_room()
        for block, author in [
            (self.block(), 99),
            (self.block().replace("<@5>", "<@6>"), 1),
            (self.block().replace("<@5>", "<@1>"), 1),
        ]:
            with self.assertRaises(ValueError):
                await self.repo.submit(100, 1000, block, author, False)
        async with self.db.pool.acquire() as c:
            self.assertEqual(await c.fetchval("SELECT sum(mmr) FROM season_players"), 0)
            self.assertEqual(await c.fetchval("SELECT count(*) FROM matches"), 0)

    async def test_poll_single_creation_votes_expiry_restart(self):
        room = await self.filled_room()
        options = [["Song", 28, "Expert"], ["Other", 29, "Master"]]
        results = await asyncio.gather(
            *(self.repo.create_poll(room["id"], options) for _ in range(10))
        )
        self.assertEqual(sum(results), 1)
        await self.repo.activate_poll(room["id"], 200)
        await asyncio.gather(
            *(self.repo.vote(room["id"], i, i % 2) for i in range(1, 6))
        )
        await self.repo.vote(room["id"], 1, 0)
        with self.assertRaises(ValueError):
            await self.repo.vote(room["id"], 99, 0)
        self.assertIsNone(await self.repo.close_poll(room["id"], random.Random(5)))
        async with self.db.pool.acquire() as c:
            await c.execute("UPDATE polls SET expires_at=now()-interval '1 second'")
        with self.assertRaises(ValueError):
            await self.repo.vote(room["id"], 1, 1)
        first = await Repository(self.db).close_poll(room["id"], random.Random(5))
        second = await self.repo.close_poll(room["id"], random.Random(100))
        self.assertEqual(first["winner"], second["winner"])
        self.assertEqual(first["winner"], 0)

    async def test_lease_rejects_second_instance_and_fences_old(self):
        other = Database(self.settings)
        await other.open()
        try:
            with self.assertRaises(RuntimeError):
                await other.claim()
            async with self.db.pool.acquire() as c:
                await c.execute(
                    "UPDATE runtime_lease SET expires_at=now()-interval '1 second'"
                )
            await other.claim()
            with self.assertRaises(RuntimeError):
                await self.repo.join(1, "10", 1, "P1")
            with self.assertRaises(RuntimeError):
                await self.db.heartbeat()
            await other.release()
        finally:
            await other.close()

    async def test_profile_register_preserves_country_and_season(self):
        await self.repo.register(1, "P1", "PE")
        await self.repo.register(1, "Renamed")
        async with self.db.pool.acquire() as c:
            row = await c.fetchrow("SELECT * FROM players WHERE user_id=1")
            self.assertEqual(row["country"], "PE")
            self.assertEqual(row["name"], "Renamed")
            self.assertEqual(
                await c.fetchval("SELECT season_id FROM season_players"), "beta"
            )

    async def test_api_precision_pagination_search_and_security(self):
        uid = 878310498720940102
        await self.repo.register(uid, "Player", "PE")
        await self.repo.register(uid + 1, "Other")
        app = create_app(self.db)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            response = await client.get("/api/players?q=Other&limit=1")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()[0]["id"], str(uid + 1))
            self.assertEqual(response.json()[0]["position"], 2)
            self.assertEqual(response.headers["X-Season-Id"], "beta")
            self.assertEqual(
                (await client.get("/api/players?limit=201")).status_code, 422
            )
            self.assertEqual(
                (
                    await client.post(
                        "/api/assign-rank",
                        json={"userId": str(uid), "newRank": "Diamond"},
                    )
                ).status_code,
                405,
            )
            self.assertEqual((await client.get("/api/health")).status_code, 200)

    async def test_season_requires_drain_and_backup(self):
        with self.assertRaises(ValueError):
            await activate(self.db)
        await self.db.release()
        summary = await activate(self.db)
        self.assertFalse(summary["execute"])
        with self.assertRaises(ValueError):
            await activate(self.db, execute=True)
        async with self.db.pool.acquire() as c:
            self.assertEqual(
                await c.fetchval("SELECT id FROM seasons WHERE status='active'"), "beta"
            )

    async def test_old_season_result_rejected(self):
        await self.filled_room()
        async with self.db.pool.acquire() as c, c.transaction():
            await c.execute("UPDATE seasons SET status='archived'")
            await c.execute(
                "INSERT INTO seasons(id,number,name,status,legacy_placement) VALUES('5',5,'Season 5 (New Era)','active',false)"
            )
        with self.assertRaises(ValueError):
            await self.repo.submit(100, 1000, self.block(), 1, True)

    async def test_backup_restore_and_season_reset_idempotence(self):
        if not os.getenv("PG_BIN_DIR"):
            self.skipTest("PG_BIN_DIR required for verified pg_dump/restore test")
        await self.repo.register(1, "Player", "PE")
        async with self.db.pool.acquire() as c:
            await c.execute(
                "UPDATE season_players SET mmr=2345,role='Diamond' WHERE user_id=1"
            )
        await self.db.release()
        restore_name = "pjsk_restore_" + uuid.uuid4().hex
        await self.admin.execute(f"CREATE DATABASE {restore_name}")
        restore_url = urlunsplit(urlsplit(ADMIN)._replace(path="/" + restore_name))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                destination = Path(tmp) / "before.dump"
                m = await backup(
                    self.db, destination, restore_url, os.environ["PG_BIN_DIR"]
                )
                self.assertTrue(m["restore_verified"])
                result = await activate(
                    self.db,
                    execute=True,
                    backup_manifest=destination.with_suffix(".manifest.json"),
                )
                self.assertEqual(result["players"], 1)
                async with self.db.pool.acquire() as c:
                    self.assertEqual(
                        await c.fetchval(
                            "SELECT mmr FROM season_players WHERE season_id='beta'"
                        ),
                        2345,
                    )
                    self.assertEqual(
                        await c.fetchval(
                            "SELECT mmr FROM season_players WHERE season_id='5'"
                        ),
                        0,
                    )
                    self.assertFalse(
                        await c.fetchval(
                            "SELECT placement_pending FROM season_players WHERE season_id='5'"
                        )
                    )
                    self.assertEqual(
                        await c.fetchval("SELECT country FROM players"), "PE"
                    )
                with self.assertRaises(ValueError):
                    await activate(
                        self.db,
                        execute=True,
                        backup_manifest=destination.with_suffix(".manifest.json"),
                    )
                await self.db.claim()
                await self.repo.register(2, "New player")
                p = await self.repo.player(2)
                self.assertEqual(p["mmr"], 0)
                self.assertFalse(p["placement_pending"])
        finally:
            await self.admin.execute(f"DROP DATABASE {restore_name} WITH (FORCE)")

    async def test_role_429_keeps_confirmed_mmr_and_retry_job(self):
        await self.filled_room()
        await self.repo.submit(100, 1000, self.block(), 1, False)

        class Role:
            name = "Diamond"
            managed = False

            def __ge__(self, other):
                return False

        role = Role()
        member = SimpleNamespace(
            roles=[],
            add_roles=AsyncMock(
                side_effect=discord.HTTPException(
                    SimpleNamespace(status=429, reason="rate limited"), "retry"
                )
            ),
            remove_roles=AsyncMock(),
        )
        guild = SimpleNamespace(
            roles=[role],
            get_member=lambda uid: member,
            me=SimpleNamespace(
                guild_permissions=SimpleNamespace(manage_roles=True), top_role=object()
            ),
        )
        bot = SimpleNamespace(db=self.db, get_guild=lambda gid: guild)
        async with self.db.pool.acquire() as c:
            job = await c.fetchrow(
                "SELECT j.*,sp.role FROM role_jobs j JOIN season_players sp USING(user_id) WHERE j.user_id=1"
            )
            before = await c.fetchval("SELECT mmr FROM season_players WHERE user_id=1")
        await AutoRoles(bot).apply(job)
        async with self.db.pool.acquire() as c:
            self.assertEqual(
                await c.fetchval("SELECT mmr FROM season_players WHERE user_id=1"),
                before,
            )
            self.assertEqual(
                await c.fetchval("SELECT attempts FROM role_jobs WHERE user_id=1"), 1
            )

    async def test_pool_recovers_after_socket_loss(self):
        async with self.db.pool.acquire() as c:
            c.terminate()
        self.assertTrue(await self.db.health())
        await self.repo.register(1, "After reconnect")
        self.assertEqual((await self.repo.player(1))["mmr"], 0)

    async def test_rls_api_role_cannot_modify_competitive_state(self):
        await self.repo.register(1, "Player")
        async with self.db.pool.acquire() as c:
            await c.execute(Path("deploy/roles.sql").read_text(encoding="utf-8"))
            async with c.transaction():
                await c.execute("SET LOCAL ROLE lounge_api")
                self.assertEqual(
                    await c.fetchval("SELECT count(*) FROM season_players"), 1
                )
                with self.assertRaises(asyncpg.InsufficientPrivilegeError):
                    async with c.transaction():
                        await c.execute("UPDATE season_players SET mmr=99999")
                with self.assertRaises(asyncpg.InsufficientPrivilegeError):
                    async with c.transaction():
                        await c.execute("SELECT legacy_data FROM players")

    async def test_restart_restores_rooms_and_duplicate_submit_stays_idempotent(self):
        room = await self.filled_room()
        await self.db.release()
        await self.db.close()
        self.db = Database(self.settings)
        await self.db.open()
        await self.db.claim()
        self.repo = Repository(self.db)
        restored = (await self.repo.rooms())[0]
        self.assertEqual(restored["id"], room["id"])
        self.assertEqual(restored["players"], [1, 2, 3, 4, 5])
        await self.repo.submit(100, 1000, self.block(), 1, False)
        _, created = await self.repo.submit(100, 1000, self.block(), 1, False)
        self.assertFalse(created)
