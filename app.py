import os
import re
import json
import hmac
import base64
import hashlib
import secrets
import traceback
import asyncio
import random
import unicodedata
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple, Any
from urllib.parse import urlparse, unquote, quote

import asyncpg
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel  # para el modelo de canciones

load_dotenv()

# ========================= LOGGING =========================
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("app")

# ========================= ENV =========================
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")

# IMPORTANTE:
# Después del proxy de Vercel, Discord debe volver a Vercel, no directo a Railway.
DISCORD_REDIRECT_URI = os.getenv(
    "DISCORD_REDIRECT_URI",
    "https://pjsk-page.vercel.app/auth02/callback",
)

# Soporta FRONTEND_URL y mantiene FRONT_URL por compatibilidad con tu env anterior.
FRONT_URL = os.getenv("FRONTEND_URL") or os.getenv("FRONT_URL", "https://pjsk-page.vercel.app")
CORS_ORIGIN = os.getenv("CORS_ORIGIN", FRONT_URL)

# Cookie firmada. Ya no dependemos de SESSIONS en memoria.
COOKIE_NAME = os.getenv("COOKIE_NAME", "pjsk_session")
SESSION_COOKIE_NAME = COOKIE_NAME
SESSION_MAX_AGE = int(os.getenv("SESSION_MAX_AGE_SECONDS", str(60 * 60 * 24 * 30)))  # 30 días
SESSION_SECRET = os.getenv("SESSION_SECRET") or DISCORD_CLIENT_SECRET

DATABASE_URL = os.getenv("DATABASE_URL", "")

# ===================== FastAPI =====================
app = FastAPI()


# ============================================================
# PJSK DEPLOY CHECK - TEMPORARY DEBUG
# ============================================================
PJSK_DEPLOY_VERSION = "PJSK_BACKEND_AUTH_PFP_LINK_V8_2026_06_16"

print("======================================================")
print("[PJSK DEPLOY CHECK] app.py loaded")
print(f"[PJSK DEPLOY CHECK] version = {PJSK_DEPLOY_VERSION}")
print(f"[PJSK DEPLOY CHECK] file = {os.path.abspath(__file__)}")
print("======================================================")


@app.get("/auth02/debug-version")
async def auth02_debug_version():
    return {
        "version": PJSK_DEPLOY_VERSION,
        "file": os.path.abspath(__file__),
        "frontUrl": FRONT_URL,
        "discordRedirectUri": DISCORD_REDIRECT_URI,
        "sessionCookieName": SESSION_COOKIE_NAME,
        "has_pjsk_token": True,
        "has_query_token": True,
        "has_hash_token": True,
        "has_bearer_auth": True,
        "has_link_candidates": True,
        "has_link_player": True,
        "has_player_not_linked": True,
    }


@app.get("/api/debug-version")
async def api_debug_version():
    return {
        "version": PJSK_DEPLOY_VERSION,
        "file": os.path.abspath(__file__),
        "api_rewrite_ok": True,
    }


@app.get("/auth02/debug-request")
async def auth02_debug_request(request: Request):
    auth_header = request.headers.get("authorization") or ""
    cookie_header = request.headers.get("cookie") or ""
    token_candidates = _candidate_session_tokens_from_request(request)
    valid_candidate_count = 0
    for candidate in token_candidates:
        if read_session_token(candidate):
            valid_candidate_count += 1

    return _no_store(
        JSONResponse(
            {
                "version": PJSK_DEPLOY_VERSION,
                "has_authorization_header": bool(auth_header),
                "authorization_starts_with_bearer": auth_header.lower().startswith("bearer "),
                "authorization_length": len(auth_header),
                "has_cookie_header": bool(cookie_header),
                "cookie_length": len(cookie_header),
                "has_pjsk_session_cookie": SESSION_COOKIE_NAME in request.cookies,
                "session_cookie_name": SESSION_COOKIE_NAME,
                "token_candidate_count": len(token_candidates),
                "valid_candidate_count": valid_candidate_count,
            }
        )
    )


@app.get("/auth02/debug-routes")
async def auth02_debug_routes():
    routes = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if "/auth02" in path or "/api/debug" in path:
            routes.append(
                {
                    "path": path,
                    "methods": sorted(list(getattr(route, "methods", []) or [])),
                }
            )
    return _no_store(JSONResponse({"version": PJSK_DEPLOY_VERSION, "routes": routes}))

_allowed_origins = []
for raw in [FRONT_URL, CORS_ORIGIN]:
    for o in str(raw or "").split(","):
        o = o.strip()
        if o and o not in _allowed_origins:
            _allowed_origins.append(o)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ================ Sesión firmada en cookie ================
# Antes esto usaba SESSIONS en memoria. Eso falla en Railway porque el proceso puede
# reiniciar, usar otro worker o perder memoria. Ahora /auth02/me lee la sesión desde
# una cookie firmada, persistente y verificable.

def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def make_session_token(user: Dict[str, Any]) -> str:
    if not SESSION_SECRET:
        raise HTTPException(500, "SESSION_SECRET or DISCORD_CLIENT_SECRET is not configured")

    now = int(datetime.now(timezone.utc).timestamp())
    payload = {
        "iat": now,
        "exp": now + SESSION_MAX_AGE,
        "user": user,
    }

    payload_raw = json.dumps(
        payload,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    payload_b64 = _b64url_encode(payload_raw)
    signature = hmac.new(
        SESSION_SECRET.encode("utf-8"),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    return f"{payload_b64}.{_b64url_encode(signature)}"


def read_session_token(token: Optional[str]) -> Optional[Dict[str, Any]]:
    if not token or "." not in token or not SESSION_SECRET:
        return None

    try:
        payload_b64, signature_b64 = token.split(".", 1)

        expected_signature = hmac.new(
            SESSION_SECRET.encode("utf-8"),
            payload_b64.encode("utf-8"),
            hashlib.sha256,
        ).digest()

        received_signature = _b64url_decode(signature_b64)

        if not hmac.compare_digest(expected_signature, received_signature):
            return None

        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))

        if int(payload.get("exp", 0)) < int(datetime.now(timezone.utc).timestamp()):
            return None

        user = payload.get("user")
        return user if isinstance(user, dict) else None

    except Exception:
        return None


def set_session_cookie(resp, session_user: Dict[str, Any], token: Optional[str] = None) -> str:
    """
    Guarda la sesión en cookie y devuelve el mismo token.
    El token también se usa como fallback móvil porque algunos WebViews no conservan
    cookies SameSite=None después del redirect de Discord.
    """
    token = token or make_session_token(session_user)
    resp.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=True,
        samesite="none",
        path="/",
        max_age=SESSION_MAX_AGE,
    )
    return token


def clear_session_cookie(resp):
    resp.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        secure=True,
        samesite="none",
    )


def _candidate_session_tokens_from_request(request: Request) -> List[str]:
    """
    Devuelve todos los tokens posibles.

    Importante: si existe una cookie vieja/expirada, NO debe bloquear el Bearer token.
    Por eso get_session_from_request prueba cada candidato hasta encontrar uno válido.
    """
    out: List[str] = []

    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_token:
        out.append(cookie_token)

    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        bearer = auth.split(" ", 1)[1].strip()
        if bearer:
            out.append(bearer)

    x_token = (request.headers.get("x-pjsk-session") or "").strip()
    if x_token:
        out.append(x_token)

    # Dedup manteniendo orden.
    seen = set()
    deduped: List[str] = []
    for token in out:
        if token and token not in seen:
            seen.add(token)
            deduped.append(token)
    return deduped


def get_session_token_from_request(request: Request) -> Optional[str]:
    """
    Compatibilidad con código anterior: devuelve el primer token recibido.
    Para validar sesión real usar get_session_from_request().
    """
    candidates = _candidate_session_tokens_from_request(request)
    return candidates[0] if candidates else None


def get_session_from_request(request: Request) -> Optional[Dict[str, Any]]:
    for token in _candidate_session_tokens_from_request(request):
        session = read_session_token(token)
        if session:
            return session
    return None


def require_auth_session(request: Request) -> Dict[str, Any]:
    session = get_session_from_request(request)
    if not session:
        raise HTTPException(status_code=401, detail="no-session")
    return session


def _no_store(resp: JSONResponse) -> JSONResponse:
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


def _rget(rec: Any, key: str, default=None):
    try:
        return rec[key]
    except Exception:
        return default


# =========================== DB & Schema Flags ===========================
db_pool: Optional[asyncpg.pool.Pool] = None

HAS_DISCORD_ID_COL: bool = False
PLAYERS_HAS_IN_GUILD: bool = False  # <--- NUEVO

SEASONS_HAS_ACTIVE: bool = False
SEASONS_HAS_NAME: bool = False
SEASONS_HAS_STARTS_AT: bool = False
SEASONS_HAS_ENDS_AT: bool = False

# seasonplayers extra stats
SP_HAS_WINS: bool = False
SP_HAS_LOSSES: bool = False
SP_HAS_GAMES_PLAYED: bool = False

# matches / match_history
HAS_MATCHES_TABLE: bool = False
HAS_MATCHPLAYERS_TABLE: bool = False  # legacy, no se usa ya

# results: columna de texto donde está el bloque con songs/diffs
RESULTS_TEXT_COL: Optional[str] = None

# Tipo SQL efectivo del user_id / discord_id ("text" o "bigint")
USER_ID_SQLTYPE: str = "text"  # default seguro
DISCORD_ID_SQLTYPE: str = "text"  # default seguro


async def _has_col(conn: asyncpg.Connection, table: str, column: str) -> bool:
    row = await conn.fetchrow(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=$1 AND column_name=$2
    """,
        table,
        column,
    )
    return bool(row)


async def _col_type(conn: asyncpg.Connection, table: str, column: str) -> Optional[str]:
    row = await conn.fetchrow(
        """
        SELECT data_type
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=$1 AND column_name=$2
        LIMIT 1
    """,
        table,
        column,
    )
    if not row:
        return None
    dt = str(row["data_type"]).lower()
    if "bigint" in dt or "int8" in dt:
        return "bigint"
    return "text"


async def _detect_schema(conn: asyncpg.Connection) -> None:
    global HAS_DISCORD_ID_COL, PLAYERS_HAS_IN_GUILD
    global SEASONS_HAS_ACTIVE, SEASONS_HAS_NAME
    global SEASONS_HAS_STARTS_AT, SEASONS_HAS_ENDS_AT, USER_ID_SQLTYPE, DISCORD_ID_SQLTYPE
    global SP_HAS_WINS, SP_HAS_LOSSES, SP_HAS_GAMES_PLAYED
    global HAS_MATCHES_TABLE, HAS_MATCHPLAYERS_TABLE, RESULTS_TEXT_COL

    try:
        # players / seasons
        HAS_DISCORD_ID_COL = await _has_col(conn, "players", "discord_id")
        DISCORD_ID_SQLTYPE = (await _col_type(conn, "players", "discord_id")) or "text"
        PLAYERS_HAS_IN_GUILD = await _has_col(conn, "players", "in_guild")
        SEASONS_HAS_ACTIVE = await _has_col(conn, "seasons", "active")
        SEASONS_HAS_NAME = await _has_col(conn, "seasons", "name")
        SEASONS_HAS_STARTS_AT = await _has_col(conn, "seasons", "starts_at")
        SEASONS_HAS_ENDS_AT = await _has_col(conn, "seasons", "ends_at")

        # seasonplayers: columnas de stats
        SP_HAS_WINS = await _has_col(conn, "seasonplayers", "wins")
        SP_HAS_LOSSES = await _has_col(conn, "seasonplayers", "losses")
        SP_HAS_GAMES_PLAYED = (
            await _has_col(conn, "seasonplayers", "games_played")
        ) or (await _has_col(conn, "seasonplayers", "games"))

        # matches / match_history
        HAS_MATCHES_TABLE = await _has_col(conn, "match_history", "season_id")
        HAS_MATCHPLAYERS_TABLE = False  # ya no la vamos a usar

        # Detectar tipo de user_id: si player_pfps existe usa su tipo; si no, usa el de players
        pfps_type = await _col_type(conn, "player_pfps", "user_id")
        players_type = await _col_type(conn, "players", "user_id")
        USER_ID_SQLTYPE = pfps_type or players_type or "text"

        # Detectar automáticamente una columna de texto en public.results
        row = await conn.fetchrow(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public'
              AND table_name='results'
              AND data_type IN ('text','character varying')
              AND (
                    column_name ILIKE '%text%' OR
                    column_name ILIKE '%result%' OR
                    column_name ILIKE '%songs%'
                  )
            ORDER BY column_name
            LIMIT 1
        """
        )
        RESULTS_TEXT_COL = row["column_name"] if row else None

        logger.info(
            "🔎 Schema: players.discord_id=%s players.in_guild=%s | seasons.active=%s name=%s starts_at=%s ends_at=%s | "
            "seasonplayers.wins=%s losses=%s games_played=%s | matches_table=%s matchplayers_table=%s | user_id_type=%s discord_id_type=%s | results_text_col=%s",
            HAS_DISCORD_ID_COL,
            PLAYERS_HAS_IN_GUILD,
            SEASONS_HAS_ACTIVE,
            SEASONS_HAS_NAME,
            SEASONS_HAS_STARTS_AT,
            SEASONS_HAS_ENDS_AT,
            SP_HAS_WINS,
            SP_HAS_LOSSES,
            SP_HAS_GAMES_PLAYED,
            HAS_MATCHES_TABLE,
            HAS_MATCHPLAYERS_TABLE,
            USER_ID_SQLTYPE,
            DISCORD_ID_SQLTYPE,
            RESULTS_TEXT_COL,
        )
    except Exception:
        traceback.print_exc()


