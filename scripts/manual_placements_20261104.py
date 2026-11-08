import asyncio,json,ssl,sys
from pathlib import Path
from urllib.parse import urlparse
import asyncpg,httpx
ROOT=Path(__file__).resolve().parents[1]
PROJECT='f8993ba3-8ccf-4886-b73e-15df02dda477'
ENV='2306315c-5efc-46fe-ba37-f52eabc4b7ce'
SERVICE='45267784-6c78-41d2-9363-0ec4f4791fa2'
STATS={1086613476937580564:[4507,19,0,0,0],1387811818394882232:[4364,150,6,1,5],1513854817359892499:[3964,446,54,8,54],1545824905084403831:[4516,10,0,0,0],823766306154217502:[4415,105,3,0,3]}
def railway(query,variables=None):
    token=json.loads(Path('C:/Users/Usuario/.railway/config.json').read_text())['user']['token']
    r=httpx.post('https://backboard.railway.com/graphql/v2',headers={'Authorization':f'Bearer {token}'},json={'query':query,'variables':variables or {}},timeout=30)
    r.raise_for_status()
    data=r.json()
    if data.get('errors'): raise RuntimeError(json.dumps(data['errors']))
    return data['data']
def variables():
    return railway('query($p:String!,$e:String!,$s:String!){variables(projectId:$p,environmentId:$e,serviceId:$s)}',{'p':PROJECT,'e':ENV,'s':SERVICE})['variables']
async def connect(v):
    url=v.get('DATABASE_URL') or v.get('DB_HOST')
    p=urlparse(url)
    print('Database:',p.hostname,p.port,p.path)
    ctx=ssl.create_default_context(cafile=str(ROOT/'.local/season5-current/certs/supabase_ca_bundle.crt'))
    return await asyncpg.connect(url,ssl=ctx,statement_cache_size=0,timeout=30)
def output(label,rows): print(label,json.dumps([dict(r) for r in rows],default=str,ensure_ascii=True))
async def inspect():
    v=variables()
    d=railway('query($s:String!,$e:String!){deployments(input:{serviceId:$s,environmentId:$e},first:1){edges{node{id status createdAt meta}}}}',{'s':SERVICE,'e':ENV})
    output('Deployment:',[d['deployments']['edges'][0]['node']])
    c=await connect(v)
    try:
        output('Seasons:',await c.fetch('SELECT * FROM seasons ORDER BY id DESC LIMIT 3'))
        output('Columns:',await c.fetch("SELECT table_name,column_name,data_type FROM information_schema.columns WHERE table_schema='public' AND table_name IN ('seasonplayers','players','results','match_history','lounge_submissions','lounge_room_state','lounge_role_jobs') ORDER BY table_name,ordinal_position"))
        output('Players:',await c.fetch('SELECT * FROM players WHERE user_id=ANY($1::bigint[])',list(STATS)))
        output('Season players:',await c.fetch('SELECT * FROM seasonplayers WHERE user_id=ANY($1::bigint[]) AND season_id=(SELECT id FROM seasons WHERE active)',list(STATS)))
        output('History:',await c.fetch('SELECT * FROM match_history WHERE user_id=ANY($1::bigint[]) AND season_id=(SELECT id FROM seasons WHERE active) ORDER BY result_id DESC LIMIT 30',list(STATS)))
        tables=await c.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'lounge_%'")
        output('Runtime tables:',tables)
        if any(r['tablename']=='lounge_room_state' for r in tables): output('Rooms:',await c.fetch("SELECT * FROM lounge_room_state WHERE status<>'finished' ORDER BY updated_at DESC LIMIT 10"))
        print('Application:',v.get('APPLICATION_ID'),'guild:',v.get('GUILD_ID'))
    finally: await c.close()
if __name__=='__main__': asyncio.run(inspect())
