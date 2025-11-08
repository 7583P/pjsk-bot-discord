import os
import re
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

HAS_DISCORD_ID_COL: bool = False  # players.discord_id
SEASONS_HAS_ACTIVE: bool    = False
SEASONS_HAS_NAME: bool      = False
SEASONS_HAS_STARTS_AT: bool = False
SEASONS_HAS_ENDS_AT: bool   = False

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
        # Migraciones mínimas para persistir cartas y variantes
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS public.sekaicards(
          page_id    BIGINT PRIMARY KEY,
          name       TEXT NOT NULL,
          href       TEXT NOT NULL,
          thumb      TEXT,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS public.sekaicard_variants(
          page_id    BIGINT NOT NULL REFERENCES public.sekaicards(page_id) ON DELETE CASCADE,
          label      TEXT   NOT NULL,   -- 'Untrained' | 'Trained'
          url        TEXT   NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY (page_id, label)
        );
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
    # >0: usa la específica (si existe)
    if requested_season_id > 0:
        if await _season_exists(conn, requested_season_id):
            return requested_season_id
        raise HTTPException(404, f"season {requested_season_id} not found")

    # 0: usa la activa si existe, si no, la última
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
# PFPs en memoria (tres llaves)
PFP_BY_NAME:    Dict[str, str] = {}  # nameLower -> url
PFP_BY_ID:      Dict[str, str] = {}  # players.user_id -> url
PFP_BY_DISCORD: Dict[str, str] = {}  # players.discord_id -> url

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

def _resolve_pfp_for_player(user_id: Optional[str], discord_id: Optional[str], name: Optional[str]) -> Optional[str]:
    # Prioridad: user_id -> discord_id -> nameLower
    if user_id:
        url = PFP_BY_ID.get(str(user_id))
        if url: return url
    if discord_id:
        url = PFP_BY_DISCORD.get(str(discord_id))
        if url: return url
    if name:
        url = PFP_BY_NAME.get((name or "").lower())
        if url: return url
    return None

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

# =================== /auth02/pfps ===================
MAX_PFPS = 5000

@app.post("/auth02/set-pfp")
async def set_pfp(request: Request):
    """
    Guarda/actualiza el PFP:
    - Si no mandan userId, intenta cookie -> discord_id -> players.user_id.
    - Se guarda por userId, por discordId y opcionalmente por nameLower.
    - url="" borra los registros asociados a esas llaves.
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
        if user_id:    PFP_BY_ID.pop(user_id, None)
        if discord_id: PFP_BY_DISCORD.pop(discord_id, None)
        if name_lower: PFP_BY_NAME.pop(name_lower, None)
        return {"ok": True, "deleted": True}

    if not _valid_https_url(url):
        raise HTTPException(400, "invalid url")

    if not (user_id or discord_id or name_lower):
        raise HTTPException(400, "missing userId or discordId or nameLower")

    if len(PFP_BY_ID) + len(PFP_BY_NAME) + len(PFP_BY_DISCORD) > MAX_PFPS:
        raise HTTPException(429, "pfp store is full")

    if user_id:    PFP_BY_ID[user_id] = url
    if discord_id: PFP_BY_DISCORD[discord_id] = url
    if name_lower: PFP_BY_NAME[name_lower] = url

    return {"ok": True, "byId": bool(user_id), "byDiscord": bool(discord_id), "byName": bool(name_lower)}

@app.get("/auth02/pfps")
async def list_pfps():
    return JSONResponse({"byId": PFP_BY_ID, "byDiscord": PFP_BY_DISCORD, "byName": PFP_BY_NAME})

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
        user_id    = r.get("user_id")
        discord_id = r.get("discord_id") if HAS_DISCORD_ID_COL else None
        name       = r.get("name")

        pfp_url = _resolve_pfp_for_player(
            str(user_id) if user_id is not None else None,
            str(discord_id) if discord_id is not None else None,
            name
        )

        out.append({
            "id": user_id,
            "discordId": discord_id,
            "name": name,
            "mmr": r.get("mmr"),
            "country": r.get("country"),
            "rank": r.get("rank"),
            "pfp": pfp_url,  # <- el front puede usar p.pfp directamente
        })
    return out

# =================== Sekaipedia: anti-bot friendly ===================
SEKAIPEDIA_LIST = "https://sekaipedia.org/wiki/List_of_cards"
SEKAIPEDIA_API  = "https://sekaipedia.org/w/api.php"

# UAs rotativos (Chrome/Win/mac/Linux/Android)
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
    # Garantiza headers ASCII para httpx / RFC
    return {k: _ascii(v) for k, v in raw.items()}

# Cache (memoria)
_cards_cache: Dict[str, object] = {"items": [], "ts": datetime.utcfromtimestamp(0)}
CARDS_TTL = timedelta(hours=6)
_cards_lock = asyncio.Lock()

async def _sleep_jitter(a=0.15, b=0.5):
    await asyncio.sleep(random.uniform(a, b))

async def _get_json(client: httpx.AsyncClient, url: str, params: Dict) -> Dict:
    # backoff con 403/429
    for attempt in range(5):
        try:
            headers = _mk_headers(SEKAIPEDIA_LIST)
            # origin=* es recomendado por MediaWiki para CORS/CDN; ayuda con 403
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
    if not u: return ""
    if u.startswith("//"): return "https:" + u
    if u.startswith("/"):  return "https://sekaipedia.org" + u
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
    return (datetime.utcnow() - row["ts"]) < CARDS_TTL

async def _variants_db_get(conn: asyncpg.Connection, page_id: int) -> Tuple[List[Dict], Optional[datetime]]:
    rows = await conn.fetch("""
        SELECT label, url, updated_at
        FROM public.sekaicard_variants
        WHERE page_id=$1
        ORDER BY label
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

# =================== Scraping ===================
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

def _is_trained(filename: str) -> bool:
    s = filename.lower()
    return ("trained" in s) or ("after training" in s) or ("idolized" in s)

def _is_untrained(filename: str) -> bool:
    s = filename.lower()
    return ("untrained" in s) or ("before training" in s) or ("base" in s)

def _prefer_square(links: List[str]) -> List[str]:
    seen, out = set(), []
    for u in links:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out

def _page_title_from_href(href: str) -> str:
    # https://sekaipedia.org/wiki/1_Page_of_Daily_Life  ->  1 Page of Daily Life
    try:
        path = urlparse(href).path  # /wiki/...
        title = path.split("/wiki/", 1)[1]
        title = unquote(title).replace("_", " ").strip()
        return title
    except Exception:
        return ""

async def _fetch_page_images(client: httpx.AsyncClient, page_title: str) -> List[str]:
    """
    Devuelve URLs de imagen de la página usando la MediaWiki API:
      1) prop=images para títulos de archivo (File:...)
      2) prop=imageinfo&iiprop=url&iiurlwidth=512 para obtener URLs (thumb o full)
    """
    if not page_title:
        return []

    # 1) Títulos de archivos asociados a la página
    params1 = {
        "action": "query",
        "prop": "images",
        "titles": page_title,
        "imlimit": "500",
        "format": "json",
    }
    data1 = await _get_json(client, SEKAIPEDIA_API, params1)
    pages = (data1.get("query", {}) or {}).get("pages", {}) or {}

    file_titles: List[str] = []
    for _, pinfo in pages.items():
        for it in (pinfo.get("images") or []):
            t = it.get("title", "")
            if t.startswith("File:"):
                file_titles.append(t)

    if not file_titles:
        return []

    # 2) Resolver URLs por lotes
    urls: List[str] = []
    for i in range(0, len(file_titles), 50):
        chunk = file_titles[i:i+50]
        params2 = {
            "action": "query",
            "titles": "|".join(chunk),
            "prop": "imageinfo",
            "iiprop": "url|size|mime",
            "iiurlwidth": "512",
            "format": "json",
        }
        data2 = await _get_json(client, SEKAIPEDIA_API, params2)
        for _, finfo in (data2.get("query", {}).get("pages", {}) or {}).items():
            for ii in (finfo.get("imageinfo") or []):
                url = ii.get("thumburl") or ii.get("url")
                if url:
                    urls.append(_abs_sekaipedia(url))
        await _sleep_jitter(0.2, 0.5)

    # Únicos y en orden
    return _prefer_square(urls)

async def _scrape_card_variants(href: str) -> Dict:
    # Nueva versión usando MediaWiki API (robusta)
    page_title = _page_title_from_href(href)
    async with httpx.AsyncClient() as client:
        urls = await _fetch_page_images(client, page_title)

    trained_links: List[str] = []
    untrained_links: List[str] = []
    unknown_links: List[str] = []

    for u in urls:
        filename = u.rsplit("/", 1)[-1]
        if _is_trained(filename):
            trained_links.append(u)
        elif _is_untrained(filename):
            untrained_links.append(u)
        else:
            unknown_links.append(u)

    if not untrained_links and unknown_links:
        untrained_links.append(unknown_links[0])
    if not trained_links and len(unknown_links) >= 2:
        trained_links.append(unknown_links[1])

    variants = []
    if untrained_links:
        variants.append({"label": "Untrained", "url": untrained_links[0]})
    if trained_links:
        variants.append({"label": "Trained", "url": trained_links[0]})

    return {"name": page_title, "variants": variants}

# =================== API: /auth02/cards (con persistencia) ===================
@app.get("/auth02/cards")
async def auth02_cards():
    now = datetime.utcnow()

    # 1) Si hay DB y está “fresh”, entrega desde BD
    if db_pool:
        async with db_pool.acquire() as conn:
            try:
                if await _cards_db_is_fresh(conn):
                    return await _cards_db_all(conn)
            except Exception:
                traceback.print_exc()

    # 2) Si el cache en memoria está fresco, úsalo
    if _cards_cache["items"] and now - _cards_cache["ts"] < CARDS_TTL:
        return _cards_cache["items"]

    # 3) Raspa, guarda en BD y en memoria, y devuelve (con lock)
    async with _cards_lock:
        now = datetime.utcnow()
        # re-check dentro del lock
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
                # si scraping falla, pero hay algo en BD, sirve lo de BD
                if db_pool:
                    async with db_pool.acquire() as conn:
                        existing = await _cards_db_all(conn)
                        if existing:
                            return existing
                raise HTTPException(status_code=503, detail="Upstream unavailable or parse error")

            _cards_cache["items"] = items
            _cards_cache["ts"] = datetime.utcnow()

            if db_pool:
                async with db_pool.acquire() as conn:
                    await _cards_db_upsert_many(conn, items)

            return items
        except HTTPException:
            raise
        except Exception as e:
            traceback.print_exc()
            # fallback a BD si existe
            if db_pool:
                async with db_pool.acquire() as conn:
                    existing = await _cards_db_all(conn)
                    if existing:
                        return existing
            raise HTTPException(status_code=503, detail=f"Failed to scrape: {e}")

# =================== API: /auth02/card-variants (con persistencia) ===================
@app.get("/auth02/card-variants")
async def get_card_variants(href: str = Query(..., description="URL absoluta de la Card:")) -> Dict:
    """
    Busca variantes en BD por page_id (usando href), si existen las devuelve.
    Si no existen o están fuera de TTL, las re-scrapea, guarda y devuelve.
    Si el href no está en sekaicards, igual se scrapea y se devuelve (sin guardar).
    """
    page_title = _page_title_from_href(href)

    # Intentar resolver page_id desde BD por href
    page_id: Optional[int] = None
    if db_pool:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT page_id FROM public.sekaicards WHERE href=$1", href)
            if row:
                page_id = int(row["page_id"])
                variants, ts = await _variants_db_get(conn, page_id)
                if variants and ts and (datetime.utcnow() - ts) < CARDS_TTL:
                    return {"name": page_title, "variants": variants}

    # Scrape
    try:
        data = await _scrape_card_variants(href)
    except Exception:
        traceback.print_exc()
        return {"name": "", "variants": []}

    # Guardar si tenemos page_id
    try:
        if db_pool and page_id:
            async with db_pool.acquire() as conn:
                await _variants_db_save(conn, page_id, data.get("variants", []))
    except Exception:
        traceback.print_exc()

    return data

# =================== Static (Front opcional) ===================
static_dir = os.path.join(os.path.dirname(__file__), "public")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
#d
