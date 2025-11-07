import os
import re
import secrets
import traceback
import asyncio
from urllib.parse import urlparse
from datetime import datetime, timedelta
from typing import List, Dict, Optional

import asyncpg
import httpx
from bs4 import BeautifulSoup  # type: ignore
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

# ========================= ENV =========================
DISCORD_CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
# ⚠️ Debe apuntar a TU dominio Railway (puedes sobreescribir por env)
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
# Mantén la lista concreta para evitar sorpresas con cookies cross-site.
# Puedes agregar previews de Vercel aquí si las usas.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONT_URL],        # añade más orígenes si corresponde
    allow_credentials=True,           # necesario para cookies cross-site
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ================ Sesiones simples en memoria ================
# En producción reemplazar por Redis/DB o cookies firmadas/JWT
SESSIONS: Dict[str, Dict] = {}     # sid -> {"id": "...", "username": "..."}

def set_session_cookie(resp: Response, sid: str):
    """
    Cookie sin 'domain' para que el navegador la asocie al dominio del backend (Railway).
    Requisitos para cross-site: Secure + SameSite=None + HTTPS.
    """
    resp.set_cookie(
        key=COOKIE_NAME,
        value=sid,
        httponly=True,
        secure=True,           # requerido para SameSite=None
        samesite="none",       # para cross-site (Vercel -> Railway)
        path="/",
        max_age=60 * 60 * 24 * 7   # 7 días
    )

# =========================== DB ===========================
db_pool: Optional[asyncpg.pool.Pool] = None

@app.on_event("startup")
async def startup():
    global db_pool
    if DATABASE_URL:
        db_pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=10,
            timeout=10.0,
            command_timeout=30.0,
        )
        print("✅ DB pool initialized")
    else:
        print("⚠️ DATABASE_URL is empty. /api endpoints will raise 500")

@app.on_event("shutdown")
async def shutdown():
    if db_pool:
        await db_pool.close()
        print("🛑 DB pool closed")

# ======================= API: Seasons/Players =======================
@app.get("/api/players")
async def get_players(
    season_id: int = Query(..., description="Season ID"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """
    Devuelve jugadores de una season ordenados por MMR (paginado).
    """
    if not db_pool:
        raise HTTPException(500, "DB pool not initialized")

    query = """
      SELECT p.user_id as id, p.name, sp.mmr, p.country, sp.role as rank
      FROM players p INNER JOIN seasonplayers sp ON p.user_id = sp.user_id
      WHERE sp.season_id = $1
      ORDER BY sp.mmr DESC
      LIMIT $2 OFFSET $3;
    """
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(query, season_id, limit, offset)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"DB error: {e}")

    return [
        {
            "id": r["id"],
            "name": r["name"],
            "mmr": r["mmr"],
            "country": r["country"],
            "rank": r["rank"],
        }
        for r in rows
    ]


@app.get("/api/seasons")
async def get_seasons():
    """
    Devuelve la season activa como 'Current' + el histórico.
    Activas primero; luego inactivas desde las más recientes.
    """
    if not db_pool:
        raise HTTPException(500, "DB pool not initialized")

    query = """
       SELECT *
       FROM (
           SELECT id, 'Current' AS name, active, startdate, enddate
           FROM seasons
           WHERE active = true
           UNION ALL
           SELECT id, name, active, startdate, enddate
           FROM seasons
           WHERE active = false
       ) s
       ORDER BY s.active DESC, s.enddate DESC NULLS LAST, s.startdate DESC NULLS LAST;
    """
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(query)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"DB error: {e}")

    return [
        {
            "id": r["id"],
            "name": r["name"],
            "startdate": r["startdate"],
            "enddate": r["enddate"],
        }
        for r in rows
    ]

# ======================= AUTH02 (Discord OAuth) =======================
# Guardamos PFPs tanto por userId como por nombre normalizado
PFP_BY_NAME: Dict[str, str] = {}  # nameLower -> url
PFP_BY_ID:   Dict[str, str] = {}  # userId/discordId -> url

# Anti-CSRF state
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
    """
    Redirige a la pantalla de autorización de Discord con state anti-CSRF.
    """
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
    """
    Intercambia code por token y crea sesión + cookie.
    """
    if not code:
        raise HTTPException(400, "Missing code")

    # Validar state
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

    # Crear sesión + setear cookie (sin domain)
    sid = secrets.token_hex(24)
    SESSIONS[sid] = {"id": str(user.get("id")), "username": user.get("username")}
    resp = RedirectResponse(url=f"{FRONT_URL}?verified=1", status_code=303)  # 303 => GET al front
    set_session_cookie(resp, sid)
    return resp

@app.get("/auth02/me")
async def auth02_me(request: Request):
    """
    Devuelve el usuario autenticado usando la cookie de sesión.
    """
    sid = request.cookies.get(COOKIE_NAME)
    if not sid or sid not in SESSIONS:
        raise HTTPException(status_code=401, detail="no-session")
    return SESSIONS[sid]

