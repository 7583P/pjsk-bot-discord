"""Read-only runtime schema validation; never alters or deduplicates player data."""


ACCEPTED_VERSIONS = ("007_publication_state.sql", "008_room_reroll.sql")


async def validate_schema(conn):
    applied = await conn.fetchval(
        "SELECT to_regclass('public.lounge_schema_migrations')"
    )
    if not applied:
        raise RuntimeError("Apply versioned runtime migrations before startup")
    version = await conn.fetchval("SELECT max(version) FROM lounge_schema_migrations")
    # 007 = previous runtime (still deployed on Railway); 008 adds lounge_room_reroll.
    # The new runtime accepts both. 008 is only *registered* once no 007-only
    # instance remains (see scripts/register_schema_008.py).
    if version not in ACCEPTED_VERSIONS:
        raise RuntimeError("Runtime schema is not up to date")
    if version == "008_room_reroll.sql" and not await conn.fetchval(
        "SELECT to_regclass('public.lounge_room_reroll')"
    ):
        raise RuntimeError("Schema 008 registered but lounge_room_reroll is missing")
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
