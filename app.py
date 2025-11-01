import os
import re
import traceback
from datetime import datetime, timedelta
from typing import List, Dict
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, JSONResponse
import asyncpg
import httpx
from bs4 import BeautifulSoup # type: ignore
from dotenv import load_dotenv

load_dotenv()

# ====== ENV ======
DISCORD_CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI  = os.getenv("DISCORD_REDIRECT_URI", "https://pjsk-page.vercel.app/auth02/callback")
FRONT_URL             = os.getenv("FRONT_URL", "https://pjsk-page.vercel.app")
CORS_ORIGIN           = os.getenv("CORS_ORIGIN", "https://pjsk-page.vercel.app")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[CORS_ORIGIN, "http://localhost:5173", "http://localhost:3000", "*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ====== DB ======
DATABASE_URL = os.getenv("DATABASE_URL")
db_pool = None

@app.on_event("startup")
async def startup():
    global db_pool
    print("DATABASE_URL:", DATABASE_URL)
    if DATABASE_URL:
        db_pool = await asyncpg.create_pool(DATABASE_URL)

@app.on_event("shutdown")
async def shutdown():
    if db_pool:
        await db_pool.close()

def get_current_season_label() -> str:
    start = datetime(2025, 5, 16)
    now = datetime.now()
    months = (now.year - start.year) * 12 + (now.month - start.month)
    period = months // 3
    return "beta" if period == 0 else str(period)

# ===================== API actuales =====================
@app.get("/api/players")
async def get_players(season_id: int = Query(..., description="Season ID")):
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
        {"id": r["id"], "name": r["name"], "mmr": r["mmr"], "country": r["country"], "rank": r["rank"]}
        for r in rows
    ]

@app.get("/api/seasons")
async def get_seasons():
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
        {"id": r["id"], "name": r["name"], "startdate": r["startdate"], "enddate": r["enddate"]}
        for r in rows
    ]

# ===================== OAuth2 Discord =====================
pfp_map: dict[str, str] = {}

@app.get("/health")
async def health():
    return {"ok": True, "tip": "auth02 up"}

@app.get("/auth02")
async def auth02_start():
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify email",
    }
    q = "&".join(f"{k}={httpx.QueryParams({k: v})[k]}" for k, v in params.items())
    return RedirectResponse(url=f"https://discord.com/oauth2/authorize?{q}")

@app.get("/auth02/callback")
async def auth02_callback(code: str | None = None):
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

    return RedirectResponse(url=f"{FRONT_URL}?verified=1")

@app.post("/auth02/set-pfp")
async def set_pfp(req: Request):
    body = await req.json()
    url = (body.get("url") or "").strip()
    name_lower = (body.get("nameLower") or "").strip().lower()
    if not url:
        raise HTTPException(400, "missing url")
    if not name_lower:
        raise HTTPException(400, "missing nameLower")
    pfp_map[name_lower] = url
    return {"ok": True}

@app.get("/auth02/pfps")
async def list_pfps():
    return JSONResponse(pfp_map)

# ===================== NUEVO: /auth02/cards =====================
SEKAIPEDIA_LIST = "https://www.sekaipedia.org/wiki/List_of_cards"
_cards_cache: Dict[str, object] = {"items": [], "ts": datetime.utcfromtimestamp(0)}
CARDS_TTL = timedelta(hours=6)

def _abs_url(u: str) -> str:
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return "https://www.sekaipedia.org" + u
    return u

def _guess_id(name: str, href: str) -> str:
    m = re.search(r"#(\d+)", name or "")
    if m:
        return m.group(1)
    if href:
        return href.rstrip("/").split("/")[-1]
    return name or ""

async def _scrape_cards_from_sekaipedia() -> List[Dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(SEKAIPEDIA_LIST, headers={"User-Agent":"pjsk-lounge/1.0"})
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

    seen = set()
    deduped = []
    for it in items:
        key = it["href"] or it["name"]
        if key and key not in seen:
            seen.add(key)
            deduped.append(it)
    return deduped

@app.get("/auth02/cards")
async def auth02_cards():
    now = datetime.utcnow()
    if now - _cards_cache["ts"] < CARDS_TTL and _cards_cache["items"]:
        return _cards_cache["items"]
    try:
        items = await _scrape_cards_from_sekaipedia()
        _cards_cache["items"] = items
        _cards_cache["ts"] = now
        return items
    except Exception as e:
        traceback.print_exc()
        return _cards_cache["items"]

# ===================== Static =====================
static_dir = os.path.join(os.path.dirname(__file__), "public")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