async def _ensure_matches_table(conn: asyncpg.Connection) -> None:
    """
    Re-detecta match_history en caliente.
    Arregla el caso donde el bot crea la tabla después de que el backend ya arrancó.
    """
    global HAS_MATCHES_TABLE
    if HAS_MATCHES_TABLE:
        return
    try:
        HAS_MATCHES_TABLE = await _has_col(conn, "match_history", "season_id")
        if HAS_MATCHES_TABLE:
            logger.info("✅ match_history detected at runtime")
    except Exception:
        traceback.print_exc()


@app.on_event("startup")
async def startup():
    global db_pool, _cards_task, _cards_stop

    if not DATABASE_URL:
        logger.warning("⚠️ DATABASE_URL is empty. /api endpoints will raise 500")
        return

    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=10,
        timeout=10.0,
        command_timeout=30.0,
    )
    logger.info("✅ DB pool initialized")

    async with db_pool.acquire() as conn:
        # Asegura que el backend pueda enlazar Discord -> player sin cambios manuales en DB.
        try:
            await conn.execute("ALTER TABLE IF EXISTS public.players ADD COLUMN IF NOT EXISTS discord_id TEXT")
        except Exception:
            logger.exception("Could not ensure public.players.discord_id column")

        await _detect_schema(conn)

        # Crear tablas persistentes
        uid_type_sql = "BIGINT" if USER_ID_SQLTYPE == "bigint" else "TEXT"
        await conn.execute(
            f"""
        CREATE TABLE IF NOT EXISTS public.sekaicards(
          page_id    BIGINT PRIMARY KEY,
          name       TEXT NOT NULL,
          href       TEXT NOT NULL,
          thumb      TEXT,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS public.sekaicard_variants(
          page_id    BIGINT NOT NULL REFERENCES public.sekaicards(page_id) ON DELETE CASCADE,
          label      TEXT   NOT NULL,
          url        TEXT   NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY (page_id, label)
        );
        CREATE TABLE IF NOT EXISTS public.player_pfps(
          user_id    {uid_type_sql} PRIMARY KEY,
          url        TEXT NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS public.player_titles(
          user_id    {uid_type_sql} PRIMARY KEY,
          title_key  TEXT NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_https_pfp') THEN
            ALTER TABLE public.player_pfps
              ADD CONSTRAINT chk_https_pfp CHECK (left(url, 8) = 'https://');
          END IF;
        END$$;

        -- tabla para canciones de cada match (result_id)
        CREATE TABLE IF NOT EXISTS public.match_songs(
          id          SERIAL PRIMARY KEY,
          result_id   BIGINT NOT NULL,
          song_name   TEXT   NOT NULL,
          level       TEXT   NOT NULL,
          difficulty  TEXT   NOT NULL,
          notes_count INTEGER NOT NULL,
          created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS idx_match_songs_result_id
          ON public.match_songs(result_id);
        """
        )

    # ---- background refresher de cartas Sekaipedia (AUTO) ----
    _cards_stop.clear()
    if _cards_task is None or _cards_task.done():
        _cards_task = asyncio.create_task(_refresh_cards_forever())


@app.on_event("shutdown")
async def shutdown():
    global _cards_task, _cards_stop

    _cards_stop.set()
    if _cards_task:
        _cards_task.cancel()
        try:
            await _cards_task
        except asyncio.CancelledError:
            pass
        except Exception:
            traceback.print_exc()
        _cards_task = None

    if db_pool:
        await db_pool.close()
        logger.info("🛑 DB pool closed")


# ======================= Helpers: Seasons =======================
async def _season_exists(conn: asyncpg.Connection, season_id: int) -> bool:
    row = await conn.fetchrow("SELECT 1 FROM public.seasons WHERE id=$1", season_id)
    return bool(row)


async def _pick_effective_season(
    conn: asyncpg.Connection, requested_season_id: int
) -> int:
    """
    requested_season_id:
      >0  -> se usa ese ID (si existe, si no, 404)
      =0  -> season activa si existe, si no la última por ID
    """
    if requested_season_id > 0:
        if await _season_exists(conn, requested_season_id):
            return requested_season_id
        raise HTTPException(404, f"season {requested_season_id} not found")

    if SEASONS_HAS_ACTIVE:
        row = await conn.fetchrow(
            """
            SELECT id FROM public.seasons
            WHERE active = TRUE ORDER BY id DESC LIMIT 1
        """
        )
        if row and row["id"]:
            return int(row["id"])

    row = await conn.fetchrow("SELECT id FROM public.seasons ORDER BY id DESC LIMIT 1")
    if row and row["id"]:
        return int(row["id"])

    raise HTTPException(404, "no seasons found")


# ======================= API: Seasons =======================
@app.get("/api/seasons")
async def list_seasons():
    if not db_pool:
        raise HTTPException(500, "DB pool not initialized")

    cols = ["id"]
    if SEASONS_HAS_NAME:
        cols.append("name")
    if SEASONS_HAS_ACTIVE:
        cols.append("active")
    if SEASONS_HAS_STARTS_AT:
        cols.append("starts_at")
    if SEASONS_HAS_ENDS_AT:
        cols.append("ends_at")
    select_cols = ", ".join(cols)

    q = f"SELECT {select_cols} FROM public.seasons ORDER BY id DESC"
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(q)

    out = []
    for r in rows:
        item = {"id": r["id"]}
        if SEASONS_HAS_NAME:
            item["name"] = _rget(r, "name")
        if SEASONS_HAS_ACTIVE:
            item["active"] = _rget(r, "active")
        if SEASONS_HAS_STARTS_AT:
            dt = _rget(r, "starts_at")
            item["startsAt"] = dt.isoformat() if dt else None
        if SEASONS_HAS_ENDS_AT:
            dt = _rget(r, "ends_at")
            item["endsAt"] = dt.isoformat() if dt else None
        out.append(item)
    return out


# ======================= AUTH02 (Discord OAuth) =======================
PFP_BY_NAME: Dict[str, str] = {}
PFP_BY_ID: Dict[str, str] = {}
PFP_BY_DISCORD: Dict[str, str] = {}

STATE_STORE: Dict[str, datetime] = {}
STATE_TTL = timedelta(minutes=10)


def _discord_avatar_url(discord_id: Optional[str], avatar_hash: Optional[str]) -> Optional[str]:
    """
    Construye la URL real del avatar de Discord.
    Si avatar_hash es None, el usuario no tiene avatar personalizado.
    """
    if not discord_id or not avatar_hash:
        return None

    ext = "gif" if str(avatar_hash).startswith("a_") else "png"
    return f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar_hash}.{ext}?size=256"


def _cleanup_state_store():
    now = datetime.now(timezone.utc)
    expired = [k for k, exp in STATE_STORE.items() if exp < now]
    for k in expired:
        STATE_STORE.pop(k, None)


def _discord_id_for_sql(discord_id_like: Optional[str]):
    """
    Convierte el Discord snowflake al tipo real de public.players.discord_id.
    Comparar siempre con discord_id::text es seguro para SELECT, pero para UPDATE
    necesitamos enviar int si la columna es BIGINT.
    """
    s = str(discord_id_like or "").strip()
    if DISCORD_ID_SQLTYPE == "bigint":
        if not re.fullmatch(r"\d+", s):
            raise HTTPException(400, "discordId debe ser numérico")
        return int(s)
    return s


def _identity_norm(value: Optional[str]) -> str:
    """Normaliza nombres para comparar Discord username/global_name con players.name."""
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = value.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", value.lower()).strip()


def _discord_name_candidates(username: Optional[str], global_name: Optional[str]) -> List[str]:
    """
    Candidatos seguros para enlazar automáticamente.
    Ejemplo: username=stickyrice39 y global_name=stickyrice => [stickyrice, stickyrice39].
    También intenta quitar dígitos finales del username, pero sólo se usa si el match es único.
    """
    out: List[str] = []
    for raw in [global_name, username]:
        n = _identity_norm(raw)
        if n and n not in out:
            out.append(n)

    username_norm = _identity_norm(username)
    stripped = re.sub(r"\d+$", "", username_norm)
    if stripped and len(stripped) >= 3 and stripped not in out:
        out.append(stripped)

    return out


async def _resolve_user_id_from_discord(discord_id: str) -> Optional[str]:
    if not (db_pool and HAS_DISCORD_ID_COL and discord_id):
        return None
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id FROM public.players WHERE discord_id::text = $1 LIMIT 1",
            str(discord_id),
        )
        if row:
            return str(row["user_id"])
    return None


async def _resolve_or_link_user_id_from_discord(
    discord_id: Optional[str],
    username: Optional[str] = None,
    global_name: Optional[str] = None,
) -> Optional[str]:
    """
    Resuelve el user_id público del leaderboard a partir del Discord ID.

    Si todavía no existe players.discord_id, intenta enlazar automáticamente por nombre:
      - compara players.name con Discord global_name / username normalizados
      - sólo actualiza si hay EXACTAMENTE un jugador candidato
      - no pisa discord_id de otro usuario

    Esto evita tener que actualizar manualmente player.discord_id para cada jugador.
    """
    did = str(discord_id or "").strip()
    if not (db_pool and HAS_DISCORD_ID_COL and did):
        return None

    existing = await _resolve_user_id_from_discord(did)
    if existing:
        return existing

    candidates = _discord_name_candidates(username, global_name)
    if not candidates:
        return None

    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id, name, discord_id
                FROM public.players
                WHERE lower(regexp_replace(coalesce(name, ''), '[^a-zA-Z0-9]+', '', 'g')) = ANY($1::text[])
                  AND (discord_id IS NULL OR discord_id::text = '' OR discord_id::text = $2)
                ORDER BY user_id::text ASC
                LIMIT 5
                """,
                candidates,
                did,
            )

            if len(rows) != 1:
                if len(rows) > 1:
                    logger.warning(
                        "Auto-link skipped for discord_id=%s username=%s global_name=%s because %s players matched",
                        did, username, global_name, len(rows),
                    )
                return None

            user_id = str(rows[0]["user_id"])
            await conn.execute(
                """
                UPDATE public.players
                SET discord_id = $2
                WHERE user_id::text = $1
                  AND (discord_id IS NULL OR discord_id::text = '' OR discord_id::text = $3)
                """,
                user_id,
                _discord_id_for_sql(did),
                did,
            )

            logger.info(
                "Auto-linked Discord user to player: discord_id=%s username=%s global_name=%s user_id=%s player_name=%s",
                did, username, global_name, user_id, rows[0]["name"],
            )
            return user_id

    except Exception:
        traceback.print_exc()
        return None


def _valid_https_url(u: str) -> bool:
    try:
        p = urlparse(u)
        if p.scheme != "https":
            return False
        if not p.netloc:
            return False
        if any(c.isspace() for c in u) or '"' in u or "'" in u:
            return False
        return True
    except Exception:
        return False


def _resolve_pfp_for_player(
    user_id: Optional[str], discord_id: Optional[str], name: Optional[str]
) -> Optional[str]:
    if user_id:
        url = PFP_BY_ID.get(str(user_id))
        if url:
            return url
    if discord_id:
        url = PFP_BY_DISCORD.get(str(discord_id))
        if url:
            return url
    if name:
        url = PFP_BY_NAME.get((name or "").lower())
        if url:
            return url
    return None


async def _persist_public_pfp(
    user_id: Optional[str],
    discord_id: Optional[str],
    url: Optional[str],
    name: Optional[str] = None,
) -> bool:
    """
    Persiste el PFP en la fuente pública que usa el leaderboard.
    Login solo no basta si no se escribe en public.player_pfps; por eso
    guardamos automáticamente cuando Discord devuelve avatar y existe user_id público.
    """
    clean_url = (url or "").strip()
    uid = str(user_id or "").strip()
    did = str(discord_id or "").strip()
    name_lower = (name or "").strip().lower()

    if not clean_url or not _valid_https_url(clean_url):
        return False

    # Cache local del proceso actual. Esto ayuda inmediatamente en este worker.
    if uid:
        PFP_BY_ID[uid] = clean_url
    if did:
        PFP_BY_DISCORD[did] = clean_url
    if name_lower:
        PFP_BY_NAME[name_lower] = clean_url

    # Persistencia pública real. Sin user_id no hay forma segura de asociarlo al player del leaderboard.
    if not (db_pool and uid):
        return False

    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO public.player_pfps(user_id, url, updated_at)
                VALUES ($1, $2, now())
                ON CONFLICT (user_id) DO UPDATE
                  SET url = EXCLUDED.url,
                      updated_at = now()
                """,
                _uid_for_sql(uid),
                clean_url,
            )
        return True
    except Exception:
        traceback.print_exc()
        return False


