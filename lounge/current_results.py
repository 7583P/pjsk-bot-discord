"""Atomic persistence around the modern competitive calculation supplied by the cog."""

import json


def compute(
    players,
    pairwise,
    tie_adjust,
    rank_for_mmr,
    placement_seed,
    *,
    placement_enabled=True,
    reset_penalty=50,
    allow_historical_seed=True,
):
    players = [dict(p) for p in players]
    for p in players:
        p["mmr_actual"] = int(p["old"])
        p["seed_mmr"] = None
        p["seed_rank"] = None
        p["was_placement"] = bool(
            placement_enabled
            and (p.get("is_season_placement") or p.get("is_rank_placement"))
        )
        if p["was_placement"]:
            if p.get("is_season_placement") and allow_historical_seed:
                seed = max(0, int(p.get("prev_season_mmr") or 0) - reset_penalty)
                label = rank_for_mmr(seed)
            else:
                seed, label = placement_seed(p["stats"])
            p.update(mmr_actual=seed, seed_mmr=seed, seed_rank=label)
    players.sort(key=lambda p: (p["total"], int(p["was_placement"])), reverse=True)
    deltas = pairwise(
        [p["mmr_actual"] for p in players],
        list(range(len(players))),
        K=50,
        scale=400,
        cap=39,
    )
    deltas = tie_adjust(players, deltas)
    for index, (p, delta) in enumerate(zip(players, deltas), 1):
        p.update(
            position=index,
            mmr_prev=p["mmr_actual"],
            mmr_delta=int(delta),
            mmr_final=max(0, p["mmr_actual"] + int(delta)),
        )
        p["final_role"] = rank_for_mmr(p["mmr_final"])
    return players


