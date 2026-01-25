import os
import re
import secrets
import traceback
import asyncio
import random
import unicodedata
import html as _html
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple, Any
from urllib.parse import urlparse, unquote

import asyncpg
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

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


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# =========================== DB & Schema Flags ===========================
db_pool: Optional[asyncpg.pool.Pool] = None

HAS_DISCORD_ID_COL: bool = False
PLAYERS_HAS_IN_GUILD: bool = False

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
        HAS_MATCHPLAYERS_TABLE = False

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

        print(
            "🔎 Schema:"
            f" players.discord_id={HAS_DISCORD_ID_COL}"
            f" players.in_guild={PLAYERS_HAS_IN_GUILD}"
            f" | seasons.active={SEASONS_HAS_ACTIVE}"
            f" name={SEASONS_HAS_NAME}"
            f" starts_at={SEASONS_HAS_STARTS_AT}"
            f" ends_at={SEASONS_HAS_ENDS_AT}"
            f" | seasonplayers.wins={SP_HAS_WINS}"
            f" losses={SP_HAS_LOSSES}"
            f" games_played={SP_HAS_GAMES_PLAYED}"
            f" | matches_table={HAS_MATCHES_TABLE}"
            f" matchplayers_table={HAS_MATCHPLAYERS_TABLE}"
            f" | user_id_type={USER_ID_SQLTYPE}"
            f" | results_text_col={RESULTS_TEXT_COL}"
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
            print("✅ match_history detected at runtime")
    except Exception:
        traceback.print_exc()


@app.on_event("startup")
async def startup():
    global db_pool, _cards_task, _cards_stop

    if not DATABASE_URL:
        print("⚠️ DATABASE_URL is empty. /api endpoints will raise 500")
        return

    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=10,
        timeout=10.0,
        command_timeout=60.0,
    )
    print("✅ DB pool initialized")

    async with db_pool.acquire() as conn:
        await _detect_schema(conn)

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
        CREATE TABLE IF NOT EXISTS public.sekaicards_meta(
          key        TEXT PRIMARY KEY,
          value      TEXT NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
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
        print("🛑 DB pool closed")


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
    now = _now_utc()
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
    return s


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
    STATE_STORE[state] = _now_utc() + STATE_TTL
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
    if not exp or _now_utc() > exp:
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
        print("OAuth OK user:", user.get("id"), user.get("username"))

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
      - peakMmr desde match_history
    """
    if not db_pool:
        raise HTTPException(500, "DB pool not initialized")

    rows = []
    games_by_user: Dict[str, int] = {}
    last10_by_user: Dict[str, Dict[str, int]] = {}
    peak_by_user: Dict[str, int] = {}

    try:
        async with db_pool.acquire() as conn:
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


# ======================= Matches: Songs model =======================
class MatchSongIn(BaseModel):
    songName: str
    level: str
    difficulty: str
    notesCount: int


def extract_songs_from_result_text(result_text: Optional[str]) -> List[Dict[str, Any]]:
    """
    Extrae canciones desde un bloque de texto del result (embed).
    Formato buscado:
      Lv 24 Expert - Song Name (1002 notes)
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
    Devuelve partidas desde match_history, agrupadas por result_id,
    ordenadas de más vieja a más nueva (asc).
    Incluye songs[] si están en match_songs o si se pueden parsear desde results_text.
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

CARDS_REV_CHECK_MIN = int(os.getenv("CARDS_REV_CHECK_MIN", "10"))
CARDS_POLL_MIN = int(os.getenv("CARDS_POLL_MIN", str(CARDS_REV_CHECK_MIN)))
CARDS_FULL_TTL_HOURS = int(os.getenv("CARDS_FULL_TTL_HOURS", "24"))

CARD_IMG_WIDTH = int(os.getenv("CARD_IMG_WIDTH", "512"))

CARDS_REV_CHECK_TTL = timedelta(minutes=max(1, CARDS_REV_CHECK_MIN))
CARDS_POLL_TTL = timedelta(minutes=max(1, CARDS_POLL_MIN))
CARDS_FULL_TTL = timedelta(hours=max(1, CARDS_FULL_TTL_HOURS))

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
    "rev_id": None,
}
_cards_lock = asyncio.Lock()