def _uid_for_sql(user_id_like: Optional[str]):
    """
    Devuelve el parámetro listo para la query según el tipo de columna:
    - BIGINT  -> int
    - TEXT    -> str
    """
    s = str(user_id_like or "")
    if USER_ID_SQLTYPE == "bigint":
        if not re.fullmatch(r"\d+", s):
            raise HTTPException(400, "userId debe ser numérico")
        return int(s)
    return s  # TEXT


def _place_suffix(place: int) -> str:
    place = int(place or 0)
    if place == 1:
        return "1st"
    if place == 2:
        return "2nd"
    if place == 3:
        return "3rd"
    return str(place)


def _season_title_key(season_id: Any, place: Any) -> str:
    return f"season:{season_id}:{int(place)}"


def _season_title_color(place: Any) -> str:
    p = int(place or 0)
    if p == 1:
        return "gold"
    if p == 2:
        return "silver"
    if p == 3:
        return "bronze"
    return "gold"


def _season_display_label(season_id: Any, season_name: Optional[str]) -> str:
    raw = str(season_name or "").strip()
    if not raw:
        return str(season_id)

    low = raw.lower()
    if low in ("current", "preseason", "pre-season"):
        return str(season_id)

    if low.startswith("season "):
        return raw.split(" ", 1)[1].strip() or str(season_id)

    return raw


def _season_title_payload(season_id: Any, season_name: Optional[str], place: Any) -> Dict[str, Any]:
    p = int(place)
    season_label = _season_display_label(season_id, season_name)
    return {
        "key": _season_title_key(season_id, p),
        "titleKey": _season_title_key(season_id, p),
        "label": f"Season {season_label} {_place_suffix(p)} Place",
        "seasonId": season_id,
        "season_id": season_id,
        "seasonName": season_name,
        "season_name": season_name,
        "place": p,
        "color": _season_title_color(p),
    }


def _parse_season_title_key(title_key: Optional[str]) -> Optional[Tuple[str, int]]:
    if not title_key:
        return None
    m = re.fullmatch(r"season:([^:]+):(1|2|3)", str(title_key).strip())
    if not m:
        return None
    return m.group(1), int(m.group(2))


async def _title_payload_from_key(conn: asyncpg.Connection, title_key: Optional[str]) -> Optional[Dict[str, Any]]:
    parsed = _parse_season_title_key(title_key)
    if not parsed:
        return None
    season_id, place = parsed
    season_name = None
    try:
        row = await conn.fetchrow(
            "SELECT name FROM public.seasons WHERE id::text = $1 LIMIT 1",
            str(season_id),
        )
        if row:
            season_name = row["name"]
    except Exception:
        season_name = None
    return _season_title_payload(season_id, season_name, place)


async def _resolved_auth_user_id(session: Dict[str, Any]) -> Optional[str]:
    discord_id = str(session.get("id") or session.get("discordId") or session.get("discord_id") or "")
    resolved = session.get("resolvedUserId") or session.get("resolved_user_id")
    if not resolved and discord_id:
        resolved = await _resolve_user_id_from_discord(discord_id)
    # Si existe players.discord_id, no uses el Discord ID como user_id público.
    # Eso causaba PFP/titles incorrectos cuando resolvedUserId venía null.
    if resolved:
        return str(resolved)
    if not HAS_DISCORD_ID_COL and discord_id:
        return discord_id
    return None


async def _eligible_titles_for_user_id(conn: asyncpg.Connection, user_id: str) -> List[Dict[str, Any]]:
    if not user_id:
        return []

    name_expr = "s.name" if SEASONS_HAS_NAME else "NULL::text"
    filters = []
    if SEASONS_HAS_ACTIVE:
        filters.append("(s.active IS DISTINCT FROM TRUE)")
    if SEASONS_HAS_NAME:
        filters.append("(s.name IS NULL OR lower(s.name) NOT IN ('current','preseason','pre-season'))")

    where_sql = ""
    if filters:
        where_sql = "WHERE " + " AND ".join(filters)

    rows = await conn.fetch(
        f"""
        WITH ranked AS (
            SELECT
                sp.season_id,
                {name_expr} AS season_name,
                sp.user_id,
                sp.mmr,
                row_number() OVER (
                    PARTITION BY sp.season_id
                    ORDER BY sp.mmr DESC, sp.user_id::text ASC
                ) AS rn
            FROM public.seasonplayers sp
            LEFT JOIN public.seasons s ON s.id = sp.season_id
            {where_sql}
        )
        SELECT season_id, season_name, rn AS place
        FROM ranked
        WHERE user_id::text = $1
          AND rn <= 3
        ORDER BY season_id DESC, rn ASC
        """,
        str(user_id),
    )

    return [
        _season_title_payload(r["season_id"], r["season_name"], r["place"])
        for r in rows
    ]


async def _selected_title_for_user_id(conn: asyncpg.Connection, user_id: str) -> Optional[Dict[str, Any]]:
    if not user_id:
        return None
    row = await conn.fetchrow(
        "SELECT title_key FROM public.player_titles WHERE user_id::text = $1 LIMIT 1",
        str(user_id),
    )
    if not row:
        return None
    return await _title_payload_from_key(conn, row["title_key"])


@app.get("/health")
async def health():
    db_ok = False
    try:
        if db_pool:
            async with db_pool.acquire() as conn:
                await conn.execute("SELECT 1;")
                db_ok = True
    except Exception:
        db_ok = False
    return {"ok": True, "tip": "auth02 up", "db": db_ok}


@app.get("/auth02")
async def auth02_start():
    _cleanup_state_store()
    state = secrets.token_urlsafe(24)
    STATE_STORE[state] = datetime.now(timezone.utc) + STATE_TTL
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify email",
        "state": state,
    }
    q = str(httpx.QueryParams(params))
    return RedirectResponse(url=f"https://discord.com/oauth2/authorize?{q}")


@app.get("/auth02/callback")
async def auth02_callback(code: Optional[str] = None, state: Optional[str] = None):
    if not code:
        raise HTTPException(400, "Missing code")

    _cleanup_state_store()
    exp = STATE_STORE.pop(state or "", None)
    if not exp or datetime.now(timezone.utc) > exp:
        raise HTTPException(400, "Invalid or expired state")

    async with httpx.AsyncClient() as client:
        token_res = await client.post(
            "https://discord.com/api/oauth2/token",
            data={
                "client_id": DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": DISCORD_REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=20.0,
        )
        if token_res.status_code != 200:
            raise HTTPException(400, f"Discord token exchange failed: {token_res.text}")
        token = token_res.json()

        user_res = await client.get(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"{token['token_type']} {token['access_token']}"},
            timeout=20.0,
        )
        if user_res.status_code != 200:
            raise HTTPException(400, f"Discord user fetch failed: {user_res.text}")
        user = user_res.json()

        logger.info(
            "OAuth OK user: id=%s username=%s avatar=%s global_name=%s",
            user.get("id"),
            user.get("username"),
            user.get("avatar"),
            user.get("global_name"),
        )

    discord_id = str(user.get("id") or "")
    username = user.get("username") or ""
    global_name = user.get("global_name")
    avatar_hash = user.get("avatar")
    avatar_url = _discord_avatar_url(discord_id, avatar_hash)

    resolved_user_id = await _resolve_or_link_user_id_from_discord(discord_id, username, global_name)

    # Publicar el avatar automáticamente al hacer login.
    # Antes el usuario sólo lo veía localmente; ahora se guarda en player_pfps
    # para que otros usuarios también lo vean en Leaderboard/History.
    await _persist_public_pfp(resolved_user_id, discord_id, avatar_url, global_name or username)

    session_user = {
        "id": discord_id,
        "discordId": discord_id,
        "username": username,
        "globalName": global_name,
        "global_name": global_name,
        "avatar": avatar_hash,
        "avatarUrl": avatar_url,
        "avatar_url": avatar_url,
        "resolvedUserId": resolved_user_id,
        "resolved_user_id": resolved_user_id,
    }

    # Cookie + token fallback:
    # - Cookie funciona en navegadores normales.
    # - pjsk_token viaja en query y hash para móvil/Discord WebView si la cookie se pierde.
    session_token = make_session_token(session_user)
    token_safe = quote(session_token, safe="")
    sep = "&" if "?" in FRONT_URL else "?"
    redirect_url = f"{FRONT_URL}{sep}verified=1&pjsk_token={token_safe}#pjsk_token={token_safe}"
    logger.info(
        "OAuth redirect ready: discord_id=%s resolved_user_id=%s has_token=%s redirect_front=%s",
        discord_id,
        resolved_user_id,
        bool(session_token),
        FRONT_URL,
    )
    resp = RedirectResponse(url=redirect_url, status_code=303)
    set_session_cookie(resp, session_user, session_token)
    return resp

@app.get("/auth02/me")
async def auth02_me(request: Request):
    payload = require_auth_session(request)

    discord_id = str(payload.get("id") or payload.get("discordId") or payload.get("discord_id") or "")
    avatar_hash = payload.get("avatar")
    avatar_url = payload.get("avatarUrl") or payload.get("avatar_url")

    if not avatar_url and avatar_hash:
        avatar_url = _discord_avatar_url(discord_id, avatar_hash)

    resolved_user_id = payload.get("resolvedUserId") or payload.get("resolved_user_id")
    if not resolved_user_id:
        resolved_user_id = await _resolve_or_link_user_id_from_discord(
            discord_id,
            payload.get("username"),
            payload.get("globalName") or payload.get("global_name"),
        )
        if resolved_user_id and avatar_url:
            await _persist_public_pfp(
                resolved_user_id,
                discord_id,
                avatar_url,
                payload.get("globalName") or payload.get("global_name") or payload.get("username"),
            )

    session_user = dict(payload)
    session_user.update(
        {
            "id": discord_id,
            "discordId": discord_id,
            "discord_id": discord_id,
            "username": payload.get("username"),
            "globalName": payload.get("globalName") or payload.get("global_name"),
            "global_name": payload.get("global_name") or payload.get("globalName"),
            "avatar": avatar_hash,
            "avatarUrl": avatar_url,
            "avatar_url": avatar_url,
            "resolvedUserId": resolved_user_id,
            "resolved_user_id": resolved_user_id,
        }
    )
    auth_token = make_session_token(session_user)

    response = JSONResponse(
        {
            "id": discord_id,
            "discordId": discord_id,
            "discord_id": discord_id,
            "username": session_user.get("username"),
            "globalName": session_user.get("globalName"),
            "global_name": session_user.get("global_name"),
            "avatar": avatar_hash,
            "avatarUrl": avatar_url,
            "avatar_url": avatar_url,
            "resolvedUserId": resolved_user_id,
            "resolved_user_id": resolved_user_id,
            "needsPlayerLink": not bool(resolved_user_id),
            "needs_player_link": not bool(resolved_user_id),
            "authToken": auth_token,
        }
    )
    set_session_cookie(response, session_user, auth_token)
    return _no_store(response)


