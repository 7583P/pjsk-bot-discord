import asyncio, ssl, json, asyncpg

env_path = r'C:\Users\Usuario\.pjsk_secure_backups\wispbyte_bot.env'
dsn = None
with open(env_path, 'r', encoding='utf-8') as f:
    for line in f:
        if line.startswith('DATABASE_URL='):
            dsn = line.strip().split('=', 1)[1].strip('"\'')

async def update_database():
    tls = ssl.create_default_context(cafile='certs/supabase_ca_bundle.crt')
    conn = await asyncpg.connect(dsn, ssl=tls, timeout=10)
    
    result_id = 280
    user_id = 1545824905084403831 # Emu Lover
    
    async with conn.transaction():
        # 1. Update match_history
        print("Updating match_history...")
        mh_res = await conn.execute("""
            UPDATE match_history
            SET s_great = 19,
                s_good = 0
            WHERE result_id = $1 AND user_id = $2
        """, result_id, user_id)
        print("  match_history result:", mh_res)
        
        # 2. Update lounge_submissions payload
        print("Updating lounge_submissions payload...")
        sub = await conn.fetchrow("SELECT payload FROM lounge_submissions WHERE result_id = $1 FOR UPDATE", result_id)
        if sub:
            payload = json.loads(sub['payload'])
            for p in payload.get('players', []):
                if p['user_id'] == user_id:
                    p['stats'] = [4817, 19, 0, 0, 0]
                    p['total'] = 19325
            await conn.execute("""
                UPDATE lounge_submissions
                SET payload = $2
                WHERE result_id = $1
            """, result_id, json.dumps(payload))
            print("  lounge_submissions updated successfully!")
            
        # 3. Insert audit into lounge_operations
        op_id = f"fix-result-280-emulover-stats"
        op_payload = {
            "result_id": result_id,
            "user_id": user_id,
            "player_name": "Emu Lover",
            "previous_stats": [4817, 0, 19, 0, 0],
            "previous_total": 19306,
            "new_stats": [4817, 19, 0, 0, 0],
            "new_total": 19325,
            "reason": "Correction of submitted stats: 19 greats instead of 19 goods",
        }
        await conn.execute("""
            INSERT INTO lounge_operations (id, kind, payload, created_at)
            VALUES ($1, 'result_stat_correction', $2, now())
            ON CONFLICT (id) DO UPDATE SET payload = excluded.payload, created_at = now()
        """, op_id, json.dumps(op_payload))
        print("  lounge_operations audit logged!")

    # Verify
    mh_new = await conn.fetchrow("SELECT * FROM match_history WHERE result_id = $1 AND user_id = $2", result_id, user_id)
    print("\nVerified match_history row:", dict(mh_new))
    
    sub_new = await conn.fetchrow("SELECT payload FROM lounge_submissions WHERE result_id = $1", result_id)
    p_sub = json.loads(sub_new['payload'])['players'][1]
    print("Verified lounge_submissions Emu Lover:", p_sub['name'], p_sub['stats'], p_sub['total'])

    await conn.close()

if __name__ == '__main__':
    asyncio.run(update_database())
