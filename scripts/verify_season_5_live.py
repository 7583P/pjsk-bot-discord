import asyncio
import asyncpg
import httpx
from dotenv import dotenv_values

creds = dotenv_values(r"C:\Users\Usuario\.pjsk_secure_backups\lounge_credentials.env")
pooler_url = creds["SUPABASE_POOLER_URL"]

async def main():
    print("=== VERIFICACIÓN DIRECTA EN SUPABASE DE SEASON 5 ===")
    conn = await asyncpg.connect(pooler_url, statement_cache_size=0)
    try:
        # 1. Seasons status
        seasons = await conn.fetch("SELECT id, name, active, startdate, enddate FROM seasons ORDER BY id")
        print("\n--- TEMPORADAS ---")
        s5_active = False
        s4_archived = False
        for s in seasons:
            print(f"ID {s['id']}: {s['name']} | active={s['active']} | start={s['startdate']} | end={s['enddate']}")
            if s['id'] == 8 and s['active'] and "Season 5" in s['name']:
                s5_active = True
            if s['id'] == 7 and not s['active']:
                s4_archived = True

        # 2. Season 5 players
        s5_id = 8
        total_s5_players = await conn.fetchval("SELECT count(*) FROM seasonplayers WHERE season_id=$1", s5_id)
        non_zero_mmr = await conn.fetchval("SELECT count(*) FROM seasonplayers WHERE season_id=$1 AND mmr <> 0", s5_id)
        non_placement = await conn.fetchval("SELECT count(*) FROM seasonplayers WHERE season_id=$1 AND role <> 'Placement'", s5_id)
        non_zero_points = await conn.fetchval("SELECT count(*) FROM seasonplayers WHERE season_id=$1 AND points <> 0", s5_id)
        starting_mmr_not_null = await conn.fetchval("SELECT count(*) FROM seasonplayers WHERE season_id=$1 AND starting_mmr IS NOT NULL", s5_id)

        # 3. Global players table
        players_total = await conn.fetchval("SELECT count(*) FROM players")
        players_non_zero_mmr = await conn.fetchval("SELECT count(*) FROM players WHERE mmr <> 0")
        players_non_placement = await conn.fetchval("SELECT count(*) FROM players WHERE role <> 'Placement'")

        # 4. History preservation
        total_matches = await conn.fetchval("SELECT count(*) FROM match_history")
        historical_seasonplayers = await conn.fetchval("SELECT count(*) FROM seasonplayers WHERE season_id < 8")
        s4_historical_players = await conn.fetchval("SELECT count(*) FROM seasonplayers WHERE season_id = 7")

        # 5. Role jobs enqueued for Discord
        pending_role_jobs = await conn.fetchval("SELECT count(*) FROM lounge_role_jobs WHERE role = 'Placement'")

        print("\n--- RESULTADOS SUPABASE ---")
        print(f"Season 5 Active: {s5_active}")
        print(f"Season 4 Archived: {s4_archived}")
        print(f"Total Jugadores Season 5: {total_s5_players} (Esperado: 1403)")
        print(f"Jugadores con MMR <> 0 en S5: {non_zero_mmr} (Esperado: 0)")
        print(f"Jugadores sin Placement en S5: {non_placement} (Esperado: 0)")
        print(f"Jugadores con points <> 0 en S5: {non_zero_points} (Esperado: 0)")
        print(f"Jugadores con starting_mmr heredado en S5: {starting_mmr_not_null} (Esperado: 0)")
        print(f"Tabla players - total: {players_total}, MMR<>0: {players_non_zero_mmr}, no-Placement: {players_non_placement}")
        print(f"Historial partidas intacto: {total_matches} (Esperado: 1360)")
        print(f"Historial seasonplayers previo intacto: {historical_seasonplayers} (Esperado: 4715)")
        print(f"Historial Season 4 jugadores: {s4_historical_players}")
        print(f"Role jobs Discord encolados a Placement: {pending_role_jobs}")

    finally:
        await conn.close()

    # 6. Web verification
    print("\n--- VERIFICACIÓN WEB VERCEL ---")
    async with httpx.AsyncClient(timeout=15.0) as client:
        r_seasons = await client.get("https://pjsk-page.vercel.app/api/seasons")
        print(f"GET /api/seasons: HTTP {r_seasons.status_code}")
        if r_seasons.status_code == 200:
            seasons_data = r_seasons.json()
            print("Web seasons count:", len(seasons_data))
            current = [s for s in seasons_data if s.get("active") or s.get("is_current")]
            print("Web current season:", current)

        r_players = await client.get("https://pjsk-page.vercel.app/api/players?season_id=8")
        print(f"GET /api/players?season_id=8: HTTP {r_players.status_code}")
        if r_players.status_code == 200:
            players_data = r_players.json()
            print(f"Web S5 players returned: {len(players_data)}")
            all_zero = all(p.get("mmr") == 0 for p in players_data)
            all_placement = all(p.get("role") == "Placement" for p in players_data)
            print(f"Web S5 all MMR == 0: {all_zero}")
            print(f"Web S5 all role == 'Placement': {all_placement}")

if __name__ == "__main__":
    asyncio.run(main())

