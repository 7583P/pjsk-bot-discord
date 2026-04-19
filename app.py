import os
import re
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
DISCORD_REDIRECT_URI = os.getenv(
    "DISCORD_REDIRECT_URI",
    "https://unique-education-production.up.railway.app/auth02/callback",
)
FRONT_URL = os.getenv("FRONT_URL", "https://pjsk-page.vercel.app")
CORS_ORIGIN = os.getenv("CORS_ORIGIN", FRONT_URL)
COOKIE_NAME = os.getenv("COOKIE_NAME", "sid")
DATABASE_URL = os.getenv("DATABASE_URL", "")

# ===================== FastAPI =====================
app = FastAPI()

_allowed_origins = []
for o in [FRONT_URL, CORS_ORIGIN]:
    if o and o not in _allowed_origins:
        _allowed_origins.append(o)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ================ Sesiones simples en memoria ================
SESSIONS: Dict[str, Dict] = {}  # sid -> {"id": "<discord_id>", "username": "..."}


def set_session_cookie(resp, sid: str):
    resp.set_cookie(
        key=COOKIE_NAME,
        value=sid,
        httponly=True,
        secure=True,
        samesite="none",
        path="/",
        max_age=60 * 60 * 24 * 7,  # 7 días
    )


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

# Tipo SQL efectivo del user_id ("text" o "bigint")
USER_ID_SQLTYPE: str = "text"  # default seguro


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
    global SEASONS_HAS_STARTS_AT, SEASONS_HAS_ENDS_AT, USER_ID_SQLTYPE
    global SP_HAS_WINS, SP_HAS_LOSSES, SP_HAS_GAMES_PLAYED
    global HAS_MATCHES_TABLE, HAS_MATCHPLAYERS_TABLE, RESULTS_TEXT_COL

    try:
        # players / seasons
        HAS_DISCORD_ID_COL = await _has_col(conn, "players", "discord_id")
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
            "seasonplayers.wins=%s losses=%s games_played=%s | matches_table=%s matchplayers_table=%s | user_id_type=%s | results_text_col=%s",
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


def _cleanup_state_store():
    now = datetime.now(timezone.utc)
    expired = [k for k, exp in STATE_STORE.items() if exp < now]
    for k in expired:
        STATE_STORE.pop(k, None)


async def _resolve_user_id_from_discord(discord_id: str) -> Optional[str]:
    if not (db_pool and HAS_DISCORD_ID_COL and discord_id):
        return None
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id FROM public.players WHERE discord_id = $1 LIMIT 1",
            discord_id,
        )
        if row:
            return str(row["user_id"])
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
        logger.info("OAuth OK user: %s %s", user.get("id"), user.get("username"))

    sid = secrets.token_hex(24)
    SESSIONS[sid] = {"id": str(user.get("id")), "username": user.get("username")}
    resp = RedirectResponse(url=f"{FRONT_URL}?verified=1", status_code=303)
    set_session_cookie(resp, sid)
    return resp


@app.get("/auth02/me")
async def auth02_me(request: Request):
    sid = request.cookies.get(COOKIE_NAME)
    if not sid or sid not in SESSIONS:
        raise HTTPException(status_code=401, detail="no-session")

    payload = dict(SESSIONS[sid])
    payload["resolvedUserId"] = await _resolve_user_id_from_discord(
        payload.get("id", "")
    )
    return payload


