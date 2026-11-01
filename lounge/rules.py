"""Competitive arithmetic characterized from cogs/matchmaking.py at 090e97f.

Season 5 explicitly disables legacy placement grants for reset players.
No change to scoring, tie order, multipliers, thresholds or negative MMR.
"""

from dataclasses import dataclass
import random
import re

RANKS = ("Placement", "Bronze", "Gold", "Diamond")
COLORS = dict(zip(RANKS, ("#ff7000", "#66311e", "#ffaf00", "#0080ff")))
ENTRY_RE = re.compile(
    r"^<@!?(?P<id>\d+)>\s*\[(?P<cc>[A-Za-z]{2})\]\s*(?P<stats>\d+,\d+,\d+,\d+,\d+)$"
)


@dataclass(frozen=True)
class Entry:
    user_id: int
    country: str
    stats: tuple[int, ...]


def parse_submit(block: str) -> list[Entry]:
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    if len(lines) != 5:
        raise ValueError("Debes enviar 5 líneas: @user [CC] p,g,gd,b,m")
    entries = []
    for line in lines:
        match = ENTRY_RE.fullmatch(line)
        if not match:
            raise ValueError("Formato incorrecto; usa @user [CC] p,g,gd,b,m")
        uid = int(match["id"])
        stats = tuple(map(int, match["stats"].split(",")))
        if not 0 < uid < 2**63 or any(s > 2**31 - 1 for s in stats):
            raise ValueError("ID o estadísticas fuera de rango")
        entries.append(Entry(uid, match["cc"].upper(), stats))
    if len({e.user_id for e in entries}) != 5:
        raise ValueError("Los cinco jugadores deben ser distintos")
    return entries


def rank_for_mmr(mmr: int) -> str:
    return "Bronze" if mmr < 1000 else "Gold" if mmr < 2000 else "Diamond"


def calculate(entries: list[Entry], states: dict) -> list[dict]:
    results = []
    for entry in entries:
        state = states[entry.user_id]
        old = state["mmr"]
        bonus = None
        if state["placement_pending"] and old == 0 and state["role"] == "Placement":
            non_perfect = sum(entry.stats[1:])
            old, label = (
                (2000, "Diamond")
                if non_perfect <= 15
                else (1000, "Gold")
                if non_perfect <= 50
                else (0, "Bronze")
            )
            bonus = f"(+{old} Placement {label})"
        results.append(
            dict(
                user_id=entry.user_id,
                country=entry.country,
                stats=list(entry.stats),
                total=sum(s * w for s, w in zip(entry.stats, (5, 3, 2, 1, 0))),
                old=old,
                bonus=bonus,
            )
        )
    results.sort(key=lambda p: p["total"], reverse=True)
    unit = max(1, int((sum(p["old"] for p in results) / 5) // 10))
    for idx, (p, multiplier) in enumerate(zip(results, (3, 2, 0.5, -1, -2)), 1):
        p.update(idx=idx, delta=int(multiplier * unit))
        p["new"] = p["old"] + p["delta"]
        p["role"] = rank_for_mmr(p["new"])
    return results


def song_range(counts: dict) -> tuple[int, int]:
    for rank, bounds in (
        ("Diamond", (31, 33)),
        ("Gold", (28, 30)),
        ("Bronze", (25, 27)),
    ):
        if counts.get(rank, 0):
            return bounds
    return 28, 30


def pick_songs(rows, low: int, high: int, rng=random):
    levels: dict[int, list[tuple[str, int, str]]] = {
        level: [] for level in range(high, low - 1, -1)
    }
    for row in rows:
        if low <= row["level"] <= high and row["diff"] in (
            "append",
            "master",
            "expert",
        ):
            levels[row["level"]].append(
                (row["title"], row["level"], row["diff"].capitalize())
            )
    picks = []
    for bucket in levels.values():
        rng.shuffle(bucket)
        picks.extend(bucket[:3])
    return picks[:9]


def choose_room(rooms, mmrs: dict[int, int], incoming_mmr: int):
    """Rooms arrive in current display order; stable ties are intentional."""
    candidates = [r for r in rooms if 0 < len(r["players"]) < 5]
    return min(
        candidates,
        key=lambda r: abs(
            sum(mmrs[u] for u in r["players"]) / len(r["players"]) - incoming_mmr
        ),
        default=None,
    )
