import os
import traceback
from datetime import datetime
from fastapi import FastAPI, HTTPException, Query
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
async def get_players(season_id: int = Query(..., description="Season ID")):
    season_label = get_current_season_label()
    # Cambia el query: usa user_id o el campo PK real
    query = """
      SELECT p.user_id as id, p.name, sp.mmr, p.country, sp.role as rank
	FROM players p INNER JOIN seasonplayers sp ON p.user_id = sp.user_id
WHERE sp.season_id = $1 order by sp.mmr desc;
    """
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(query, season_id)
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

@app.get("/api/seasons")
async def get_seasons():    
    # Cambia el query: usa user_id o el campo PK real
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
        {"id":      r["id"],
         "name":    r["name"],
         "startdate":     r["startdate"],
         "enddate": r["enddate"]}
        for r in rows
    ]
static_dir = os.path.join(os.path.dirname(__file__), "public")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
