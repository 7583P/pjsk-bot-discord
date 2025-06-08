import os
import traceback
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import asyncpg
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Conexión global (pool)
DATABASE_URL = os.getenv("DB_HOST")  # postgresql://user:pass@host:port/dbname
db_pool = None

@app.on_event("startup")
async def startup():
    global db_pool
    print("TESTING")
    print(DATABASE_URL)
    db_pool = await asyncpg.create_pool(DATABASE_URL)

@app.on_event("shutdown")
async def shutdown():
    await db_pool.close()

def get_current_season_label() -> str:
    start = datetime(2025, 5, 16)
    now = datetime.now()
    months = (now.year - start.year) * 12 + (now.month - start.month)
    period = months // 3
    return "beta" if period == 0 else str(period)

@app.get("/api/players")
async def get_players():
    season_label = get_current_season_label()
    # Cambia el query: usa user_id o el campo PK real
    query = """
      SELECT user_id AS id,
             name,
             mmr,
             country,
             role AS rank
        FROM players
       WHERE season = $1
       ORDER BY mmr DESC
    """
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(query, season_label)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"DB error: {e}")

    return [
        {"id":      r["id"],
         "name":    r["name"],
         "mmr":     r["mmr"],
         "country": r["country"],
         "rank":    r["rank"]}
        for r in rows
    ]

static_dir = os.path.join(os.path.dirname(__file__), "public")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
