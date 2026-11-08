"""Read-only inventory and aggregate integrity checks. Never return player rows."""

import hashlib
from urllib.parse import urlsplit

BASE_TABLES = {
    "players",
    "seasons",
    "seasonplayers",
    "results",
    "match_history",
    "match_songs",
    "songs",
}
RELATIONS = [
    ("seasonplayers", "user_id", "players", "user_id"),
    ("seasonplayers", "season_id", "seasons", "id"),
    ("match_history", "user_id", "players", "user_id"),
    ("match_history", "season_id", "seasons", "id"),
    ("match_history", "result_id", "results", "result_id"),
    ("results", "season_id", "seasons", "id"),
    ("match_songs", "result_id", "results", "result_id"),
    ("player_pfps", "user_id", "players", "user_id"),
    ("player_titles", "user_id", "players", "user_id"),
    ("player_title_inventory", "user_id", "players", "user_id"),
]


def ident(value):
    return '"' + value.replace('"', '""') + '"'


async def database_fingerprint(conn, connection_url=""):
    identity = await conn.fetchrow(
        "SELECT current_database() AS db,inet_server_addr()::text AS host,inet_server_port() AS port"
    )
    url = urlsplit(connection_url)
    target = "|".join(
        str(x) for x in [url.hostname, url.port or 5432, url.username, url.path]
    )
    return hashlib.sha256(
        (
            target + "|" + "|".join(str(identity[k]) for k in ["host", "port", "db"])
        ).encode()
    ).hexdigest()


async def inspect(pool, *, connection_url=""):
    async with (
        pool.acquire() as conn,
        conn.transaction(isolation="repeatable_read", readonly=True),
    ):
        await conn.execute("SET LOCAL statement_timeout='15s'")
        fingerprint = await database_fingerprint(conn, connection_url)
        tables = await conn.fetch(
            "SELECT c.relname AS name,c.relrowsecurity AS rls,c.relowner=(SELECT oid FROM pg_roles WHERE rolname=current_user) AS owned FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='r' ORDER BY c.relname"
        )
        columns = await conn.fetch(
            "SELECT table_name,column_name,data_type,is_nullable FROM information_schema.columns WHERE table_schema='public' ORDER BY table_name,ordinal_position"
        )
        available = {(r["table_name"], r["column_name"]) for r in columns}
        counts = {}
        for table in tables:
            name = table["name"]
            counts[name] = await conn.fetchval(
                f"SELECT count(*) FROM public.{ident(name)}"
            )
        constraints = await conn.fetch(
            "SELECT c.conname AS name,c.conrelid::regclass::text AS table_name,c.contype AS kind,c.convalidated AS validated FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname='public' ORDER BY c.conname"
        )
        indexes = await conn.fetch(
            "SELECT t.relname AS table_name,i.relname AS name,x.indisunique AS is_unique,x.indisvalid AS valid FROM pg_index x JOIN pg_class i ON i.oid=x.indexrelid JOIN pg_class t ON t.oid=x.indrelid JOIN pg_namespace n ON n.oid=t.relnamespace WHERE n.nspname='public' ORDER BY t.relname,i.relname"
        )
        policies = await conn.fetch(
            "SELECT tablename,policyname,roles,cmd FROM pg_policies WHERE schemaname='public' ORDER BY tablename,policyname"
        )
        access = await conn.fetchrow(
            "SELECT rolsuper AS superuser,rolbypassrls AS bypass_rls FROM pg_roles WHERE rolname=current_user"
        )
        issues = {}
        for child, key, parent, pk in RELATIONS:
            if (child, key) in available and (parent, pk) in available:
                issues[f"{child}.{key}->{parent}.{pk}"] = await conn.fetchval(
                    f"SELECT count(*) FROM public.{ident(child)} c WHERE c.{ident(key)} IS NOT NULL AND NOT EXISTS(SELECT 1 FROM public.{ident(parent)} p WHERE p.{ident(pk)}::text=c.{ident(key)}::text)"
                )
        if ("seasonplayers", "season_id") in available and (
            "seasonplayers",
            "user_id",
        ) in available:
            issues["duplicate_season_players"] = await conn.fetchval(
                "SELECT count(*) FROM (SELECT season_id,user_id FROM seasonplayers GROUP BY season_id,user_id HAVING count(*)>1) d"
            )
        if all(
            (t, k) in available
            for t, k in [
                ("match_history", "season_id"),
                ("match_history", "user_id"),
                ("seasonplayers", "season_id"),
                ("seasonplayers", "user_id"),
            ]
        ):
            issues["history_without_season_player"] = await conn.fetchval(
                "SELECT count(*) FROM match_history h WHERE NOT EXISTS(SELECT 1 FROM seasonplayers p WHERE p.season_id=h.season_id AND p.user_id::text=h.user_id::text)"
            )
        seasons = []
        if all(
            ("seasons", c) in available
            for c in ["id", "name", "active", "startdate", "enddate"]
        ):
            seasons = [
                dict(r)
                for r in await conn.fetch(
                    "SELECT id,name,active,startdate,enddate FROM seasons ORDER BY id"
                )
            ]
            issues["active_season_count"] = sum(s["active"] for s in seasons)
        missing = sorted(BASE_TABLES - set(counts))
        blockers = [f"missing_table:{t}" for t in missing]
        blockers += [f"invalid_index:{i['name']}" for i in indexes if not i["valid"]]
        blockers += [
            f"integrity:{k}"
            for k, v in issues.items()
            if v and k != "active_season_count"
        ]
        if issues.get("active_season_count") != 1:
            blockers.append("exactly_one_active_season_required")
        user_type = next(
            (
                r["data_type"]
                for r in columns
                if r["table_name"] == "players" and r["column_name"] == "user_id"
            ),
            None,
        )
        if user_type != "bigint":
            blockers.append("bot_requires_verified_bigint_user_ids")
        complete = bool(
            access["superuser"]
            or access["bypass_rls"]
            or all(t["owned"] or not t["rls"] for t in tables)
        )
        if not complete:
            blockers.append("counts_may_be_filtered_by_rls")
        return dict(
            source_fingerprint=fingerprint,
            read_only=True,
            counts=counts,
            counts_complete=complete,
            schema=[dict(r) for r in columns],
            tables=[dict(r) for r in tables],
            constraints=[dict(r) for r in constraints],
            indexes=[dict(r) for r in indexes],
            policies=[dict(r) for r in policies],
            integrity=issues,
            seasons=seasons,
            blockers=blockers,
            production_identity_verified=False,
        )
