"""Create missing queue registrations without replacing existing player state."""

async def ensure_season_registration(pool, user_id: int, name: str) -> int:
    async with pool.acquire() as conn, conn.transaction():
        season = await conn.fetchrow("SELECT id FROM seasons WHERE active FOR SHARE")
        if season is None:
            raise ValueError("No active season")
        season_id = int(season["id"])
        enabled = await conn.fetchval(
            "SELECT placement_enabled FROM lounge_season_policy WHERE season_id=$1", season_id,
        )
        role = "Iron" if enabled is False else "Placement"
        await conn.execute(
            """INSERT INTO players(user_id,name,country,mmr,role)
            VALUES($1,$2,'',0,$3) ON CONFLICT(user_id) DO NOTHING""",
            user_id, name, role,
        )
        await conn.execute(
            """INSERT INTO seasonplayers(user_id,season_id,points,mmr,role,is_season_placement)
            VALUES($1,$2,0,0,$3,false) ON CONFLICT(user_id,season_id) DO NOTHING""",
            user_id, season_id, role,
        )
        return season_id
