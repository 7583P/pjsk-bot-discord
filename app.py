import os
import re
import traceback
from datetime import datetime, timedelta
from typing import List, Dict

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

DATABASE_URL = os.getenv("DATABASE_URL")

# ===================== FastAPI + CORS =====================
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[CORS_ORIGIN, FRONT_URL],
    allow_credentials=True,          # necesario para cookies cross-site
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ================ Sesiones simples en memoria ================
SESSIONS: Dict[str, Dict] = {}     # sid -> {"id": "...", "username": "..."}

def set_session_cookie(resp: Response, sid: str):
    resp.set_cookie(
        key=COOKIE_NAME,
        value=sid,
        httponly=True,
        secure=True,           # requerido para SameSite=None
        samesite="none",
        path="/",
        max_age=60 * 60 * 24 * 7   # 7 días
    )

# =========================== DB ===========================
db_pool: asyncpg.pool.Pool | None = None

@app.on_event("startup")
async def startup():
    global db_pool
    if DATABASE_URL:
        db_pool = await asyncpg.create_pool(DATABASE_URL)

@app.on_event("shutdown")
async def shutdown():
    if db_pool:
        await db_pool.close()

# ======================= API: Seasons/Players =======================
@app.get("/api/players")
async def get_players(season_id: int = Query(..., description="Season ID")):
    """
    Devuelve jugadores de una season ordenados por MMR.
    """
    if not db_pool:
        raise HTTPException(500, "DB pool not initialized")

    query = """
      SELECT p.user_id as id, p.name, sp.mmr, p.country, sp.role as rank
      FROM players p INNER JOIN seasonplayers sp ON p.user_id = sp.user_id
      WHERE sp.season_id = $1
      ORDER BY sp.mmr DESC;
    """
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(query, season_id)
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
       ORDER BY s.active DESC, s.startdate ASC
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
pfp_map: dict[str, str] = {}

@app.get("/health")
async def health():
    return {"ok": True, "tip": "auth02 up"}

@app.get("/auth02")
async def auth02_start():
    """
    Redirige a la pantalla de autorización de Discord.
    """
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify email",
    }
    # encode seguro
    q = "&".join(f"{k}={httpx.QueryParams({k: v})[k]}" for k, v in params.items())
    return RedirectResponse(url=f"https://discord.com/oauth2/authorize?{q}")

@app.get("/auth02/callback")
async def auth02_callback(code: str | None = None):
    """
    Intercambia code por token y crea sesión + cookie.
    """
    if not code:
        raise HTTPException(400, "Missing code")

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

    # Crear sesión + setear cookie
    import secrets
    sid = secrets.token_hex(24)
    SESSIONS[sid] = {"id": str(user.get("id")), "username": user.get("username")}
    resp = RedirectResponse(url=f"{FRONT_URL}?verified=1", status_code=307)
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

@app.post("/auth02/set-pfp")
async def set_pfp(request: Request):
    """
    Guarda o borra la PFP asociada.
    - Si url == '' → se BORRA la PFP.
    - nameLower: si no se envía, intenta usar el username del usuario verificado.
    """
    body = await request.json()
    url = (body.get("url") or "").strip()
    name_lower = (body.get("nameLower") or "").strip().lower()

    # Fallback al usuario verificado si no llega nameLower
    if not name_lower:
        sid = request.cookies.get(COOKIE_NAME)
        if sid and sid in SESSIONS:
            name_lower = (SESSIONS[sid].get("username") or "").lower()

    if not name_lower:
        raise HTTPException(400, "missing nameLower")

    # Borrar
    if url == "":
        pfp_map.pop(name_lower, None)
        return {"ok": True, "deleted": True}

    # Guardar/actualizar
    pfp_map[name_lower] = url
    return {"ok": True}

@app.get("/auth02/pfps")
async def list_pfps():
    return JSONResponse(pfp_map)

# =================== /auth02/cards (Sekaipedia) ===================
SEKAIPEDIA_LIST = "https://www.sekaipedia.org/wiki/List_of_cards"
_cards_cache: Dict[str, object] = {"items": [], "ts": datetime.utcfromtimestamp(0)}
CARDS_TTL = timedelta(hours=6)

def _abs_url(u: str) -> str:
    if not u: return ""
    if u.startswith("//"): return "https:" + u
    if u.startswith("/"):  return "https://www.sekaipedia.org" + u
    return u

def _guess_id(name: str, href: str) -> str:
    m = re.search(r"#(\d+)", name or "")
    if m: return m.group(1)
    return (href or "").rstrip("/").split("/")[-1] or (name or "")

async def _scrape_cards_from_sekaipedia() -> List[Dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(SEKAIPEDIA_LIST, headers={"User-Agent": "pjsk-lounge/1.0"})
        r.raise_for_status()
        html = r.text

    soup = BeautifulSoup(html, "lxml")
    root = soup.select_one("#mw-content-text") or soup

    items: List[Dict] = []
    for a in root.select('a[href*="/wiki/Card:"]'):
        href = a.get("href") or ""
        href_abs = _abs_url(href)
        name = a.get_text(strip=True) or ""
        img = a.find("img")
        thumb = None
        if img:
            thumb = img.get("data-src") or img.get("src")
            thumb = _abs_url(thumb)
        if not thumb:
            continue
        items.append({
            "id": _guess_id(name, href_abs),
            "name": name,
            "thumb": thumb,
            "href": href_abs
        })

    # de-dup
    seen, dedup = set(), []
    for it in items:
        key = it["href"] or it["name"]
        if key and key not in seen:
            seen.add(key)
            dedup.append(it)
    return dedup

@app.get("/auth02/cards")
async def auth02_cards():
    now = datetime.utcnow()
    if _cards_cache["items"] and now - _cards_cache["ts"] < CARDS_TTL:
        return _cards_cache["items"]
    try:
        items = await _scrape_cards_from_sekaipedia()
        _cards_cache["items"] = items
        _cards_cache["ts"] = now
        return items
    except Exception:
        traceback.print_exc()
        # devuelve cache (aunque esté vacío) si falla scrape
        return _cards_cache["items"]

# =================== Static (Front opcional) ===================
static_dir = os.path.join(os.path.dirname(__file__), "public")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
