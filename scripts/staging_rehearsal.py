"""Exercise the supplied snapshot on disposable local PostgreSQL, never on production."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import uuid
import asyncpg
from lounge.config import Settings
from lounge.database import Database
from scripts.migrate import migrate
from scripts.import_legacy import import_legacy
from scripts.backup_postgres import backup
from scripts.activate_season_5 import activate
from scripts.benchmark import benchmark


async def rehearse(args):
    admin_url = os.environ["TEST_DATABASE_ADMIN_URL"]
    parts = urlsplit(admin_url)
    if parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Solo se admite servidor local")
    suffix = uuid.uuid4().hex
    name = "pjsk_test_rehearsal_" + suffix
    restored = "pjsk_restore_" + suffix
    admin = await asyncpg.connect(admin_url, ssl=False)
    db = None
    try:
        await admin.execute(f"CREATE DATABASE {name}")
        await admin.execute(f"CREATE DATABASE {restored}")
        settings = Settings(urlunsplit(parts._replace(path="/" + name)), ssl=False)
        await migrate(settings)
        db = Database(settings)
        await db.open()
        imported = await import_legacy(db, args.snapshot, args.active_label)
        repeated = await import_legacy(db, args.snapshot, args.active_label)
        async with db.pool.acquire() as c:
            counts = {
                t: await c.fetchval(f"SELECT count(*) FROM {t}")
                for t in ["players", "songs", "legacy_placements"]
            }
            before = await c.fetchval(
                "SELECT md5(string_agg(legacy_data::text,'' ORDER BY user_id)) FROM players"
            )
        performance = await benchmark(db)
        destination = Path("backups") / f"staging-{suffix}.dump"
        manifest = await backup(
            db,
            destination,
            urlunsplit(parts._replace(path="/" + restored)),
            os.getenv("PG_BIN_DIR", ""),
        )
        dry_run = await activate(db)
        activation = await activate(
            db, execute=True, backup_manifest=destination.with_suffix(".manifest.json")
        )
        async with db.pool.acquire() as c:
            after = await c.fetchval(
                "SELECT md5(string_agg(legacy_data::text,'' ORDER BY user_id)) FROM players"
            )
            season = await c.fetchrow(
                "SELECT name,started_at FROM seasons WHERE id='5'"
            )
            zeros = await c.fetchval(
                "SELECT count(*) FROM season_players WHERE season_id='5' AND mmr=0 AND NOT placement_pending AND peak_mmr=0 AND matches_played=0 AND wins=0 AND losses=0"
            )
            archive = await c.fetchval(
                "SELECT count(*) FROM season_players WHERE season_id=$1",
                args.active_label,
            )
        assert (
            imported
            and not repeated
            and before == after
            and zeros == counts["players"]
            and archive == counts["players"]
        )
        report = dict(
            environment="disposable local PostgreSQL; production unchanged",
            created_at=datetime.now(timezone.utc).isoformat(),
            imported=counts,
            import_idempotent=not repeated,
            backup_restored_and_compared=manifest["restore_verified"],
            backup_sha256=manifest["sha256"],
            preserved_legacy_profiles=before == after,
            dry_run=dry_run,
            activation=activation,
            zero_mmr_without_placement=zeros,
            archived_players=archive,
            season=dict(
                name=season["name"], staging_started_at=season["started_at"].isoformat()
            ),
            performance=performance,
        )
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {k: v for k, v in report.items() if k != "performance"}, indent=2
            )
        )
    finally:
        if db:
            await db.close()
        for target in [name, restored]:
            await admin.execute(f"DROP DATABASE IF EXISTS {target} WITH (FORCE)")
        await admin.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot")
    parser.add_argument("--active-label", required=True)
    parser.add_argument("--output", default="docs/EVIDENCE.json")
    asyncio.run(rehearse(parser.parse_args()))
