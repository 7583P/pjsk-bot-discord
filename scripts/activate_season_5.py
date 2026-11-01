"""Explicit, transactional season launch. Dry run by default; no startup hook."""

import argparse
import asyncio
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
from dotenv import load_dotenv
from lounge.config import Settings
from lounge.database import Database


async def activate(db, *, execute=False, backup_manifest=None):
    async with db.pool.acquire() as c, c.transaction():
        await c.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('pjsk:season',0))"
        )
        # Exclusive lock drains existing competitive transactions (which hold FOR SHARE).
        current = await c.fetchrow(
            "SELECT * FROM seasons WHERE status='active' FOR UPDATE"
        )
        if not current:
            raise ValueError("Sin temporada activa; importar y auditar primero")
        if await c.fetchval("SELECT 1 FROM seasons WHERE id='5' OR number=5"):
            raise ValueError("Season 5 ya existe: no se permite un segundo reset")
        if await c.fetchval("SELECT 1 FROM runtime_lease WHERE expires_at>now()"):
            raise ValueError("Detén el bot y drena operaciones antes de activar")
        if await c.fetchval(
            "SELECT 1 FROM rooms WHERE status NOT IN('finished','closed')"
        ):
            raise ValueError(
                "Hay salas pendientes; resolverlas antes de cerrar temporada"
            )
        summary = dict(
            previous_season=current["id"],
            players=await c.fetchval("SELECT count(*) FROM players"),
            previous_mmr_total=int(
                await c.fetchval(
                    "SELECT coalesce(sum(mmr),0) FROM season_players WHERE season_id=$1",
                    current["id"],
                )
            ),
            new_season="Season 5 (New Era)",
            new_mmr=0,
            execute=execute,
        )
        if not execute:
            return summary
        if not backup_manifest:
            raise ValueError("Se requiere manifest de pg_dump restaurado y verificado")
        mpath = Path(backup_manifest).resolve()
        m = json.loads(mpath.read_text(encoding="utf-8"))
        dump = mpath.parent / m["file"]
        if (
            m.get("format") != "postgres-custom-v1"
            or not m.get("restore_verified")
            or hashlib.sha256(dump.read_bytes()).hexdigest() != m["sha256"]
        ):
            raise ValueError("Backup PostgreSQL no verificado")
        # Bind the backup to this database state, not an unrelated or old dump.
        state = await fingerprint(c)
        if m.get("state") != state:
            raise ValueError("La base cambió después del backup; crear otro")
        timestamp = datetime.fromisoformat(m["created_at"])
        if datetime.now(timezone.utc) - timestamp > timedelta(hours=24):
            raise ValueError("Backup demasiado antiguo")
        await c.execute(
            "UPDATE seasons SET status='archived',ended_at=now() WHERE id=$1",
            current["id"],
        )
        await c.execute(
            "INSERT INTO seasons(id,number,name,status,legacy_placement,started_at) VALUES('5',5,'Season 5 (New Era)','active',false,now())"
        )
        await c.execute(
            "INSERT INTO season_players(season_id,user_id,mmr,role,placement_pending,peak_mmr) SELECT '5',user_id,0,'Placement',false,0 FROM players"
        )
        bad = await c.fetchval(
            "SELECT count(*) FROM season_players WHERE season_id='5' AND (mmr<>0 OR placement_pending OR matches_played<>0 OR wins<>0 OR losses<>0 OR peak_mmr<>0)"
        )
        if bad:
            raise RuntimeError("Validación del reset falló; rollback")
        # All configured Discord guilds get an eventual role reconciliation.
        guilds = {int(q["guild_id"]) for q in db.settings.queues} or {
            db.settings.guild_id
        }
        for guild in guilds:
            if guild:
                await c.execute(
                    "INSERT INTO role_jobs(guild_id,user_id) SELECT $1,user_id FROM players ON CONFLICT(guild_id,user_id) DO UPDATE SET next_attempt=now(),attempts=0",
                    guild,
                )
        summary["backup_sha256"] = m["sha256"]
        await c.execute(
            "INSERT INTO system_operations(id,kind,details) VALUES('activate_season_5','season_activation',$1)",
            json.dumps(summary),
        )
        return summary


async def fingerprint(c):
    return dict(
        players=await c.fetchval("SELECT count(*) FROM players"),
        standings=await c.fetchval(
            "SELECT md5(coalesce(string_agg(concat_ws('|',season_id,user_id,mmr,role,placement_pending,peak_mmr,matches_played,wins,losses),';' ORDER BY season_id,user_id),'')) FROM season_players"
        ),
        profiles=await c.fetchval(
            "SELECT md5(coalesce(string_agg(concat_ws('|',user_id,name,country,legacy_data),';' ORDER BY user_id),'')) FROM players"
        ),
        matches=await c.fetchval("SELECT count(*) FROM matches"),
        active_season=await c.fetchval("SELECT id FROM seasons WHERE status='active'"),
    )


async def main(args):
    db = Database(Settings.load())
    await db.open()
    try:
        print(
            json.dumps(
                await activate(
                    db, execute=args.execute, backup_manifest=args.backup_manifest
                ),
                indent=2,
            )
        )
    finally:
        await db.close()


if __name__ == "__main__":
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("--execute", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--backup-manifest")
    args = p.parse_args()
    if args.execute and args.dry_run:
        p.error("Elige dry-run o execute")
    asyncio.run(main(args))
