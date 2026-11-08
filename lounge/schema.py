"""Read-only runtime schema validation; never alters or deduplicates player data."""


async def validate_schema(conn):
    applied = await conn.fetchval(
        "SELECT to_regclass('public.lounge_schema_migrations')"
    )
    if not applied:
        raise RuntimeError("Apply versioned runtime migrations before startup")
    version = await conn.fetchval("SELECT max(version) FROM lounge_schema_migrations")
    if version != "007_publication_state.sql":
        raise RuntimeError("Runtime schema is not up to date")
    for table in (
        "players",
        "seasons",
        "seasonplayers",
        "match_history",
        "player_title_inventory",
        "player_pfps",
        "lounge_room_state",
    ):
        if not await conn.fetchval("SELECT to_regclass($1)", "public." + table):
            raise RuntimeError("Missing required table: " + table)
