"""Backup with pg_dump, restore into an empty local DB, compare every table.

Credentials go to child-process environment, never command arguments or stdout.
Stop the bot before creating a season-activation backup.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, unquote
import asyncpg
from dotenv import load_dotenv
from lounge.config import Settings
from lounge.database import Database
from scripts.activate_season_5 import fingerprint


def pg_env(url, settings):
    p = urlsplit(url)
    env = os.environ.copy()
    env.update(
        PGHOST=p.hostname,
        PGPORT=str(p.port or 5432),
        PGDATABASE=p.path.lstrip("/"),
        PGUSER=unquote(p.username or ""),
        PGPASSWORD=unquote(p.password or ""),
        PGSSLMODE="verify-full" if settings.ssl else "disable",
    )
    if settings.ca_file:
        env["PGSSLROOTCERT"] = settings.ca_file
    return env


async def run_tool(tool, args, env):
    process = await asyncio.create_subprocess_exec(
        tool,
        *args,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await process.communicate()
    if process.returncode:
        raise RuntimeError(
            f"{Path(tool).name} falló (código {process.returncode}); revisar conectividad, versión y permisos"
        )


async def table_hashes(c):
    tables = await c.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
    )
    result = {}
    for t in tables:
        name = t["tablename"].replace('"', '""')
        rows = await c.fetch(
            f'SELECT row_to_json(t)::text AS value FROM public."{name}" t ORDER BY row_to_json(t)::text'
        )
        result[t["tablename"]] = dict(
            count=len(rows),
            sha256=hashlib.sha256(
                "\n".join(r["value"] for r in rows).encode()
            ).hexdigest(),
        )
    return result


async def backup(db, destination, restore_url, bin_dir=""):
    path = Path(destination).resolve()
    if path.exists():
        raise ValueError("Backup destino ya existe")
    target = urlsplit(restore_url)
    if target.hostname not in (
        "127.0.0.1",
        "localhost",
        "::1",
    ) or not target.path.startswith("/pjsk_restore_"):
        raise ValueError("Restaurar solo en base local aislada pjsk_restore_*")
    if restore_url == db.settings.database_url:
        raise ValueError("Origen y destino de restauración deben diferir")
    path.parent.mkdir(parents=True, exist_ok=True)
    restore = await asyncpg.connect(restore_url, ssl=False)
    try:
        if await restore.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_tables WHERE schemaname NOT IN('pg_catalog','information_schema'))"
        ):
            raise ValueError("La base de restauración debe estar vacía")
        async with db.pool.acquire() as c, c.transaction():
            await c.fetchrow("SELECT id FROM seasons WHERE status='active' FOR UPDATE")
            if await c.fetchval("SELECT 1 FROM runtime_lease WHERE expires_at>now()"):
                raise ValueError("Detén el bot antes de backup de activación")
            before = await fingerprint(c)
            hashes = await table_hashes(c)
            # Managed Supabase schemas are outside this application's backup.
            policy_roles = await c.fetch(
                "SELECT DISTINCT unnest(roles)::text AS name FROM pg_policies WHERE schemaname='public'"
            )
            for row in policy_roles:
                role = row["name"]
                if role == "public":
                    continue
                if not await restore.fetchval(
                    "SELECT 1 FROM pg_roles WHERE rolname=$1", role
                ):
                    await restore.execute(
                        'CREATE ROLE "' + role.replace('"', '""') + '" NOLOGIN'
                    )
            tool = (
                lambda name: str(
                    Path(bin_dir) / (name + (".exe" if os.name == "nt" else ""))
                )
                if bin_dir
                else name
            )
            await run_tool(
                tool("pg_dump"),
                [
                    "--format=custom",
                    "--schema=public",
                    "--no-owner",
                    "--file",
                    str(path),
                ],
                pg_env(db.settings.database_url, db.settings),
            )
            local = Settings(restore_url, ssl=False)
            # Empty local pjsk_restore_* target has already been verified; no CASCADE.
            await restore.execute("DROP SCHEMA public")
            await run_tool(
                tool("pg_restore"),
                [
                    "--exit-on-error",
                    "--no-owner",
                    "--no-acl",
                    "--dbname",
                    target.path.lstrip("/"),
                    str(path),
                ],
                pg_env(restore_url, local),
            )
            restored = await table_hashes(restore)
            if hashes != restored:
                raise ValueError("Restauración difiere del origen")
            if before != await fingerprint(c):
                raise ValueError("La base cambió durante el backup")
            manifest = dict(
                format="postgres-custom-v1",
                file=path.name,
                created_at=datetime.now(timezone.utc).isoformat(),
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                state=before,
                tables=hashes,
                restore_verified=True,
            )
            path.with_suffix(".manifest.json").write_text(
                json.dumps(manifest, indent=2), encoding="utf-8"
            )
            return manifest
    finally:
        await restore.close()


async def main(args):
    db = Database(Settings.load())
    await db.open()
    try:
        manifest = await backup(
            db, args.destination, os.environ["RESTORE_DATABASE_URL"], args.bin_dir
        )
        print(
            json.dumps(
                {
                    "restore_verified": manifest["restore_verified"],
                    "tables": len(manifest["tables"]),
                    "sha256": manifest["sha256"],
                }
            )
        )
    finally:
        await db.close()


if __name__ == "__main__":
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("destination")
    p.add_argument("--bin-dir", default="")
    asyncio.run(main(p.parse_args()))
