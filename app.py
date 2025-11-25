import os
import re
import secrets
import traceback
import asyncio
import random
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse, unquote

import asyncpg
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

# ========================= ENV =========================
DISCORD_CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI  = os.getenv("DISCORD_REDIRECT_URI", "https://unique-education-production.up.railway.app/auth02/callback")
FRONT_URL   = os.getenv("FRONT_URL", "https://pjsk-page.vercel.app")
CORS_ORIGIN = os.getenv("CORS_ORIGIN", FRONT_URL)
COOKIE_NAME = os.getenv("COOKIE_NAME", "sid")
DATABASE_URL = os.getenv("DATABASE_URL", "")

# ===================== FastAPI =====================
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONT_URL],
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
        max_age=60 * 60 * 24 * 7  # 7 días
    )

# =========================== DB & Schema Flags ===========================
db_pool: Optional[asyncpg.pool.Pool] = None

HAS_DISCORD_ID_COL: bool = False
SEASONS_HAS_ACTIVE: bool    = False
SEASONS_HAS_NAME: bool      = False
SEASONS_HAS_STARTS_AT: bool = False
SEASONS_HAS_ENDS_AT: bool   = False

# seasonplayers extra stats
SP_HAS_WINS: bool          = False
SP_HAS_LOSSES: bool        = False
SP_HAS_GAMES_PLAYED: bool  = False

# matches / matchplayers
HAS_MATCHES_TABLE: bool        = False
HAS_MATCHPLAYERS_TABLE: bool   = False

# Tipo SQL efectivo del user_id ("text" o "bigint")
USER_ID_SQLTYPE: str = "text"   # default seguro

