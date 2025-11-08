# main.py
import os
import secrets
import traceback
import asyncio
import random
import unicodedata
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse, unquote

import asyncpg
import httpx
from bs4 import BeautifulSoup  # type: ignore
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

# ================ Sesiones en memoria (solo sesión) ================
SESSIONS: Dict[str, Dict] = {}  # sid -> {"id": "<discord_id>", "username": "..."}

def set_session_cookie(resp, sid: str):
    resp.set_cookie(
        key=COOKIE_NAME,
        value=sid,
        httponly=True,
        secure=True,
        samesite="none",
        path="/",
        max_age=60 * 60 * 24 * 7
    )

# =========================== DB & Schema Flags ===========================
db_pool: Optional[asyncpg.pool.Pool] = None

HAS_DISCORD_ID_COL: bool = False  # players.discord_id
SEASONS_HAS_ACTIVE: bool    = False
SEASONS_HAS_NAME: bool      = False
SEASONS_HAS_STARTS_AT: bool = False
SEASONS_HAS_ENDS_AT: bool   = False

# ======================= Arranque de BD =======================
async def _has_col(conn: asyncpg.Connection, table: str, column: str) -> bool:
    row = await conn.fetchrow(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=$1 AND column_name=$2
        """,
        table, column
    )
    return bool(row)

async def _detect_schema(conn: asyncpg.Connection) -> None:
    global HAS_DISCORD_ID_COL, SEASONS_HAS_ACTIVE, SEASONS_HAS_NAME, SEASONS_HAS_STARTS_AT, SEASONS_HAS_ENDS_AT
    try:
        HAS_DISCORD_ID_COL   = await _has_col(conn, "players", "discord_id")
        SEASONS_HAS_ACTIVE   = await _has_col(conn, "seasons", "active")
        SEASONS_HAS_NAME     = await _has_col(conn, "seasons", "name")
        SEASONS_HAS_STARTS_AT= await _has_col(conn, "seasons", "starts_at")
        SEASONS_HAS_ENDS_AT  = await _has_col(conn, "seasons", "ends_at")
        print(f"🔎 Schema: players.discord_id={HAS_DISCORD_ID_COL} | seasons.active={SEASONS_HAS_ACTIVE} name={SEASONS_HAS_NAME} starts_at={SEASONS_HAS_STARTS_AT} ends_at={SEASONS_HAS_ENDS_AT}")
    except Exception:
        traceback.print_exc()

async def _ensure_pfp_table(conn: asyncpg.Connection) -> None:
    # PFPs persistentes
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS public.player_pfps (
          id         BIGSERIAL PRIMARY KEY,
          user_id    TEXT UNIQUE,
          discord_id TEXT,
          name_lower TEXT,
          url        TEXT NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    await conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_player_pfps_discord_id
        ON public.player_pfps(discord_id)
        WHERE discord_id IS NOT NULL;
        """
    )
    await conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_player_pfps_name_lower
        ON public.player_pfps(name_lower)
        WHERE name_lower IS NOT NULL;
        """
    )

async def _ensure_cards_tables(conn: asyncpg.Connection) -> None:
    # Lista de cartas
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS public.cards_cache (
          id        TEXT PRIMARY KEY,     -- pageid de sekaipedia (string)
          name      TEXT NOT NULL,
          href      TEXT NOT NULL,
          thumb     TEXT,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    # Variantes por carta
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS public.card_variants_cache (
          href           TEXT PRIMARY KEY,   -- URL de la wiki
          title          TEXT,
          untrained_url  TEXT,
          trained_url    TEXT,
          updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_cards_cache_updated_at ON public.cards_cache(updated_at DESC);"
    )

@app.on_event("startup")
async def startup():
    global db_pool
    if not DATABASE_URL:
        print("⚠️ DATABASE_URL is empty. /api endpoints will raise 500")
        return

    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=10,
        timeout=10.0,
        command_timeout=30.0,
    )
    print("✅ DB pool initialized")
    async with db_pool.acquire() as conn:
        await _detect_schema(conn)
        await _ensure_pfp_table(conn)
        await _ensure_cards_tables(conn)

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
    if requested_season_id > 0:
        if await _season_exists(conn, requested_season_id):
            return requested_season_id
        raise HTTPException(404, f"season {requested_season_id} not found")

    if SEASONS_HAS_ACTIVE:
        row = await conn.fetchrow("""
            SELECT id
            FROM public.seasons
            WHERE active = TRUE
            ORDER BY id DESC
            LIMIT 1
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
STATE_STORE: Dict[str, datetime] = {}  # state -> expiry (UTC)
STATE_TTL = timedelta(minutes=10)

