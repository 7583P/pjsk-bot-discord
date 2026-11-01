"""Import a verified snapshot into an empty PostgreSQL database. No overwrites."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
from dotenv import load_dotenv
from lounge.config import Settings
from lounge.database import Database


async def import_legacy(db, snapshot, active_label):
    path = Path(snapshot).resolve()
    manifest = json.loads(
        path.with_suffix(".manifest.json").read_text(encoding="utf-8")
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != manifest["sha256"] or not manifest.get("restore_verified"):
        raise ValueError("Backup sin verificación de restauración válida")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as legacy:
        legacy.row_factory = sqlite3.Row
        players = [dict(r) for r in legacy.execute("SELECT * FROM players")]
        songs = [dict(r) for r in legacy.execute("SELECT * FROM songs")]
        placements = [dict(r) for r in legacy.execute("SELECT * FROM placements")]
    labels = {p["season"] or "unknown" for p in players}
    if active_label not in labels:
        raise ValueError("La temporada activa debe existir en la fuente auditada")
    async with db.pool.acquire() as c, c.transaction():
        await c.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('pjsk:import',0))"
        )
        op = "legacy:" + digest
        if await c.fetchval("SELECT 1 FROM system_operations WHERE id=$1", op):
            return False
        if await c.fetchval(
            "SELECT EXISTS(SELECT 1 FROM players) OR EXISTS(SELECT 1 FROM seasons)"
        ):
            raise ValueError("Importar solo en destino vacío; no sobrescribir datos")
        for label in sorted(labels):
            await c.execute(
                "INSERT INTO seasons(id,name,status) VALUES($1,$2,$3)",
                label,
                label,
                "active" if label == active_label else "archived",
            )
        for p in players:
            await c.execute(
                "INSERT INTO players(user_id,name,country,legacy_data) VALUES($1,$2,$3,$4)",
                p["user_id"],
                p["name"] or "",
                p["country"] or "",
                json.dumps(p),
            )
            await c.execute(
                "INSERT INTO season_players(season_id,user_id,mmr,role,placement_pending) VALUES($1,$2,$3,$4,$5)",
                p["season"] or "unknown",
                p["user_id"],
                p["mmr"],
                p["role"],
                p["mmr"] == 0 and p["role"] == "Placement",
            )
        await c.executemany(
            "INSERT INTO songs(id,title,diff,level) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING",
            [(p["id"], p["title"], p["diff"], p["level"]) for p in songs],
        )
        await c.executemany(
            "INSERT INTO legacy_placements(user_id,data) VALUES($1,$2)",
            [(p["user_id"], json.dumps(p)) for p in placements],
        )
        if await c.fetchval("SELECT count(*) FROM players") != len(players):
            raise RuntimeError("Recuento de importación incorrecto")
        await c.execute(
            "INSERT INTO system_operations(id,kind,details) VALUES($1,'legacy_import',$2)",
            op,
            json.dumps(manifest),
        )
    return True


async def main(args):
    db = Database(Settings.load())
    await db.open()
    try:
        print("imported:", await import_legacy(db, args.snapshot, args.active_label))
    finally:
        await db.close()


if __name__ == "__main__":
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("snapshot")
    p.add_argument("--active-label", required=True)
    asyncio.run(main(p.parse_args()))