async def commit_result(
    pool,
    room,
    entries,
    *,
    pairwise,
    tie_adjust,
    rank_for_mmr,
    placement_seed,
    source_message_id,
    result_channel_id,
    owner=None,
):
    thread_id = int(room["thread"])
    season_id = int(room["season_id"])
    ids = sorted(int(p["user_id"]) for p in entries)
    if not 2 <= len(ids) <= 5 or len(set(ids)) != len(ids):
        raise ValueError("Two to five distinct room participants required")
    for entry in entries:
        stats = entry.get("stats", [])
        if len(stats) != 5 or any(not isinstance(n, int) or n < 0 for n in stats):
            raise ValueError("Invalid PGGBM statistics")
        if room.get("total_notes") is not None and sum(stats) != int(
            room["total_notes"]
        ):
            raise ValueError("Submitted note count differs from room")
    async with pool.acquire() as conn, conn.transaction():
        if owner is not None:
            valid = await conn.fetchval(
                "SELECT owner=$1 AND expires_at>clock_timestamp() FROM lounge_runtime_lease WHERE id=1 FOR SHARE",
                owner,
            )
            if not valid:
                raise RuntimeError("Bot lease lost")
        season = await conn.fetchrow(
            "SELECT id,name FROM seasons WHERE id=$1 AND active FOR SHARE", season_id
        )
        if not season:
            raise ValueError("Result belongs to a closed season")
        await conn.execute("SELECT pg_advisory_xact_lock($1)", thread_id)
        previous = await conn.fetchrow(
            "SELECT result_id,payload FROM lounge_submissions WHERE thread_id=$1",
            thread_id,
        )
        if previous:
            return json.loads(previous["payload"]), False
        persisted = await conn.fetchrow(
            "SELECT season_id,payload,status FROM lounge_room_state WHERE thread_id=$1 FOR UPDATE",
            thread_id,
        )
        if not persisted or persisted["season_id"] != season_id:
            raise ValueError("Room must be persisted in its original season")
        if persisted["status"] != "active":
            raise ValueError("Room requires staff review or is already closed")
        state = json.loads(persisted["payload"])
        if set(map(int, state.get("player_ids", []))) != set(ids):
            raise ValueError("Submission participants do not match persisted room")
        rows = await conn.fetch(
            "SELECT * FROM seasonplayers WHERE season_id=$1 AND user_id=ANY($2::bigint[]) ORDER BY user_id FOR UPDATE",
            season_id,
            ids,
        )
        by_id = {int(r["user_id"]): r for r in rows}
        if len(by_id) != len(ids):
            raise ValueError("Missing seasonal player records")
        policy = await conn.fetchrow(
            "SELECT * FROM lounge_season_policy WHERE season_id=$1", season_id
        )
        allow_placement = policy["placement_enabled"] if policy else True
        reset_kind = policy["reset_kind"] if policy else "legacy"
        import re

        is_season_5 = bool(re.search(r"\bseason\s*5\b", season["name"], re.I))
        allow_historical_seed = (
            reset_kind not in ("extraordinary_new_era", "zero_start_no_seed")
            and not is_season_5
        )
        prepared = []
        for entry in entries:
            p = dict(entry)
            row = by_id[int(p["user_id"])]
            p["old"] = int(row["mmr"])
            p["is_rank_placement"] = row["role"] == "Placement"
            p["is_season_placement"] = (
                bool(row["is_season_placement"])
                if (allow_placement and allow_historical_seed)
                else False
            )
            if not allow_historical_seed:
                p["prev_season_mmr"] = 0
            prepared.append(p)
        result = compute(
            prepared,
            pairwise,
            tie_adjust,
            rank_for_mmr,
            placement_seed,
            placement_enabled=allow_placement,
            allow_historical_seed=allow_historical_seed,
        )
        # Single serialized global counter; seeded from existing results/history if absent.
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('pjsk:result-counter',0))"
        )
        maximum = await conn.fetchval(
            "SELECT greatest(coalesce((SELECT max(result_id) FROM results),-1),coalesce((SELECT max(result_id) FROM match_history),-1))"
        )
        result_id = await conn.fetchval(
            "INSERT INTO global_settings(key,value) VALUES('last_result_id',$1) ON CONFLICT(key) DO UPDATE SET value=greatest(global_settings.value+1,excluded.value) RETURNING value",
            int(maximum) + 1,
        )
        songs = room.get("songs") or []
        songs_block = "\n".join(
            f"Lv {level} {diff} — {title} ({notes} notes)"
            for title, level, diff, notes in songs
        )
        average = round(sum(p["mmr_actual"] for p in result) / len(result))
        await conn.execute(
            "INSERT INTO results(result_id,season_id,season_name,channel_id,message_id,gap_low,gap_high,room_mmr,songs_block) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            result_id,
            season_id,
            season["name"],
            result_channel_id,
            source_message_id,
            room.get("gap_low"),
            room.get("gap_high"),
            average,
            songs_block or None,
        )
        for p in result:
            uid = int(p["user_id"])
            stats = list(map(int, p["stats"]))
            await conn.execute(
                "UPDATE seasonplayers SET mmr=$3,role=$4,is_season_placement=false,starting_mmr=NULL WHERE season_id=$1 AND user_id=$2",
                season_id,
                uid,
                p["mmr_final"],
                p["final_role"],
            )
            await conn.execute(
                "UPDATE players SET mmr=$2, role=$3 WHERE user_id=$1",
                uid,
                p["mmr_final"],
                p["final_role"],
            )
            await conn.execute(
                "INSERT INTO match_history(season_id,result_id,user_id,pos,s_perfect,s_great,s_good,s_bad,s_miss,mmr_before,mmr_after,mmr_delta,was_placement,is_win) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)",
                season_id,
                result_id,
                uid,
                p["position"],
                *stats,
                p["mmr_prev"],
                p["mmr_final"],
                p["mmr_delta"],
                p["was_placement"],
                p["mmr_delta"] >= 0,
            )
            await conn.execute(
                "INSERT INTO lounge_role_jobs(guild_id,user_id,role) VALUES($1,$2,$3) ON CONFLICT(guild_id,user_id) DO UPDATE SET role=excluded.role,attempts=0,next_attempt=now()",
                room["guild_id"],
                str(uid),
                p["final_role"],
            )
        if songs:
            await conn.executemany(
                "INSERT INTO match_songs(result_id,song_name,level,difficulty,notes_count) VALUES($1,$2,$3::integer,$4,$5)",
                [
                    (result_id, title, int(level), diff, int(notes))
                    for title, level, diff, notes in songs
                ],
            )
        payload = {
            "result_id": result_id,
            "season_id": season_id,
            "season_name": season["name"],
            "room_mmr": average,
            "players": result,
        }
        await conn.execute(
            "INSERT INTO lounge_submissions(thread_id,result_id,season_id,payload) VALUES($1,$2,$3,$4)",
            thread_id,
            result_id,
            season_id,
            json.dumps(payload),
        )
        await conn.execute(
            "UPDATE lounge_room_state SET status='finished',updated_at=now() WHERE thread_id=$1",
            thread_id,
        )
        await conn.execute(
            "DELETE FROM lounge_room_members WHERE thread_id=$1", thread_id
        )
        return payload, True