async def _resolve_user_id_from_discord(discord_id: str) -> Optional[str]:
    if not (db_pool and HAS_DISCORD_ID_COL and discord_id):
        return None
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id FROM public.players WHERE discord_id = $1 LIMIT 1",
            discord_id
        )
        if row:
            return str(row["user_id"])
    return None

def _valid_https_url(u: str) -> bool:
    try:
        p = urlparse(u)
        if p.scheme != "https": return False
        if not p.netloc: return False
        if any(c.isspace() for c in u) or '"' in u or "'" in u: return False
        return True
    except Exception:
        return False

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
    STATE_STORE[state] = datetime.utcnow() + STATE_TTL

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
    if not exp or datetime.utcnow() > exp:
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

# =================== Persistencia de PFP en DB ===================
def _ascii(s: str) -> str:
    return (
        unicodedata.normalize("NFKD", s or "")
        .encode("ascii", "ignore")
        .decode("ascii")
    )

_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Mobile Safari/537.36",
]

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

@app.post("/auth02/set-pfp")
async def set_pfp(request: Request):
    if not db_pool:
        raise HTTPException(500, "DB pool not initialized")

    body = await request.json()
    url = (body.get("url") or "").strip()
    name_lower = (body.get("nameLower") or "").strip().lower() or None
    user_id = (body.get("userId") or "").strip() or None

    sid = request.cookies.get(COOKIE_NAME)
    session = SESSIONS.get(sid or "", {})
    discord_id = str(session.get("id") or "") or None

    if not user_id and discord_id:
        resolved = await _resolve_user_id_from_discord(discord_id)
        if resolved:
            user_id = resolved

    if not (user_id or discord_id or name_lower):
        raise HTTPException(400, "missing userId or discordId or nameLower")

    async with db_pool.acquire() as conn:
        if url == "":
            if user_id:
                await conn.execute("DELETE FROM public.player_pfps WHERE user_id = $1", user_id)
            if discord_id:
                await conn.execute("DELETE FROM public.player_pfps WHERE discord_id = $1", discord_id)
            if name_lower:
                await conn.execute("DELETE FROM public.player_pfps WHERE name_lower = $1", name_lower)
            return {"ok": True, "deleted": True}

        if not _valid_https_url(url):
            raise HTTPException(400, "invalid url")

        if user_id:
            await conn.execute(
                """
                INSERT INTO public.player_pfps(user_id, discord_id, name_lower, url)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT (user_id) DO UPDATE SET
                  discord_id = EXCLUDED.discord_id,
                  name_lower = EXCLUDED.name_lower,
                  url        = EXCLUDED.url,
                  updated_at = now()
                """,
                user_id, discord_id, name_lower, url
            )
        elif discord_id:
            await conn.execute(
                """
                INSERT INTO public.player_pfps(discord_id, name_lower, url)
                VALUES ($1,$2,$3)
                ON CONFLICT ON CONSTRAINT ux_player_pfps_discord_id DO UPDATE SET
                  name_lower = EXCLUDED.name_lower,
                  url        = EXCLUDED.url,
                  updated_at = now()
                """,
                discord_id, name_lower, url
            )
        else:
            await conn.execute(
                """
                INSERT INTO public.player_pfps(name_lower, url)
                VALUES ($1,$2)
                ON CONFLICT ON CONSTRAINT ux_player_pfps_name_lower DO UPDATE SET
                  url        = EXCLUDED.url,
                  updated_at = now()
                """,
                name_lower, url
            )

    return {"ok": True, "byUser": bool(user_id), "byDiscord": bool(discord_id), "byName": bool(name_lower)}

