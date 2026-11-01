-- Apply once as migration owner; set LOGIN passwords privately in the DB console.
-- Use these roles only on the server. No browser receives either URL.
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='lounge_api') THEN CREATE ROLE lounge_api NOLOGIN; END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='lounge_bot') THEN CREATE ROLE lounge_bot NOLOGIN; END IF;
END $$;
GRANT USAGE ON SCHEMA public TO lounge_api,lounge_bot;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT SELECT ON schema_migrations,seasons,season_players,matches,match_participants TO lounge_api;
GRANT SELECT(user_id,name,country) ON players TO lounge_api;
GRANT SELECT ON schema_migrations TO lounge_bot;
GRANT SELECT,INSERT,UPDATE,DELETE ON seasons,players,season_players,rooms,thread_jobs,room_members,polls,poll_votes,poll_choices,matches,match_participants,role_jobs,songs,system_operations,runtime_lease TO lounge_bot;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO lounge_bot;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['schema_migrations','seasons','season_players','matches','match_participants','players'] LOOP
  EXECUTE format('DROP POLICY IF EXISTS api_read ON %I',t);
  EXECUTE format('CREATE POLICY api_read ON %I FOR SELECT TO lounge_api USING(true)',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['schema_migrations','seasons','players','season_players','rooms','thread_jobs','room_members','polls','poll_votes','poll_choices','matches','match_participants','role_jobs','songs','system_operations','runtime_lease'] LOOP
  EXECUTE format('DROP POLICY IF EXISTS bot_access ON %I',t);
  EXECUTE format('CREATE POLICY bot_access ON %I TO lounge_bot USING(true) WITH CHECK(true)',t);
 END LOOP;
END $$;
ALTER ROLE lounge_api SET default_transaction_read_only=on;
ALTER ROLE lounge_api SET statement_timeout='5s';
ALTER ROLE lounge_bot SET statement_timeout='15s';
