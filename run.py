import asyncio
import os
import uvicorn

from app import app
from main import run_bot_async

async def run_api():
    port = int(os.environ.get("PORT", 8000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

async def main():
    await asyncio.gather(
        run_bot_async(),  # Tu bot Discord
        run_api()         # Tu FastAPI
    )

if __name__ == "__main__":
    asyncio.run(main())