_cards_stop = asyncio.Event()
_cards_task: Optional[asyncio.Task] = None

# Cache liviano del “max id remoto” para no spamear MediaWiki
_remote_max_cache: Dict[str, Any] = {
    "value": None,
    "ts": datetime.fromtimestamp(0, tz=timezone.utc),
}
REMOTE_MAX_CACHE_TTL = timedelta(minutes=2)


async def _sleep_jitter(a=0.15, b=0.5):
    await asyncio.sleep(random.uniform(a, b))


async def _get_json(client: httpx.AsyncClient, url: str, params: Dict) -> Dict:
    for attempt in range(6):
        try:
            headers = _mk_headers(SEKAIPEDIA_LIST)
            params2 = dict(params)
            params2.setdefault("origin", "*")
            r = await client.get(
                url,
                params=params2,
                headers=headers,
                follow_redirects=True,
                timeout=60,
            )
            if r.status_code in (403, 429):
                await asyncio.sleep(1.5 + attempt * 0.7)
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == 5:
                raise
            await asyncio.sleep(1.0 + attempt * 0.6)
    return {}


def _abs_sekaipedia(u: str) -> str:
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return "https://sekaipedia.org" + u
    return u


# =================== Persistencia: helpers BD (cartas / meta / variantes) ===================
async def _meta_get(conn: asyncpg.Connection, key: str) -> Optional[str]:
    row = await conn.fetchrow(
        "SELECT value FROM public.sekaicards_meta WHERE key=$1", key
    )
    return row["value"] if row else None


async def _meta_set(conn: asyncpg.Connection, key: str, value: str) -> None:
    await conn.execute(
        """
        INSERT INTO public.sekaicards_meta(key, value, updated_at)
        VALUES ($1, $2, now())
        ON CONFLICT (key) DO UPDATE
          SET value = EXCLUDED.value,
              updated_at = now()
        """,
        key,
        value,
    )


