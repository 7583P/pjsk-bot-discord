import asyncio
import asyncpg
import httpx
from dotenv import dotenv_values

creds = dotenv_values(r"C:\Users\Usuario\.pjsk_secure_backups\lounge_credentials.env")

async def main():
    conn = await asyncpg.connect(creds["SUPABASE_POOLER_URL"], statement_cache_size=0)
    
    # 1. Season 5 = ACTIVE
    s5 = await conn.fetchrow("SELECT id, name, active, startdate, enddate FROM seasons WHERE active = true")
    print(f"1. ACTIVE SEASON: id={s5['id']}, name='{s5['name']}', active={s5['active']}")
    
    # 2. Total players in Season 5 = 1403
    s5_players = await conn.fetchval("SELECT count(*) FROM seasonplayers WHERE season_id = $1", s5['id'])
    players_total = await conn.fetchval("SELECT count(*) FROM players")
    print(f"2. PLAYERS: Season 5 count = {s5_players}, players table count = {players_total}")
    
    # 3. Jugadores con MMR distinto de 0 = 0
    non_zero_mmr = await conn.fetchval("SELECT count(*) FROM seasonplayers WHERE season_id = $1 AND mmr <> 0", s5['id'])
    players_non_zero = await conn.fetchval("SELECT count(*) FROM players WHERE mmr <> 0")
    print(f"3. MMR: Non-zero in seasonplayers = {non_zero_mmr}, non-zero in players table = {players_non_zero}")
    
    # 4. Jugadores sin Placement = 0
    non_placement = await conn.fetchval("SELECT count(*) FROM seasonplayers WHERE season_id = $1 AND role <> 'Placement'", s5['id'])
    players_non_placement = await conn.fetchval("SELECT count(*) FROM players WHERE role <> 'Placement'")
    print(f"4. RANK: Non-Placement in seasonplayers = {non_placement}, non-Placement in players table = {players_non_placement}")
    
    # 5. Historial de Seasons 1-4 intacto
    total_matches = await conn.fetchval("SELECT count(*) FROM match_history")
    past_seasonplayers = await conn.fetchval("SELECT count(*) FROM seasonplayers WHERE season_id < $1", s5['id'])
    s4_players = await conn.fetchval("SELECT count(*) FROM seasonplayers WHERE season_id = 7")
    print(f"5. HISTORIAL: Matches = {total_matches}, Past seasonplayers = {past_seasonplayers}, Season 4 players = {s4_players}")
    
    # 6. Discord bot
    lease = await conn.fetchrow("SELECT id, owner, expires_at, expires_at > now() as live FROM lounge_runtime_lease WHERE id = 1")
    print(f"6. DISCORD BOT LEASE: active={lease['live']}, expires_at={lease['expires_at']}")
    
    # 7. Web
    async with httpx.AsyncClient(timeout=30.0) as client:
        r_seasons = await client.get("https://pjsk-page.vercel.app/api/seasons")
        cur_web_season = [s for s in r_seasons.json() if s.get("active")]
        r_players = await client.get(f"https://pjsk-page.vercel.app/api/players?season_id={s5['id']}")
        web_players = r_players.json()
        web_all_zero = all(p.get("mmr") == 0 for p in web_players)
        web_all_placement = all(p.get("rank") == "Placement" for p in web_players)
        print(f"7. WEB: Current season = {cur_web_season[0]['name']}, Returned players = {len(web_players)}, All MMR 0 = {web_all_zero}, All Rank Placement = {web_all_placement}")

    await conn.close()

if __name__ == "__main__":
    asyncio.run(main())

