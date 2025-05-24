import os
import traceback
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import aiosqlite

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

def get_current_season_label() -> str:
    start = datetime(2025, 5, 16)
    now = datetime.now()
    months = (now.year - start.year) * 12 + (now.month - start.month)
    period = months // 3
    return "beta" if period == 0 else str(period)

@app.get("/api/players")
async def get_players():
    season_label = get_current_season_label()
    query = """
      SELECT rowid    AS id,
             name,
             mmr,
             country,
             role    AS rank
        FROM players
       WHERE season = ?
       ORDER BY mmr DESC
    """

    base = os.path.dirname(os.path.abspath(__file__))
    db_path = os.path.join(base, "matchmaking.db")

    try:
        async with aiosqlite.connect(db_path) as db:
            cur = await db.execute(query, (season_label,))
            rows = await cur.fetchall()
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"DB error: {e}")

    return [
        {"id":      r[0],
         "name":    r[1],
         "mmr":     r[2],
         "country": r[3],
         "rank":    r[4]}
        for r in rows
    ]

static_dir = os.path.join(os.path.dirname(__file__), "public")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