async def _has_col(conn: asyncpg.Connection, table: str, column: str) -> bool:
    row = await conn.fetchrow("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=$1 AND column_name=$2
    """, table, column)
    return bool(row)

async def _col_type(conn: asyncpg.Connection, table: str, column: str) -> Optional[str]:
    row = await conn.fetchrow("""
        SELECT data_type
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=$1 AND column_name=$2
        LIMIT 1
    """, table, column)
    if not row:
        return None
    dt = str(row["data_type"]).lower()
    if "bigint" in dt or "int8" in dt:
        return "bigint"
    return "text"

async def _detect_schema(conn: asyncpg.Connection) -> None:
    global HAS_DISCORD_ID_COL, SEASONS_HAS_ACTIVE, SEASONS_HAS_NAME
    global SEASONS_HAS_STARTS_AT, SEASONS_HAS_ENDS_AT, USER_ID_SQLTYPE
    global SP_HAS_WINS, SP_HAS_LOSSES, SP_HAS_GAMES_PLAYED
    global HAS_MATCHES_TABLE, HAS_MATCHPLAYERS_TABLE

    try:
        # players / seasons
        HAS_DISCORD_ID_COL    = await _has_col(conn, "players", "discord_id")
        SEASONS_HAS_ACTIVE    = await _has_col(conn, "seasons", "active")
        SEASONS_HAS_NAME      = await _has_col(conn, "seasons", "name")
        SEASONS_HAS_STARTS_AT = await _has_col(conn, "seasons", "starts_at")
        SEASONS_HAS_ENDS_AT   = await _has_col(conn, "seasons", "ends_at")

        # seasonplayers: columnas de stats
        SP_HAS_WINS         = await _has_col(conn, "seasonplayers", "wins")
        SP_HAS_LOSSES       = await _has_col(conn, "seasonplayers", "losses")
        SP_HAS_GAMES_PLAYED = await _has_col(conn, "seasonplayers", "games_played") \
                              or await _has_col(conn, "seasonplayers", "games")

        # matches / matchplayers (si no existen, no consultamos esas tablas)
        # Usar la tabla que realmente llena el bot (match_history)
        HAS_MATCHES_TABLE      = await _has_col(conn, "match_history", "season_id")
        HAS_MATCHPLAYERS_TABLE = False  # ya no la vamos a usar

        # Detectar tipo de user_id: si player_pfps existe usa su tipo; si no, usa el de players
        pfps_type = await _col_type(conn, "player_pfps", "user_id")
        players_type = await _col_type(conn, "players", "user_id")
        USER_ID_SQLTYPE = pfps_type or players_type or "text"

        print(
            "🔎 Schema:"
            f" players.discord_id={HAS_DISCORD_ID_COL}"
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
        )
    except Exception:
        traceback.print_exc()

@app.on_event("startup")
async def startup():
    global db_pool
    if not DATABASE_URL:
        print("⚠️ DATABASE_URL is empty. /api endpoints will raise 500")
        return

    db_pool = await asyncpg.create_pool(
        DATABASE_URL, min_size=1, max_size=10, timeout=10.0, command_timeout=30.0,
    )
    print("✅ DB pool initialized")

    async with db_pool.acquire() as conn:
        await _detect_schema(conn)

        # Crear tablas persistentes
        uid_type_sql = "BIGINT" if USER_ID_SQLTYPE == "bigint" else "TEXT"
        await conn.execute(f"""
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
        """)

@app.on_event("shutdown")
async def shutdown():
    if db_pool:
        await db_pool.close()
        print("🛑 DB pool closed")

# ======================= Helpers: Seasons =======================
async def _season_exists(conn: asyncpg.Connection, season_id: int) -> bool:
    row = await conn.fetchrow("SELECT 1 FROM public.seasons WHERE id=$1", season_id)
    return bool(row)

async def _pick_effective_season(conn: asyncpg.Connection, requested_season_id: int) -> int:
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
        row = await conn.fetchrow("""
            SELECT id FROM public.seasons
            WHERE active = TRUE ORDER BY id DESC LIMIT 1
        """)
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
    if SEASONS_HAS_NAME:      cols.append("name")
    if SEASONS_HAS_ACTIVE:    cols.append("active")
    if SEASONS_HAS_STARTS_AT: cols.append("starts_at")
    if SEASONS_HAS_ENDS_AT:   cols.append("ends_at")
    select_cols = ", ".join(cols)

    q = f"SELECT {select_cols} FROM public.seasons ORDER BY id DESC"
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(q)

    out = []
    for r in rows:
        item = {"id": r["id"]}
        if SEASONS_HAS_NAME:      item["name"] = r.get("name")
        if SEASONS_HAS_ACTIVE:    item["active"] = r.get("active")
        if SEASONS_HAS_STARTS_AT: item["startsAt"] = r.get("starts_at").isoformat() if r.get("starts_at") else None
        if SEASONS_HAS_ENDS_AT:   item["endsAt"] = r.get("ends_at").isoformat() if r.get("ends_at") else None
        out.append(item)
    return out

# ======================= AUTH02 (Discord OAuth) =======================
PFP_BY_NAME:    Dict[str, str] = {}
PFP_BY_ID:      Dict[str, str] = {}
PFP_BY_DISCORD: Dict[str, str] = {}

STATE_STORE: Dict[str, datetime] = {}
STATE_TTL = timedelta(minutes=10)

async def _resolve_user_id_from_discord(discord_id: str) -> Optional[str]:
    if not (db_pool and HAS_DISCORD_ID_COL and discord_id):
        return None
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id FROM public.players WHERE discord_id = $1 LIMIT 1", discord_id
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

def _resolve_pfp_for_player(user_id: Optional[str], discord_id: Optional[str], name: Optional[str]) -> Optional[str]:
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
    payload["resolvedUserId"] = await _resolve_user_id_from_discord(payload.get("id", ""))
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
                    _uid_for_sql(user_id)
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

    # Persistir en BD (sin casts; parámetro con tipo correcto)
    if db_pool:
        if not user_id and discord_id:
            resolved = await _resolve_user_id_from_discord(discord_id)
            if resolved:
                user_id = resolved
        if user_id:
            async with db_pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO public.player_pfps(user_id, url, updated_at)
                    VALUES ($1, $2, now())
                    ON CONFLICT (user_id) DO UPDATE
                      SET url = EXCLUDED.url,
                          updated_at = now()
                """, _uid_for_sql(user_id), url)

    return {"ok": True, "byId": bool(user_id), "byDiscord": bool(discord_id), "byName": bool(name_lower)}

