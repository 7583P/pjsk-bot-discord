# app.py
import os
import secrets
from typing import Dict, Optional

from fastapi import FastAPI, Request, Response, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

# ========= Config =========
APP_ORIGIN  = os.environ.get("APP_ORIGIN", "https://pjsk-page.vercel.app")
COOKIE_NAME = os.environ.get("COOKIE_NAME", "sid")

app = FastAPI(title="PJSK Lounge API")

# CORS global para endpoints que usan cookies (credenciales)
# Nota: allow_credentials=True impide usar '*' aquí; por eso
# para endpoints PÚBLICOS pondremos CORS 'manual' (Access-Control-Allow-Origin: *).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[APP_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========= Sesión en memoria (usa Redis/DB en prod) =========
# sid -> {"discordId": "...", "username": "..."}
session_store: Dict[str, Dict[str, str]] = {}

def new_sid() -> str:
    return secrets.token_urlsafe(32)

def set_session_cookie(resp: Response, sid: str) -> None:
    resp.set_cookie(
        key=COOKIE_NAME,
        value=sid,
        httponly=True,
        secure=True,         # requerido si SameSite=None
        samesite="none",
        path="/",
        max_age=60 * 60 * 24 * 7,  # 7 días
    )

def get_session(request: Request) -> Optional[Dict[str, str]]:
    sid = request.cookies.get(COOKIE_NAME)
    if not sid:
        return None
    return session_store.get(sid)

# ========= Utils CORS para endpoints públicos =========
def set_public_cors(resp: Response) -> None:
    # Estos headers permiten lectura pública desde cualquier origen (sin credenciales)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"

# ========= PFP stores (en memoria) =========
# Claves:
#   - byId: "discordId" o "userId" (string)
#   - byName: "nameLower" (string lowercased)
pfp_by_id: Dict[str, str] = {}
pfp_by_name: Dict[str, str] = {}

# ========= Rutas auth02 =========

@app.get("/auth02")
def auth02_entry():
    """
    Mock de inicio de OAuth. En prod: redirige a Discord.
    Aquí simplemente redirigimos al callback para crear sesión.
    """
    return RedirectResponse(url="/auth02/callback", status_code=302)

@app.get("/auth02/callback")
def auth02_callback():
    """
    Mock del callback de OAuth.
    En prod: intercambiar 'code' por token y obtener el usuario de Discord.
    """
    discord_user = {"id": "1234567890", "username": "Diego#0001"}  # MOCK

    sid = new_sid()
    session_store[sid] = {"discordId": discord_user["id"], "username": discord_user["username"]}

    # Seteamos cookie y redirigimos a tu frontend con verified=1
    resp = RedirectResponse(url=f"{APP_ORIGIN}/?verified=1", status_code=302)
    set_session_cookie(resp, sid)
    return resp

@app.get("/auth02/me")
def auth02_me(request: Request):
    """
    Devuelve el usuario de la sesión actual.
    Requiere cookie (CORS ya habilitado globalmente para tu dominio).
    """
    sess = get_session(request)
    if not sess:
        raise HTTPException(status_code=401, detail="unauthorized")
    return {"id": sess["discordId"], "username": sess["username"]}

@app.get("/auth02/pfps")
def auth02_pfps():
    """
    Mapa público de PFPs para el leaderboard.
    Entrega { byId: {...}, byName: {...} } con CORS público ('*').
    """
    data = {
        "byId": dict(pfp_by_id),
        "byName": dict(pfp_by_name),
    }
    resp = JSONResponse(content=data)
    set_public_cors(resp)
    return resp

@app.post("/auth02/set-pfp")
async def auth02_set_pfp(request: Request):
    """
    Guarda o borra el PFP del usuario verificado.
    Body JSON: { url: string, nameLower?: string, userId?: string }
    Requiere cookie (usuario verificado).
    """
    sess = get_session(request)
    if not sess:
        raise HTTPException(status_code=401, detail="unauthorized")

    payload = await request.json()
    url = payload.get("url", None)
    name_lower = payload.get("nameLower")
    user_id = payload.get("userId")  # opcional, si no viene usamos discordId de la sesión

    if not isinstance(url, str):
        raise HTTPException(status_code=400, detail="invalid url")

    if name_lower:
        name_lower = str(name_lower).lower()

    key_id = str(user_id) if user_id else str(sess["discordId"])

    if url == "":  # borrar
        pfp_by_id.pop(key_id, None)
        if name_lower:
            pfp_by_name.pop(name_lower, None)
    else:          # guardar
        pfp_by_id[key_id] = url
        if name_lower:
            pfp_by_name[name_lower] = url

    return {"ok": True}

@app.get("/auth02/cards")
def auth02_cards():
    """
    Listado de cartas (MOCK).
    Si antes veías '[]', es porque tu versión Python devolvía lista vacía.
    Aquí te entrego un mock estable. Puedes reemplazar 'thumb' con Sekaipedia.
    """
    cards = [
        {"id": 1, "name": "Miku 01",   "thumb": "https://via.placeholder.com/512?text=Miku01"},
        {"id": 2, "name": "Mafuyu 02", "thumb": "https://via.placeholder.com/512?text=Mafuyu02"},
        {"id": 3, "name": "Ichika 03", "thumb": "https://via.placeholder.com/512?text=Ichika03"},
        # añade más si quieres
    ]
    resp = JSONResponse(content=cards)
    set_public_cors(resp)
    return resp

# ========= Health =========
@app.get("/health")
def health():
    return {"ok": True}
