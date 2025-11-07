import os
import re
import secrets
import traceback
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Optional

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
DISCORD_REDIRECT_URI  = os.getenv(
    "DISCORD_REDIRECT_URI",
    "https://unique-education-production.up.railway.app/auth02/callback"
)
FRONT_URL   = os.getenv("FRONT_URL", "https://pjsk-page.vercel.app")
CORS_ORIGIN = os.getenv("CORS_ORIGIN", FRONT_URL)
COOKIE_NAME = os.getenv("COOKIE_NAME", "sid")
DATABASE_URL = os.getenv("DATABASE_URL", "")

# ===================== FastAPI =====================
app = FastAPI()

# ============== CORS (con credenciales) ==============
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONT_URL],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ================ Sesiones simples en memoria ================
SESSIONS: Dict[str, Dict] = {}  # sid -> {"id": "...", "username": "..."}

def set_session_cookie(resp, sid: str):
    resp.set_cookie(
        key=COOKIE_NAME,
        value=sid,
        httponly=True,
        secure=True,
        samesite="none",
        path="/",
        max_age=60 * 60 * 24 * 7,
    )

# =========================== DB ===========================
db_pool: Optional[asyncpg.pool.Pool] = None
HAS_DISCORD_ID_COL: bool = False  # detectado en startup

async def _detect_schema_features(conn: asyncpg.Connection) -> None:
    global HAS_DISCORD_ID_COL
    try:
        row = await conn.fetchrow("""
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema='public'
              AND table_name='players'
              AND column_name='discord_id'
        """)
        HAS_DISCORD_ID_COL = bool(row)
        print(f"🔎 Schema: players.discord_id = {HAS_DISCORD_ID_COL}")
    except Exception:
        traceback.print_exc()
        HAS_DISCORD_ID_COL = False

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
        await _detect_schema_features(conn)

@app.on_event("shutdown")
async def shutdown():
    if db_pool:
        await db_pool.close()
        print("🛑 DB pool closed")

# ======================= Helpers de Seasons =======================
async def _pick_effective_season(conn: asyncpg.Connection) -> Optional[int]:
    """
    Devuelve la season 'activa' si existe; si no, la más reciente (id desc).
    Asume tabla public.seasons con columnas estándar:
      - id (PK)
      - name (opcional)
      - active (bool) o is_active (bool)
      - starts_at/ends_at (opcionales)
    """
    # 1) Activa (active o is_active)
    row = await conn.fetchrow("""
        SELECT id
        FROM public.seasons
        WHERE COALESCE(active, is_active, FALSE) = TRUE
        ORDER BY id DESC
        LIMIT 1
    """)
    if row:
        return row["id"]

    # 2) Última por id
    row = await conn.fetchrow("""
        SELECT id
        FROM public.seasons
        ORDER BY id DESC
        LIMIT 1
    """)
    if row:
        return row["id"]
    return None

# ======================= API: Seasons =======================
@app.get("/api/seasons")
async def get_seasons():
    """
    Devuelve la lista de temporadas ordenadas por id desc.
    Campos devueltos: id, name, startsAt, endsAt, active
    (los que no existan salen como null)
    """
    if not db_pool:
        raise HTTPException(500, "DB pool not initialized")

    query = """
    SELECT
      s.id                              AS id,
      COALESCE(s.name, CONCAT('Season ', s.id::text)) AS name,
      (CASE WHEN EXISTS (
         SELECT 1 FROM information_schema.columns
          WHERE table_schema='public' AND table_name='seasons' AND column_name='starts_at'
       ) THEN s.starts_at ELSE NULL END)               AS starts_at,
      (CASE WHEN EXISTS (
         SELECT 1 FROM information_schema.columns
          WHERE table_schema='public' AND table_name='seasons' AND column_name='ends_at'
       ) THEN s.ends_at ELSE NULL END)                 AS ends_at,
      COALESCE(s.active, s.is_active, FALSE)          AS active
    FROM public.seasons s
    ORDER BY s.id DESC
    """
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(query)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"DB error: {e}")

    out = []
    for r in rows:
        out.append({
            "id": r.get("id"),
            "name": r.get("name"),
            "startsAt": r.get("starts_at"),
            "endsAt": r.get("ends_at"),
            "active": r.get("active"),
        })
    return out