@app.post("/auth02/logout")
async def auth02_logout(request: Request):
    """
    Cierra sesión y borra cookie (sin domain).
    """
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
    """
    Guarda o borra la PFP asociada.
    Prioridad de llave: userId (Discord ID) > nameLower.
    - Si url == '' → borra.
    """
    body = await request.json()
    url = (body.get("url") or "").strip()
    name_lower = (body.get("nameLower") or "").strip().lower()
    user_id = (body.get("userId") or "").strip()

    # Si no viene userId en body, intenta leerlo de la sesión
    if not user_id:
        sid = request.cookies.get(COOKIE_NAME)
        if sid and sid in SESSIONS:
            user_id = str(SESSIONS[sid].get("id") or "")

    # Si no viene nameLower, intenta usar username de sesión
    if not name_lower:
        sid = request.cookies.get(COOKIE_NAME)
        if sid and sid in SESSIONS:
            name_lower = (SESSIONS[sid].get("username") or "").lower()

    # Borrado
    if url == "":
        if user_id:
            PFP_BY_ID.pop(user_id, None)
        if name_lower:
            PFP_BY_NAME.pop(name_lower, None)
        return {"ok": True, "deleted": True}

    if not (user_id or name_lower):
        raise HTTPException(400, "missing userId or nameLower")

    # Validar URL y cap de memoria
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
    """
    Devuelve ambos mapas de PFPs:
    - byId:   { userId: url }
    - byName: { nameLower: url }
    """
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

    # 1) Enlaces al namespace Card:
    for a in root.select('a[href^="/wiki/Card:"]'):
        href = a.get("href") or ""
        href_abs = _abs_url(href)
        name = a.get_text(strip=True) or ""

        # Intenta conseguir thumbnail de forma laxa (opcional)
        thumb = None
        img = a.find("img")
        if not img and a.parent:         # a veces la imagen está en el padre
            img = a.parent.find("img")
        if not img:                       # o en un hermano cercano
            sib = a.find_next_sibling("img")
            if sib:
                img = sib

        if img:
            thumb = img.get("data-src") or img.get("src")
            thumb = _abs_url(thumb)

        items.append({
            "id": _guess_id(name, href_abs),
            "name": name,
            "thumb": thumb,               # puede ser None
            "href": href_abs
        })

    # 2) De-dup por href o nombre
    seen, dedup = set(), []
    for it in items:
        key = it["href"] or it["name"]
        if key and key not in seen:
            seen.add(key)
            dedup.append(it)
    return dedup

async def _fetch_list_html_via_http() -> str:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(SEKAIPEDIA_LIST, headers=_HTTP_HEADERS)
        r.raise_for_status()
        html = r.text
        print(f"[cards] GET List_of_cards status={r.status_code} len(html)={len(html)} url={r.url}")
        return html

async def _fetch_list_html_via_api() -> str:
    params = {
        "action": "parse",
        "page": "List_of_cards",
        "prop": "text",
        "format": "json",
        "formatversion": "2",
    }
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(SEKAIPEDIA_API, params=params, headers=_HTTP_HEADERS)
        r.raise_for_status()
        data = r.json()
        html = (data.get("parse") or {}).get("text") or ""
        print(f"[cards] API parse len(html)={len(html)}")
        if not html:
            raise RuntimeError("MediaWiki API returned empty html")
        return html

async def _scrape_cards_from_sekaipedia() -> List[Dict]:
    # 1) HTML directo
    html = await _fetch_list_html_via_http()
    items = _parse_cards_from_html(html)
    print(f"[cards] parsed_count (http) = {len(items)}")
    if items:
        return items

    # 2) Fallback a API si no encontró nada (Cloudflare/estructura cambió)
    html_api = await _fetch_list_html_via_api()
    items_api = _parse_cards_from_html(html_api)
    print(f"[cards] parsed_count (api) = {len(items_api)}")
    return items_api

@app.get("/auth02/cards")
async def auth02_cards():
    now = datetime.utcnow()
    if _cards_cache["items"] and now - _cards_cache["ts"] < CARDS_TTL:
        return _cards_cache["items"]

    async with _cards_lock:
        # double-check dentro del lock
        now = datetime.utcnow()
        if _cards_cache["items"] and now - _cards_cache["ts"] < CARDS_TTL:
            return _cards_cache["items"]
        try:
            items = await _scrape_cards_from_sekaipedia()
            if not items:
                # Si después de todo no hay items, expón el problema.
                raise HTTPException(status_code=503, detail="Upstream unavailable or parse error")
            _cards_cache["items"] = items
            _cards_cache["ts"] = datetime.utcnow()
            return items
        except HTTPException:
            raise
        except Exception as e:
            traceback.print_exc()
            raise HTTPException(status_code=503, detail=f"Failed to scrape: {e}")


# =================== Static (Front opcional) ===================
static_dir = os.path.join(os.path.dirname(__file__), "public")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
