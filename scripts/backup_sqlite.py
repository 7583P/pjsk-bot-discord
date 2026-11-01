"""Consistent online backup of the supplied legacy database; source opened read-only."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from contextlib import closing


def backup(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or destination.exists():
        raise ValueError("El destino debe ser nuevo")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src,
        closing(sqlite3.connect(destination)) as dst,
    ):
        src.backup(dst)
        if dst.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Backup SQLite inválido")
        counts = {
            t: dst.execute('SELECT count(*) FROM "' + t + '"').fetchone()[0]
            for (t,) in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        schema = list(
            dst.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        )
    # Restore to a separate database and validate counts/schema/integrity, not just file existence.
    with tempfile.TemporaryDirectory() as tmp:
        restore = Path(tmp) / "restored.db"
        with (
            closing(
                sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True)
            ) as src,
            closing(sqlite3.connect(restore)) as dst,
        ):
            src.backup(dst)
            assert dst.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert (
                list(
                    dst.execute(
                        "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
                    )
                )
                == schema
            )
            for table, count in counts.items():
                assert (
                    dst.execute('SELECT count(*) FROM "' + table + '"').fetchone()[0]
                    == count
                )
    manifest = dict(
        created_at=datetime.now(timezone.utc).isoformat(),
        file=destination.name,
        sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
        counts=counts,
        schema=schema,
        restore_verified=True,
        format="sqlite-online-backup-v1",
    )
    destination.with_suffix(".manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("destination")
    args = parser.parse_args()
    print(json.dumps(backup(args.source, args.destination), ensure_ascii=True))
