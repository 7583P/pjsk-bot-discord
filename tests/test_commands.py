from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from cogs.matchmaking import Matchmaking
from cogs.players import PlayersCog


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_command_names_and_parameters_preserved(self):
        bot = SimpleNamespace(db=None)
        matchmaking = Matchmaking(bot)
        players = PlayersCog(bot)
        self.assertEqual(
            {c.name for c in matchmaking.get_app_commands()}
            | {c.name for c in players.get_app_commands()},
            {"c", "d", "mmr", "mmr_user", "top10", "register"},
        )
        self.assertEqual(
            {c.name for c in matchmaking.get_commands()},
            {"submit5", "debug_poll", "debug_diffs"},
        )
        self.assertEqual([p.name for p in matchmaking.mmr_user.parameters], ["user"])
        self.assertEqual([p.name for p in players.register.parameters], ["country"])

    async def test_join_acknowledges_before_database(self):
        response = SimpleNamespace(defer=AsyncMock())

        async def slow_db(*args):
            response.defer.assert_awaited_once()
            raise ValueError("test db unavailable")

        bot = SimpleNamespace(
            db=None,
            repo=SimpleNamespace(join=slow_db),
            settings=SimpleNamespace(queue_key=lambda *args: "10"),
        )
        cog = Matchmaking(bot)
        inter = SimpleNamespace(
            response=response,
            guild_id=1,
            channel_id=10,
            channel=SimpleNamespace(name="join"),
            user=SimpleNamespace(id=1, display_name="P1"),
        )
        with self.assertRaises(ValueError):
            await cog.join_room.callback(cog, inter)

    async def test_register_acknowledges_before_database(self):
        response = SimpleNamespace(defer=AsyncMock())

        async def db(*args):
            response.defer.assert_awaited_once()

        cog = PlayersCog(SimpleNamespace(repo=SimpleNamespace(register=db)))
        inter = SimpleNamespace(
            response=response,
            followup=SimpleNamespace(send=AsyncMock()),
            user=SimpleNamespace(id=1, display_name="P1"),
        )
        await cog.register.callback(cog, inter, "PE")
