import os
import sys
import asyncio
import secrets
from typing import Dict

import discord
from discord.ext import commands
from discord import app_commands

from dotenv import load_dotenv

# -------------------------
# Cargar variables de entorno
# -------------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
APP_ID = int(os.getenv("APPLICATION_ID", "0"))
GUILD_ID = os.getenv("GUILD_ID", "")

# Para el front y cookies cross-site
APP_ORIGIN = os.getenv("APP_ORIGIN", "https://pjsk-page.vercel.app")
COOKIE_NAME = os.getenv("COOKIE_NAME", "sid")
HTTP_PORT = int(os.getenv("PORT", "8000"))  # Railway suele inyectar PORT

# -------------------------
# FastAPI (servidor HTTP)
# -------------------------
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI()

# CORS con credenciales para tu frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[APP_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Sesiones en memoria: en prod usa Redis/DB
_SESSIONS: Dict[str, Dict] = {}  # sid -> {"id": "...", "username": "..."}
_PFPS: Dict[str, str] = {}  # key(nameLower|userId|discordId) -> url


def _set_session_cookie(resp: Response, sid: str):
    resp.set_cookie(
        key=COOKIE_NAME,
        value=sid,
        httponly=True,
        secure=True,  # Requerido para SameSite=None
        samesite="none",  # Para que viaje entre dominios (Railway <-> Vercel)
        path="/",
        max_age=60 * 60 * 24 * 7,  # 7 días
    )


@app.get("/auth02/callback")
async def auth02_callback():
    """
    Aquí normalmente intercambias el "code" con Discord y obtienes el user real.
    De momento dejamos un mock para probar el flujo completo end-to-end.
    """
    user = {"id": "1234567890", "username": "Diego#0001"}  # <-- reemplaza por el real
    sid = secrets.token_hex(24)
    _SESSIONS[sid] = user

    resp = RedirectResponse(url=f"{APP_ORIGIN}/?verified=1", status_code=302)
    _set_session_cookie(resp, sid)
    return resp


@app.get("/auth02/me")
async def auth02_me(request: Request):
    sid = request.cookies.get(COOKIE_NAME)
    if not sid or sid not in _SESSIONS:
        raise HTTPException(status_code=401, detail="no-session")
    u = _SESSIONS[sid]
    return {"id": u["id"], "username": u["username"]}


@app.get("/auth02/pfps")
async def auth02_pfps():
    # Devuelve el mapa de PFPs
    return _PFPS


@app.post("/auth02/set-pfp")
async def auth02_set_pfp(request: Request):
    sid = request.cookies.get(COOKIE_NAME)
    if not sid or sid not in _SESSIONS:
        raise HTTPException(status_code=401, detail="unauthorized")

    payload = await request.json()
    url = payload.get("url")
    nameLower = payload.get("nameLower")
    userId = payload.get("userId")

    if not isinstance(url, str):
        raise HTTPException(status_code=400, detail="invalid url")

    # prioridad: nameLower > userId > discordId de sesión
    sess = _SESSIONS[sid]
    key = (
        (str(nameLower).lower() if nameLower else None)
        or (str(userId) if userId else None)
        or sess["id"]
    )

    if url == "" or url is None:
        _PFPS.pop(key, None)
    else:
        _PFPS[key] = url
    return {"ok": True}


@app.get("/auth02/cards")
async def auth02_cards():
    # Mock de cartas (reemplaza por tu fuente)
    return [
        {
            "id": 1,
            "name": "Miku 01",
            "thumb": "https://via.placeholder.com/512?text=Miku01",
        },
        {
            "id": 2,
            "name": "Mafuyu 02",
            "thumb": "https://via.placeholder.com/512?text=Mafuyu02",
        },
        {
            "id": 3,
            "name": "Ichika 03",
            "thumb": "https://via.placeholder.com/512?text=Ichika03",
        },
    ]


# -------------------------
# Discord Bot
# -------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True


class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
            application_id=APP_ID,
            help_command=None,
        )

    async def setup_hook(self):
        # Cargar cogs
        try:
            await self.load_extension("cogs.matchmaking")
            print("✅ Cog cargado: cogs.matchmaking")
        except Exception as e:
            print(f"❌ Error cargando cogs.matchmaking: {e}")

        try:
            await self.load_extension("cogs.rooms")
            print("✅ Cog cargado: cogs.rooms")
        except Exception as e:
            print(f"❌ Error cargando cogs.rooms: {e}")

        try:
            await self.load_extension("cogs.autoroles")
            print("✅ Cog cargado: cogs.autoroles")
        except Exception as e:
            print(f"❌ Error cargando cogs.autoroles: {e}")

        try:
            await self.load_extension("cogs.players")
            print("✅ Cog cargado: cogs.players")
        except Exception as e:
            print(f"❌ Error cargando cogs.players: {e}")

        # Sincronizar comandos slash
        try:
            if GUILD_ID:
                guild = discord.Object(id=int(GUILD_ID))
                synced = await self.tree.sync(guild=guild)
                print(
                    f"📋 Slash commands sincronizados en guild {GUILD_ID}: {[c.name for c in synced]}"
                )
            else:
                synced = await self.tree.sync()
                print(f"📋 Slash commands globales: {[c.name for c in synced]}")
        except Exception as e:
            print(f"❗ Error al sincronizar comandos: {e}")

    async def on_ready(self):
        print(f"🤖 Conectado como {self.user} (ID: {self.user.id})")


bot = MyBot()


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ Error: {error}", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"❌ Error: {error}", ephemeral=True
            )
    except Exception as e:
        print(f"⚠️ Falló el envío del mensaje de error: {e}")


# -------------------------
# Arranque conjunto (HTTP + Bot)
# -------------------------
async def serve_http():
    """Levanta FastAPI con uvicorn en el mismo proceso."""
    config = uvicorn.Config(app, host="0.0.0.0", port=HTTP_PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def run_bot_async():
    if not TOKEN:
        print("❌ DISCORD_TOKEN no definido.")
        return
    await bot.start(TOKEN)


async def main():
    # Levantar HTTP y Bot en paralelo
    http_task = asyncio.create_task(serve_http())
    bot_task = asyncio.create_task(run_bot_async())
    await asyncio.gather(http_task, bot_task)


if __name__ == "__main__":
    if not TOKEN:
        print("❌ DISCORD_TOKEN no definido.")
        sys.exit(1)
    asyncio.run(main())