def _dt_to_iso(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _iso_to_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        ss = s.replace("Z", "+00:00")
        return datetime.fromisoformat(ss)
    except Exception:
        return None


async def _cards_db_count(conn: asyncpg.Connection) -> int:
    row = await conn.fetchrow("SELECT COUNT(*) AS c FROM public.sekaicards")
    return int(row["c"] or 0) if row else 0


async def _cards_db_max_id(conn: asyncpg.Connection) -> Optional[int]:
    row = await conn.fetchrow("SELECT MAX(page_id) AS m FROM public.sekaicards")
    if not row:
        return None
    v = row["m"]
    try:
        return int(v) if v is not None else None
    except Exception:
        return None


async def _cards_db_all(
    conn: asyncpg.Connection,
    order: str = "desc",
    limit: Optional[int] = None,
    offset: int = 0,
) -> List[Dict]:
    order_sql = "DESC" if str(order).lower() != "asc" else "ASC"
    lim_sql = ""
    params: List[Any] = []
    if limit is not None:
        params.append(int(limit))
        lim_sql += f" LIMIT ${len(params)}"
    if offset:
        params.append(int(offset))
        lim_sql += f" OFFSET ${len(params)}"

    rows = await conn.fetch(
        f"""
        SELECT page_id AS id, name, href, thumb
        FROM public.sekaicards
        ORDER BY page_id {order_sql}
        {lim_sql}
        """,
        *params,
    )
    return [
        {"id": str(r["id"]), "name": r["name"], "href": r["href"], "thumb": r["thumb"]}
        for r in rows
    ]


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


async def _variants_db_get(
    conn: asyncpg.Connection, page_id: int
) -> Tuple[List[Dict], Optional[datetime]]:
    rows = await conn.fetch(
        """
        SELECT label, url, updated_at
        FROM public.sekaicard_variants
        WHERE page_id=$1
        ORDER BY
          CASE label
            WHEN 'Thumb' THEN 0
            WHEN 'Untrained' THEN 1
            WHEN 'Trained' THEN 2
            ELSE 3
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


# =================== Revisión de List_of_cards ===================
async def _sekaipedia_list_revision() -> Tuple[Optional[str], Optional[datetime]]:
    async with httpx.AsyncClient() as client:
        data = await _get_json(
            client,
            SEKAIPEDIA_API,
            {
                "action": "query",
                "prop": "revisions",
                "rvprop": "ids|timestamp",
                "rvslots": "main",
                "titles": "List_of_cards",
                "format": "json",
                "formatversion": 2,
            },
        )

    rev_id = None
    rev_ts = None
    try:
        pages = (data.get("query") or {}).get("pages") or []
        if pages and (pages[0].get("revisions") or []):
            r0 = pages[0]["revisions"][0]
            rev_id = str(r0.get("revid") or "")
            ts = r0.get("timestamp") or ""
            rev_ts = _iso_to_dt(ts)
    except Exception:
        rev_id, rev_ts = None, None

    return (rev_id or None), rev_ts


_SECTION_RANGE_RE = re.compile(r"\((\d+)\s*[-–—]\s*(\d+)\)")


async def _sekaipedia_remote_max_card_id_uncached() -> Optional[int]:
    """
    Muy barato: action=parse&prop=sections
    Busca rangos "List of cards (1301–1400)" y devuelve el max end.
    """
    async with httpx.AsyncClient() as client:
        data = await _get_json(
            client,
            SEKAIPEDIA_API,
            {
                "action": "parse",
                "page": "List_of_cards",
                "prop": "sections",
                "format": "json",
                "formatversion": 2,
            },
        )

    try:
        parse = (data or {}).get("parse") or {}
        sections = parse.get("sections") or []
        best = None
        for s in sections:
            line = str((s or {}).get("line") or "")
            m = _SECTION_RANGE_RE.search(line)
            if not m:
                continue
            end = int(m.group(2))
            if best is None or end > best:
                best = end
        return best
    except Exception:
        return None


async def _sekaipedia_remote_max_card_id() -> Optional[int]:
    now = _now_utc()
    ts = _remote_max_cache.get("ts") or datetime.fromtimestamp(0, tz=timezone.utc)
    if isinstance(ts, datetime) and (now - ts) < REMOTE_MAX_CACHE_TTL:
        return _remote_max_cache.get("value")

    try:
        v = await _sekaipedia_remote_max_card_id_uncached()
    except Exception:
        v = None

    _remote_max_cache["value"] = v
    _remote_max_cache["ts"] = now
    return v


async def _cards_needs_refresh(conn: asyncpg.Connection, force: bool) -> Tuple[bool, str]:
    """
    Decide si hay que refrescar:
    - force => sí
    - si DB vacía => sí
    - check remoto max id (sections) => si remote_max > local_max => sí
    - si verifiqué hace poco => NO
    - si full TTL => sí
    - si rev cambió => sí
    """
    if force:
        return True, "force"

    count = await _cards_db_count(conn)
    if count <= 0:
        return True, "empty"

    now = _now_utc()

    # 1) chequeo rápido por “max id remoto”
    local_max = await _cards_db_max_id(conn)
    remote_max = await _sekaipedia_remote_max_card_id()
    if remote_max is not None and local_max is not None and remote_max > local_max:
        return True, f"max-id-bump({local_max}->{remote_max})"

    last_verified_at = _iso_to_dt(await _meta_get(conn, "cards_verified_at"))
    last_full_fetch_at = _iso_to_dt(await _meta_get(conn, "cards_fetched_at"))
    stored_rev_id = await _meta_get(conn, "list_rev_id")

    if last_full_fetch_at and (now - last_full_fetch_at) > CARDS_FULL_TTL:
        return True, "full-ttl"

    # Si verifiqué hace poco, confío en estado actual (ya hicimos max-id arriba)
    if last_verified_at and (now - last_verified_at) < CARDS_REV_CHECK_TTL:
        return False, "recently-verified"

    # 2) Check revision remoto
    remote_rev_id, remote_rev_ts = await _sekaipedia_list_revision()
    if remote_rev_id and stored_rev_id and remote_rev_id == stored_rev_id:
        await _meta_set(conn, "cards_verified_at", _dt_to_iso(now))
        if remote_rev_ts:
            await _meta_set(conn, "list_rev_ts", _dt_to_iso(remote_rev_ts))
        return False, "rev-same"

    # Si no pude verificar revision (rev_id None), prefiero refrescar para no quedarme atrás
    if not remote_rev_id:
        return True, "rev-unknown"

    return True, "rev-changed"


# =================== Scraping (HTML renderizado) ===================
def _strip_tags(s: str) -> str:
    s = re.sub(r"<script.*?>.*?</script>", "", s, flags=re.S | re.I)
    s = re.sub(r"<style.*?>.*?</style>", "", s, flags=re.S | re.I)
    s = re.sub(r"<.*?>", "", s, flags=re.S)
    s = _html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


_TABLE_RE = re.compile(
    r"<table[^>]*class=[\"'][^\"']*wikitable[^\"']*[\"'][^>]*>(.*?)</table>",
    re.S | re.I,
)
_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_TD_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_A_RE = re.compile(
    r"<a[^>]+href=[\"'](?P<href>/wiki/[^\"'#?]+)[\"'][^>]*>(?P<txt>.*?)</a>",
    re.S | re.I,
)
_IMG_RE = re.compile(
    r"<img[^>]+(?:data-src|src)=[\"'](?P<src>[^\"']+)[\"']",
    re.I,
)


def _dedup_cards(items: List[Dict]) -> List[Dict]:
    seen = set()
    out = []
    for it in items:
        cid = str(it.get("id") or "")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        out.append(it)
    try:
        out.sort(key=lambda x: int(str(x["id"])))
    except Exception:
        pass
    return out


def _pick_best_href_and_name(tr_html: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Elige el mejor <a href="/wiki/..."> del row evitando File:/Category: etc.
    """
    best = None
    best_score = -10_000

    for m in _A_RE.finditer(tr_html):
        href_rel = m.group("href") or ""
        href_low = href_rel.lower()

        if href_low.startswith("/wiki/file:"):
            continue
        if href_low.startswith("/wiki/category:"):
            continue
        if href_low.startswith("/wiki/help:"):
            continue
        if href_low.startswith("/wiki/special:"):
            continue

        txt = _strip_tags(m.group("txt") or "")
        if not txt or txt.lower() in ("edit", "view", "file"):
            continue

        score = 0
        if len(txt) >= 3:
            score += 5
        if not txt.isdigit():
            score += 3
        if "card" in href_low:
            score += 1

        if score > best_score:
            best_score = score
            best = (href_rel, txt)

    if not best:
        return None, None
    return best[0], best[1]


def _pick_best_thumb(tr_html: str, card_id: str) -> Optional[str]:
    """
    Elige mejor thumb del row. Prioriza urls que contengan el card_id en el filename.
    """
    imgs = list(_IMG_RE.finditer(tr_html))
    if not imgs:
        return None

    best = None
    best_score = -10_000
    for im in imgs:
        src = _abs_sekaipedia(im.group("src") or "")
        low = src.lower()
        fn = low.rsplit("/", 1)[-1]

        score = 0
        if card_id and card_id.isdigit() and card_id in fn:
            score += 100
        if "card" in fn:
            score += 10
        if "thumb" in fn or "thumbnail" in fn:
            score += 3
        if any(k in fn for k in ("icon", "logo", "star", "attribute", "type", "unit")):
            score -= 20

        if score > best_score:
            best_score = score
            best = src

    return best


def _parse_cards_from_html(html_text: str) -> List[Dict]:
    items: List[Dict] = []
    if not html_text:
        return items

    tables = _TABLE_RE.findall(html_text)
    if not tables:
        tables = [html_text]

    for t in tables:
        for tr in _TR_RE.findall(t):
            cells = _TD_RE.findall(tr)
            if len(cells) < 2:
                continue

            id_txt = _strip_tags(cells[0])
            m_id = re.search(r"\b(\d{1,6})\b", id_txt)
            if not m_id:
                continue
            card_id = m_id.group(1)

            href_rel, display_name = _pick_best_href_and_name(tr)
            if href_rel:
                href = _abs_sekaipedia(href_rel)
            else:
                # fallback: intenta sacar nombre de alguna celda
                display_name = None
                for c in cells[1:]:
                    txt = _strip_tags(c)
                    if txt and not txt.isdigit():
                        display_name = txt
                        break
                display_name = display_name or f"Card {card_id}"
                page_title = display_name.replace(" ", "_")
                href = f"https://sekaipedia.org/wiki/{page_title}"

            thumb = _pick_best_thumb(tr, card_id)

            items.append(
                {"id": card_id, "name": display_name, "href": href, "thumb": thumb}
            )

    return _dedup_cards(items)


async def _scrape_cards_from_sekaipedia() -> List[Dict]:
    async with httpx.AsyncClient() as client:
        data = await _get_json(
            client,
            SEKAIPEDIA_API,
            {
                "action": "parse",
                "page": "List_of_cards",
                "prop": "text",
                "format": "json",
                "formatversion": 2,
            },
        )

    html_text = ""
    try:
        parse = (data or {}).get("parse") or {}
        txt = parse.get("text")
        if isinstance(txt, dict):
            html_text = txt.get("*") or ""
        else:
            html_text = txt or ""
    except Exception:
        html_text = ""

    items = _parse_cards_from_html(html_text)
    return items


# =================== Refresh orchestration ===================
async def _refresh_cards_once(force: bool = False) -> None:
    if not db_pool:
        return

    async with _cards_lock:
        try:
            async with db_pool.acquire() as conn:
                do_refresh, reason = await _cards_needs_refresh(conn, force=force)
                if not do_refresh:
                    if not _cards_cache["items"]:
                        _cards_cache["items"] = await _cards_db_all(conn, order="desc")
                        _cards_cache["ts"] = _now_utc()
                    return
                print(f"♻️ Cards refresh: {reason}")

            remote_rev_id, remote_rev_ts = await _sekaipedia_list_revision()

            items = await _scrape_cards_from_sekaipedia()
            if not items:
                print("⚠️ Cards scrape returned 0 items; keeping existing DB")
                return

            async with db_pool.acquire() as conn:
                async with conn.transaction():
                    await _cards_db_upsert_many(conn, items)
                    await _meta_set(conn, "cards_fetched_at", _dt_to_iso(_now_utc()))
                    await _meta_set(conn, "cards_verified_at", _dt_to_iso(_now_utc()))
                    if remote_rev_id:
                        await _meta_set(conn, "list_rev_id", remote_rev_id)
                    if remote_rev_ts:
                        await _meta_set(conn, "list_rev_ts", _dt_to_iso(remote_rev_ts))

            _cards_cache["items"] = items
            _cards_cache["ts"] = _now_utc()
            _cards_cache["rev_id"] = remote_rev_id

            print(f"✅ Cards refreshed: {len(items)}")
        except Exception:
            traceback.print_exc()


async def _refresh_cards_forever() -> None:
    await asyncio.sleep(3)
    while not _cards_stop.is_set():
        await _refresh_cards_once(force=False)
        try:
            await asyncio.wait_for(
                _cards_stop.wait(), timeout=CARDS_POLL_TTL.total_seconds()
            )
        except asyncio.TimeoutError:
            pass


# =================== API: /auth02/cards ===================
async def _cards_meta_payload(conn: asyncpg.Connection) -> Dict[str, Any]:
    count = await _cards_db_count(conn)
    max_id = await _cards_db_max_id(conn)
    list_rev_ts = await _meta_get(conn, "list_rev_ts")
    return {
        "ok": True,
        "revId": await _meta_get(conn, "list_rev_id"),
        "revTs": list_rev_ts,
        "listRevTs": list_rev_ts,
        "count": count,
        "maxId": max_id,
        "verifiedAt": await _meta_get(conn, "cards_verified_at"),
        "fetchedAt": await _meta_get(conn, "cards_fetched_at"),
        "serverTs": _dt_to_iso(_now_utc()),
        "source": "db",
    }


@app.get("/auth02/cards")
async def auth02_cards(
    request: Request,
    force: int = Query(0, description="1 = fuerza refresh inmediato (requiere sesión)"),
    meta: int = Query(0, description="1 = devuelve solo metadata liviana"),
    order: str = Query("desc", description="desc = nuevas arriba, asc = viejas arriba"),
    limit: Optional[int] = Query(None, ge=1, le=20000, description="limita cantidad"),
    offset: int = Query(0, ge=0),
):
    """
    Devuelve lista de cartas de Sekaipedia (persistente en BD + cache).

    - ?meta=1  => respuesta liviana (revId/count/maxId/timestamps).
    - ?force=1 => refresh completo inmediato (requiere sesión).
    - ?order=desc => nuevas arriba (default).
    """
    no_cache_headers = {
        "Cache-Control": "no-store, max-age=0",
        "Pragma": "no-cache",
    }

    if not db_pool:
        if meta:
            return JSONResponse(
                {"ok": False, "revId": None, "revTs": None, "count": 0, "maxId": None},
                headers=no_cache_headers,
            )
        return JSONResponse([], headers=no_cache_headers)

    if meta:
        async with db_pool.acquire() as conn:
            payload = await _cards_meta_payload(conn)
        return JSONResponse(payload, headers=no_cache_headers)

    force_bool = bool(force)
    if force_bool:
        sid = request.cookies.get(COOKIE_NAME)
        if not sid or sid not in SESSIONS:
            raise HTTPException(status_code=401, detail="no-session")

    if force_bool:
        await _refresh_cards_once(force=True)
        async with db_pool.acquire() as conn:
            data = await _cards_db_all(conn, order=order, limit=limit, offset=offset)
        return JSONResponse(data, headers=no_cache_headers)

    async with db_pool.acquire() as conn:
        try:
            do_refresh, _reason = await _cards_needs_refresh(conn, force=False)
        except Exception:
            do_refresh = True

    if do_refresh:
        await _refresh_cards_once(force=False)

    async with db_pool.acquire() as conn:
        data = await _cards_db_all(conn, order=order, limit=limit, offset=offset)

    return JSONResponse(data, headers=no_cache_headers)


# =================== API: /auth02/card-variants ===================
# Nota: aquí está el fix para que NO salgan outfits/vestidos, solo imágenes de cartas.
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
    "april",
    "joke",
    "video",
    "movie",
    "mv",
    "promo",
    "teaser",
    # ropa / modelos (evitar “vestidos” y assets no-card)
    "outfit",
    "dress",
    "wardrobe",
    "uniform",
    "clothes",
    "clothing",
    "model",
    "3d",
    "live2d",
    "l2d",
    "mesh",
    "rig",
]

# Penalización adicional por keywords de ropa (por si algún filename no cae en _BAD_KEYWORDS)
_CLOTHING_KEYWORDS = [
    "outfit",
    "dress",
    "wardrobe",
    "uniform",
    "clothes",
    "clothing",
    "costume",
    "model",
    "3d",
    "live2d",
    "l2d",
    "mesh",
    "rig",
]

_TRAINED_RE = re.compile(
    r"(trained|after[_\- ]?training|idolized|evolv(ed|e)|aftertrain)", re.I
)
_UNTRAINED_RE = re.compile(
    r"(untrained|before[_\- ]?training|base|normal|default|beforetrain)", re.I
)


def _filename_from_url(u: str) -> str:
    try:
        path = urlparse(u).path
        return path.rsplit("/", 1)[-1].lower()
    except Exception:
        return (u or "").lower()


def _is_trained(fn: str) -> bool:
    return bool(_TRAINED_RE.search(fn or ""))


def _is_untrained(fn: str) -> bool:
    return bool(_UNTRAINED_RE.search(fn or ""))


def _looks_card_image(fn: str) -> bool:
    s = (fn or "").lower()
    if any(bad in s for bad in _BAD_KEYWORDS):
        return False
    return s.endswith((".png", ".jpg", ".jpeg", ".webp"))


def _looks_card_art(fn: str, card_id_hint: Optional[str] = None) -> bool:
    """
    Filtro estricto: intenta quedarse SOLO con artwork de carta (no outfits, no assets sueltos).
    Reglas:
      - Debe pasar _looks_card_image.
      - Debe contener "card" o indicar trained/untrained en el filename.
      - Si hay card_id_hint numérico, exige que el ID aparezca en el filename (cuando existan matches).
    """
    s = (fn or "").lower()
    if not _looks_card_image(s):
        return False

    # Debe parecer imagen de carta
    if "card" not in s and not (_is_trained(s) or _is_untrained(s)):
        return False

    # Si tenemos id, exigimos que aparezca; esto mata la mayoría de outfits
    if card_id_hint and str(card_id_hint).isdigit():
        if str(card_id_hint) not in s:
            return False

    return True


def _prefer_unique(links: List[str]) -> List[str]:
    seen, out = set(), []
    for u in links:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _page_title_from_href(href: str) -> str:
    try:
        path = urlparse(href).path
        title = path.split("/wiki/", 1)[1]
        return unquote(title).replace("_", " ").strip()
    except Exception:
        return ""


async def _fetch_page_images(client: httpx.AsyncClient, page_title: str) -> List[str]:
    """
    1) lista "images" del page (File:...)
    2) resuelve urls con imageinfo (thumb)
    Filtra por mime image/* y descarta svg.
    """
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
                "iiurlwidth": str(max(256, min(2048, CARD_IMG_WIDTH))),
                "format": "json",
            },
        )
        for _, finfo in (data2.get("query", {}).get("pages", {}) or {}).items():
            for ii in finfo.get("imageinfo") or []:
                mime = (ii.get("mime") or "").lower()
                if not mime.startswith("image/"):
                    continue
                if mime == "image/svg+xml":
                    continue
                url = ii.get("thumburl") or ii.get("url")
                if url:
                    urls.append(_abs_sekaipedia(url))
        await _sleep_jitter(0.2, 0.5)

    return _prefer_unique(urls)