# ======================= API: Players =======================
@app.get("/api/players")
async def get_players(
    season_id: int = Query(..., description="Season ID (0 = usar activa / última)"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    q: Optional[str] = Query(None, description="Filtro por nombre (icontains)"),
    rank: Optional[str] = Query(None, description="Filtro por rango exacto"),
):
    if not db_pool:
        raise HTTPException(500, "DB pool not initialized")

    try:
        async with db_pool.acquire() as conn:
            effective_season = season_id
            if season_id == 0:
                picked = await _pick_effective_season(conn)
                if not picked:
                    return []  # no hay seasons
                effective_season = picked

            # columnas dinámicas
            select_cols = [
                "p.user_id  AS user_id",
                "p.name     AS name",
                "p.country  AS country",
                "sp.mmr     AS mmr",
                "sp.role    AS rank",
            ]
            if HAS_DISCORD_ID_COL:
                select_cols.insert(1, "p.discord_id AS discord_id")

            # filtros dinámicos
            where = ["sp.season_id = $1"]
            params = [effective_season, limit, offset]
            param_i = 4  # $1 season, $2 limit, $3 offset → siguiente es $4

            if q:
                where.append(f"p.name ILIKE ${param_i}")
                params.insert(1, f"%{q}%")  # mantenemos limit/offset al final
                param_i += 1
            if rank:
                where.append(f"sp.role = ${param_i}")
                params.insert(-2, rank)     # antes de limit/offset
                param_i += 1

            query = f"""
                SELECT {', '.join(select_cols)}
                FROM public.players p
                INNER JOIN public.seasonplayers sp ON p.user_id = sp.user_id
                WHERE {' AND '.join(where)}
                ORDER BY sp.mmr DESC
                LIMIT $2 OFFSET $3
            """

            rows = await conn.fetch(query, *params)

    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"DB error: {e}")

    out = []
    for r in rows:
        pid = r.get("user_id") or r.get("id")
        out.append({
            "id": pid,
            "discordId": r.get("discord_id"),  # solo si existe la col
            "name": r.get("name"),
            "mmr": r.get("mmr"),
            "country": r.get("country"),
            "rank": r.get("rank"),
        })
    return out

# ======================= AUTH02 (Discord OAuth) =======================
PFP_BY_NAME: Dict[str, str] = {}  # nameLower -> url
PFP_BY_ID:   Dict[str, str] = {}  # userId/discordId -> url

STATE_STORE: Dict[str, datetime] = {}  # state -> expiry (UTC)
STATE_TTL = timedelta(minutes=10)

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
    return SESSIONS[sid]

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
URL_RE = re.compile(r"^https:\/\/[a-z0-9\.\-\/_%\?\=&]+$", re.I)

@app.post("/auth02/set-pfp")
async def set_pfp(request: Request):
    body = await request.json()
    url = (body.get("url") or "").strip()
    name_lower = (body.get("nameLower") or "").strip().lower()
    user_id = (body.get("userId") or "").strip()

    if not user_id:
        sid = request.cookies.get(COOKIE_NAME)
        if sid and sid in SESSIONS:
            user_id = str(SESSIONS[sid].get("id") or "")

    if not name_lower:
        sid = request.cookies.get(COOKIE_NAME)
        if sid and sid in SESSIONS:
            name_lower = (SESSIONS[sid].get("username") or "").lower()

    if url == "":
        if user_id:
            PFP_BY_ID.pop(user_id, None)
        if name_lower:
            PFP_BY_NAME.pop(name_lower, None)
        return {"ok": True, "deleted": True}

    if not (user_id or name_lower):
        raise HTTPException(400, "missing userId or nameLower")

    if not URL_RE.match(url):
        raise HTTPException(400, "invalid url")
    if len(PFP_BY_ID) + len(PFP_BY_NAME) > MAX_PFPS:
        raise HTTPException(429, "pfp store is full")

    if user_id:
        PFP_BY_ID[user_id] = url
    if name_lower:
        PFP_BY_NAME[name_lower] = url

    return {"ok": True}

@app.get("/auth02/pfps")
async def list_pfps():
    return JSONResponse({"byId": PFP_BY_ID, "byName": PFP_BY_NAME})

# =================== /auth02/cards (Sekaipedia) ===================
SEKAIPEDIA_LIST = "https://sekaipedia.org/wiki/List_of_cards"
SEKAIPEDIA_API  = "https://sekaipedia.org/w/api.php"

_cards_cache: Dict[str, object] = {"items": [], "ts": datetime.utcfromtimestamp(0)}
CARDS_TTL = timedelta(hours=6)
_cards_lock = asyncio.Lock()

_HTTP_HEADERS = {
    "User-Agent": "pjsk-lounge/1.1 (+worker; contact: admin@pjsk-lounge)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Referer": "https://sekaipedia.org/",
}

def _abs_url(u: str) -> str:
    if not u: return ""
    if u.startswith("//"): return "https:" + u
    if u.startswith("/"):  return "https://sekaipedia.org" + u
    return u

def _guess_id(name: str, href: str) -> str:
    m = re.search(r"#(\d+)", name or "")
    if m: return m.group(1)
    return (href or "").rstrip("/").split("/")[-1] or (name or "")

