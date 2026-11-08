"""Season 5 Extraordinary Reset Activation.

Safely and transactionally executes the Season 5 (New Era) reset on Supabase.
"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import sys
from pathlib import Path
import re
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg
from dotenv import dotenv_values, load_dotenv

from lounge.config import Settings


async def fingerprint(conn):
    tables = await conn.fetch("""
        SELECT tablename FROM pg_tables 
        WHERE schemaname='public' 
        AND tablename NOT IN (
            'lounge_runtime_lease', 
            'lounge_oauth_states', 
            'lounge_revoked_tokens', 
            'lounge_role_jobs',
            'lounge_queue_state',
            'lounge_room_state',
            'lounge_submissions',
            'lounge_room_members'
        )
        ORDER BY tablename
    """)
    state = {}
    for table in tables:
        name = table["tablename"].replace('"', '""')
        rows = await conn.fetch(
            f'SELECT to_jsonb(t)::text AS value FROM "{name}" t ORDER BY to_jsonb(t)::text'
        )
        state[table["tablename"]] = dict(
            count=len(rows),
            sha256=hashlib.sha256(
                "\n".join(r["value"] for r in rows).encode()
            ).hexdigest(),
        )
    return state


async def activate(
    conn, *, execute=False, backup_manifest=None, database_url="", guild_id=1371306026917298267
):
    async with conn.transaction():
        # 1. Acquire transactional advisory lock to fence all concurrent season actions
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('pjsk:season-activation',0))"
        )
        
        # 2. Inspect existing seasons
        seasons = await conn.fetch("SELECT * FROM seasons ORDER BY id FOR UPDATE")
        if any(re.search(r"\bseason\s*5\b", s["name"], re.I) for s in seasons):
            raise ValueError("Season 5 already exists; reset rejected")
        active = [s for s in seasons if s["active"]]
        if len(active) != 1:
            raise ValueError("Exactly one verified current season required")
        old = active[0]

        # 3. Check worker exclusivity: ensure no duplicate competing workers
        active_leases = await conn.fetchval(
            "SELECT count(DISTINCT owner) FROM lounge_runtime_lease WHERE expires_at > now()"
        )
        if active_leases > 1:
            raise ValueError("Multiple active bot workers detected; worker exclusivity violated")

        # 4. Check for pending competitive matches or rooms
        if await conn.fetchval(
            "SELECT 1 FROM lounge_room_state WHERE status IN ('active', 'needs_attention')"
        ):
            raise ValueError("Resolve pending rooms before activation")
        if await conn.fetchval(
            "SELECT 1 FROM lounge_queue_state WHERE jsonb_array_length(payload->'members') > 0"
        ):
            raise ValueError("Drain matchmaking queues before activation")
        if await conn.fetchval(
            "SELECT 1 FROM lounge_submissions WHERE publication_status = 'pending'"
        ):
            raise ValueError("Resolve pending result publications before activation")

        total_players = await conn.fetchval("SELECT count(*) FROM players")
        report = dict(
            previous_season_id=old["id"],
            previous_season_name=old["name"],
            players=total_players,
            previous_mmr_total=int(
                await conn.fetchval(
                    "SELECT coalesce(sum(mmr),0) FROM seasonplayers WHERE season_id=$1",
                    old["id"],
                )
            ),
            new_season="Season 5 (New Era)",
            placement_policy="extraordinary_new_era",
            execute=execute,
        )
        if not execute:
            return report

        # 5. Destination validation: Supabase or authorized test database
        target = urlsplit(database_url)
        authorized_hosts = (
            "localhost",
            "127.0.0.1",
            "::1",
            "aws-1-sa-east-1.pooler.supabase.com",
            "db.pbctytoygckgwdkymsrb.supabase.co",
        )
        if target.hostname not in authorized_hosts and "supabase" not in (target.hostname or ""):
            raise ValueError(f"Target host '{target.hostname}' is not authorized for Season 5 activation")

        # 6. Verify backup manifest
        if not backup_manifest:
            raise ValueError("Verified restored PostgreSQL backup manifest required")
        manifest_path = Path(backup_manifest).resolve()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        dump = manifest_path.parent / manifest["file"]
        if (
            manifest.get("format") != "postgres-custom-v1"
            or not manifest.get("restore_verified")
            or hashlib.sha256(dump.read_bytes()).hexdigest() != manifest["sha256"]
        ):
            raise ValueError("Backup verification failed")
        age = datetime.now(timezone.utc) - datetime.fromisoformat(manifest["created_at"])
        if not timedelta(0) <= age <= timedelta(hours=24):
            raise ValueError("Backup outside valid time window")

        # 7. Check database fingerprint consistency
        current_fp = await fingerprint(conn)
        manifest_fp = manifest.get("state", {})
        for tbl, expected in manifest_fp.items():
            if tbl not in current_fp:
                raise ValueError(f"Table '{tbl}' missing from current database")
            if current_fp[tbl]["count"] != expected["count"] or current_fp[tbl]["sha256"] != expected["sha256"]:
                raise ValueError(f"Table '{tbl}' differs from verified backup")

        # 8. Archive Season 4 standings and player snapshot
        archived = await conn.fetch(
            "SELECT to_jsonb(sp) AS row FROM seasonplayers sp WHERE season_id=$1 ORDER BY user_id",
            old["id"],
        )
        archive_players = await conn.fetch(
            "SELECT to_jsonb(p) AS row FROM players p ORDER BY user_id"
        )
        await conn.execute(
            "INSERT INTO lounge_operations(id, kind, payload) VALUES($1, $2, $3)",
            f"archive-season:{old['id']}",
            "season_archive",
            json.dumps(
                {
                    "season": dict(old),
                    "standings": [json.loads(r["row"]) for r in archived],
                    "players": [json.loads(r["row"]) for r in archive_players],
                },
                default=str,
            ),
        )

        # 9. Grant finished season titles for Season 4 idempotently
        from cogs.matchmaking import Matchmaking
        cog = object.__new__(Matchmaking)
        await cog._grant_finished_season_titles(conn, int(old["id"]), str(old["name"]))

        # 10. Archive Season 4
        await conn.execute(
            "UPDATE seasons SET active=false, enddate=coalesce(enddate, now()) WHERE id=$1",
            old["id"],
        )

        # 11. Insert Season 5 (New Era)
        new_id = await conn.fetchval(
            "INSERT INTO seasons(id, name, active, startdate, enddate) SELECT coalesce(max(id),0)+1, 'Season 5 (New Era)', true, now(), NULL FROM seasons RETURNING id"
        )
        await conn.execute(
            "SELECT setval(pg_get_serial_sequence('seasons','id'), greatest($1, coalesce((SELECT max(id) FROM seasons), 1)), true)",
            new_id,
        )

        # 12. Register Season 5 extraordinary reset policy
        await conn.execute(
            "INSERT INTO lounge_season_policy(season_id, reset_kind, placement_enabled) VALUES($1, 'extraordinary_new_era', true) ON CONFLICT (season_id) DO UPDATE SET reset_kind=excluded.reset_kind, placement_enabled=excluded.placement_enabled",
            new_id,
        )

        # 13. Populate all 1,403 players into Season 5 at strictly 0 MMR, Placement
        await conn.execute(
            """INSERT INTO seasonplayers(season_id, user_id, points, mmr, role, is_season_placement, starting_mmr)
            SELECT $1, p.user_id, 0, 0, 'Placement', false, NULL
            FROM players p""",
            new_id,
        )

        # 14. Update players table
        await conn.execute("UPDATE players SET mmr=0, role='Placement'")

        # 15. Enqueue Discord competitive roles reconciliation
        configured_guild = guild_id or 1371306026917298267
        if configured_guild:
            await conn.execute(
                """INSERT INTO lounge_role_jobs(guild_id, user_id, role)
                SELECT $1, user_id::text, 'Placement'
                FROM seasonplayers WHERE season_id=$2
                ON CONFLICT(guild_id, user_id) DO UPDATE SET role=excluded.role, attempts=0, next_attempt=now()""",
                configured_guild,
                new_id,
            )

        # 16. Postcondition validation
        s5_count = await conn.fetchval("SELECT count(*) FROM seasonplayers WHERE season_id=$1", new_id)
        if s5_count != total_players:
            raise RuntimeError(f"Expected {total_players} players in Season 5, found {s5_count}")

        bad = await conn.fetchval(
            "SELECT count(*) FROM seasonplayers WHERE season_id=$1 AND (mmr<>0 OR coalesce(starting_mmr,0)<>0 OR points<>0 OR role<>'Placement' OR is_season_placement<>false)",
            new_id,
        )
        if bad:
            raise RuntimeError(f"Season 5 postconditions failed: {bad} anomalous players")

        bad_players = await conn.fetchval(
            "SELECT count(*) FROM players WHERE mmr<>0 OR role<>'Placement'"
        )
        if bad_players:
            raise RuntimeError(f"players table postconditions failed: {bad_players} non-zero MMR or non-Placement")

        report.update(
            new_season_id=new_id,
            s5_players_enrolled=s5_count,
            backup_sha256=manifest["sha256"],
            status="SUCCESS",
        )
        await conn.execute(
            "INSERT INTO lounge_operations(id, kind, payload) VALUES('activate_season_5', 'season_activation', $1)",
            json.dumps(report),
        )
        return report


async def main(args):
    # Load credentials
    sec_env = r"C:\Users\Usuario\.pjsk_secure_backups\lounge_credentials.env"
    if os.path.exists(sec_env):
        creds = dotenv_values(sec_env)
        for k, v in creds.items():
            if v and k not in os.environ:
                os.environ[k] = v

    db_url = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_POOLER_URL") or os.getenv("SUPABASE_DATABASE_URL")
    if not db_url:
        raise ValueError("No database URL configured")

    manifest_file = args.backup_manifest
    if not manifest_file:
        default_manifest = Path(r"D:\Projects\PJSK Bot\backups\supabase_pre_s5.manifest.json")
        if default_manifest.exists():
            manifest_file = str(default_manifest)

    # Connect directly with statement_cache_size=0 for Supavisor compatibility
    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        result = await activate(
            conn,
            execute=args.execute,
            backup_manifest=manifest_file,
            database_url=db_url,
            guild_id=int(os.getenv("DISCORD_GUILD_ID", 1371306026917298267)),
        )
        print(json.dumps(result, indent=2))
    finally:
        await conn.close()


if __name__ == "__main__":
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("--execute", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--backup-manifest")
    args = p.parse_args()
    if args.execute and args.dry_run:
        p.error("Choose dry-run or execute")
    asyncio.run(main(args))
