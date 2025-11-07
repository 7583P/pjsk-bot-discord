import asyncio
import os
import uvicorn
import logging

from app import app           # tu FastAPI
from main import run_bot_async  # tu bot (o donde esté)

log = logging.getLogger("run")
logging.basicConfig(level=logging.INFO)

async def run_api():
    port = int(os.environ.get("PORT", 8000))
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        # importantísimo en prod:
        reload=False
    )
    server = uvicorn.Server(config)
    await server.serve()

async def run_bot_safe():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        log.warning("DISCORD_TOKEN no definido: NO se iniciará el bot. La API seguirá funcionando.")
        # Mantén viva la task para no terminar el gather
        while True:
            await asyncio.sleep(3600)
    # Si tienes run_bot_async() que ya hace login/start internamente, usa try/except
    while True:
        try:
            log.info("Iniciando bot…")
            await run_bot_async()     # debe hacer await bot.start(token) o equivalente
        except Exception as e:
            log.exception("El bot se cayó: %s. Reintentando en 10s…", e)
            await asyncio.sleep(10)

async def main():
    # NO uses gather sin protección: si una task crashea, cancela la otra.
    api_task = asyncio.create_task(run_api())
    bot_task = asyncio.create_task(run_bot_safe())
    # Espera a que cualquiera termine y maneja señales
    done, pending = await asyncio.wait(
        {api_task, bot_task},
        return_when=asyncio.FIRST_EXCEPTION
    )
    # Si alguna terminó con error, cancela la otra y propaga
    for t in pending:
        t.cancel()
    for t in done:
        exc = t.exception()
        if exc:
            raise exc

if __name__ == "__main__":
    asyncio.run(main())
