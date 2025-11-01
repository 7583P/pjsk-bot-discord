import os
import traceback
from datetime import datetime
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, JSONResponse  # ✅ <-- agrega esta línea
import asyncpg
import httpx
from dotenv import load_dotenv


load_dotenv()

# ====== ENV de OAuth y Front ======
DISCORD_CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI  = os.getenv("DISCORD_REDIRECT_URI", "https://pjsk-page.vercel.app/auth02/callback")
FRONT_URL             = os.getenv("FRONT_URL", "https://pjsk-page.vercel.app")
CORS_ORIGIN           = os.getenv("CORS_ORIGIN", "https://pjsk-page.vercel.app")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[CORS_ORIGIN, "http://localhost:5173", "http://localhost:3000", "*"],  # puedes dejar solo FRONT_URL en prod
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],  # ⬅️ permitir POST para set-pfp
    allow_headers=["*"],
)

# ====== DB (usa la URL completa de Railway) ======
# En Railway crea una Variable Reference a DATABASE_URL del servicio Postgres.
DATABASE_URL = os.getenv("DATABASE_URL")  # ⬅️ antes tenías DB_HOST
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
    season_label = get_current_season_label()
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
# Mapa en memoria para PFP (puedes moverlo a Postgres luego)
pfp_map: dict[str, str] = {}

@app.get("/health")
async def health():
    return {"ok": True, "tip": "auth02 up"}  # endpoint de chequeo

# 1) Iniciar OAuth
@app.get("/auth02")
async def auth02_start():
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify email",  # quita "email" si no lo necesitas
    }
    q = "&".join(f"{k}={httpx.QueryParams({k: v})[k]}" for k, v in params.items())
    return RedirectResponse(url=f"https://discord.com/oauth2/authorize?{q}")

# 2) Callback de Discord
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

    # vuelve a tu front con bandera de verificado
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"{FRONT_URL}?verified=1")

# 3) Guardar PFP
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

# 4) Mapa de PFPs para el leaderboard
@app.get("/auth02/pfps")
async def list_pfps():
    from fastapi.responses import JSONResponse
    return JSONResponse(pfp_map)

# ===================== Static (deja esto al final) =====================
static_dir = os.path.join(os.path.dirname(__file__), "public")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