@app.get("/auth02/pfps")
async def list_pfps():
    return JSONResponse({"byId": PFP_BY_ID, "byDiscord": PFP_BY_DISCORD, "byName": PFP_BY_NAME})

# ======================= API: Players =======================
@app.get("/api/players")
async def get_players(
    season_id: int = Query(..., description="Season ID (0 = season actual activa)"),
    limit: int = Query(10000, ge=1, le=100000),
    offset: int = Query(0, ge=0),
):
    if not db_pool:
        raise HTTPException(500, "DB pool not initialized")

    rows = []
    games_by_user: Dict[int, int] = {}
    last10_by_user: Dict[int, Dict[str, int]] = {}

    try:
        async with db_pool.acquire() as conn:
            picked = await _pick_effective_season(conn, season_id)

            select_cols = [
                "p.user_id  AS user_id",
                "p.name     AS name",
                "p.country  AS country",
                "sp.mmr     AS mmr",
                "sp.role    AS rank",
                "pp.url     AS pfp_url",
                "pp.updated_at AS pfp_updated_at"
            ]
            if HAS_DISCORD_ID_COL:
                select_cols.insert(1, "p.discord_id AS discord_id")
            if SP_HAS_WINS:
                select_cols.append("sp.wins AS wins")
            if SP_HAS_LOSSES:
                select_cols.append("sp.losses AS losses")
            if SP_HAS_GAMES_PLAYED:
                # intentamos ambos nombres, el que exista
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

            # NEW: contar partidas y W-L last 10 desde match_history
            if HAS_MATCHES_TABLE and rows:
                try:
                    uid_ints: List[int] = []
                    for rr in rows:
                        uid = rr.get("user_id")
                        if uid is None:
                            continue
                        try:
                            uid_ints.append(int(uid))
                        except Exception:
                            continue

                    if uid_ints:
                        # Total games per user en la season
                        stats_rows = await conn.fetch("""
                            SELECT user_id, COUNT(*) AS games_played
                            FROM public.match_history
                            WHERE season_id = $1
                              AND user_id = ANY($2::bigint[])
                            GROUP BY user_id
                        """, picked, uid_ints)
                        games_by_user = {
                            int(sr["user_id"]): sr["games_played"]
                            for sr in stats_rows
                        }

                        # W-L / MMR delta en las últimas 10 partidas por usuario
                        last10_rows = await conn.fetch("""
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
                                  AND user_id = ANY($2::bigint[])
                            )
                            SELECT
                                user_id,
                                COUNT(*) FILTER (WHERE rn <= 10) AS last10_games,
                                COUNT(*) FILTER (WHERE rn <= 10 AND is_win = TRUE) AS last10_wins,
                                COUNT(*) FILTER (WHERE rn <= 10 AND (is_win = FALSE OR is_win IS NULL)) AS last10_losses,
                                COALESCE(SUM(mmr_delta) FILTER (WHERE rn <= 10), 0) AS last10_mmr_delta
                            FROM ranked
                            WHERE rn <= 10
                            GROUP BY user_id
                        """, picked, uid_ints)
                        last10_by_user = {
                            int(r["user_id"]): {
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

    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"DB error: {e}")

    out = []
    for r in rows:
        user_id    = r.get("user_id")
        discord_id = r.get("discord_id") if HAS_DISCORD_ID_COL else None
        name       = r.get("name")

        # PFP
        pfp_url = r.get("pfp_url")
        pfp_ts  = r.get("pfp_updated_at")
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
                name
            )

        wins   = r.get("wins")   if SP_HAS_WINS   else None
        losses = r.get("losses") if SP_HAS_LOSSES else None

        # Preferimos el conteo real desde match_history; si no, usamos la columna de seasonplayers (si existe)
        games_played = None
        try:
            uid_int = int(user_id) if user_id is not None else None
        except Exception:
            uid_int = None

        if uid_int is not None and uid_int in games_by_user:
            games_played = games_by_user[uid_int]
        elif SP_HAS_GAMES_PLAYED:
            games_played = r.get("games_played")

        # W-L last 10 / mmrDelta last 10
        last10_games = None
        last10_wins = None
        last10_losses = None
        last10_mmr_delta = None
        if uid_int is not None and uid_int in last10_by_user:
            s = last10_by_user[uid_int]
            last10_games = s.get("games")
            last10_wins = s.get("wins")
            last10_losses = s.get("losses")
            last10_mmr_delta = s.get("mmr_delta")

        out.append({
            "id": user_id,
            "discordId": discord_id,
            "name": name,
            "mmr": r.get("mmr"),
            "country": r.get("country"),
            "rank": r.get("rank"),
            "pfp": pfp_url,
            # stats extra (para W-L y matches en el front)
            "wins": wins,
            "losses": losses,
            "gamesPlayed": games_played,
            "matchesPlayed": games_played,
            "matches": games_played,
            # last 10 info
            "last10Games": last10_games,
            "last10Wins": last10_wins,
            "last10Losses": last10_losses,
            "last10MmrDelta": last10_mmr_delta,
        })
    return out

