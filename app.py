"""Public read-only API and existing static website. No Discord Gateway here."""

from contextlib import asynccontextmanager
import logging
import time
from pathlib import Path
import os
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from lounge.config import Settings
from lounge.database import Database
from lounge.rules import COLORS

log = logging.getLogger(__name__)


def create_app(database=None):
    @asynccontextmanager
    async def lifespan(app):
        load_dotenv()
        app.state.db = database or Database(Settings.load(api=True))
        if database is None:
            await app.state.db.open()
        try:
            yield
        finally:
            if database is None:
                await app.state.db.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def measure(request, call_next):
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            log.error("api_request_failed type=%s", type(exc).__name__)
            from fastapi.responses import JSONResponse

            response = JSONResponse(
                status_code=503,
                content={"detail": "Base de datos temporalmente no disponible"},
            )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Server-Timing"] = (
            f"app;dur={(time.perf_counter() - start) * 1000:.2f}"
        )
        return response

    @app.get("/api/health")
    async def health(request: Request):
        try:
            ok = await request.app.state.db.health()
            if not ok:
                raise RuntimeError("database_not_ready")
            return {"database": True}
        except Exception:
            raise HTTPException(503, "Base de datos no disponible") from None

    @app.get("/api/season")
    async def season(request: Request, response: Response):
        async with request.app.state.db.pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT id,number,name,started_at FROM seasons WHERE status='active'"
            )
        if not row:
            raise HTTPException(503, "Sin temporada activa")
        response.headers["Cache-Control"] = "public, max-age=0, s-maxage=5"
        return dict(row)

    @app.get("/api/seasons")
    async def seasons(request: Request):
        async with request.app.state.db.pool.acquire() as c:
            return [
                dict(r)
                for r in await c.fetch(
                    "SELECT id,number,name,status,started_at,ended_at FROM seasons ORDER BY started_at DESC NULLS LAST,id"
                )
            ]

    @app.get("/api/players")
    async def players(
        request: Request,
        response: Response,
        limit: int = Query(100, ge=1, le=200),
        offset: int = Query(0, ge=0, le=100000),
        q: str = Query("", max_length=100),
        season: str | None = Query(None, max_length=50),
    ):
        async with (
            request.app.state.db.pool.acquire() as c,
            c.transaction(readonly=True),
        ):
            label = season or await c.fetchval(
                "SELECT id FROM seasons WHERE status='active'"
            )
            rows = await c.fetch(
                """WITH ranked AS(
                SELECT p.user_id::text AS id,p.name,p.country,sp.mmr,sp.role AS rank,
                row_number() OVER(ORDER BY sp.mmr DESC,p.user_id) AS position,sp.season_id,
                sp.peak_mmr,sp.matches_played,sp.wins,sp.losses
                FROM players p JOIN season_players sp USING(user_id) WHERE sp.season_id=$1)
                SELECT * FROM ranked WHERE strpos(lower(name),lower($2))>0 ORDER BY position LIMIT $3 OFFSET $4""",
                label,
                q,
                limit,
                offset,
            )
        response.headers["Cache-Control"] = "public, max-age=0, s-maxage=5"
        response.headers["X-Season-Id"] = label or ""
        return [dict(row) for row in rows]

    @app.get("/api/players/{user_id}/matches")
    async def matches(
        user_id: str,
        request: Request,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        if not user_id.isdigit() or not 0 < int(user_id) < 2**63:
            raise HTTPException(400, "ID inválido")
        async with request.app.state.db.pool.acquire() as c:
            rows = await c.fetch(
                "SELECT mp.*,m.created_at FROM match_participants mp JOIN matches m ON m.id=mp.match_id WHERE mp.user_id=$1 ORDER BY m.created_at DESC,m.id LIMIT $2 OFFSET $3",
                int(user_id),
                limit,
                offset,
            )
        return [{**dict(r), "user_id": str(r["user_id"])} for r in rows]

    @app.get("/api/rank-colors")
    async def rank_colors():
        return COLORS

    # Competitive/profile writes are deliberately absent. Never trust a browser Discord ID.
    if not os.getenv("VERCEL"):
        app.mount(
            "/",
            StaticFiles(directory=Path(__file__).parent / "public", html=True),
            name="static",
        )
    return app


app = create_app()