@app.post("/auth02/logout")
async def auth02_logout(request: Request):
    resp = JSONResponse({"ok": True})
    clear_session_cookie(resp)
    return _no_store(resp)


# =================== /auth02/pfps ===================
MAX_PFPS = 5000


@app.post("/auth02/set-pfp")
async def set_pfp(request: Request):
    """
    Guarda/actualiza el PFP público del usuario autenticado.
    Regla nueva: si Discord todavía no está linkeado a un player del leaderboard,
    NO se guarda falso "Saved". Primero debe usarse /auth02/link-player.
    """
    session = require_auth_session(request)
    body = await request.json()

    url = (body.get("url") or body.get("avatarUrl") or body.get("avatar_url") or "").strip()

    discord_id = str(
        session.get("id")
        or session.get("discordId")
        or session.get("discord_id")
        or body.get("discordId")
        or body.get("discord_id")
        or ""
    ).strip()

    resolved_user_id = (
        session.get("resolvedUserId")
        or session.get("resolved_user_id")
        or None
    )

    if not resolved_user_id and discord_id:
        resolved_user_id = await _resolve_or_link_user_id_from_discord(
            discord_id,
            session.get("username"),
            session.get("globalName") or session.get("global_name"),
        )

    if not resolved_user_id:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "player-not-linked",
                "message": "Link your Discord account to your leaderboard player before saving PFP.",
            },
        )

    user_id = str(resolved_user_id).strip()
    player_name = str(
        body.get("playerName")
        or body.get("player_name")
        or body.get("nameLower")
        or session.get("globalName")
        or session.get("global_name")
        or session.get("username")
        or ""
    ).strip()
    name_lower = player_name.lower()

    # Borrado público
    if url == "":
        PFP_BY_ID.pop(str(user_id), None)
        if discord_id:
            PFP_BY_DISCORD.pop(str(discord_id), None)
        if name_lower:
            PFP_BY_NAME.pop(name_lower, None)

        if db_pool:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM public.player_pfps WHERE user_id = $1",
                    _uid_for_sql(user_id),
                )

        return _no_store(JSONResponse({"ok": True, "deleted": True, "userId": user_id}))

    if not _valid_https_url(url):
        raise HTTPException(400, "invalid url")

    if len(PFP_BY_ID) + len(PFP_BY_NAME) + len(PFP_BY_DISCORD) > MAX_PFPS:
        raise HTTPException(429, "pfp store is full")

    PFP_BY_ID[str(user_id)] = url
    if discord_id:
        PFP_BY_DISCORD[str(discord_id)] = url
    if name_lower:
        PFP_BY_NAME[name_lower] = url

    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO public.player_pfps(user_id, url, updated_at)
                VALUES ($1, $2, now())
                ON CONFLICT (user_id) DO UPDATE
                  SET url = EXCLUDED.url,
                      updated_at = now()
                """,
                _uid_for_sql(user_id),
                url,
            )

    return _no_store(
        JSONResponse(
            {
                "ok": True,
                "userId": user_id,
                "discordId": discord_id or None,
                "nameLower": name_lower or None,
                "byId": True,
                "byDiscord": bool(discord_id),
                "byName": bool(name_lower),
            }
        )
    )


@app.get("/auth02/link-candidates")
async def auth02_link_candidates(
    request: Request,
    q: str = Query("", description="Search player name"),
    limit: int = Query(80, ge=1, le=300),
):
    """
    Devuelve jugadores del leaderboard que el usuario logueado puede reclamar/linkear.
    Sólo muestra players sin discord_id o ya linkeados al mismo Discord ID.
    """
    session = require_auth_session(request)
    if not db_pool:
        raise HTTPException(500, "DB pool not initialized")
    if not HAS_DISCORD_ID_COL:
        raise HTTPException(500, "players.discord_id column is not available")

    discord_id = str(session.get("id") or session.get("discordId") or session.get("discord_id") or "").strip()
    if not discord_id:
        raise HTTPException(401, "missing-discord-session")

    search = (q or "").strip()
    async with db_pool.acquire() as conn:
        try:
            picked = await _pick_effective_season(conn, 0)
        except Exception:
            picked = None

        linked_row = await conn.fetchrow(
            "SELECT user_id::text AS user_id FROM public.players WHERE discord_id::text = $1 LIMIT 1",
            discord_id,
        )
        linked_user_id = linked_row["user_id"] if linked_row else None

        params: List[Any] = [discord_id]
        where = ["(p.discord_id IS NULL OR p.discord_id::text = '' OR p.discord_id::text = $1)"]
        if search:
            params.append(f"%{search}%")
            where.append(f"p.name ILIKE ${len(params)}")

        where_sql = " AND ".join(where)

        if picked is not None:
            params.extend([picked, limit])
            season_idx = len(params) - 1
            limit_idx = len(params)
            rows = await conn.fetch(
                f"""
                SELECT
                    p.user_id::text AS user_id,
                    p.name AS name,
                    p.country AS country,
                    p.discord_id::text AS discord_id,
                    sp.mmr AS mmr,
                    sp.role AS rank,
                    pp.url AS pfp
                FROM public.players p
                INNER JOIN public.seasonplayers sp ON sp.user_id = p.user_id
                LEFT JOIN public.player_pfps pp ON pp.user_id::text = p.user_id::text
                WHERE {where_sql}
                  AND sp.season_id = ${season_idx}
                ORDER BY
                    CASE
                      WHEN lower(p.name) = lower($2) THEN 0
                      WHEN p.name ILIKE $2 THEN 1
                      ELSE 2
                    END,
                    sp.mmr DESC,
                    p.name ASC
                LIMIT ${limit_idx}
                """ if search else f"""
                SELECT
                    p.user_id::text AS user_id,
                    p.name AS name,
                    p.country AS country,
                    p.discord_id::text AS discord_id,
                    sp.mmr AS mmr,
                    sp.role AS rank,
                    pp.url AS pfp
                FROM public.players p
                INNER JOIN public.seasonplayers sp ON sp.user_id = p.user_id
                LEFT JOIN public.player_pfps pp ON pp.user_id::text = p.user_id::text
                WHERE {where_sql}
                  AND sp.season_id = ${season_idx}
                ORDER BY sp.mmr DESC, p.name ASC
                LIMIT ${limit_idx}
                """,
                *params,
            )
        else:
            params.append(limit)
            limit_idx = len(params)
            rows = await conn.fetch(
                f"""
                SELECT
                    p.user_id::text AS user_id,
                    p.name AS name,
                    p.country AS country,
                    p.discord_id::text AS discord_id,
                    NULL::integer AS mmr,
                    NULL::text AS rank,
                    pp.url AS pfp
                FROM public.players p
                LEFT JOIN public.player_pfps pp ON pp.user_id::text = p.user_id::text
                WHERE {where_sql}
                ORDER BY p.name ASC
                LIMIT ${limit_idx}
                """,
                *params,
            )

    candidates = []
    for r in rows:
        candidates.append(
            {
                "userId": r["user_id"],
                "user_id": r["user_id"],
                "name": r["name"],
                "country": r["country"],
                "discordId": r["discord_id"],
                "discord_id": r["discord_id"],
                "mmr": r["mmr"],
                "rank": r["rank"],
                "pfp": r["pfp"],
                "alreadyLinkedToMe": str(r["discord_id"] or "") == discord_id,
            }
        )

    return _no_store(
        JSONResponse(
            {
                "ok": True,
                "discordId": discord_id,
                "linkedUserId": linked_user_id,
                "needsPlayerLink": not bool(linked_user_id),
                "candidates": candidates,
            }
        )
    )


@app.post("/auth02/link-player")
async def auth02_link_player(request: Request):
    """
    Link explícito Discord -> public.players.user_id.
    Esto elimina el error resolvedUserId=null sin editar PostgreSQL manualmente.
    """
    session = require_auth_session(request)
    if not db_pool:
        raise HTTPException(500, "DB pool not initialized")
    if not HAS_DISCORD_ID_COL:
        raise HTTPException(500, "players.discord_id column is not available")

    body = await request.json()
    user_id = str(
        body.get("userId")
        or body.get("user_id")
        or body.get("playerId")
        or body.get("player_id")
        or ""
    ).strip()
    if not user_id:
        raise HTTPException(400, "missing userId")

    discord_id = str(session.get("id") or session.get("discordId") or session.get("discord_id") or "").strip()
    if not discord_id:
        raise HTTPException(401, "missing-discord-session")

    avatar_hash = session.get("avatar")
    avatar_url = session.get("avatarUrl") or session.get("avatar_url")
    if not avatar_url and avatar_hash:
        avatar_url = _discord_avatar_url(discord_id, avatar_hash)

    async with db_pool.acquire() as conn:
        target = await conn.fetchrow(
            """
            SELECT user_id::text AS user_id, name, discord_id::text AS discord_id
            FROM public.players
            WHERE user_id::text = $1
            LIMIT 1
            """,
            user_id,
        )
        if not target:
            raise HTTPException(404, "player not found")

        used_by_other = await conn.fetchrow(
            """
            SELECT user_id::text AS user_id, name
            FROM public.players
            WHERE discord_id::text = $1
              AND user_id::text <> $2
            LIMIT 1
            """,
            discord_id,
            user_id,
        )
        if used_by_other:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "discord-already-linked",
                    "message": "This Discord account is already linked to another leaderboard player.",
                    "linkedUserId": used_by_other["user_id"],
                    "linkedName": used_by_other["name"],
                },
            )

        target_discord = str(target["discord_id"] or "").strip()
        if target_discord and target_discord != discord_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "player-already-linked",
                    "message": "This leaderboard player is already linked to another Discord account.",
                    "linkedDiscordId": target_discord,
                },
            )

        await conn.execute(
            """
            UPDATE public.players
            SET discord_id = $2
            WHERE user_id::text = $1
            """,
            user_id,
            _discord_id_for_sql(discord_id),
        )

    await _persist_public_pfp(user_id, discord_id, avatar_url, target["name"] or session.get("username"))

    session_user = dict(session)
    session_user.update(
        {
            "id": discord_id,
            "discordId": discord_id,
            "discord_id": discord_id,
            "avatarUrl": avatar_url,
            "avatar_url": avatar_url,
            "resolvedUserId": user_id,
            "resolved_user_id": user_id,
            "linkedPlayerName": target["name"],
        }
    )
    auth_token = make_session_token(session_user)

    resp = JSONResponse(
        {
            "ok": True,
            "userId": user_id,
            "resolvedUserId": user_id,
            "resolved_user_id": user_id,
            "discordId": discord_id,
            "playerName": target["name"],
            "avatarUrl": avatar_url,
            "needsPlayerLink": False,
            "authToken": auth_token,
        }
    )
    set_session_cookie(resp, session_user, auth_token)
    return _no_store(resp)


@app.get("/auth02/pfps")
async def list_pfps():
    """
    Mapa público de PFPs. Devuelve memoria + DB.
    Esto evita que otro usuario/incógnito no vea el PFP después de un redeploy.
    """
    by_id = dict(PFP_BY_ID)
    by_discord = dict(PFP_BY_DISCORD)
    by_name = dict(PFP_BY_NAME)

    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                discord_select = "p.discord_id" if HAS_DISCORD_ID_COL else "NULL::text AS discord_id"
                rows = await conn.fetch(
                    f"""
                    SELECT
                        pp.user_id::text AS user_id,
                        pp.url AS url,
                        p.name AS name,
                        {discord_select}
                    FROM public.player_pfps pp
                    LEFT JOIN public.players p ON p.user_id::text = pp.user_id::text
                    ORDER BY pp.updated_at DESC
                    LIMIT $1
                    """,
                    MAX_PFPS,
                )

                for r in rows:
                    uid = str(r["user_id"]) if r["user_id"] is not None else ""
                    url = r["url"]
                    name = (r["name"] or "").strip().lower()
                    did = str(r["discord_id"] or "").strip()

                    if uid:
                        by_id[uid] = url
                    if did:
                        by_discord[did] = url
                    if name:
                        by_name[name] = url
        except Exception:
            traceback.print_exc()

    return _no_store(JSONResponse({"byId": by_id, "byDiscord": by_discord, "byName": by_name}))


class TitleSelectIn(BaseModel):
    titleKey: Optional[str] = None


@app.get("/auth02/titles")
async def get_auth_titles(request: Request):
    """
    Devuelve los titles disponibles para el usuario autenticado.
    Un title se desbloquea si el usuario terminó Top 1, 2 o 3 en una season cerrada.
    """
    session = require_auth_session(request)
    if not db_pool:
        raise HTTPException(500, "DB pool not initialized")

    user_id = await _resolved_auth_user_id(session)
    if not user_id:
        return {"available": [], "selected": None}

    async with db_pool.acquire() as conn:
        available = await _eligible_titles_for_user_id(conn, user_id)
        selected = await _selected_title_for_user_id(conn, user_id)

    available_keys = {t["key"] for t in available}
    if selected and selected.get("key") not in available_keys:
        selected = None

    return {"available": available, "selected": selected}


@app.post("/auth02/title")
async def set_auth_title(request: Request, body: TitleSelectIn):
    """
    Guarda el title activo del usuario autenticado.
    Solo permite guardar titles que el usuario realmente desbloqueó.
    """
    session = require_auth_session(request)
    if not db_pool:
        raise HTTPException(500, "DB pool not initialized")

    user_id = await _resolved_auth_user_id(session)
    if not user_id:
        raise HTTPException(400, "could not resolve user id")

    title_key = (body.titleKey or "").strip()

    async with db_pool.acquire() as conn:
        if title_key == "":
            await conn.execute(
                "DELETE FROM public.player_titles WHERE user_id = $1",
                _uid_for_sql(user_id),
            )
            return {"ok": True, "selected": None}

        available = await _eligible_titles_for_user_id(conn, user_id)
        allowed = {t["key"]: t for t in available}
        if title_key not in allowed:
            raise HTTPException(403, "title not unlocked")

        await conn.execute(
            """
            INSERT INTO public.player_titles(user_id, title_key, updated_at)
            VALUES ($1, $2, now())
            ON CONFLICT (user_id) DO UPDATE
              SET title_key = EXCLUDED.title_key,
                  updated_at = now()
            """,
            _uid_for_sql(user_id),
            title_key,
        )

        return {"ok": True, "selected": allowed[title_key]}


# ======================= API: Players =======================
@app.get("/api/players")
async def get_players(
    season_id: int = Query(..., description="Season ID (0 = season actual activa)"),
    limit: int = Query(10000, ge=1, le=100000),
    offset: int = Query(0, ge=0),
):
    """
    Devuelve el leaderboard de players para una season.
    Calcula:
      - matchesPlayed desde match_history (si existe) o seasonplayers.games*
      - last10Games / last10Wins / last10Losses / last10MmrDelta desde match_history
      - peakMmr desde match_history (igual que el bot)
    """
    if not db_pool:
        raise HTTPException(500, "DB pool not initialized")

    rows = []
    games_by_user: Dict[str, int] = {}
    last10_by_user: Dict[str, Dict[str, int]] = {}
    peak_by_user: Dict[str, int] = {}

    try:
        async with db_pool.acquire() as conn:
            # Re-detectar match_history si el bot la creó después del startup
            await _ensure_matches_table(conn)

            picked = await _pick_effective_season(conn, season_id)

            select_cols = [
                "p.user_id  AS user_id",
                "p.name     AS name",
                "p.country  AS country",
                "sp.mmr     AS mmr",
                "sp.role    AS rank",
                "pp.url     AS pfp_url",
                "pp.updated_at AS pfp_updated_at",
                "pt.title_key AS title_key",
                "ts.name AS title_season_name",
            ]
            if HAS_DISCORD_ID_COL:
                select_cols.insert(1, "p.discord_id AS discord_id")
            if PLAYERS_HAS_IN_GUILD:
                select_cols.append("p.in_guild AS in_guild")
            if SP_HAS_WINS:
                select_cols.append("sp.wins AS wins")
            if SP_HAS_LOSSES:
                select_cols.append("sp.losses AS losses")
            if SP_HAS_GAMES_PLAYED:
                if await _has_col(conn, "seasonplayers", "games_played"):
                    select_cols.append("sp.games_played AS games_played")
                elif await _has_col(conn, "seasonplayers", "games"):
                    select_cols.append("sp.games AS games_played")

            q = f"""
                SELECT {", ".join(select_cols)}
                FROM public.players p
                INNER JOIN public.seasonplayers sp ON p.user_id = sp.user_id
                LEFT JOIN public.player_pfps pp ON pp.user_id::text = p.user_id::text
                LEFT JOIN public.player_titles pt ON pt.user_id::text = p.user_id::text
                LEFT JOIN public.seasons ts ON ts.id::text = split_part(pt.title_key, ':', 2)
                WHERE sp.season_id = $1
                ORDER BY sp.mmr DESC
                LIMIT $2 OFFSET $3
            """
            rows = await conn.fetch(q, picked, limit, offset)

            # Contar partidas, peakMmr y W-L / MMR last 10 desde match_history
            if HAS_MATCHES_TABLE and rows:
                try:
                    uid_strs: List[str] = []
                    for rr in rows:
                        uid = _rget(rr, "user_id")
                        if uid is None:
                            continue
                        uid_strs.append(str(uid))

                    if uid_strs:
                        stats_rows = await conn.fetch(
                            """
                            SELECT
                                user_id,
                                COUNT(*) AS games_played,
                                GREATEST(MAX(mmr_before), MAX(mmr_after)) AS peak_mmr
                            FROM public.match_history
                            WHERE season_id = $1
                              AND user_id::text = ANY($2::text[])
                            GROUP BY user_id
                        """,
                            picked,
                            uid_strs,
                        )

                        games_by_user = {
                            str(sr["user_id"]): sr["games_played"] for sr in stats_rows
                        }
                        peak_by_user = {
                            str(sr["user_id"]): sr["peak_mmr"] for sr in stats_rows
                        }

                        last10_rows = await conn.fetch(
                            """
                            WITH ranked AS (
                                SELECT
                                    user_id,
                                    is_win,
                                    mmr_delta,
                                    played_at,
                                    id,
                                    row_number() OVER (
                                        PARTITION BY user_id
                                        ORDER BY played_at DESC, id DESC
                                    ) AS rn
                                FROM public.match_history
                                WHERE season_id = $1
                                  AND user_id::text = ANY($2::text[])
                            )
                            SELECT
                                user_id,
                                COUNT(*) FILTER (WHERE rn <= 10) AS last10_games,
                                COUNT(*) FILTER (WHERE rn <= 10 AND is_win = TRUE) AS last10_wins,
                                COUNT(*) FILTER (WHERE rn <= 10 AND (is_win = FALSE OR is_win IS NULL)) AS last10_losses,
                                COALESCE(SUM(mmr_delta) FILTER (WHERE rn <= 10), 0) AS last10_mmr_delta
                            FROM ranked
                            GROUP BY user_id
                        """,
                            picked,
                            uid_strs,
                        )
                        last10_by_user = {
                            str(r["user_id"]): {
                                "games": r["last10_games"],
                                "wins": r["last10_wins"],
                                "losses": r["last10_losses"],
                                "mmr_delta": r["last10_mmr_delta"],
                            }
                            for r in last10_rows
                        }
                except Exception:
                    traceback.print_exc()
                    games_by_user = {}
                    last10_by_user = {}
                    peak_by_user = {}

    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"DB error: {e}")

    out = []
    for r in rows:
        user_id = _rget(r, "user_id")
        user_key = str(user_id) if user_id is not None else None
        discord_id = _rget(r, "discord_id") if HAS_DISCORD_ID_COL else None
        name = _rget(r, "name")

        # PFP
        pfp_url = _rget(r, "pfp_url")
        pfp_ts = _rget(r, "pfp_updated_at")
        if pfp_url:
            try:
                epoch = int(pfp_ts.timestamp()) if pfp_ts else None
            except Exception:
                epoch = None
            if epoch:
                sep = "&" if "?" in pfp_url else "?"
                pfp_url = f"{pfp_url}{sep}v={epoch}"
        else:
            pfp_url = _resolve_pfp_for_player(
                str(user_id) if user_id is not None else None,
                str(discord_id) if discord_id is not None else None,
                name,
            )

        wins = _rget(r, "wins") if SP_HAS_WINS else None
        losses = _rget(r, "losses") if SP_HAS_LOSSES else None

        games_played = None
        if user_key is not None and user_key in games_by_user:
            games_played = games_by_user[user_key]
        elif SP_HAS_GAMES_PLAYED:
            games_played = _rget(r, "games_played")

        if (
            user_key is not None
            and user_key in peak_by_user
            and peak_by_user[user_key] is not None
        ):
            peak_mmr = peak_by_user[user_key]
        else:
            peak_mmr = _rget(r, "mmr")

        last10_games = None
        last10_wins = None
        last10_losses = None
        last10_mmr_delta = None
        if user_key is not None and user_key in last10_by_user:
            s = last10_by_user[user_key]
            last10_games = s.get("games")
            last10_wins = s.get("wins")
            last10_losses = s.get("losses")
            last10_mmr_delta = s.get("mmr_delta")

        selected_title = None
        title_key = _rget(r, "title_key")
        parsed_title = _parse_season_title_key(title_key)
        if parsed_title:
            title_season_id, title_place = parsed_title
            selected_title = _season_title_payload(
                title_season_id,
                _rget(r, "title_season_name"),
                title_place,
            )

        out.append(
            {
                "id": user_id,
                "discordId": discord_id,
                "name": name,
                "mmr": _rget(r, "mmr"),
                "peakMmr": peak_mmr,
                "peak_mmr": peak_mmr,
                "country": _rget(r, "country"),
                "rank": _rget(r, "rank"),
                "pfp": pfp_url,
                "seasonTitle": selected_title,
                "season_title": selected_title,
                "selectedTitle": selected_title,
                "selected_title": selected_title,
                "activeTitle": selected_title,
                "active_title": selected_title,
                "in_guild": _rget(r, "in_guild") if PLAYERS_HAS_IN_GUILD else None,
                "wins": wins,
                "losses": losses,
                "gamesPlayed": games_played,
                "matchesPlayed": games_played,
                "matches": games_played,
                "last10Games": last10_games,
                "last10Wins": last10_wins,
                "last10Losses": last10_losses,
                "last10MmrDelta": last10_mmr_delta,
            }
        )
    return out


# Modelo Pydantic para las canciones de un match
class MatchSongIn(BaseModel):
    songName: str
    level: str
    difficulty: str
    notesCount: int


# --------- Helper: parsear canciones desde el texto del result ---------
def extract_songs_from_result_text(result_text: Optional[str]) -> List[Dict[str, Any]]:
    """
    Intenta extraer canciones desde un bloque de texto de result (como el embed).
    Busca líneas que empiecen con 'Lv ' y tengan el formato:

      Lv 24 Expert - Song Name (1002 notes)

    Devuelve una lista de dicts con: songName, level, difficulty, notesCount.
    """
    if not result_text:
        return []

    lines = result_text.splitlines()
    song_lines: List[str] = []

    for ln in lines:
        s = ln.strip().lstrip("|").strip()
        if s.startswith("Lv "):
            song_lines.append(s)

    if not song_lines:
        return []

    song_re = re.compile(
        r"^Lv\s*(\d+)\s+([A-Za-z]+)\s+[-–]\s+(.+?)(?:\s*\((\d+)\s*notes?\))?\s*$"
    )

    songs: List[Dict[str, Any]] = []
    for line in song_lines:
        m = song_re.match(line)
        if not m:
            continue
        level = m.group(1)
        difficulty = m.group(2)
        title = m.group(3).strip()
        notes = m.group(4)

        try:
            notes_int = int(notes) if notes is not None else 0
        except ValueError:
            notes_int = 0

        songs.append(
            {
                "songName": title,
                "level": level,
                "difficulty": difficulty,
                "notesCount": notes_int,
            }
        )

    return songs


# ======================= API: Matches =======================
@app.get("/api/matches")
async def get_matches(
    request: Request,
    season_id: Optional[int] = Query(
        None,
        description=(
            "Season ID: "
            "omitir = todas las seasons, "
            "0 = season activa/última, "
            ">0 = season específica"
        ),
    ),
    user_id: Optional[str] = Query(
        None, description="Filtra solo partidas donde participa este user_id (opcional)"
    ),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
):
    """
    Devuelve las partidas desde match_history, agrupadas por result_id (una entrada por match),
    ordenadas de más vieja a más nueva.

    - No enviado -> todas las seasons.
    - 0         -> season activa/última.
    - >0        -> season específica (valida que exista).
    Si user_id está presente, solo devuelve matches donde participa ese jugador,
    pero cada match incluye a TODOS los jugadores en players[].
    Incluye songs[] con la lista de canciones registradas para ese match.
    """
    if not db_pool:
        raise HTTPException(500, "DB pool not initialized")

    songs_by_result: Dict[int, List[Dict]] = {}

    async with db_pool.acquire() as conn:
        await _ensure_matches_table(conn)
        if not HAS_MATCHES_TABLE:
            return []

        picked_season: Optional[int] = None
        raw_q_season = request.query_params.get("season_id")

        if raw_q_season is not None:
            try:
                requested = int(season_id or 0)
            except (TypeError, ValueError):
                raise HTTPException(400, "season_id must be an integer")

            try:
                picked_season = await _pick_effective_season(conn, requested)
            except HTTPException:
                return []

        where_clauses = []
        params: List = []

        if picked_season is not None:
            where_clauses.append(f"season_id = ${len(params) + 1}")
            params.append(picked_season)

        if user_id is not None:
            where_clauses.append(f"user_id::text = ${len(params) + 1}")
            params.append(str(user_id))

        where_sql = ""
        if where_clauses:
            where_sql = "WHERE " + " AND ".join(where_clauses)

        params.append(limit)
        params.append(offset)
        limit_idx = len(params) - 1
        offset_idx = len(params)

        sql_results = f"""
            SELECT result_id,
                   MIN(played_at) AS first_played_at
            FROM public.match_history
            {where_sql}
            GROUP BY result_id
            ORDER BY first_played_at ASC, result_id ASC
            LIMIT ${limit_idx} OFFSET ${offset_idx}
        """

        res_rows = await conn.fetch(sql_results, *params)
        if not res_rows:
            return []

        result_ids_text: List[str] = [
            str(r["result_id"]) for r in res_rows if r["result_id"] is not None
        ]
        if not result_ids_text:
            return []

        if RESULTS_TEXT_COL:
            select_result_text = f'r."{RESULTS_TEXT_COL}" AS result_text'
        else:
            select_result_text = "NULL::text AS result_text"

        rows = await conn.fetch(
            f"""
            SELECT
                mh.id,
                mh.season_id,
                s.name AS season_name,
                mh.result_id,
                mh.user_id,
                mh.pos,
                mh.s_perfect,
                mh.s_great,
                mh.s_good,
                mh.s_bad,
                mh.s_miss,
                mh.mmr_before,
                mh.mmr_after,
                mh.mmr_delta,
                mh.was_placement,
                mh.is_win,
                mh.played_at,
                p.name    AS player_name,
                p.country AS player_country,
                r.gap_low   AS gap_low,
                r.gap_high  AS gap_high,
                r.room_mmr  AS room_mmr,
                {select_result_text}
            FROM public.match_history mh
            LEFT JOIN public.players p ON p.user_id = mh.user_id
            LEFT JOIN public.seasons s ON s.id = mh.season_id
            LEFT JOIN public.results  r ON r.result_id = mh.result_id
            WHERE mh.result_id::text = ANY($1::text[])
            ORDER BY mh.played_at ASC, mh.result_id ASC, mh.pos ASC, mh.id ASC
            """,
            result_ids_text,
        )

        # Cargar canciones asociadas
        try:
            song_rows = await conn.fetch(
                """
                SELECT
                    id,
                    result_id,
                    song_name,
                    level,
                    difficulty,
                    notes_count
                FROM public.match_songs
                WHERE result_id::text = ANY($1::text[])
                ORDER BY id
                """,
                result_ids_text,
            )

            for sr in song_rows:
                rid_raw = sr["result_id"]
                try:
                    rid = int(rid_raw)
                except Exception:
                    rid = rid_raw

                songs_by_result.setdefault(rid, []).append(
                    {
                        "songName": sr["song_name"],
                        "level": sr["level"],
                        "difficulty": sr["difficulty"],
                        "notesCount": sr["notes_count"],
                    }
                )
        except Exception:
            traceback.print_exc()
            songs_by_result = {}

    matches_by_result: Dict[int, Dict] = {}

    for r in rows:
        rid_raw = r["result_id"]
        try:
            rid = int(rid_raw)
        except Exception:
            rid = rid_raw

        played_dt: Optional[datetime] = r["played_at"]

        m = matches_by_result.get(rid)
        if not m:
            m = {
                "id": rid,
                "season_id": r["season_id"],
                "seasonId": r["season_id"],
                "season_name": _rget(r, "season_name"),
                "result_id": rid,
                "resultId": rid,
                "played_at": played_dt,
                "playedAt": played_dt.isoformat() if played_dt else None,
                "gap_low": _rget(r, "gap_low"),
                "gapLow": _rget(r, "gap_low"),
                "gap_high": _rget(r, "gap_high"),
                "gapHigh": _rget(r, "gap_high"),
                "room_mmr": _rget(r, "room_mmr"),
                "roomMmr": _rget(r, "room_mmr"),
                "avg_mmr": None,
                "avgMmr": None,
                "players": [],
                "songs": songs_by_result.get(rid, []),
                "result_text": _rget(r, "result_text"),
            }
            matches_by_result[rid] = m

        s_p = int(r["s_perfect"] or 0)
        s_g = int(r["s_great"] or 0)
        s_g2 = int(r["s_good"] or 0)
        s_b = int(r["s_bad"] or 0)
        s_m = int(r["s_miss"] or 0)
        points = s_p * 4 + s_g * 3 + s_g2 * 2 + s_b * 1

        player_entry = {
            "user_id": r["user_id"],
            "userId": r["user_id"],
            "name": _rget(r, "player_name"),
            "country": _rget(r, "player_country"),
            "pos": r["pos"],
            "mmr_before": r["mmr_before"],
            "mmrBefore": r["mmr_before"],
            "mmr_after": r["mmr_after"],
            "mmrAfter": r["mmr_after"],
            "mmr_delta": r["mmr_delta"],
            "mmrDelta": r["mmr_delta"],
            "is_win": r["is_win"],
            "isWin": r["is_win"],
            "was_placement": r["was_placement"],
            "wasPlacement": r["was_placement"],
            "s_perfect": s_p,
            "sPerfect": s_p,
            "s_great": s_g,
            "sGreat": s_g,
            "s_good": s_g2,
            "sGood": s_g2,
            "s_bad": s_b,
            "sBad": s_b,
            "s_miss": s_m,
            "sMiss": s_m,
            "points": points,
            "pts": points,
        }

        matches_by_result[rid].setdefault("players", []).append(player_entry)

    for m in matches_by_result.values():
        mmrs = [p["mmr_before"] for p in m["players"] if p["mmr_before"] is not None]
        avg = int(sum(mmrs) / len(mmrs)) if mmrs else None
        m["avg_mmr"] = avg
        m["avgMmr"] = avg

        if m.get("room_mmr") is None:
            m["room_mmr"] = avg
            m["roomMmr"] = avg

        if not m.get("songs") and m.get("result_text"):
            try:
                parsed_songs = extract_songs_from_result_text(m["result_text"])
            except Exception:
                parsed_songs = []
            if parsed_songs:
                m["songs"] = parsed_songs

        m.pop("result_text", None)

    matches_list = list(matches_by_result.values())
    matches_list.sort(
        key=lambda x: (
            x["played_at"] if isinstance(x["played_at"], datetime) else datetime.min,
            x["id"],
        )
    )
    for m in matches_list:
        dt = m.get("played_at")
        m["played_at"] = dt.isoformat() if isinstance(dt, datetime) else None
        m["playedAt"] = m["played_at"]

    return matches_list


@app.post("/api/matches/{result_id}/songs")
async def save_match_songs(result_id: int, songs: List[MatchSongIn]):
    """
    Guarda la lista de canciones jugadas en un match (result_id).
    Sobrescribe las que hubiera antes.
    """
    if not db_pool:
        raise HTTPException(500, "DB pool not initialized")

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM public.match_songs WHERE result_id = $1", result_id
            )

            records = [
                (result_id, s.songName, s.level, s.difficulty, s.notesCount)
                for s in songs
            ]
            if records:
                await conn.executemany(
                    """
                    INSERT INTO public.match_songs(result_id, song_name, level, difficulty, notes_count)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    records,
                )

    return {"ok": True, "resultId": result_id, "songsSaved": len(songs)}


