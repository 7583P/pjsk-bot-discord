CREATE TABLE schema_migrations(version text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE seasons(
 id text PRIMARY KEY, number integer UNIQUE, name text NOT NULL,
 status text NOT NULL CHECK(status IN('active','archived','planned')),
 legacy_placement boolean NOT NULL DEFAULT true,
 started_at timestamptz, ended_at timestamptz
);
CREATE UNIQUE INDEX seasons_one_active ON seasons(status) WHERE status='active';
CREATE TABLE players(
 user_id bigint PRIMARY KEY CHECK(user_id>0), name text NOT NULL DEFAULT '',
 country text NOT NULL DEFAULT '' CHECK(country='' OR country ~ '^[A-Za-z]{2}$'),
 legacy_data jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE season_players(
 season_id text NOT NULL REFERENCES seasons(id), user_id bigint NOT NULL REFERENCES players(user_id),
 mmr bigint NOT NULL DEFAULT 0, role text NOT NULL DEFAULT 'Placement' CHECK(role IN('Placement','Bronze','Gold','Diamond')),
 placement_pending boolean NOT NULL, peak_mmr bigint, matches_played integer NOT NULL DEFAULT 0 CHECK(matches_played>=0),
 wins integer NOT NULL DEFAULT 0 CHECK(wins>=0), losses integer NOT NULL DEFAULT 0 CHECK(losses>=0),
 updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(season_id,user_id)
);
CREATE INDEX season_leaderboard ON season_players(season_id,mmr DESC,user_id);
CREATE TABLE rooms(
 id uuid PRIMARY KEY, season_id text NOT NULL REFERENCES seasons(id), guild_id bigint NOT NULL,
 queue_id text NOT NULL, display_number integer NOT NULL,
 thread_id bigint UNIQUE, status text NOT NULL CHECK(status IN('provisioning','open','polling','ready','finished','closed','needs_attention')),
 issue text, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(id,season_id), UNIQUE(id,guild_id)
);
CREATE INDEX rooms_queue ON rooms(guild_id,queue_id,status);
CREATE TABLE thread_jobs(
 room_id uuid NOT NULL REFERENCES rooms(id),user_id bigint NOT NULL REFERENCES players(user_id),
 action text NOT NULL CHECK(action IN('add','remove')),attempts integer NOT NULL DEFAULT 0,
 next_attempt timestamptz NOT NULL DEFAULT now(),last_error text,PRIMARY KEY(room_id,user_id)
);
CREATE TABLE room_members(
 room_id uuid NOT NULL, guild_id bigint NOT NULL, user_id bigint NOT NULL REFERENCES players(user_id),
 joined_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(room_id,user_id), UNIQUE(guild_id,user_id),
 FOREIGN KEY(room_id,guild_id) REFERENCES rooms(id,guild_id)
);
CREATE FUNCTION guard_room_capacity() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE n integer;
BEGIN
 PERFORM 1 FROM rooms WHERE id=NEW.room_id AND status IN('open','provisioning','polling','ready') FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'room is not joinable'; END IF;
 SELECT count(*) INTO n FROM room_members WHERE room_id=NEW.room_id;
 IF n>=5 THEN RAISE EXCEPTION 'room capacity exceeded'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER room_capacity BEFORE INSERT ON room_members FOR EACH ROW EXECUTE FUNCTION guard_room_capacity();
CREATE TABLE polls(
 room_id uuid PRIMARY KEY REFERENCES rooms(id), message_id bigint UNIQUE,
 options jsonb NOT NULL, expires_at timestamptz NOT NULL,
 status text NOT NULL CHECK(status IN('sending','active','closed','cancelled','needs_attention')),
 winner integer, announced boolean NOT NULL DEFAULT false
);
CREATE TABLE poll_votes(
 room_id uuid NOT NULL REFERENCES polls(room_id), user_id bigint NOT NULL REFERENCES players(user_id),
 choice integer NOT NULL CHECK(choice>=0), vote_order bigint GENERATED ALWAYS AS IDENTITY,
 PRIMARY KEY(room_id,user_id)
);
CREATE TABLE poll_choices(
 room_id uuid NOT NULL REFERENCES polls(room_id),choice integer NOT NULL,
 first_order bigint GENERATED ALWAYS AS IDENTITY,PRIMARY KEY(room_id,choice)
);
CREATE TABLE matches(
 id uuid PRIMARY KEY, room_id uuid NOT NULL UNIQUE, season_id text NOT NULL,
 source_message_id bigint NOT NULL UNIQUE, results jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(room_id,season_id) REFERENCES rooms(id,season_id), UNIQUE(id,season_id)
);
CREATE TABLE match_participants(
 match_id uuid NOT NULL, season_id text NOT NULL, user_id bigint NOT NULL,
 position integer NOT NULL CHECK(position BETWEEN 1 AND 5), score bigint NOT NULL,
 old_mmr bigint NOT NULL, delta bigint NOT NULL, new_mmr bigint NOT NULL, stats jsonb NOT NULL,
 PRIMARY KEY(match_id,user_id), UNIQUE(match_id,position),
 FOREIGN KEY(match_id,season_id) REFERENCES matches(id,season_id),
 FOREIGN KEY(season_id,user_id) REFERENCES season_players(season_id,user_id)
);
CREATE TABLE role_jobs(
 guild_id bigint NOT NULL,user_id bigint NOT NULL REFERENCES players(user_id),
 attempts integer NOT NULL DEFAULT 0, next_attempt timestamptz NOT NULL DEFAULT now(), last_error text,
 PRIMARY KEY(guild_id,user_id)
);
CREATE TABLE songs(id integer NOT NULL,title text NOT NULL,diff text NOT NULL,level integer NOT NULL,
 PRIMARY KEY(id,diff));
CREATE TABLE legacy_placements(user_id bigint PRIMARY KEY, data jsonb NOT NULL);
CREATE TABLE system_operations(id text PRIMARY KEY,kind text NOT NULL,details jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE runtime_lease(id integer PRIMARY KEY CHECK(id=1),owner uuid NOT NULL,expires_at timestamptz NOT NULL);

-- No direct client access. The backend uses a separately configured read-only login.
DO $$ DECLARE t record; r text; BEGIN
 FOR t IN SELECT tablename FROM pg_tables WHERE schemaname='public' LOOP
  EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',t.tablename);
  EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC',t.tablename);
  FOREACH r IN ARRAY ARRAY['anon','authenticated'] LOOP
   IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=r) THEN
    EXECUTE format('REVOKE ALL ON public.%I FROM %I',t.tablename,r);
   END IF;
  END LOOP;
 END LOOP;
END $$;
