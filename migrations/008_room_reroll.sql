-- Additive only: audit trail for administrative /reroll. No existing table or row is touched.
-- Deliberately does NOT insert into lounge_schema_migrations so the previously deployed
-- runtime (which requires max(version)='007_publication_state.sql') keeps working for rollback.
CREATE TABLE IF NOT EXISTS lounge_room_reroll(
  id bigserial PRIMARY KEY,
  operation_id text NOT NULL UNIQUE,
  thread_id bigint NOT NULL REFERENCES lounge_room_state(thread_id),
  room_id integer NOT NULL,
  season_id integer NOT NULL,
  admin_user_id bigint NOT NULL,
  reason text NOT NULL,
  mode text NOT NULL CHECK(mode IN('auto','manual')),
  previous_selection jsonb,
  new_selection jsonb NOT NULL,
  status text NOT NULL CHECK(status IN('selected','published')),
  published_message_id bigint,
  created_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_lounge_room_reroll_thread ON lounge_room_reroll(thread_id, id DESC);
ALTER TABLE lounge_room_reroll ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON lounge_room_reroll FROM PUBLIC;
DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='anon') THEN REVOKE ALL ON lounge_room_reroll FROM anon; END IF;
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN REVOKE ALL ON lounge_room_reroll FROM authenticated; END IF;
END $$;
DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='lounge_bot') THEN
    GRANT SELECT,INSERT,UPDATE ON lounge_room_reroll TO lounge_bot;
    GRANT USAGE,SELECT ON SEQUENCE lounge_room_reroll_id_seq TO lounge_bot;
    IF NOT EXISTS(SELECT 1 FROM pg_policies WHERE tablename='lounge_room_reroll' AND policyname='bot_runtime') THEN
      CREATE POLICY bot_runtime ON lounge_room_reroll TO lounge_bot USING(true) WITH CHECK(true);
    END IF;
  END IF;
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='lounge_api') THEN
    GRANT SELECT ON lounge_room_reroll TO lounge_api;
    IF NOT EXISTS(SELECT 1 FROM pg_policies WHERE tablename='lounge_room_reroll' AND policyname='api_read') THEN
      CREATE POLICY api_read ON lounge_room_reroll FOR SELECT TO lounge_api USING(true);
    END IF;
  END IF;
END $$;