@app.get("/api/matches/{result_id}/songs")
async def get_match_songs(result_id: int):
    """
    Devuelve las canciones asociadas a un match (result_id).
    """
    if not db_pool:
        raise HTTPException(500, "DB pool not initialized")

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT song_name, level, difficulty, notes_count
            FROM public.match_songs
            WHERE result_id = $1
            ORDER BY id
            """,
            result_id,
        )

    return [
        {
            "songName": r["song_name"],
            "level": r["level"],
            "difficulty": r["difficulty"],
            "notesCount": r["notes_count"],
        }
        for r in rows
    ]


# =================== Sekaipedia: anti-bot friendly ===================
SEKAIPEDIA_LIST = "https://sekaipedia.org/wiki/List_of_cards"
SEKAIPEDIA_API = "https://sekaipedia.org/w/api.php"

_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Mobile Safari/537.36",
]


def _ascii(s: str) -> str:
    return (
        unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    )


def _mk_headers(referer: Optional[str] = None) -> Dict[str, str]:
    raw = {
        "User-Agent": random.choice(_UA_POOL),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Referer": referer or "https://sekaipedia.org/",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "DNT": "1",
        "Sec-Fetch-Mode": "navigate",
    }
    return {k: _ascii(v) for k, v in raw.items()}


_cards_cache: Dict[str, object] = {
    "items": [],
    "ts": datetime.fromtimestamp(0, tz=timezone.utc),
}
CARDS_TTL = timedelta(minutes=int(os.getenv("CARDS_TTL_MINUTES", "15")))
_cards_lock = asyncio.Lock()

# --------- AUTO REFRESH (background poller) ----------
_cards_stop = asyncio.Event()
_cards_task: Optional[asyncio.Task] = None

# Heurística de "incompleto": si baja de 90% del conteo previo, asumimos truncado/fallo upstream.
CARDS_INCOMPLETE_RATIO = float(os.getenv("CARDS_INCOMPLETE_RATIO", "0.90"))


async def _sleep_jitter(a=0.15, b=0.5):
    await asyncio.sleep(random.uniform(a, b))


async def _get_json(client: httpx.AsyncClient, url: str, params: Dict) -> Dict:
    for attempt in range(5):
        try:
            headers = _mk_headers(SEKAIPEDIA_LIST)
            params2 = dict(params)
            params2.setdefault("origin", "*")
            r = await client.get(
                url, params=params2, headers=headers, follow_redirects=True, timeout=30
            )
            if r.status_code in (403, 429):
                await asyncio.sleep(1.2 + attempt * 0.6)
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == 4:
                raise
            await asyncio.sleep(0.8 + attempt * 0.5)
    return {}


def _abs_sekaipedia(u: str) -> str:
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return "https://sekaipedia.org" + u
    return u


# =================== Persistencia: helpers BD (cartas / variantes) ===================
async def _cards_db_all(conn: asyncpg.Connection) -> List[Dict]:
    rows = await conn.fetch(
        """
        SELECT page_id AS id, name, href, thumb
        FROM public.sekaicards
        ORDER BY page_id DESC
    """
    )
    return [
        {"id": str(r["id"]), "name": r["name"], "href": r["href"], "thumb": r["thumb"]}
        for r in rows
    ]


async def _cards_db_count(conn: asyncpg.Connection) -> int:
    row = await conn.fetchrow("SELECT COUNT(*) AS n FROM public.sekaicards")
    return int(row["n"] or 0)


async def _cards_db_upsert_many(conn: asyncpg.Connection, items: List[Dict]) -> None:
    if not items:
        return
    await conn.executemany(
        """
        INSERT INTO public.sekaicards(page_id, name, href, thumb, updated_at)
        VALUES ($1, $2, $3, $4, now())
        ON CONFLICT (page_id) DO UPDATE
          SET name = EXCLUDED.name,
              href = EXCLUDED.href,
              thumb = EXCLUDED.thumb,
              updated_at = now()
    """,
        [(int(i["id"]), i["name"], i["href"], i.get("thumb")) for i in items],
    )


async def _cards_db_is_fresh(conn: asyncpg.Connection) -> bool:
    row = await conn.fetchrow("SELECT max(updated_at) AS ts FROM public.sekaicards")
    if not row or not row["ts"]:
        return False
    return (datetime.now(timezone.utc) - row["ts"]) < CARDS_TTL


async def _variants_db_get(
    conn: asyncpg.Connection, page_id: int
) -> Tuple[List[Dict], Optional[datetime]]:
    rows = await conn.fetch(
        """
        SELECT label, url, updated_at
        FROM public.sekaicard_variants
        WHERE page_id=$1
        ORDER BY
          CASE
            WHEN label = 'Untrained' THEN 0
            WHEN label = 'Trained'   THEN 1
            ELSE 2
          END,
          label
    """,
        page_id,
    )
    variants = [{"label": r["label"], "url": r["url"]} for r in rows]
    ts = max((r["updated_at"] for r in rows), default=None)
    return variants, ts


async def _variants_db_save(
    conn: asyncpg.Connection, page_id: int, variants: List[Dict]
) -> None:
    await conn.execute(
        "DELETE FROM public.sekaicard_variants WHERE page_id=$1", page_id
    )
    if not variants:
        return
    await conn.executemany(
        """
        INSERT INTO public.sekaicard_variants(page_id, label, url, updated_at)
        VALUES ($1, $2, $3, now())
    """,
        [(page_id, v["label"], v["url"]) for v in variants],
    )


# =================== Scraping helpers ===================
_BAD_KEYWORDS = [
    "banner",
    "sprite",
    "chibi",
    "icon",
    "logo",
    "title",
    "sticker",
    "stamp",
    "background",
    "bg",
    "skill",
    "cutin",
    "home",
    "mini",
    "ui",
    "costume",
    "profile",
    "story",
    "stamp",
    "april",
    "joke",
]


def _filename_from_url(u: str) -> str:
    try:
        return urlparse(u).path.rsplit("/", 1)[-1].lower()
    except Exception:
        return u.lower()


def _is_trained(fn: str) -> bool:
    s = fn.lower()
    return ("trained" in s) or ("after training" in s) or ("idolized" in s)


def _is_untrained(fn: str) -> bool:
    s = fn.lower()
    return (
        ("untrained" in s)
        or ("before training" in s)
        or ("base" in s)
        or ("normal" in s)
    )


def _looks_card_image(fn: str) -> bool:
    if any(bad in fn for bad in _BAD_KEYWORDS):
        return False
    return fn.endswith((".png", ".jpg", ".jpeg", ".webp"))


def _prefer_square(links: List[str]) -> List[str]:
    seen, out = set(), []
    for u in links:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _pick_first_distinct(urls: List[str], *exclude: Optional[str]) -> Optional[str]:
    excluded = {u for u in exclude if u}
    for u in urls:
        if u and u not in excluded:
            return u
    return None


def _page_title_from_href(href: str) -> str:
    try:
        path = urlparse(href).path
        title = path.split("/wiki/", 1)[1]
        return unquote(title).replace("_", " ").strip()
    except Exception:
        return ""


_VARIANT_TOKEN_STOPWORDS = {
    "file",
    "card",
    "art",
    "image",
    "full",
    "transparent",
    "render",
    "trimmed",
    "cropped",
    "small",
    "large",
    "thumb",
    "thumbnail",
    "after",
    "before",
    "training",
    "trained",
    "untrained",
    "normal",
    "base",
}


def _normalize_media_key(source: str) -> str:
    raw = unquote(str(source or "")).strip().lower()
    if "://" in raw or "/" in raw:
        raw = _filename_from_url(raw)
    raw = raw.rsplit(".", 1)[0]
    raw = raw.replace("_", " ").replace("-", " ")
    raw = re.sub(r"\([^)]*\)", " ", raw)
    raw = re.sub(r"(?:after|before)\s+training", " ", raw)
    raw = re.sub(
        r"(?:trained|untrained|idolized|normal|base|card|art|image|full|render|transparent|trimmed|cropped|thumb|thumbnail|small|large)",
        " ",
        raw,
    )
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def _media_tokens(source: str) -> List[str]:
    key = _normalize_media_key(source)
    return [
        tok
        for tok in re.findall(r"[a-z0-9]+", key)
        if len(tok) >= 2 and tok not in _VARIANT_TOKEN_STOPWORDS
    ]


def _variant_candidate_score(
    candidate_title: str,
    candidate_url: str,
    page_title: str,
    thumb_url: Optional[str],
    page_id: Optional[int] = None,
) -> int:
    combined = f"{candidate_title} {candidate_url}".strip()
    key = _normalize_media_key(combined)
    tokens = set(_media_tokens(combined))

    page_key = _normalize_media_key(page_title)
    page_tokens = set(_media_tokens(page_title))
    thumb_key = _normalize_media_key(thumb_url or "")
    thumb_tokens = set(_media_tokens(thumb_url or ""))

    score = 0
    if page_id and str(page_id) in key:
        score += 12

    if thumb_key:
        if key == thumb_key:
            score += 100
        if thumb_key and (thumb_key in key or key in thumb_key):
            score += 35
        score += len(tokens & thumb_tokens) * 8

    if page_key:
        if page_key and (page_key in key or key in page_key):
            score += 18
        score += len(tokens & page_tokens) * 4

    filename = _filename_from_url(candidate_url)
    if _is_trained(filename) or _is_trained(candidate_title):
        score += 3
    if _is_untrained(filename) or _is_untrained(candidate_title):
        score += 3

    if any(bad in key for bad in _BAD_KEYWORDS):
        score -= 20

    return score


async def _fetch_page_images(client: httpx.AsyncClient, page_title: str) -> List[Dict[str, Any]]:
    if not page_title:
        return []
    data1 = await _get_json(
        client,
        SEKAIPEDIA_API,
        {
            "action": "query",
            "prop": "images",
            "titles": page_title,
            "imlimit": "500",
            "format": "json",
        },
    )
    pages = (data1.get("query", {}) or {}).get("pages", {}) or {}
    file_titles: List[str] = []
    for _, pinfo in pages.items():
        for it in pinfo.get("images") or []:
            t = it.get("title", "")
            if t.startswith("File:"):
                file_titles.append(t)
    if not file_titles:
        return []

    items: List[Dict[str, Any]] = []
    seen = set()
    for i in range(0, len(file_titles), 50):
        chunk = file_titles[i : i + 50]
        data2 = await _get_json(
            client,
            SEKAIPEDIA_API,
            {
                "action": "query",
                "titles": "|".join(chunk),
                "prop": "imageinfo",
                "iiprop": "url|size|mime",
                "iiurlwidth": "512",
                "format": "json",
            },
        )
        for _, finfo in (data2.get("query", {}).get("pages", {}) or {}).items():
            title = finfo.get("title", "") or ""
            for ii in finfo.get("imageinfo") or []:
                url = ii.get("thumburl") or ii.get("url")
                if not url:
                    continue
                abs_url = _abs_sekaipedia(url)
                if abs_url in seen:
                    continue
                seen.add(abs_url)
                items.append(
                    {
                        "title": title,
                        "url": abs_url,
                        "width": ii.get("thumbwidth") or ii.get("width"),
                        "height": ii.get("thumbheight") or ii.get("height"),
                    }
                )
        await _sleep_jitter(0.2, 0.5)
    return items


async def _scrape_card_variants(
    href: str,
    thumb_url: Optional[str] = None,
    page_id: Optional[int] = None,
) -> Dict:
    """
    Variante robusta y conservadora:
      - usa el thumb conocido como referencia principal
      - solo acepta imágenes con suficiente similitud con esa carta
      - prefiere devolver 1 variante correcta antes que 2 con personaje equivocado
    """
    page_title = _page_title_from_href(href)
    stage = "start"
    try:
        stage = "fetch_images"
        async with httpx.AsyncClient() as client:
            raw_items = await _fetch_page_images(client, page_title)

        stage = "score_candidates"
        candidates: List[Dict[str, Any]] = []
        for item in raw_items:
            title = str(item.get("title") or "")
            url = str(item.get("url") or "")
            if not url:
                continue

            file_ref = f"{title} {_filename_from_url(url)}".lower()
            if not _looks_card_image(file_ref):
                continue

            score = _variant_candidate_score(title, url, page_title, thumb_url, page_id)
            min_score = 8 if thumb_url else 4
            if score < min_score:
                continue

            candidates.append(
                {
                    "title": title,
                    "url": url,
                    "score": score,
                    "filename": _filename_from_url(url),
                }
            )

        candidates.sort(key=lambda x: x["score"], reverse=True)

        if not raw_items:
            logger.error(
                "[variants] no images found via MediaWiki images for page=%s href=%s",
                page_title,
                href,
            )
        if raw_items and not candidates:
            logger.warning(
                "[variants] images found but none matched enough relevance page=%s href=%s count=%s",
                page_title,
                href,
                len(raw_items),
            )

        labeled_untrained = [
            c
            for c in candidates
            if _is_untrained(c["filename"]) or _is_untrained(c["title"])
        ]
        labeled_trained = [
            c
            for c in candidates
            if _is_trained(c["filename"]) or _is_trained(c["title"])
        ]

        untrained_url: Optional[str] = thumb_url or None
        if not untrained_url and labeled_untrained:
            untrained_url = labeled_untrained[0]["url"]
        if not untrained_url and candidates:
            untrained_url = candidates[0]["url"]

        trained_url: Optional[str] = None
        for cand in labeled_trained:
            if cand["url"] != untrained_url:
                trained_url = cand["url"]
                break

        if not trained_url:
            min_secondary = 12 if thumb_url else 8
            for cand in candidates:
                if cand["url"] == untrained_url:
                    continue
                if cand["score"] < min_secondary:
                    continue
                trained_url = cand["url"]
                break

        variants: List[Dict[str, str]] = []
        if untrained_url:
            variants.append({"label": "Untrained", "url": untrained_url})
        if trained_url and trained_url != untrained_url:
            variants.append({"label": "Trained", "url": trained_url})

        if candidates:
            logger.info(
                "[variants] page=%s href=%s candidates=%s best=%s returned=%s",
                page_title,
                href,
                len(candidates),
                candidates[0]["score"],
                len(variants),
            )

        return {"name": page_title, "variants": variants}
    except Exception as e:
        logger.exception(
            "[variants] scrape failed stage=%s page=%s href=%s error=%s",
            stage,
            page_title,
            href,
            str(e),
        )
        raise


def _build_thumb_from_filename(filename: str, width: int = 256) -> str:
    safe_name = quote(filename)
    return f"https://sekaipedia.org/wiki/Special:FilePath/{safe_name}?width={width}"


# =================== NUEVO: Parser robusto del wikitext de List_of_cards ===================
def _strip_wiki_markup(s: str) -> str:
    s = re.sub(r"\{\{.*?\}\}", "", s, flags=re.S)  # templates
    s = re.sub(r"<.*?>", "", s, flags=re.S)  # html
    return s.strip()


def _cells_from_row(row: str) -> List[str]:
    """
    Soporta:
      | 1292 || [[Page|Name]] || ...
    y también:
      | 1292
      | [[Page|Name]]
      | ...
    """
    cells: List[str] = []
    for ln in row.splitlines():
        s = ln.lstrip()
        if not s.startswith("|"):
            continue
        s = s[1:].strip()
        parts = [p.strip() for p in s.split("||")]
        for p in parts:
            if p != "":
                cells.append(p)
    return cells


def _extract_wikitable_blocks(wikitext: str) -> List[str]:
    # Captura todas las wikitables completas: {| ... |}
    return re.findall(r"\{\|.*?\n\|\}", wikitext, flags=re.S)


def _parse_cards_from_wikitext(wikitext: str) -> Tuple[List[Dict], Dict[str, int]]:
    """
    Parseo tolerante: recorre todas las wikitables, extrae filas con:
      - id numérico en la primera celda
      - link [[...]] en la segunda celda (o en alguna celda cercana)
      - primer File:... para thumb (si existe)

    Retorna (items, diag)
    """
    items: List[Dict] = []
    tables = _extract_wikitable_blocks(wikitext)
    if not tables:
        # fallback: intentar split simple si no matchea
        tables = re.split(r"\n\|\}", wikitext)

    rows_scanned = 0
    for table in tables:
        # saltamos tablas irrelevantes si definitivamente no tienen IDs numéricos
        if not re.search(r"\b\d{1,6}\b", table):
            continue

        rows = re.split(r"\n\|-+", table)
        for row in rows:
            rows_scanned += 1
            # ignorar headers
            if row.strip().startswith("!"):
                continue
            if "! " in row:
                continue

            cells = _cells_from_row(row)
            if not cells:
                continue

            # 1) ID (buscar en la primera celda, y si falla, en toda la fila)
            id_cell = _strip_wiki_markup(cells[0])
            m_id = re.search(r"\b(\d{1,6})\b", id_cell)
            if not m_id:
                m_id = re.search(r"\b(\d{1,6})\b", row)
            if not m_id:
                continue
            card_id = m_id.group(1)

            # 2) Nombre + href
            name_cell = cells[1] if len(cells) >= 2 else row
            m_name = re.search(r"\[\[([^|\]]+)(?:\|([^]]+))?\]\]", name_cell)
            if not m_name:
                m_name = re.search(r"\[\[([^|\]]+)(?:\|([^]]+))?\]\]", row)
            if m_name:
                page_title = m_name.group(1).strip()
                display_name = (m_name.group(2) or page_title).strip()
            else:
                display_name = _strip_wiki_markup(name_cell)
                page_title = display_name.replace(" ", "_")

            href = f"https://sekaipedia.org/wiki/{page_title.replace(' ', '_')}"

            # 3) imagen (thumb)
            m_file = re.search(r"\[\[\s*File:([^|\]]+)", row, flags=re.I)
            thumb_url = (
                _build_thumb_from_filename(m_file.group(1), width=256)
                if m_file
                else None
            )

            items.append(
                {"id": card_id, "name": display_name, "href": href, "thumb": thumb_url}
            )

    seen = set()
    out: List[Dict] = []
    for it in items:
        cid = str(it.get("id"))
        if cid in seen:
            continue
        seen.add(cid)
        out.append(it)

    try:
        out.sort(key=lambda x: int(str(x["id"])))
    except Exception:
        pass

    diag = {
        "tables_found": len(tables),
        "rows_scanned": rows_scanned,
        "items_raw": len(items),
        "items_dedup": len(out),
    }
    return out, diag


# ---------- Scraper real: página única List_of_cards (sin límites) ----------
async def _scrape_cards_from_sekaipedia() -> List[Dict]:
    """
    Lee el wikitext de "List_of_cards" y lo parsea.
    Usa action=query+revisions (más estable para payloads grandes),
    y fallback a action=parse si no viene content.

    Logs detallados de fallos / parse incompleto.
    """
    stage = "start"
    t0 = datetime.now(timezone.utc)
    async with httpx.AsyncClient() as client:
        # 1) Preferido: revisions content (menos propenso a truncar)
        stage = "fetch_revisions"
        data = await _get_json(
            client,
            SEKAIPEDIA_API,
            {
                "action": "query",
                "prop": "revisions",
                "rvprop": "content",
                "rvslots": "main",
                "titles": "List_of_cards",
                "format": "json",
                "formatversion": 2,
            },
        )

        wikitext = ""
        try:
            pages = (data.get("query") or {}).get("pages") or []
            if pages and (pages[0].get("revisions") or []):
                rev0 = pages[0]["revisions"][0]
                slots = rev0.get("slots") or {}
                main = slots.get("main") or {}
                wikitext = main.get("content") or ""
        except Exception:
            wikitext = ""

        # 2) Fallback: action=parse
        if not wikitext:
            stage = "fetch_parse_fallback"
            data2 = await _get_json(
                client,
                SEKAIPEDIA_API,
                {
                    "action": "parse",
                    "page": "List_of_cards",
                    "prop": "wikitext",
                    "format": "json",
                    "formatversion": 2,
                },
            )
            parse = (data2 or {}).get("parse") or {}
            wikitext = parse.get("wikitext") or ""

    if not wikitext:
        logger.error("[cards] wikitext empty stage=%s (upstream returned no content)", stage)
        return []

    stage = "parse_wikitext"
    items, diag = _parse_cards_from_wikitext(wikitext)

    elapsed_ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
    logger.info("[cards] parse done ms=%s diag=%s", elapsed_ms, diag)

    # sanity minimum: si devuelve demasiado poco, lo tratamos como fallo (no como lista válida)
    if len(items) < 200:
        logger.error("[cards] parse produced too few items count=%s diag=%s", len(items), diag)
        return []

    return items


async def _refresh_cards_once() -> None:
    """
    Refresca cartas si la BD no está fresca.
    Actualiza BD + cache en memoria.
    Usa _cards_lock para evitar scrapes concurrentes con /auth02/cards.

    Si el scrape devuelve "menos de 90% (configurable)" del conteo previo en BD,
    se considera INCOMPLETO y se loggea error (sin pisar DB).
    """
    if not db_pool:
        return

    async with _cards_lock:
        try:
            async with db_pool.acquire() as conn:
                if await _cards_db_is_fresh(conn):
                    return
                old_count = await _cards_db_count(conn)

            stage = "scrape"
            items = await _scrape_cards_from_sekaipedia()
            if not items:
                logger.error("[cards] refresh failed: scrape returned 0 items stage=%s", stage)
                return

            new_count = len(items)
            if old_count and new_count < int(old_count * CARDS_INCOMPLETE_RATIO):
                logger.error(
                    "[cards] INCOMPLETE scrape detected (auto). old=%s new=%s ratio=%s -> SKIP DB update",
                    old_count,
                    new_count,
                    CARDS_INCOMPLETE_RATIO,
                )
                return

            stage = "db_upsert"
            async with db_pool.acquire() as conn:
                await _cards_db_upsert_many(conn, items)

            _cards_cache["items"] = items
            _cards_cache["ts"] = datetime.now(timezone.utc)
            logger.info("✅ Cards refreshed (auto): %s (old_db=%s)", len(items), old_count)
        except Exception as e:
            logger.exception("[cards] refresh failed (auto) error=%s", str(e))


async def _refresh_cards_forever() -> None:
    await asyncio.sleep(3)
    while not _cards_stop.is_set():
        await _refresh_cards_once()
        try:
            await asyncio.wait_for(
                _cards_stop.wait(), timeout=CARDS_TTL.total_seconds()
            )
        except asyncio.TimeoutError:
            pass


# =================== API: /auth02/cards (con persistencia + force refresh) ===================
@app.get("/auth02/cards")
async def auth02_cards(
    request: Request,
    force: int = Query(0, description="1 = fuerza refresh inmediato (requiere sesión)"),
):
    """
    Devuelve lista de cartas de Sekaipedia (persistente en BD + cache).

    - Por defecto, usa BD/cache si está fresco.
    - Si force=1, ignora freshness y fuerza scrape + upsert.
      Protegido por sesión (cookie sid) para evitar abuso.

    Si el scrape devuelve una lista INCOMPLETA (menos de 90% del conteo previo en BD),
    se loggea error y NO se pisa la BD; se devuelve la lista existente si hay.
    """
    force = bool(force)
    if force:
        require_auth_session(request)

    now = datetime.now(timezone.utc)

    if not force and db_pool:
        async with db_pool.acquire() as conn:
            try:
                if await _cards_db_is_fresh(conn):
                    items = await _cards_db_all(conn)
                    _cards_cache["items"] = items
                    _cards_cache["ts"] = datetime.now(timezone.utc)
                    return items
            except Exception:
                traceback.print_exc()

    if not force and _cards_cache["items"] and now - _cards_cache["ts"] < CARDS_TTL:
        return _cards_cache["items"]

    async with _cards_lock:
        now = datetime.now(timezone.utc)

        # recheck dentro del lock
        if not force and db_pool:
            async with db_pool.acquire() as conn:
                try:
                    if await _cards_db_is_fresh(conn):
                        items = await _cards_db_all(conn)
                        _cards_cache["items"] = items
                        _cards_cache["ts"] = datetime.now(timezone.utc)
                        return items
                except Exception:
                    traceback.print_exc()

        if not force and _cards_cache["items"] and now - _cards_cache["ts"] < CARDS_TTL:
            return _cards_cache["items"]

        old_count = 0
        existing: List[Dict] = []
        if db_pool:
            async with db_pool.acquire() as conn:
                try:
                    old_count = await _cards_db_count(conn)
                    existing = await _cards_db_all(conn)
                except Exception:
                    traceback.print_exc()
                    old_count = 0
                    existing = []

        stage = "scrape"
        try:
            items = await _scrape_cards_from_sekaipedia()
            if not items:
                logger.error("[cards] scrape returned 0 items stage=%s force=%s", stage, force)
                if existing:
                    return existing
                raise HTTPException(
                    status_code=503, detail="Upstream unavailable or parse error"
                )

            new_count = len(items)
            if old_count and new_count < int(old_count * CARDS_INCOMPLETE_RATIO):
                logger.error(
                    "[cards] INCOMPLETE scrape detected (/auth02/cards). old=%s new=%s ratio=%s -> KEEP EXISTING",
                    old_count,
                    new_count,
                    CARDS_INCOMPLETE_RATIO,
                )
                if existing:
                    return existing
                raise HTTPException(
                    status_code=503,
                    detail=f"Incomplete scrape detected (old={old_count}, new={new_count})",
                )

            _cards_cache["items"] = items
            _cards_cache["ts"] = datetime.now(timezone.utc)

            if db_pool:
                async with db_pool.acquire() as conn:
                    stage = "db_upsert"
                    await _cards_db_upsert_many(conn, items)

            logger.info("[cards] served fresh list count=%s old_db=%s force=%s", new_count, old_count, force)
            return items
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("[cards] failed stage=%s error=%s", stage, str(e))
            if existing:
                return existing
            raise HTTPException(status_code=503, detail=f"Failed to scrape: {e}")


# =================== API: /auth02/card-variants ===================
@app.get("/auth02/card-variants")
async def get_card_variants(
    href: str = Query(..., description="URL absoluta de la Card:")
) -> Dict:
    """
    Devuelve variantes separadas:
      - Untrained  (si no se detecta, fallback a thumb de sekaicards)
      - Trained    (si se detecta)
    Guarda cache en BD.
    Logs detallados cuando falle el scraping o venga vacío.
    """
    page_title = _page_title_from_href(href)

    page_id: Optional[int] = None
    thumb_url: Optional[str] = None

    # 1) intentamos cache BD
    if db_pool:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT page_id, thumb FROM public.sekaicards WHERE href=$1", href
            )
            if row:
                page_id = int(row["page_id"])
                thumb_url = row["thumb"]

            if page_id:
                variants, ts = await _variants_db_get(conn, page_id)
                if variants and ts and (datetime.now(timezone.utc) - ts) < CARDS_TTL:
                    return {"name": page_title, "variants": variants}

    # 2) scrape variantes
    scraped_variants: List[Dict] = []
    try:
        scraped = await _scrape_card_variants(href, thumb_url=thumb_url, page_id=page_id)
        scraped_variants = scraped.get("variants", []) or []
    except Exception:
        # ya se loggea dentro del scraper con stage
        scraped_variants = []

    # 3) normalizar: Untrained + Trained separados
    untrained_url = None
    trained_url = None
    for v in scraped_variants:
        lbl = (v.get("label") or "").strip().lower()
        if lbl == "untrained" and v.get("url"):
            untrained_url = v["url"]
        if lbl == "trained" and v.get("url"):
            trained_url = v["url"]

    variants: List[Dict] = []
    if untrained_url:
        variants.append({"label": "Untrained", "url": untrained_url})
    elif thumb_url:
        # fallback a thumb si no hay untrained detectado
        variants.append({"label": "Untrained", "url": thumb_url})

    if trained_url and trained_url != (variants[0]["url"] if variants else None):
        variants.append({"label": "Trained", "url": trained_url})

    if not variants:
        logger.error("[variants] EMPTY response for href=%s (thumb=%s scraped=%s)", href, bool(thumb_url), len(scraped_variants))

    # 4) persistir
    try:
        if db_pool and page_id:
            async with db_pool.acquire() as conn:
                await _variants_db_save(conn, page_id, variants)
    except Exception:
        logger.exception("[variants] failed to save variants page_id=%s href=%s", page_id, href)

    return {"name": page_title, "variants": variants}

# =================== Static ===================
static_dir = os.path.join(os.path.dirname(__file__), "public")
if os.path.isdir(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