@app.get("/auth02/pfps")
async def list_pfps():
    if not db_pool:
        raise HTTPException(500, "DB pool not initialized")

    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, discord_id, name_lower, url FROM public.player_pfps")

    byId, byDiscord, byName = {}, {}, {}
    for r in rows:
        if r["user_id"]:
            byId[str(r["user_id"])] = r["url"]
        if r["discord_id"]:
            byDiscord[str(r["discord_id"])] = r["url"]
        if r["name_lower"]:
            byName[str(r["name_lower"]).lower()] = r["url"]
    return JSONResponse({"byId": byId, "byDiscord": byDiscord, "byName": byName})

# ======================= API: Players =======================
@app.get("/api/players")
async def get_players(
    season_id: int = Query(..., description="Season ID (0 = season actual activa)"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    if not db_pool:
        raise HTTPException(500, "DB pool not initialized")

    try:
        async with db_pool.acquire() as conn:
            picked = await _pick_effective_season(conn, season_id)

            select_cols = [
                "p.user_id  AS user_id",
                "p.name     AS name",
                "p.country  AS country",
                "sp.mmr     AS mmr",
                "sp.role    AS rank",
            ]
            if HAS_DISCORD_ID_COL:
                select_cols.insert(1, "p.discord_id AS discord_id")

            q = f"""
                SELECT {", ".join(select_cols)}
                FROM public.players p
                INNER JOIN public.seasonplayers sp ON p.user_id = sp.user_id
                WHERE sp.season_id = $1
                ORDER BY sp.mmr DESC
                LIMIT $2 OFFSET $3
            """
            rows = await conn.fetch(q, picked, limit, offset)

    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"DB error: {e}")

    out = []
    for r in rows:
        out.append({
            "id": r.get("user_id"),
            "discordId": r.get("discord_id") if HAS_DISCORD_ID_COL else None,
            "name": r.get("name"),
            "mmr": r.get("mmr"),
            "country": r.get("country"),
            "rank": r.get("rank"),
            "pfp": None,  # el front combina con /auth02/pfps
        })
    return out

# =================== Sekaipedia ===================
SEKAIPEDIA_LIST = "https://sekaipedia.org/wiki/List_of_cards"
SEKAIPEDIA_API  = "https://sekaipedia.org/w/api.php"

_cards_lock = asyncio.Lock()
CARDS_TTL = timedelta(hours=24)           # TTL largo (persistido)
VARIANTS_TTL = timedelta(days=30)         # rara vez cambian

async def _sleep_jitter(a=0.12, b=0.45):
    await asyncio.sleep(random.uniform(a, b))

async def _get_json(client: httpx.AsyncClient, url: str, params: Dict) -> Dict:
    for attempt in range(5):
        try:
            headers = _mk_headers(SEKAIPEDIA_LIST)
            p2 = dict(params); p2.setdefault("origin", "*")
            r = await client.get(url, params=p2, headers=headers, follow_redirects=True, timeout=30)
            if r.status_code in (403, 429):
                await asyncio.sleep(1.0 + attempt * 0.6)
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == 4:
                raise
            await asyncio.sleep(0.8 + attempt * 0.5)
    return {}

def _abs_sekaipedia(u: str) -> str:
    if not u: return ""
    if u.startswith("//"): return "https:" + u
    if u.startswith("/"):  return "https://sekaipedia.org" + u
    return u

# -------- Persistencia: cards_cache ----------
async def _cards_from_db(conn: asyncpg.Connection) -> List[Dict]:
    rows = await conn.fetch("SELECT id,name,href,thumb FROM public.cards_cache ORDER BY name ASC")
    return [{"id": r["id"], "name": r["name"], "href": r["href"], "thumb": r["thumb"]} for r in rows]

async def _cards_stale(conn: asyncpg.Connection) -> bool:
    row = await conn.fetchrow("SELECT MAX(updated_at) AS m FROM public.cards_cache")
    if not row or not row["m"]:
        return True
    return (datetime.utcnow() - row["m"]) > CARDS_TTL

async def _upsert_cards(conn: asyncpg.Connection, items: List[Dict]) -> None:
    for c in items:
        await conn.execute(
            """
            INSERT INTO public.cards_cache(id, name, href, thumb)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (id) DO UPDATE SET
              name = EXCLUDED.name,
              href = EXCLUDED.href,
              thumb = EXCLUDED.thumb,
              updated_at = now()
            """,
            c["id"], c["name"], c["href"], c.get("thumb")
        )

async def _scrape_cards_from_sekaipedia() -> List[Dict]:
    items: List[Dict] = []
    async with httpx.AsyncClient() as client:
        apcontinue = None
        while True:
            params = {
                "action": "query",
                "list": "categorymembers",
                "cmtitle": "Category:Cards",
                "cmlimit": "500",
                "format": "json",
            }
            if apcontinue: params["cmcontinue"] = apcontinue
            data = await _get_json(client, SEKAIPEDIA_API, params)
            for m in data.get("query", {}).get("categorymembers", []):
                title = m.get("title", "")
                pageid = str(m.get("pageid", ""))
                href = f"https://sekaipedia.org/wiki/{title.replace(' ', '_')}"
                items.append({"id": pageid, "name": title, "href": href, "thumb": None})
            apcontinue = data.get("continue", {}).get("cmcontinue")
            if not apcontinue: break
            await _sleep_jitter(0.25, 0.7)

        # pageimages por lotes
        for i in range(0, len(items), 50):
            chunk = items[i:i+50]
            pageids = "|".join(c["id"] for c in chunk)
            params = {
                "action": "query",
                "pageids": pageids,
                "prop": "pageimages",
                "piprop": "thumbnail",
                "pithumbsize": "256",
                "format": "json",
            }
            data = await _get_json(client, SEKAIPEDIA_API, params)
            for pid, pinfo in (data.get("query", {}).get("pages", {})).items():
                thumb = (pinfo.get("thumbnail") or {}).get("source")
                if thumb:
                    for c in chunk:
                        if c["id"] == pid:
                            c["thumb"] = _abs_sekaipedia(thumb)
            await _sleep_jitter(0.25, 0.7)
    return items

@app.get("/auth02/cards")
async def auth02_cards():
    if not db_pool:
        raise HTTPException(500, "DB pool not initialized")

    async with db_pool.acquire() as conn:
        items = await _cards_from_db(conn)
        stale = await _cards_stale(conn)

    # Responder lo que haya en DB (no se pierde en redeploy)
    if items:
        # refresh silencioso si está viejo
        if stale:
            asyncio.create_task(_refresh_cards_background())
        return items

    # Si DB vacío: construir y persistir
    async with _cards_lock:
        try:
            items = await _scrape_cards_from_sekaipedia()
            async with db_pool.acquire() as conn:
                await _upsert_cards(conn, items)
            return items
        except Exception as e:
            traceback.print_exc()
            raise HTTPException(status_code=503, detail=f"Failed to scrape: {e}")

async def _refresh_cards_background():
    try:
        items = await _scrape_cards_from_sekaipedia()
        async with db_pool.acquire() as conn:
            await _upsert_cards(conn, items)
        print(f"↻ Cards cache refreshed: {len(items)}")
    except Exception:
        traceback.print_exc()

# -------- Variants (infobox tabber) persistencia ----------
def _page_title_from_href(href: str) -> str:
    try:
        path = urlparse(href).path
        title = path.split("/wiki/", 1)[1]
        title = unquote(title).replace("_", " ").strip()
        return title
    except Exception:
        return ""

def _classify_variants_from_html(html: str) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Devuelve (title, untrained_url, trained_url) extrayendo del INFObox/tabber.
    Si no se encuentran tabs, intenta por texto en alt/filename (fallback).
    """
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    title_el = soup.select_one("#firstHeading")
    page_title = title_el.get_text(strip=True) if title_el else ""

    root = soup.select_one("#mw-content-text") or soup

    # Tabber típico: .tabber .tabbertab[title=Untrained|Trained]
    un_url = None
    tr_url = None
    for tab in root.select(".tabber .tabbertab"):
        t = (tab.get("title") or "").strip().lower()
        img = tab.select_one("img")
        if not img: 
            continue
        src = img.get("data-src") or img.get("src") or ""
        src = _abs_sekaipedia(src)
        if "untrained" in t:
            un_url = src
        elif "trained" in t or "after training" in t or "idolized" in t:
            tr_url = src

    # Fallback: buscar imágenes cerca del infobox
    if not (un_url or tr_url):
        imgs = root.select("img")
        for img in imgs:
            alt = (img.get("alt") or "").lower()
            src = _abs_sekaipedia(img.get("data-src") or img.get("src") or "")
            if not src:
                continue
            if any(k in alt for k in ["untrained", "before training", "base"]):
                un_url = un_url or src
            elif any(k in alt for k in ["trained", "after training", "idolized"]):
                tr_url = tr_url or src

    return page_title, un_url, tr_url

async def _get_variants_from_db(conn: asyncpg.Connection, href: str) -> Optional[Dict]:
    row = await conn.fetchrow(
        "SELECT title, untrained_url, trained_url, updated_at FROM public.card_variants_cache WHERE href=$1",
        href
    )
    if not row:
        return None
    # TTL
    if (datetime.utcnow() - row["updated_at"]) > VARIANTS_TTL:
        return {"stale": True, "title": row["title"], "variants": [
            *( [{"label":"Untrained","url":row["untrained_url"]}] if row["untrained_url"] else [] ),
            *( [{"label":"Trained","url":row["trained_url"]}]   if row["trained_url"]   else [] ),
        ]}
    return {"stale": False, "title": row["title"], "variants": [
        *( [{"label":"Untrained","url":row["untrained_url"]}] if row["untrained_url"] else [] ),
        *( [{"label":"Trained","url":row["trained_url"]}]   if row["trained_url"]   else [] ),
    ]}

async def _upsert_variants(conn: asyncpg.Connection, href: str, title: str, un_url: Optional[str], tr_url: Optional[str]) -> None:
    await conn.execute(
        """
        INSERT INTO public.card_variants_cache(href, title, untrained_url, trained_url)
        VALUES ($1,$2,$3,$4)
        ON CONFLICT (href) DO UPDATE SET
          title = EXCLUDED.title,
          untrained_url = EXCLUDED.untrained_url,
          trained_url   = EXCLUDED.trained_url,
          updated_at    = now()
        """,
        href, title, un_url, tr_url
    )

async def _scrape_variants_html(href: str) -> Tuple[str, Optional[str], Optional[str]]:
    async with httpx.AsyncClient() as client:
        r = await client.get(href, headers=_mk_headers(href), follow_redirects=True, timeout=30)
        if r.status_code in (403, 429):
            await asyncio.sleep(1.0)
            r = await client.get(href, headers=_mk_headers(href), follow_redirects=True, timeout=30)
        r.raise_for_status()
        html = r.text
    return _classify_variants_from_html(html)

@app.get("/auth02/card-variants")
async def get_card_variants(href: str = Query(..., description="URL absoluta de la Card:")) -> Dict:
    if not db_pool:
        raise HTTPException(500, "DB pool not initialized")

    try:
        async with db_pool.acquire() as conn:
            cached = await _get_variants_from_db(conn, href)

        if cached and not cached.get("stale"):
            return {"name": cached["title"], "variants": cached["variants"]}

        # scrape (o refresh si estaba stale)
        title, un_url, tr_url = await _scrape_variants_html(href)

        # Si la carta no tiene variantes, devolver solo 1 (front no debe mostrar selector)
        variants = []
        if un_url: variants.append({"label": "Untrained", "url": un_url})
        if tr_url: variants.append({"label": "Trained", "url": tr_url})
        if not variants:
            # ultimo fallback: al menos un img del infobox ya vino en un_url o tr_url,
            # pero si ambos None, devolver vacío (front ignora).
            pass

        async with db_pool.acquire() as conn:
            await _upsert_variants(conn, href, title or _page_title_from_href(href), un_url, tr_url)

        return {"name": title or _page_title_from_href(href), "variants": variants}

    except Exception:
        traceback.print_exc()
        return {"name": _page_title_from_href(href), "variants": []}

# =================== Static (Front opcional) ===================
static_dir = os.path.join(os.path.dirname(__file__), "public")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
