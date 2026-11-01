import json
import uuid
from lounge.rules import calculate, choose_room, parse_submit


class Repository:
    def __init__(self, db):
        self.db = db

    async def ensure_player(self, c, season, user_id, name="", country=None):
        await c.execute(
            """INSERT INTO players(user_id,name,country) VALUES($1,$2,coalesce($3,''))
            ON CONFLICT(user_id) DO UPDATE SET name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE players.name END,
            country=coalesce($3,players.country)""",
            user_id,
            name,
            country,
        )
        await c.execute(
            """INSERT INTO season_players(season_id,user_id,placement_pending,peak_mmr)
            VALUES($1,$2,$3,0) ON CONFLICT DO NOTHING""",
            season["id"],
            user_id,
            season["legacy_placement"],
        )

    async def register(self, user_id, name, country=None):
        async with self.db.competitive() as (c, season):
            await self.ensure_player(c, season, user_id, name, country)

    async def player(self, user_id, name=""):
        async with self.db.competitive() as (c, s):
            await self.ensure_player(c, s, user_id, name)
            return dict(
                await c.fetchrow(
                    "SELECT * FROM season_players WHERE season_id=$1 AND user_id=$2",
                    s["id"],
                    user_id,
                )
            )

    async def join(self, guild_id, queue_id, user_id, name):
        async with self.db.competitive() as (c, s):
            # Same lock order for joins/leaves; different queues can proceed independently.
            await c.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                f"user:{guild_id}:{user_id}",
            )
            await c.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                f"queue:{guild_id}:{queue_id}",
            )
            if await c.fetchval(
                "SELECT 1 FROM room_members WHERE guild_id=$1 AND user_id=$2",
                guild_id,
                user_id,
            ):
                raise ValueError("Ya estás en una sala; usa /d primero")
            await self.ensure_player(c, s, user_id, name)
            rows = await c.fetch(
                """SELECT r.*,coalesce(array_agg(m.user_id ORDER BY m.joined_at,m.user_id)
                FILTER(WHERE m.user_id IS NOT NULL),'{}') AS players FROM rooms r
                LEFT JOIN room_members m ON m.room_id=r.id
                WHERE r.guild_id=$1 AND r.queue_id=$2 AND r.season_id=$3 AND r.status IN('open','provisioning','polling','ready')
                GROUP BY r.id ORDER BY r.display_number,r.created_at""",
                guild_id,
                queue_id,
                s["id"],
            )
            ids = {user_id} | {uid for row in rows for uid in row["players"]}
            states = await c.fetch(
                "SELECT user_id,mmr FROM season_players WHERE season_id=$1 AND user_id=ANY($2::bigint[])",
                s["id"],
                list(ids),
            )
            mmrs = {p["user_id"]: p["mmr"] for p in states}
            room = choose_room(rows, mmrs, mmrs[user_id])
            if room is None:
                room = await c.fetchrow(
                    """INSERT INTO rooms(id,season_id,guild_id,queue_id,display_number,status)
                    SELECT $1,$2,$3,$4,coalesce(max(display_number),0)+1,'provisioning'
                    FROM rooms WHERE guild_id=$3 AND queue_id=$4 AND status NOT IN('closed','finished') RETURNING *""",
                    uuid.uuid4(),
                    s["id"],
                    guild_id,
                    queue_id,
                )
            await c.execute(
                "INSERT INTO room_members(room_id,guild_id,user_id) VALUES($1,$2,$3)",
                room["id"],
                guild_id,
                user_id,
            )
            await c.execute(
                "INSERT INTO thread_jobs(room_id,user_id,action) VALUES($1,$2,'add') ON CONFLICT(room_id,user_id) DO UPDATE SET action='add',next_attempt=now(),attempts=0",
                room["id"],
                user_id,
            )
            await self.renumber(c, guild_id, queue_id, s["id"])
            return dict(
                await c.fetchrow("SELECT * FROM rooms WHERE id=$1", room["id"])
            ), mmrs[user_id]

    async def renumber(self, c, guild_id, queue_id, season_id):
        await c.execute(
            """WITH ordered AS(
            SELECT r.id,row_number() OVER(ORDER BY avg(sp.mmr) DESC NULLS LAST,r.display_number,r.created_at) AS n
            FROM rooms r LEFT JOIN room_members rm ON rm.room_id=r.id
            LEFT JOIN season_players sp ON sp.season_id=r.season_id AND sp.user_id=rm.user_id
            WHERE r.guild_id=$1 AND r.queue_id=$2 AND r.season_id=$3 AND r.status NOT IN('closed','finished') GROUP BY r.id)
            UPDATE rooms r SET display_number=o.n FROM ordered o WHERE r.id=o.id""",
            guild_id,
            queue_id,
            season_id,
        )

    async def leave(self, guild_id, user_id):
        async with self.db.competitive() as (c, s):
            await c.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                f"user:{guild_id}:{user_id}",
            )
            room = await c.fetchrow(
                "SELECT r.* FROM rooms r JOIN room_members m ON m.room_id=r.id WHERE m.guild_id=$1 AND m.user_id=$2",
                guild_id,
                user_id,
            )
            if not room:
                raise ValueError("No estás en ninguna sala")
            await c.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                f"queue:{guild_id}:{room['queue_id']}",
            )
            await c.execute(
                "DELETE FROM room_members WHERE room_id=$1 AND user_id=$2",
                room["id"],
                user_id,
            )
            await c.execute(
                "INSERT INTO thread_jobs(room_id,user_id,action) VALUES($1,$2,'remove') ON CONFLICT(room_id,user_id) DO UPDATE SET action='remove',next_attempt=now(),attempts=0",
                room["id"],
                user_id,
            )
            left = await c.fetchval(
                "SELECT count(*) FROM room_members WHERE room_id=$1", room["id"]
            )
            if not left:
                await c.execute(
                    "UPDATE rooms SET status='closed',issue='archive_pending',updated_at=now() WHERE id=$1",
                    room["id"],
                )
                await c.execute(
                    "UPDATE polls SET status='cancelled' WHERE room_id=$1 AND status IN('active','sending')",
                    room["id"],
                )
            await self.renumber(c, guild_id, room["queue_id"], s["id"])
            return dict(room), left

    async def rooms(self):
        async with self.db.pool.acquire() as c:
            return [
                dict(r)
                for r in await c.fetch("""SELECT r.*,coalesce(array_agg(m.user_id ORDER BY m.joined_at,m.user_id)
                FILTER(WHERE m.user_id IS NOT NULL),'{}') AS players FROM rooms r
                LEFT JOIN room_members m ON m.room_id=r.id WHERE r.status NOT IN('closed','finished') GROUP BY r.id ORDER BY display_number""")
            ]

    async def attach_thread(self, room_id, thread_id):
        async with self.db.competitive() as (c, s):
            await c.execute(
                "UPDATE rooms SET thread_id=$2,status='open',updated_at=now() WHERE id=$1 AND season_id=$3 AND status='provisioning'",
                room_id,
                thread_id,
                s["id"],
            )

    async def issue(self, room_id, reason):
        async with self.db.pool.acquire() as c:
            await c.execute(
                "UPDATE rooms SET status='needs_attention',issue=$2,updated_at=now() WHERE id=$1",
                room_id,
                reason,
            )

    async def create_poll(self, room_id, options):
        async with self.db.competitive() as (c, s):
            room = await c.fetchrow(
                "SELECT * FROM rooms WHERE id=$1 AND season_id=$2 FOR UPDATE",
                room_id,
                s["id"],
            )
            if not room or room["status"] != "open":
                return False
            if (
                await c.fetchval(
                    "SELECT count(*) FROM room_members WHERE room_id=$1", room_id
                )
                != 5
            ):
                return False
            created = await c.fetchval(
                """INSERT INTO polls(room_id,options,expires_at,status) VALUES($1,$2,clock_timestamp()+interval '60 seconds','sending')
                ON CONFLICT DO NOTHING RETURNING true""",
                room_id,
                json.dumps(options),
            )
            if created:
                await c.execute(
                    "UPDATE rooms SET status='polling' WHERE id=$1", room_id
                )
            return bool(created)

    async def activate_poll(self, room_id, message_id):
        async with self.db.competitive() as (c, s):
            await c.execute(
                "UPDATE polls SET message_id=$2,status='active' WHERE room_id=$1 AND status='sending'",
                room_id,
                message_id,
            )

    async def vote(self, room_id, user_id, choice):
        async with self.db.competitive() as (c, s):
            poll = await c.fetchrow(
                "SELECT p.*,r.season_id FROM polls p JOIN rooms r ON r.id=p.room_id WHERE room_id=$1 FOR UPDATE OF p",
                room_id,
            )
            if not poll or poll["season_id"] != s["id"] or poll["status"] != "active":
                raise ValueError("Votación cerrada")
            if not await c.fetchval("SELECT clock_timestamp()<$1", poll["expires_at"]):
                raise ValueError("Votación expirada")
            options = json.loads(poll["options"])
            if not 0 <= choice < len(options):
                raise ValueError("Opción inválida")
            if not await c.fetchval(
                "SELECT 1 FROM room_members WHERE room_id=$1 AND user_id=$2",
                room_id,
                user_id,
            ):
                raise ValueError("Solo pueden votar los participantes de la sala")
            await c.execute(
                "INSERT INTO poll_choices(room_id,choice) VALUES($1,$2) ON CONFLICT DO NOTHING",
                room_id,
                choice,
            )
            await c.execute(
                "INSERT INTO poll_votes(room_id,user_id,choice) VALUES($1,$2,$3) ON CONFLICT(room_id,user_id) DO UPDATE SET choice=excluded.choice",
                room_id,
                user_id,
                choice,
            )
            return options[choice]

    async def close_poll(self, room_id, rng):
        async with self.db.competitive() as (c, s):
            poll = await c.fetchrow(
                "SELECT p.*,r.season_id FROM polls p JOIN rooms r ON r.id=p.room_id WHERE room_id=$1 FOR UPDATE OF p",
                room_id,
            )
            if (
                not poll
                or poll["season_id"] != s["id"]
                or poll["status"] not in ("active", "sending", "closed")
            ):
                return None
            if poll["status"] != "closed":
                if await c.fetchval("SELECT clock_timestamp()<$1", poll["expires_at"]):
                    return None
                # Preserve original insertion order of choices even when a voter changes their vote.
                votes = await c.fetch(
                    "SELECT pc.choice,count(v.user_id) AS n,pc.first_order FROM poll_choices pc LEFT JOIN poll_votes v ON v.room_id=pc.room_id AND v.choice=pc.choice WHERE pc.room_id=$1 GROUP BY pc.choice,pc.first_order ORDER BY pc.first_order",
                    room_id,
                )
                max_votes = max((v["n"] for v in votes), default=0)
                winners = [v["choice"] for v in votes if v["n"] == max_votes]
                winner = (
                    rng.choice(winners)
                    if len(winners) > 1
                    else winners[0]
                    if winners
                    else None
                )
                await c.execute(
                    "UPDATE polls SET status='closed',winner=$2 WHERE room_id=$1",
                    room_id,
                    winner,
                )
                await c.execute(
                    "UPDATE rooms SET status='ready',updated_at=now() WHERE id=$1",
                    room_id,
                )
                poll = dict(poll)
                poll.update(status="closed", winner=winner)
            return dict(poll)

    async def submit(self, thread_id, message_id, block, author_id, admin):
        entries = parse_submit(block)
        async with self.db.competitive() as (c, s):
            room = await c.fetchrow(
                "SELECT * FROM rooms WHERE thread_id=$1 FOR UPDATE", thread_id
            )
            if not room or room["season_id"] != s["id"]:
                raise ValueError("La sala no pertenece a la temporada activa")
            existing = await c.fetchval(
                "SELECT results FROM matches WHERE room_id=$1", room["id"]
            )
            if existing:
                if not admin and not await c.fetchval(
                    "SELECT 1 FROM match_participants WHERE match_id=(SELECT id FROM matches WHERE room_id=$1) AND user_id=$2",
                    room["id"],
                    author_id,
                ):
                    raise ValueError(
                        "Solo participantes o administradores pueden consultar este resultado"
                    )
                return json.loads(existing), False
            members = await c.fetch(
                "SELECT user_id FROM room_members WHERE room_id=$1", room["id"]
            )
            ids = {m["user_id"] for m in members}
            if not admin and author_id not in ids:
                raise ValueError(
                    "Solo participantes o administradores pueden enviar resultados"
                )
            if {e.user_id for e in entries} != ids or len(ids) != 5:
                raise ValueError(
                    "El resultado debe contener exactamente los cinco miembros de esta sala"
                )
            if room["status"] not in ("ready", "open", "polling"):
                raise ValueError("Sala no disponible para resultados")
            states = await c.fetch(
                "SELECT * FROM season_players WHERE season_id=$1 AND user_id=ANY($2::bigint[]) ORDER BY user_id FOR UPDATE",
                s["id"],
                sorted(ids),
            )
            result = calculate(entries, {p["user_id"]: p for p in states})
            match_id = uuid.uuid4()
            await c.execute(
                "INSERT INTO matches(id,room_id,season_id,source_message_id,results) VALUES($1,$2,$3,$4,$5)",
                match_id,
                room["id"],
                s["id"],
                message_id,
                json.dumps(result),
            )
            for p in result:
                await c.execute(
                    """UPDATE season_players SET mmr=$3,role=$4,placement_pending=false,
                    peak_mmr=greatest(coalesce(peak_mmr,mmr),$3),matches_played=matches_played+1,
                    wins=wins+$5,losses=losses+$6,updated_at=now() WHERE season_id=$1 AND user_id=$2""",
                    s["id"],
                    p["user_id"],
                    p["new"],
                    p["role"],
                    int(p["idx"] == 1),
                    int(p["idx"] != 1),
                )
                await c.execute(
                    "INSERT INTO match_participants VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                    match_id,
                    s["id"],
                    p["user_id"],
                    p["idx"],
                    p["total"],
                    p["old"],
                    p["delta"],
                    p["new"],
                    json.dumps(p["stats"]),
                )
                await c.execute(
                    "INSERT INTO role_jobs(guild_id,user_id) VALUES($1,$2) ON CONFLICT(guild_id,user_id) DO UPDATE SET next_attempt=now(),attempts=0",
                    room["guild_id"],
                    p["user_id"],
                )
            await c.execute(
                "UPDATE rooms SET status='finished',updated_at=now() WHERE id=$1",
                room["id"],
            )
            await c.execute(
                "UPDATE polls SET status='cancelled' WHERE room_id=$1 AND status IN('active','sending')",
                room["id"],
            )
            await c.execute("DELETE FROM room_members WHERE room_id=$1", room["id"])
            return result, True

    async def leaderboard(self, limit=10):
        async with self.db.pool.acquire() as c:
            return await c.fetch(
                """SELECT p.user_id,p.name,sp.mmr,sp.role FROM players p JOIN season_players sp USING(user_id)
                JOIN seasons s ON s.id=sp.season_id WHERE s.status='active' ORDER BY sp.mmr DESC,p.user_id LIMIT $1""",
                limit,
            )