async def _scrape_card_variants(href: str, card_id_hint: Optional[str] = None) -> Dict:
    """
    Devuelve Untrained + Trained según naming, filtrando fuerte para evitar outfits/vestidos.
    """
    page_title = _page_title_from_href(href)
    async with httpx.AsyncClient() as client:
        urls = await _fetch_page_images(client, page_title)

    # 1) base: imágenes razonables por extensión + blacklist
    all_imgs = [u for u in urls if _looks_card_image(_filename_from_url(u))]

    # 2) estricto: parecer de carta + id (si hay)
    strict_imgs = [
        u for u in all_imgs if _looks_card_art(_filename_from_url(u), card_id_hint)
    ]

    # Si estricto encontró algo, usamos eso; si no, fallback a all_imgs (para no romper páginas raras)
    candidate_imgs = strict_imgs or all_imgs

    # Si hay ID, intentamos quedarnos con los que contienen el ID (solo si existen matches)
    if card_id_hint and str(card_id_hint).isdigit():
        cid = str(card_id_hint)
        id_filtered = [u for u in candidate_imgs if cid in _filename_from_url(u)]
        if id_filtered:
            candidate_imgs = id_filtered

    def score(u: str, target: str) -> int:
        fn = _filename_from_url(u)
        sc = 0

        # castigo fuerte si parece ropa/modelo
        if any(k in fn for k in _CLOTHING_KEYWORDS):
            sc -= 2000

        if card_id_hint and str(card_id_hint).isdigit() and str(card_id_hint) in fn:
            sc += 200
        if "card" in fn:
            sc += 50
        if target == "untrained" and _is_untrained(fn):
            sc += 150
        if target == "trained" and _is_trained(fn):
            sc += 150

        # preferir archivos que claramente sean arte (a veces "full"/"art" aparece)
        if any(k in fn for k in ("full", "art", "illustration")):
            sc += 10

        return sc

    untrained_sorted = sorted(
        candidate_imgs, key=lambda u: score(u, "untrained"), reverse=True
    )
    trained_sorted = sorted(
        candidate_imgs, key=lambda u: score(u, "trained"), reverse=True
    )

    untrained_url = untrained_sorted[0] if untrained_sorted else None
    trained_url = trained_sorted[0] if trained_sorted else None

    # Evitar duplicado
    if trained_url == untrained_url:
        trained_url = None

    variants: List[Dict[str, str]] = []
    if untrained_url:
        variants.append({"label": "Untrained", "url": untrained_url})
    if trained_url:
        variants.append({"label": "Trained", "url": trained_url})

    return {"name": page_title, "variants": variants}