def _parse_cards_from_html(html: str) -> List[Dict]:
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    root = soup.select_one("#mw-content-text") or soup
    items: List[Dict] = []

    for a in root.select('a[href^="/wiki/Card:"]'):
        href = a.get("href") or ""
        href_abs = _abs_url(href)
        name = a.get_text(strip=True) or ""

        thumb = None
        img = a.find("img") or (a.parent.find("img") if a.parent else None) or a.find_next_sibling("img")
        if img:
            thumb = img.get("data-src") or img.get("src")
            thumb = _abs_url(thumb)

        items.append({
            "id": _guess_id(name, href_abs),
            "name": name,
            "thumb": thumb,
            "href": href_abs
        })

    seen, dedup = set(), []
    for it in items:
        key = it["href"] or it["name"]
        if key and key not in seen:
            seen.add(key)
            dedup.append(it)
    return dedup

async def _scrape_cards_from_sekaipedia() -> List[Dict]:
    items: List[Dict] = []

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        apcontinue = None
        while True:
            params = {
                "action": "query",
                "list": "categorymembers",
                "cmtitle": "Category:Cards",
                "cmlimit": "500",
                "format": "json",
            }
            if apcontinue:
                params["cmcontinue"] = apcontinue

            r = await client.get(SEKAIPEDIA_API, params=params, headers=_HTTP_HEADERS)
            r.raise_for_status()
            data = r.json()

            for m in data.get("query", {}).get("categorymembers", []):
                title = m.get("title", "")
                pageid = str(m.get("pageid", ""))
                href = f"https://sekaipedia.org/wiki/{title.replace(' ', '_')}"
                items.append({"id": pageid, "name": title, "href": href, "thumb": None})

            apcontinue = data.get("continue", {}).get("cmcontinue")
            if not apcontinue:
                break

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
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
            r = await client.get(SEKAIPEDIA_API, params=params, headers=_HTTP_HEADERS)
            r.raise_for_status()
            for pid, pinfo in (r.json().get("query", {}).get("pages", {})).items():
                thumb = (pinfo.get("thumbnail") or {}).get("source")
                if thumb:
                    for c in chunk:
                        if c["id"] == pid:
                            c["thumb"] = thumb

    return items

@app.get("/auth02/cards")
async def auth02_cards():
    now = datetime.utcnow()
    if _cards_cache["items"] and now - _cards_cache["ts"] < CARDS_TTL:
        return _cards_cache["items"]

    async with _cards_lock:
        now = datetime.utcnow()
        if _cards_cache["items"] and now - _cards_cache["ts"] < CARDS_TTL:
            return _cards_cache["items"]
        try:
            items = await _scrape_cards_from_sekaipedia()
            if not items:
                raise HTTPException(status_code=503, detail="Upstream unavailable or parse error")
            _cards_cache["items"] = items
            _cards_cache["ts"] = datetime.utcnow()
            return items
        except HTTPException:
            raise
        except Exception as e:
            traceback.print_exc()
            raise HTTPException(status_code=503, detail=f"Failed to scrape: {e}")

# =================== /auth02/card-variants ===================
def _abs_sekaipedia(u: str) -> str:
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return "https://sekaipedia.org" + u
    return u

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

async def _scrape_card_variants(href: str) -> Dict:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(href, headers=_HTTP_HEADERS)
        r.raise_for_status()
        html = r.text

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    title = soup.select_one("#firstHeading")
    card_name = title.get_text(strip=True) if title else ""
    root = soup.select_one("#mw-content-text") or soup
    imgs = root.select("img")

    trained_links: List[str] = []
    untrained_links: List[str] = []
    unknown_links: List[str] = []

    for img in imgs:
        src = img.get("data-src") or img.get("src") or ""
        alt = (img.get("alt") or "").strip()
        src = _abs_sekaipedia(src)

        if not src or ("sekaipedia" not in src and "wikimg" not in src and "wikipedia" not in src):
            continue

        filename = alt or src
        if _is_trained(filename):
            trained_links.append(src)
        elif _is_untrained(filename):
            untrained_links.append(src)
        else:
            unknown_links.append(src)

    trained_links   = _prefer_square(trained_links)
    untrained_links = _prefer_square(untrained_links)

    if not untrained_links and len(unknown_links) >= 1:
        untrained_links.append(unknown_links[0])
    if not trained_links and len(unknown_links) >= 2:
        trained_links.append(unknown_links[1])

    variants = []
    if untrained_links:
        variants.append({"label": "Untrained", "url": untrained_links[0]})
    if trained_links:
        variants.append({"label": "Trained", "url": trained_links[0]})

    return {"name": card_name, "variants": variants}

@app.get("/auth02/card-variants")
async def get_card_variants(href: str = Query(..., description="URL absoluta de la Card:")) -> Dict:
    try:
        data = await _scrape_card_variants(href)
        return data
    except Exception:
        traceback.print_exc()
        return {"name": "", "variants": []}

# =================== Static (Front opcional) ===================
static_dir = os.path.join(os.path.dirname(__file__), "public")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