# ======================= API: Matches =======================
@app.get("/api/matches")
async def get_matches(
    season_id: int = Query(
        0,
        description="Season ID (0 = season actual activa). Hacer GET /api/matches sin query ya no da 422."
    ),
    user_id: Optional[str] = Query(
        None,
        description="Filtra solo partidas donde participa este user_id (opcional)"
    ),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
):
    """
    Devuelve las partidas desde match_history, agrupadas por result_id (una entrada por match),
    ordenadas de más vieja a más nueva.
    Si user_id está presente, solo devuelve matches donde participa ese jugador,
    pero cada match incluye a TODOS los jugadores en el campo players[].
    """
    if not db_pool:
        raise HTTPException(500, "DB pool not initialized")

    if not HAS_MATCHES_TABLE:
        # No existe match_history en este schema → devolvemos lista vacía
        return []

    async with db_pool.acquire() as conn:
        try:
            picked = await _pick_effective_season(conn, season_id)
        except HTTPException:
            # si no hay seasons, devolvemos [] para no romper el front
            return []

        # ---- 1) Determinar qué result_ids entran en esta página (limit/offset por match) ----
        base_where = ["season_id = $1"]
        params: List = [picked]

        uid_int: Optional[int] = None
        if user_id is not None:
            if not user_id.isdigit():
                raise HTTPException(400, "user_id debe ser numérico")
            uid_int = int(user_id)
            params.append(uid_int)
            base_where.append(f"user_id = ${len(params)}")

        params.append(limit)
        params.append(offset)
        limit_idx  = len(params) - 1
        offset_idx = len(params)

        sql_results = f"""
            SELECT result_id,
                   MIN(played_at) AS first_played_at
            FROM public.match_history
            WHERE {' AND '.join(base_where)}
            GROUP BY result_id
            ORDER BY first_played_at ASC, result_id ASC
            LIMIT ${limit_idx} OFFSET ${offset_idx}
        """

        res_rows = await conn.fetch(sql_results, *params)
        if not res_rows:
            return []

        result_ids: List[int] = [
            int(r["result_id"]) for r in res_rows if r["result_id"] is not None
        ]
        if not result_ids:
            return []

        # ---- 2) Cargar TODAS las filas (jugadores) para esos result_ids ----
        rows = await conn.fetch("""
            SELECT
                mh.id,
                mh.season_id,
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
                p.country AS player_country
            FROM public.match_history mh
            LEFT JOIN public.players p ON p.user_id = mh.user_id
            WHERE mh.result_id = ANY($1::bigint[])
            ORDER BY mh.played_at ASC, mh.result_id ASC, mh.pos ASC, mh.id ASC
        """, result_ids)

    # ---- 3) Agrupar en matches[result_id].players[] ----
    matches_by_result: Dict[int, Dict] = {}

    for r in rows:
        rid = int(r["result_id"])
        played_dt: Optional[datetime] = r["played_at"]

        m = matches_by_result.get(rid)
        if not m:
            m = {
                "id": rid,
                "season_id": r["season_id"],
                "seasonId": r["season_id"],
                "result_id": rid,
                "resultId": rid,
                "played_at": played_dt,
                "playedAt": played_dt.isoformat() if played_dt else None,
                "avg_mmr": None,
                "avgMmr": None,
                "players": [],
            }
            matches_by_result[rid] = m

        player_entry = {
            "user_id": r["user_id"],
            "userId": r["user_id"],
            "name": r["player_name"],
            "country": r["player_country"],
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
            "s_perfect": r["s_perfect"],
            "sPerfect": r["s_perfect"],
            "s_great": r["s_great"],
            "sGreat": r["s_great"],
            "s_good": r["s_good"],
            "sGood": r["s_good"],
            "s_bad": r["s_bad"],
            "sBad": r["s_bad"],
            "s_miss": r["s_miss"],
            "sMiss": r["s_miss"],
        }
        m["players"].append(player_entry)

    # ---- 4) Calcular avg_mmr por match (room MMR) ----
    for m in matches_by_result.values():
        mmrs = [p["mmr_before"] for p in m["players"] if p["mmr_before"] is not None]
        if mmrs:
            avg = int(sum(mmrs) / len(mmrs))
        else:
            avg = None
        m["avg_mmr"] = avg
        m["avgMmr"] = avg

    # ---- 5) Ordenar por played_at ASC (más viejas primero) ----
    matches_list = list(matches_by_result.values())
    matches_list.sort(
        key=lambda x: (
            x["played_at"] if isinstance(x["played_at"], datetime) else datetime.min,
            x["id"],
        )
    )
    # convertir played_at datetime -> iso string definitivo
    for m in matches_list:
        dt = m.get("played_at")
        m["played_at"] = dt.isoformat() if isinstance(dt, datetime) else None
        m["playedAt"] = m["played_at"]

    return matches_list

