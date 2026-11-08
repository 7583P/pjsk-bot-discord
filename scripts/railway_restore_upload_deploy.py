"""Roll Railway's worker back to the CLI-uploaded deployment (not the GitHub-main source build).
Variable changes on this service trigger a GitHub-source rebuild, so the token is restored with skipDeploys and the
deployment is then rolled back by id.  Token is only held in memory and is ALWAYS restored.
   python -m scripts.railway_restore_upload_deploy <deployment-id> [--keep-disabled]"""
import asyncio, json, os, ssl, subprocess, sys, time
import asyncpg, httpx
P, E, S = "f8993ba3-8ccf-4886-b73e-15df02dda477", "2306315c-5efc-46fe-ba37-f52eabc4b7ce", "45267784-6c78-41d2-9363-0ec4f4791fa2"
RB = sys.argv[1]
KEEP_DISABLED = "--keep-disabled" in sys.argv  # cutover: new-era API code stays up, Gateway stays OFF (token remains DISABLED...)
H = {"Authorization": "Bearer " + json.load(open(os.path.expanduser("~/.railway/config.json")))["user"]["token"]}
def gql(query, variables=None):
    r = httpx.post("https://backboard.railway.com/graphql/v2", json={"query": query, "variables": variables or {}}, headers=H, timeout=60).json()
    if r.get("errors"): raise RuntimeError(r["errors"][0]["message"][:200])
    return r["data"]
def upsert(token, skip):
    gql("mutation($i:VariableCollectionUpsertInput!){variableCollectionUpsert(input:$i)}", {"i": {"projectId": P, "environmentId": E, "serviceId": S, "variables": {"DISCORD_TOKEN": token}, "skipDeploys": skip}})
def current():
    out = subprocess.run("railway variables --kv", shell=True, capture_output=True, text=True, check=True).stdout
    return dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
async def probe(url):
    c = await asyncpg.connect(url, ssl=ssl.create_default_context(cafile="certs/supabase_ca_bundle.crt"), statement_cache_size=0)
    try:
        await c.execute("SET default_transaction_read_only=on")
        return (await c.fetchval("select count(*) from pg_stat_activity where usename='postgres' and application_name='Supavisor' and state<>'idle'"),
                await c.fetchval("select coalesce((select expires_at>now() from lounge_runtime_lease where id=1),false)"))
    finally: await c.close()
async def main():
    v = current(); url = v["DATABASE_URL"]; tok = v["DISCORD_TOKEN"]
    if tok.startswith("DISABLED"): tok = tok[len("DISABLED"):]
    try:
        print("1) stop bot (GitHub-source deploy with DISABLED token)"); upsert("DISABLED" + tok, False)
        t0 = time.time()
        while time.time() - t0 < 240:
            await asyncio.sleep(12); act, lease = await probe(url); print(f"   t+{int(time.time()-t0)}s active_sessions={act} lease_live={lease}")
            if not lease and time.time() - t0 > 60: break
    finally:
        if KEEP_DISABLED:
            print("2) --keep-disabled: Gateway stays OFF on Railway (variable left as DISABLED<token>, no deploy)")
        else:
            print("2) token restored WITHOUT deploy"); upsert(tok, True)
    print("3) rollback ->", RB[:8]); gql("mutation($id:String!){deploymentRollback(id:$id)}", {"id": RB})
    t0 = time.time()
    while time.time() - t0 < 330:
        await asyncio.sleep(15); act, lease = await probe(url); print(f"   t+{int(time.time()-t0)}s lease_live={lease}")
        if KEEP_DISABLED and time.time() - t0 > 75:
            print("API redeployed from the CLI-uploaded build; Gateway OFF; lease_live =", lease); return
        if lease and not KEEP_DISABLED: print("BOT ONLINE (lease held)"); return
    print("bot did not claim the lease in 330s")
asyncio.run(main())
