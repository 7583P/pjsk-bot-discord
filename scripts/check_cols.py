import asyncio, asyncpg
async def check():
    conn = await asyncpg.connect("postgresql://postgres:postgres@127.0.0.1:55432/pjsk_test_rehearsal_s5")
    rows = await conn.fetch("SELECT column_name FROM information_schema.columns WHERE table_name = 'seasonplayers'")
    print([r['column_name'] for r in rows])
    await conn.close()
asyncio.run(check())