# =================== Sekaipedia: anti-bot friendly ===================
SEKAIPEDIA_LIST = "https://sekaipedia.org/wiki/List_of_cards"
SEKAIPEDIA_API  = "https://sekaipedia.org/w/api.php"

_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Mobile Safari/537.36",
]

def _ascii(s: str) -> str:
    return (
        unicodedata.normalize("NFKD", s or "")
        .encode("ascii", "ignore")
        .decode("ascii")
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

_cards_cache: Dict[str, object] = {"items": [], "ts": datetime.fromtimestamp(0, tz=timezone.utc)}
CARDS_TTL = timedelta(hours=6)
_cards_lock = asyncio.Lock()

async def _sleep_jitter(a=0.15, b=0.5):
    await asyncio.sleep(random.uniform(a, b))

async def _get_json(client: httpx.AsyncClient, url: str, params: Dict) -> Dict:
    for attempt in range(5):
        try:
            headers = _mk_headers(SEKAIPEDIA_LIST)
            params2 = dict(params)
            params2.setdefault("origin", "*")
            r = await client.get(url, params=params2, headers=headers, follow_redirects=True, timeout=30)
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
    rows = await conn.fetch("""
        SELECT page_id AS id, name, href, thumb
        FROM public.sekaicards
        ORDER BY page_id DESC
    """)
    return [{"id": str(r["id"]), "name": r["name"], "href": r["href"], "thumb": r["thumb"]} for r in rows]

async def _cards_db_upsert_many(conn: asyncpg.Connection, items: List[Dict]) -> None:
    if not items:
        return
    await conn.executemany("""
        INSERT INTO public.sekaicards(page_id, name, href, thumb, updated_at)
        VALUES ($1, $2, $3, $4, now())
        ON CONFLICT (page_id) DO UPDATE
          SET name = EXCLUDED.name,
              href = EXCLUDED.href,
              thumb = EXCLUDED.thumb,
              updated_at = now()
    """, [(int(i["id"]), i["name"], i["href"], i.get("thumb")) for i in items])

async def _cards_db_is_fresh(conn: asyncpg.Connection) -> bool:
    row = await conn.fetchrow("SELECT max(updated_at) AS ts FROM public.sekaicards")
    if not row or not row["ts"]:
        return False
    # row["ts"] ya viene aware (timestamptz)
    return (datetime.now(timezone.utc) - row["ts"]) < CARDS_TTL

async def _variants_db_get(conn: asyncpg.Connection, page_id: int) -> Tuple[List[Dict], Optional[datetime]]:
    rows = await conn.fetch("""
        SELECT label, url, updated_at
        FROM public.sekaicard_variants
        WHERE page_id=$1
        ORDER BY CASE label WHEN 'Thumb' THEN 0 WHEN 'Trained' THEN 1 ELSE 2 END, label
    """, page_id)
    variants = [{"label": r["label"], "url": r["url"]} for r in rows]
    ts = max((r["updated_at"] for r in rows), default=None)
    return variants, ts

async def _variants_db_save(conn: asyncpg.Connection, page_id: int, variants: List[Dict]) -> None:
    await conn.execute("DELETE FROM public.sekaicard_variants WHERE page_id=$1", page_id)
    if not variants:
        return
    await conn.executemany("""
        INSERT INTO public.sekaicard_variants(page_id, label, url, updated_at)
        VALUES ($1, $2, $3, now())
    """, [(page_id, v["label"], v["url"]) for v in variants])

# =================== Scraping helpers ===================
_BAD_KEYWORDS = [
    "banner","sprite","chibi","icon","logo","title","sticker","stamp",
    "background","bg","skill","cutin","home","mini","ui","costume",
    "profile","story","stamp","april","joke"
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
    return ("untrained" in s) or ("before training" in s) or ("base" in s) or ("normal" in s)

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
    data1 = await _get_json(client, SEKAIPEDIA_API, {
        "action":"query","prop":"images","titles":page_title,"imlimit":"500","format":"json"
    })
    pages = (data1.get("query", {}) or {}).get("pages", {}) or {}
    file_titles: List[str] = []
    for _, pinfo in pages.items():
        for it in (pinfo.get("images") or []):
            t = it.get("title", "")
            if t.startswith("File:"):
                file_titles.append(t)
    if not file_titles:
        return []
    urls: List[str] = []
    for i in range(0, len(file_titles), 50):
        chunk = file_titles[i:i+50]
        data2 = await _get_json(client, SEKAIPEDIA_API, {
            "action":"query","titles":"|".join(chunk),"prop":"imageinfo",
            "iiprop":"url|size|mime","iiurlwidth":"512","format":"json"
        })
        for _, finfo in (data2.get("query", {}).get("pages", {}) or {}).items():
            for ii in (finfo.get("imageinfo") or []):
                url = ii.get("thumburl") or ii.get("url")
                if url:
                    urls.append(_abs_sekaipedia(url))
        await _sleep_jitter(0.2, 0.5)
    return _prefer_square(urls)

async def _scrape_card_variants(href: str) -> Dict:
    page_title = _page_title_from_href(href)
    async with httpx.AsyncClient() as client:
        urls = await _fetch_page_images(client, page_title)
    clean_urls = [u for u in urls if _looks_card_image(_filename_from_url(u))]
    trained_links: List[str] = []
    untrained_links: List[str] = []
    for u in clean_urls:
        fn = _filename_from_url(u)
        if _is_trained(fn):
            trained_links.append(u)
        elif _is_untrained(fn):
            untrained_links.append(u)
    trained_url = trained_links[0] if trained_links else None
    return {
        "name": page_title,
        "variants": ([] + ([{"label": "Trained", "url": trained_url}] if trained_url else []))
    }

# ---------- STUB: listado de cartas ----------
async def _scrape_cards_from_sekaipedia() -> List[Dict]:
    """
    Stub mínimo para evitar NameError.
    Si ya tienes tu propia implementación, reemplaza esta función.
    Debe devolver: [{"id": "<page_id>", "name": "...", "href": "https://...", "thumb": "https://..."}]
    """
    return []  # deja que el endpoint haga fallback a BD si existe información

# =================== API: /auth02/cards (con persistencia) ===================
@app.get("/auth02/cards")
async def auth02_cards():
    now = datetime.now(timezone.utc)
    if db_pool:
        async with db_pool.acquire() as conn:
            try:
                if await _cards_db_is_fresh(conn):
                    return await _cards_db_all(conn)
            except Exception:
                traceback.print_exc()

    if _cards_cache["items"] and now - _cards_cache["ts"] < CARDS_TTL:
        return _cards_cache["items"]

    async with _cards_lock:
        now = datetime.now(timezone.utc)
        if db_pool:
            async with db_pool.acquire() as conn:
                try:
                    if await _cards_db_is_fresh(conn):
                        return await _cards_db_all(conn)
                except Exception:
                    traceback.print_exc()

        if _cards_cache["items"] and now - _cards_cache["ts"] < CARDS_TTL:
            return _cards_cache["items"]

        try:
            items = await _scrape_cards_from_sekaipedia()
            if not items:
                if db_pool:
                    async with db_pool.acquire() as conn:
                        existing = await _cards_db_all(conn)
                        if existing:
                            return existing
                raise HTTPException(status_code=503, detail="Upstream unavailable or parse error")

            _cards_cache["items"] = items
            _cards_cache["ts"] = datetime.now(timezone.utc)

            if db_pool:
                async with db_pool.acquire() as conn:
                    await _cards_db_upsert_many(conn, items)

            return items
        except HTTPException:
            raise
        except Exception as e:
            traceback.print_exc()
            if db_pool:
                async with db_pool.acquire() as conn:
                    existing = await _cards_db_all(conn)
                    if existing:
                        return existing
            raise HTTPException(status_code=503, detail=f"Failed to scrape: {e}")

# =================== API: /auth02/card-variants ===================
@app.get("/auth02/card-variants")
async def get_card_variants(href: str = Query(..., description="URL absoluta de la Card:")) -> Dict:
    page_title = _page_title_from_href(href)

    page_id: Optional[int] = None
    thumb_url: Optional[str] = None
    if db_pool:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT page_id, thumb FROM public.sekaicards WHERE href=$1", href)
            if row:
                page_id = int(row["page_id"])
                thumb_url = row["thumb"]
            if page_id:
                variants, ts = await _variants_db_get(conn, page_id)
                if variants and ts and (datetime.now(timezone.utc) - ts) < CARDS_TTL:
                    return {"name": page_title, "variants": variants}

    data = {"name": page_title, "variants": []}
    try:
        scraped = await _scrape_card_variants(href)
        data["variants"] = scraped.get("variants", [])
    except Exception:
        traceback.print_exc()

    variants: List[Dict] = []
    if thumb_url:
        variants.append({"label": "Thumb", "url": thumb_url})
    if data["variants"]:
        for v in data["variants"]:
            if v.get("label") == "Trained" and v.get("url") and v["url"] != thumb_url:
                variants.append(v)

    try:
        if db_pool and page_id:
            async with db_pool.acquire() as conn:
                await _variants_db_save(conn, page_id, variants)
    except Exception:
        traceback.print_exc()

    return {"name": page_title, "variants": variants}

# =================== Static ===================
static_dir = os.path.join(os.path.dirname(__file__), "public")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
