from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from lounge.credentials import is_exposed

log = logging.getLogger(__name__)


def resolve_ca_file(candidate: str | Path | None = None) -> str | None:
    """Resolve an absolute, verified path to the Supabase CA certificate bundle.

    Searches in order:
    1. Explicit candidate path if provided.
    2. DB_CA_FILE environment variable.
    3. Standard repository / container locations:
       - <repo_root>/certs/supabase_ca_bundle.crt
       - <cwd>/certs/supabase_ca_bundle.crt
       - <lounge_dir>/certs/supabase_ca_bundle.crt
       - /home/container/certs/supabase_ca_bundle.crt (Wispbyte standard container path)
       - /var/task/certs/supabase_ca_bundle.crt (Vercel / AWS Lambda)
    Returns an absolute path string if found and is a file, else None.
    """
    repo_root = Path(__file__).resolve().parent.parent
    paths_to_check: list[Path] = []

    raw = candidate or os.getenv("DB_CA_FILE")
    if raw:
        p = Path(raw)
        paths_to_check.extend([
            p,
            repo_root / p,
            Path.cwd() / p,
        ])

    paths_to_check.extend([
        repo_root / "certs" / "supabase_ca_bundle.crt",
        Path.cwd() / "certs" / "supabase_ca_bundle.crt",
        Path(__file__).resolve().parent / "certs" / "supabase_ca_bundle.crt",
        Path("/home/container/certs/supabase_ca_bundle.crt"),
        Path("/var/task/certs/supabase_ca_bundle.crt"),
    ])

    for path in paths_to_check:
        try:
            resolved = path.resolve()
            if resolved.is_file():
                return str(resolved)
        except OSError:
            continue
    return None


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    pool_mode: str = "session"
    pool_max: int = 4
    ssl: bool = True
    ca_file: str | None = None
    guild_id: int = 0
    application_id: int = 0
    token: str = field(default="", repr=False)
    queues: tuple[dict, ...] = ()

    @classmethod
    def load(cls, *, api=False):
        if not api and is_exposed(os.getenv("DISCORD_TOKEN", "")):
            raise ValueError(
                "El token Discord configurado aparece en el historial Git. Rótalo antes de arrancar el bot."
            )
        canonical = os.getenv("DATABASE_URL") or os.getenv("DB_HOST") or ""
        url = (os.getenv("API_DATABASE_URL") or canonical) if api else canonical
        parts = urlsplit(url)
        if (
            parts.scheme not in ("postgres", "postgresql")
            or not parts.hostname
            or not parts.username
            or not parts.path.strip("/")
        ):
            raise ValueError(
                "Configura una URL PostgreSQL válida; no se admite SQLite en runtime"
            )
        mode = os.getenv("DB_POOL_MODE", "transaction" if api else "session")
        if mode not in ("direct", "session", "transaction"):
            raise ValueError("DB_POOL_MODE debe ser direct, session o transaction")
        local = parts.hostname in ("localhost", "127.0.0.1", "::1")
        use_ssl = os.getenv("DB_SSL", "true").lower() == "true"
        if not use_ssl and not local:
            raise ValueError("TLS obligatorio para PostgreSQL remoto")
        if not local and parse_qs(parts.query).get("sslmode", [""])[0] in (
            "disable",
            "allow",
            "prefer",
        ):
            raise ValueError("La URL remota no puede desactivar TLS")
        pool_max = int(os.getenv("DB_POOL_MAX", "1" if api else "4"))
        if not 1 <= pool_max <= (2 if api else 8):
            raise ValueError("Pool fuera de límites: API 1–2, bot 1–8")
        queues: tuple[dict, ...] = ()
        config = os.getenv("QUEUES_FILE")
        if config:
            queues = tuple(json.loads(Path(config).read_text(encoding="utf-8")))
            keys = [(int(q["guild_id"]), int(q["channel_id"])) for q in queues]
            if len(keys) != len(set(keys)) or any(g <= 0 or c <= 0 for g, c in keys):
                raise ValueError("Colas duplicadas o IDs inválidos")
        return cls(
            url,
            mode,
            pool_max,
            use_ssl,
            resolve_ca_file(os.getenv("DB_CA_FILE")),
            int(os.getenv("GUILD_ID") or "0"),
            int(os.getenv("APPLICATION_ID") or "0"),
            os.getenv("DISCORD_TOKEN", ""),
            queues,
        )

    def queue_key(self, guild_id, channel_id, channel_name):
        if self.queues:
            return (
                str(channel_id)
                if any(
                    int(q["guild_id"]) == guild_id
                    and int(q["channel_id"]) == channel_id
                    for q in self.queues
                )
                else None
            )
        # Preserve the only routing rule found, now scoped by guild and channel ID.
        return (
            str(channel_id)
            if guild_id == self.guild_id and channel_name == "join"
            else None
        )

    @property
    def guild_ids(self):
        return {self.guild_id} | {int(q["guild_id"]) for q in self.queues}
