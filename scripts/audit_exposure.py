"""Read-only exposure audit: what can anon/authenticated/PUBLIC reach in schema public?"""
import asyncio, json, os, ssl, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
from scripts.perf_baseline import load_from_railway
load_from_railway()
import asyncpg
async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"], ssl=ssl.create_default_context(cafile=os.environ["DB_CA_FILE"]), statement_cache_size=0)
    await c.execute("SET default_transaction_read_only=on")
    out = {}
    out["roles"] = [r[0] for r in await c.fetch("select rolname from pg_roles where rolname in ('anon','authenticated','service_role')")]
    rows = await c.fetch("""
      select c.relname, c.relkind, c.relrowsecurity rls, c.relforcerowsecurity frls, c.reltuples::bigint est,
        coalesce((select string_agg(distinct privilege_type,',') from information_schema.role_table_grants g where g.table_schema='public' and g.table_name=c.relname and g.grantee='anon'),'') anon,
        coalesce((select string_agg(distinct privilege_type,',') from information_schema.role_table_grants g where g.table_schema='public' and g.table_name=c.relname and g.grantee='authenticated'),'') authd,
        coalesce((select string_agg(distinct privilege_type,',') from information_schema.role_table_grants g where g.table_schema='public' and g.table_name=c.relname and g.grantee='PUBLIC'),'') pub,
        (select count(*) from pg_policies p where p.schemaname='public' and p.tablename=c.relname) npol
      from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' and c.relkind in ('r','v','m','p') order by 1""")
    print(f"{'table':44s} kind rls pol  anon / authenticated / PUBLIC")
    for r in rows:
        flag = "  <-- EXPOSED" if (r["anon"] or r["authd"] or r["pub"]) and (not r["rls"] or r["relkind"] != "r") else ""
        print(f"{r['relname']:44s} {r['relkind']}    {'Y' if r['rls'] else 'N'}   {r['npol']:<3d} {r['anon'] or '-'} / {r['authd'] or '-'} / {r['pub'] or '-'}{flag}")
    print("\nFunctions executable by anon/authenticated/PUBLIC (public schema):")
    for r in await c.fetch("""select p.proname, p.prosecdef secdef, has_function_privilege('anon',p.oid,'execute') anon_x from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='public' and p.prokind='f' and not exists (select 1 from pg_depend d where d.objid=p.oid and d.deptype='e')"""):
        print("  ", dict(r))
    print("\nViews depending on suspicious tables:")
    for r in await c.fetch("""select distinct v.relname view, t.relname tbl from pg_depend d join pg_rewrite rw on rw.oid=d.objid join pg_class v on v.oid=rw.ev_class join pg_class t on t.oid=d.refobjid join pg_namespace n on n.oid=v.relnamespace where n.nspname='public' and v.relkind in('v','m') and t.relkind='r' and v.oid<>t.oid"""):
        print("  ", dict(r))
    print("\nDefault privileges (pg_default_acl):", [tuple(r) for r in await c.fetch("select pg_get_userbyid(defaclrole), defaclobjtype, defaclacl::text from pg_default_acl")])
asyncio.run(main())
