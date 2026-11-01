import random
import unittest
from lounge.rules import (
    Entry,
    calculate,
    rank_for_mmr,
    parse_submit,
    pick_songs,
    song_range,
    choose_room,
)


def legacy(entries, states):
    # Frozen baseline of the original submit5 arithmetic (090e97f), independent of services.
    players = []
    for e in entries:
        old = states[e.user_id]["mmr"]
        role = states[e.user_id]["role"]
        bonus = old
        if old == 0 and role == "Placement":
            n = sum(e.stats[1:])
            bonus = 2000 if n <= 15 else 1000 if n <= 50 else 0
        players.append(
            [e.user_id, sum(s * w for s, w in zip(e.stats, [5, 3, 2, 1, 0])), bonus]
        )
    players.sort(key=lambda x: x[1], reverse=True)
    unit = max(1, int((sum(p[2] for p in players) / 5) // 10))
    return [
        (
            p[0],
            p[2],
            int({1: 3, 2: 2, 3: 0.5, 4: -1, 5: -2}[i] * unit),
            p[2] + int({1: 3, 2: 2, 3: 0.5, 4: -1, 5: -2}[i] * unit),
        )
        for i, p in enumerate(players, 1)
    ]


class CompetitiveTests(unittest.TestCase):
    def test_equivalence_1000_seeded_matches(self):
        rng = random.Random(5)
        for _ in range(1000):
            entries = [
                Entry(i, "PE", tuple(rng.randrange(100) for _ in range(5)))
                for i in range(5)
            ]
            states = {
                i: dict(
                    mmr=rng.choice([0, 0, -10, 999, 1000, 1999, 2000, 5000]),
                    role=rng.choice(["Placement", "Bronze", "Gold", "Diamond"]),
                    placement_pending=True,
                )
                for i in range(5)
            }
            result = calculate(entries, states)
            self.assertEqual(
                [(p["user_id"], p["old"], p["delta"], p["new"]) for p in result],
                legacy(entries, states),
            )

    def test_season5_zero_does_not_grant_placement(self):
        entries = [Entry(i, "PE", (1000, 0, 0, 0, 0)) for i in range(5)]
        result = calculate(
            entries,
            {
                i: dict(mmr=0, role="Placement", placement_pending=False)
                for i in range(5)
            },
        )
        self.assertEqual([p["new"] for p in result], [3, 2, 0, -1, -2])
        self.assertTrue(all(p["bonus"] is None for p in result))
        self.assertEqual([p["user_id"] for p in result], list(range(5)))

    def test_placement_boundaries(self):
        for n, expected in [(15, 2000), (16, 1000), (50, 1000), (51, 0)]:
            entries = [Entry(i, "PE", (100, n, 0, 0, 0)) for i in range(5)]
            results = calculate(
                entries,
                {
                    i: dict(mmr=0, role="Placement", placement_pending=True)
                    for i in range(5)
                },
            )
            self.assertEqual(results[0]["old"], expected)

    def test_rank_boundaries(self):
        self.assertEqual(
            [rank_for_mmr(n) for n in [-100, 0, 999, 1000, 1999, 2000]],
            ["Bronze"] * 3 + ["Gold"] * 2 + ["Diamond"],
        )

    def test_submit_validates_all_five_unique_players(self):
        valid = "\n".join(f"<@{i}> [PE] 10,2,3,4,5" for i in range(1, 6))
        self.assertEqual(len(parse_submit(valid)), 5)
        for block in [
            valid.replace("<@5>", "<@1>"),
            valid + "\nextra",
            valid.replace("10,2", "-1,2"),
        ]:
            with self.assertRaises(ValueError):
                parse_submit(block)

    def test_song_selection_seed_and_limits(self):
        rows = [
            dict(title=str(i), level=level, diff="expert")
            for level in range(25, 34)
            for i in range(10)
        ]
        a = pick_songs(rows, 28, 30, random.Random(5))
        self.assertEqual(a, pick_songs(rows, 28, 30, random.Random(5)))
        self.assertEqual([s[1] for s in a], [30] * 3 + [29] * 3 + [28] * 3)
        self.assertEqual(song_range({"Diamond": 1, "Gold": 4}), (31, 33))
        self.assertEqual(song_range({}), (28, 30))

    def test_room_choice_and_tie_order(self):
        rooms = [dict(id=1, players=[1]), dict(id=2, players=[2])]
        self.assertEqual(choose_room(rooms, {1: 1200, 2: 800}, 1000)["id"], 1)
        self.assertEqual(choose_room(rooms, {1: 1200, 2: 800}, 700)["id"], 2)
        self.assertIsNone(choose_room([dict(players=[1] * 5)], {1: 0}, 0))