@app.post("/auth02/logout")
async def auth02_logout(request: Request):
    sid = request.cookies.get(COOKIE_NAME)
    if sid:
        SESSIONS.pop(sid, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


# =================== /auth02/pfps ===================
MAX_PFPS = 5000


@app.post("/auth02/set-pfp")
async def set_pfp(request: Request):
    """
    Guarda/actualiza el PFP global del usuario.
    """
    body = await request.json()
    url = (body.get("url") or "").strip()
    name_lower = (body.get("nameLower") or "").strip().lower()
    user_id = (body.get("userId") or "").strip()

    sid = request.cookies.get(COOKIE_NAME)
    session = SESSIONS.get(sid or "", {})
    discord_id = str(session.get("id") or "")

    if not user_id:
        resolved = await _resolve_user_id_from_discord(discord_id)
        if resolved:
            user_id = resolved

    if not name_lower and session:
        name_lower = (session.get("username") or "").lower()

    # Borrado
    if url == "":
        if user_id:
            PFP_BY_ID.pop(user_id, None)
        if discord_id:
            PFP_BY_DISCORD.pop(discord_id, None)
        if name_lower:
            PFP_BY_NAME.pop(name_lower, None)
        if db_pool and user_id:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM public.player_pfps WHERE user_id = $1",
                    _uid_for_sql(user_id),
                )
        return {"ok": True, "deleted": True}

    if not _valid_https_url(url):
        raise HTTPException(400, "invalid url")

    if not (user_id or discord_id or name_lower):
        raise HTTPException(400, "missing userId or discordId or nameLower")

    if len(PFP_BY_ID) + len(PFP_BY_NAME) + len(PFP_BY_DISCORD) > MAX_PFPS:
        raise HTTPException(429, "pfp store is full")

    # Guardar en memoria
    if user_id:
        PFP_BY_ID[user_id] = url
    if discord_id:
        PFP_BY_DISCORD[discord_id] = url
    if name_lower:
        PFP_BY_NAME[name_lower] = url

    # Persistir en BD
    if db_pool:
        if not user_id and discord_id:
            resolved = await _resolve_user_id_from_discord(discord_id)
            if resolved:
                user_id = resolved
        if user_id:
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

    return {
        "ok": True,
        "byId": bool(user_id),
        "byDiscord": bool(discord_id),
        "byName": bool(name_lower),
    }


@app.get("/auth02/pfps")
async def list_pfps():
    return JSONResponse(
        {"byId": PFP_BY_ID, "byDiscord": PFP_BY_DISCORD, "byName": PFP_BY_NAME}
    )


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


async def _fetch_page_images(client: httpx.AsyncClient, page_title: str) -> List[str]:
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
    urls: List[str] = []
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
            for ii in finfo.get("imageinfo") or []:
                url = ii.get("thumburl") or ii.get("url")
                if url:
                    urls.append(_abs_sekaipedia(url))
        await _sleep_jitter(0.2, 0.5)
    return _prefer_square(urls)


async def _scrape_card_variants(href: str) -> Dict:
    """
    Devuelve variantes separadas:
      - Untrained
      - Trained

    Estrategia:
      1) intenta detectar por nombre de archivo
      2) si solo encuentra una, usa otra imagen distinta como complemento
      3) si no encuentra etiquetas pero hay 2 imágenes válidas, usa las 2 primeras
    """
    page_title = _page_title_from_href(href)
    stage = "start"
    try:
        stage = "fetch_images"
        async with httpx.AsyncClient() as client:
            urls = await _fetch_page_images(client, page_title)

        stage = "filter_images"
        clean_urls = [u for u in urls if _looks_card_image(_filename_from_url(u))]
        clean_urls = _prefer_square(clean_urls)

        trained_links: List[str] = []
        untrained_links: List[str] = []
        for u in clean_urls:
            fn = _filename_from_url(u)
            if _is_trained(fn):
                trained_links.append(u)
            elif _is_untrained(fn):
                untrained_links.append(u)

        trained_url = trained_links[0] if trained_links else None
        untrained_url = untrained_links[0] if untrained_links else None

        if untrained_url and not trained_url:
            trained_url = _pick_first_distinct(clean_urls, untrained_url)
        elif trained_url and not untrained_url:
            untrained_url = _pick_first_distinct(clean_urls, trained_url)
        elif not untrained_url and not trained_url and len(clean_urls) >= 2:
            untrained_url = clean_urls[0]
            trained_url = _pick_first_distinct(clean_urls, untrained_url)

        if not urls:
            logger.error("[variants] no images found via MediaWiki images for page=%s href=%s", page_title, href)
        if urls and not clean_urls:
            logger.error("[variants] images found but none passed filter for page=%s href=%s count=%s", page_title, href, len(urls))
        if clean_urls and not (trained_url or untrained_url):
            logger.error("[variants] no trained/untrained matched after filter page=%s href=%s clean=%s", page_title, href, len(clean_urls))

        variants = []
        if untrained_url:
            variants.append({"label": "Untrained", "url": untrained_url})
        if trained_url and trained_url != untrained_url:
            variants.append({"label": "Trained", "url": trained_url})

        return {"name": page_title, "variants": variants}
    except Exception as e:
        logger.exception("[variants] scrape failed stage=%s page=%s href=%s error=%s", stage, page_title, href, str(e))
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
        sid = request.cookies.get(COOKIE_NAME)
        if not sid or sid not in SESSIONS:
            raise HTTPException(status_code=401, detail="no-session")

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
        scraped = await _scrape_card_variants(href)
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
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
