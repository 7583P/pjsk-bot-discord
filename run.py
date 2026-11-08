import asyncio
import os
import uvicorn
import logging

from app import app  # FastAPI
from main import run_bot_async  # Discord Bot

log = logging.getLogger("run")
logging.basicConfig(level=logging.INFO)


async def run_api():
    port = int(os.environ.get("PORT", 8000))
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        reload=False,
    )
    server = uvicorn.Server(config)
    await server.serve()


async def run_bot_safe():
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token or token.startswith("DISABLED"):
        log.warning(
            "DISCORD_TOKEN no configurado o pendiente de rotación segura. El bot no se conectará hasta rotar el token. La API FastAPI seguirá activa."
        )
        while True:
            await asyncio.sleep(3600)

    while True:
        try:
            log.info("Iniciando bot...")
            await run_bot_async()
        except Exception as e:
            log.exception("El bot reportó excepción: %s. Esperando 30s...", e)
            await asyncio.sleep(30)


async def main():
    api_task = asyncio.create_task(run_api())
    bot_task = asyncio.create_task(run_bot_safe())
    done, pending = await asyncio.wait(
        {api_task, bot_task}, return_when=asyncio.FIRST_EXCEPTION
    )
    for t in pending:
        t.cancel()
    for t in done:
        exc = t.exception()
        if exc:
            raise exc


if __name__ == "__main__":
    asyncio.run(main())