async def _get_card_variants_for_href(href: str) -> Dict[str, Any]:
    page_title = _page_title_from_href(href)

    page_id: Optional[int] = None
    thumb_url: Optional[str] = None
    cached_variants: List[Dict] = []
    cached_ts: Optional[datetime] = None

    if db_pool:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT page_id, thumb FROM public.sekaicards WHERE href=$1", href
            )
            if row:
                page_id = int(row["page_id"])
                thumb_url = row["thumb"]

            if page_id:
                cached_variants, cached_ts = await _variants_db_get(conn, page_id)
                if cached_variants and cached_ts and (_now_utc() - cached_ts) < CARDS_FULL_TTL:
                    return {"name": page_title, "variants": cached_variants}

    scraped_variants: List[Dict] = []
    try:
        scraped = await _scrape_card_variants(
            href, card_id_hint=str(page_id) if page_id else None
        )
        scraped_variants = scraped.get("variants", []) or []
    except Exception:
        traceback.print_exc()
        if cached_variants:
            return {"name": page_title, "variants": cached_variants}

    final: List[Dict] = []
    seen = set()

    def add(label: str, url: Optional[str]):
        if not url:
            return
        if url in seen:
            return
        seen.add(url)
        final.append({"label": label, "url": url})

    # Thumb primero
    add("Thumb", thumb_url)

    for v in scraped_variants:
        lab = (v.get("label") or "").strip()
        url = v.get("url")
        low = lab.lower()
        if "untrained" in low or low in ("default", "normal", "base", "before"):
            lab = "Untrained"
        elif "trained" in low or "after" in low or "evolved" in low or "idolized" in low:
            lab = "Trained"
        add(lab or "Variant", url)

    try:
        if db_pool and page_id is not None:
            async with db_pool.acquire() as conn:
                await _variants_db_save(conn, page_id, final)
    except Exception:
        traceback.print_exc()

    return {"name": page_title, "variants": final}


@app.get("/auth02/card-variants")
async def get_card_variants(
    href: str = Query(..., description="URL absoluta de la Card:")
) -> Dict:
    return await _get_card_variants_for_href(href)


class CardVariantsBatchIn(BaseModel):
    hrefs: List[str]


@app.post("/auth02/card-variants/batch")
async def get_card_variants_batch(body: CardVariantsBatchIn) -> Dict[str, Any]:
    hrefs = [h for h in (body.hrefs or []) if isinstance(h, str) and h.strip()]
    hrefs = hrefs[:200]

    sem = asyncio.Semaphore(6)

    async def one(h: str):
        async with sem:
            try:
                return h, await _get_card_variants_for_href(h)
            except Exception:
                traceback.print_exc()
                return h, {"name": _page_title_from_href(h), "variants": []}

    pairs = await asyncio.gather(*[one(h) for h in hrefs])
    return {"ok": True, "items": {k: v for (k, v) in pairs}}


# =================== Static ===================
static_dir = os.path.join(os.path.dirname(__file__), "public")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
