# ... imports existentes ...
import os
import secrets
from typing import Dict
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx

# ====== ENV ======
DISCORD_CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
# ⚠️ Debe ser el callback de TU WORKER (no del front)
DISCORD_REDIRECT_URI  = os.getenv("DISCORD_REDIRECT_URI", "https://worker-production-1ad2.up.railway.app/auth02/callback")
FRONT_URL             = os.getenv("FRONT_URL", "https://pjsk-page.vercel.app")

COOKIE_NAME = os.getenv("COOKIE_NAME", "sid")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONT_URL],     # no usar "*" si hay credenciales
    allow_credentials=True,        # necesario para que viaje la cookie
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# =========== Sesiones en memoria ===========
SESSIONS: Dict[str, Dict] = {}     # sid -> {"id": "...", "username": "..."}

def set_session_cookie(resp: Response, sid: str):
    resp.set_cookie(
        key=COOKIE_NAME,
        value=sid,
        httponly=True,
        secure=True,           # requerido para SameSite=None
        samesite="none",       # para cross-site (Vercel -> Railway)
        path="/",
        max_age=60*60*24*7     # 7 días
    )

# --- tus endpoints /api/... se quedan como están ---

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

    # ====== NUEVO: crear sesión + cookie ======
    sid = secrets.token_hex(24)
    SESSIONS[sid] = {"id": str(user.get("id")), "username": user.get("username")}
    resp = RedirectResponse(url=f"{FRONT_URL}?verified=1", status_code=307)
    set_session_cookie(resp, sid)
    return resp

# ====== NUEVO: devolver usuario usando la cookie ======
@app.get("/auth02/me")
async def auth02_me(request: Request):
    sid = request.cookies.get(COOKIE_NAME)
    if not sid or sid not in SESSIONS:
        raise HTTPException(status_code=401, detail="no-session")
    return SESSIONS[sid]

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

# --- /auth02/cards y todo lo demás se queda igual ---

# ===================== Static =====================
static_dir = os.path.join(os.path.dirname(__file__), "public")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
