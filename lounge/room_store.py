import json


def snapshot(room):
    def clean(value):
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, (list, tuple, set)):
            return [clean(v) for v in value]
        if isinstance(value, dict):
            return {str(k): clean(v) for k, v in value.items()}
        if hasattr(value, "id"):
            return int(value.id)
        raise TypeError("Unsupported persisted room field: " + type(value).__name__)

    result = {k: clean(v) for k, v in room.items() if k != "players"}
    result["player_ids"] = [int(p.id) for p in room.get("players", []) if p is not None]
    return result


class RoomStore:
    def __init__(self, pool):
        self.pool = pool

    async def save(self, room_id, room):
        if not room.get("season_id") or not room.get("guild_id"):
            raise ValueError("Room has no verified season/guild")
        ids = [int(m.id) for m in room.get("players", []) if m is not None]
        if len(ids) > 5 or len(set(ids)) != len(ids):
            raise ValueError("Room capacity or duplicate membership violation")
        payload = json.dumps(snapshot(room), ensure_ascii=False)
        status = (
            "finished"
            if room.get("finished")
            else "closed"
            if room.get("closed")
            else "active"
        )
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """INSERT INTO lounge_room_state(thread_id,room_id,season_id,guild_id,queue_id,payload,status)
                VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(thread_id) DO UPDATE SET
                payload=excluded.payload,status=CASE WHEN lounge_room_state.status='finished' THEN 'finished' ELSE excluded.status END,updated_at=now()
                WHERE lounge_room_state.season_id=excluded.season_id
                  AND lounge_room_state.status NOT IN ('needs_attention','finished')
                  AND COALESCE((lounge_room_state.payload->>'reroll_count')::int,0)
                      <= COALESCE((excluded.payload->>'reroll_count')::int,0)""",
                int(room["thread"]),
                int(room_id),
                int(room["season_id"]),
                int(room["guild_id"]),
                int(room["join_channel_id"]),
                payload,
                status,
            )
            stored = await conn.fetchrow(
                "SELECT status,season_id FROM lounge_room_state WHERE thread_id=$1 FOR UPDATE",
                int(room["thread"]),
            )
            if stored["season_id"] != int(room["season_id"]):
                raise ValueError("Room season cannot change")
            current = stored["status"]
            if current == "needs_attention":
                return
            await conn.execute(
                "DELETE FROM lounge_room_members WHERE thread_id=$1",
                int(room["thread"]),
            )
            if status == "active" and current == "active":
                await conn.executemany(
                    "INSERT INTO lounge_room_members(thread_id,guild_id,user_id,position) VALUES($1,$2,$3,$4)",
                    [
                        (int(room["thread"]), int(room["guild_id"]), str(uid), i)
                        for i, uid in enumerate(ids, 1)
                    ],
                )

    async def pending(self):
        async with self.pool.acquire() as c:
            return await c.fetch(
                "SELECT * FROM lounge_room_state WHERE status='active' ORDER BY room_id"
            )

    async def issue(self, thread_id, reason):
        async with self.pool.acquire() as c:
            await c.execute(
                "UPDATE lounge_room_state SET status='needs_attention',issue=$2,updated_at=now() WHERE thread_id=$1",
                thread_id,
                reason,
            )

    async def save_queues(self, queues, orders):
        async with self.pool.acquire() as c, c.transaction():
            for queue_id, members in queues.items():
                payload = {
                    "members": sorted(members),
                    "order": orders.get(queue_id, {}),
                }
                await c.execute(
                    "INSERT INTO lounge_queue_state(queue_id,payload) VALUES($1,$2) ON CONFLICT(queue_id) DO UPDATE SET payload=excluded.payload,updated_at=now()",
                    int(queue_id),
                    json.dumps(payload),
                )

    async def load_queues(self):
        async with self.pool.acquire() as c:
            return await c.fetch("SELECT * FROM lounge_queue_state ORDER BY queue_id")
