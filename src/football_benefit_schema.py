"""Preparatory benefit ledger. No selection, attendance or notification engine."""


def schema_sql(postgres=False):
    identity = 'BIGSERIAL' if postgres else 'INTEGER'
    key = 'BIGINT' if postgres else 'INTEGER'
    valid_date = ("{0}=to_char(CAST({0} AS DATE),'YYYY-MM-DD')" if postgres else
                  "date({0},'+0 days') IS NOT NULL AND date({0},'+0 days')={0}")
    start_check = valid_date.format('starts_on')
    end_check = valid_date.format('ends_on')
    sql = f"""
CREATE TABLE IF NOT EXISTS football_directorships (
 id {identity} PRIMARY KEY,
 player_id {key} NOT NULL REFERENCES players(id) ON DELETE RESTRICT,
 category TEXT NOT NULL CHECK(category IN ('DIRETORIA','OFICIAL_GENERAL')),
 starts_on TEXT NOT NULL CHECK(length(starts_on)=10 AND ({start_check})),
 ends_on TEXT CHECK(ends_on IS NULL OR (length(ends_on)=10 AND ({end_check}) AND ends_on>=starts_on)),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
 created_by {key} NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 updated_by {key} NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 change_reason TEXT NOT NULL CHECK(length(trim(change_reason)) BETWEEN 3 AND 1000)
);
CREATE INDEX IF NOT EXISTS football_directorships_player_dates
 ON football_directorships(player_id,starts_on,ends_on);
CREATE UNIQUE INDEX IF NOT EXISTS football_directorships_open
 ON football_directorships(player_id) WHERE ends_on IS NULL;
CREATE TABLE IF NOT EXISTS football_presence_events (
 id {identity} PRIMARY KEY,
 sumula_id {key} NOT NULL REFERENCES football_sumulas(id) ON DELETE RESTRICT,
 player_id {key} NOT NULL REFERENCES players(id) ON DELETE RESTRICT,
 event_type TEXT NOT NULL CHECK(event_type IN ('CONFIRMED','REVOKED','NETWORK_OBSERVED')),
 source TEXT NOT NULL CHECK(source IN ('MANUAL','AGENT')),
 source_event_id TEXT NOT NULL,
 occurred_at TEXT NOT NULL,
 recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 actor_id {key} REFERENCES users(id) ON DELETE RESTRICT,
 reason TEXT NOT NULL,
 supersedes_id {key} REFERENCES football_presence_events(id) ON DELETE RESTRICT,
 CHECK(source!='MANUAL' OR actor_id IS NOT NULL),
 CHECK(event_type='NETWORK_OBSERVED' OR source='MANUAL'),
 UNIQUE(source,source_event_id)
);
CREATE INDEX IF NOT EXISTS football_presence_player
 ON football_presence_events(sumula_id,player_id,id);
CREATE UNIQUE INDEX IF NOT EXISTS football_matches_benefit_parent
 ON football_matches(sumula_id,id);
CREATE TABLE IF NOT EXISTS football_benefit_sessions (
 id {identity} PRIMARY KEY,
 sumula_id {key} NOT NULL UNIQUE REFERENCES football_sumulas(id) ON DELETE RESTRICT,
 first_match_id {key} NOT NULL,
 status TEXT NOT NULL DEFAULT 'PREPARATION'
  CHECK(status IN ('PREPARATION','DEFINED','BOARD_CONFIRMED','CLOSED','CANCELED')),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
 policy_version TEXT NOT NULL DEFAULT 'stage1',
 candidate_snapshot TEXT NOT NULL DEFAULT '{{}}',
 created_by {key} NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 FOREIGN KEY(sumula_id,first_match_id) REFERENCES football_matches(sumula_id,id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX IF NOT EXISTS football_directorships_identity ON football_directorships(id,player_id);
CREATE TABLE IF NOT EXISTS football_benefit_allocations (
 id {identity} PRIMARY KEY,
 session_id {key} NOT NULL REFERENCES football_benefit_sessions(id) ON DELETE RESTRICT,
 player_id {key} NOT NULL REFERENCES players(id) ON DELETE RESTRICT,
 directorship_id {key} NOT NULL REFERENCES football_directorships(id) ON DELETE RESTRICT,
 position TEXT NOT NULL CHECK(position IN ('DEFESA','MEIO','ATAQUE')),
 status TEXT NOT NULL CHECK(status IN ('RESERVED','BOARD_CONFIRMED','WITHDRAWN','REPLACED','CANCELED','CORRECTED')),
 reserved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 board_confirmed_at TEXT,
 supersedes_id {key} REFERENCES football_benefit_allocations(id) ON DELETE RESTRICT,
 CHECK(status!='BOARD_CONFIRMED' OR board_confirmed_at IS NOT NULL),
 FOREIGN KEY(directorship_id,player_id) REFERENCES football_directorships(id,player_id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX IF NOT EXISTS football_benefit_position_live
 ON football_benefit_allocations(session_id,position) WHERE status IN ('RESERVED','BOARD_CONFIRMED');
CREATE UNIQUE INDEX IF NOT EXISTS football_benefit_player_live
 ON football_benefit_allocations(session_id,player_id) WHERE status IN ('RESERVED','BOARD_CONFIRMED');
CREATE INDEX IF NOT EXISTS football_benefit_usage
 ON football_benefit_allocations(player_id,position,board_confirmed_at);
CREATE TABLE IF NOT EXISTS football_benefit_events (
 id {identity} PRIMARY KEY,
 directorship_id {key} REFERENCES football_directorships(id) ON DELETE RESTRICT,
 session_id {key} REFERENCES football_benefit_sessions(id) ON DELETE RESTRICT,
 allocation_id {key} REFERENCES football_benefit_allocations(id) ON DELETE RESTRICT,
 player_id {key} NOT NULL REFERENCES players(id) ON DELETE RESTRICT,
 event_type TEXT NOT NULL CHECK(event_type IN ('REGISTERED','AMENDED','ENDED','RESERVED',
 'BOARD_CONFIRMED','WITHDRAWN','REPLACED','CANCELED','CORRECTED','HISTORICAL_USAGE')),
 position TEXT CHECK(position IN ('DEFESA','MEIO','ATAQUE')),
 occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 actor_id {key} NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 3 AND 1000),
 before_snapshot TEXT NOT NULL DEFAULT '{{}}',
 after_snapshot TEXT NOT NULL DEFAULT '{{}}',
 corrects_event_id {key} REFERENCES football_benefit_events(id) ON DELETE RESTRICT,
 request_id TEXT UNIQUE,
 CHECK(event_type NOT IN ('BOARD_CONFIRMED','HISTORICAL_USAGE') OR position IS NOT NULL),
 CHECK(event_type!='CORRECTED' OR corrects_event_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS football_benefit_events_player
 ON football_benefit_events(player_id,position,occurred_at,id);
CREATE INDEX IF NOT EXISTS football_benefit_events_membership
 ON football_benefit_events(directorship_id,id);
"""
    overlap = """SELECT 1 FROM football_directorships d WHERE d.player_id=NEW.player_id
 AND d.id<>COALESCE(NEW.id,-1)
 AND d.starts_on<=COALESCE(NEW.ends_on,'9999-12-31')
 AND NEW.starts_on<=COALESCE(d.ends_on,'9999-12-31')"""
    if postgres:
        sql += f"""
CREATE OR REPLACE FUNCTION football_benefit_membership_guard() RETURNS trigger
 LANGUAGE plpgsql SET search_path FROM CURRENT AS $body$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Benefit history cannot be deleted'; END IF;
 -- Lock a stable existing parent even when no membership exists yet.
 PERFORM 1 FROM players WHERE id=NEW.player_id FOR UPDATE;
 IF TG_OP='UPDATE' AND (NEW.player_id<>OLD.player_id OR NEW.revision<>OLD.revision+1) THEN
  RAISE EXCEPTION 'Invalid membership revision or player change';
 END IF;
 IF EXISTS({overlap}) THEN RAISE EXCEPTION 'Overlapping benefit membership'; END IF;
 RETURN NEW;
END;
$body$;
DROP TRIGGER IF EXISTS football_benefit_membership_guard ON football_directorships;
CREATE TRIGGER football_benefit_membership_guard BEFORE INSERT OR UPDATE OR DELETE
 ON football_directorships FOR EACH ROW EXECUTE FUNCTION football_benefit_membership_guard();
CREATE OR REPLACE FUNCTION football_benefit_first_match_guard() RETURNS trigger
 LANGUAGE plpgsql SET search_path FROM CURRENT AS $body$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM football_matches WHERE id=NEW.first_match_id
  AND sumula_id=NEW.sumula_id AND number=1 FOR SHARE) THEN
  RAISE EXCEPTION 'Benefit requires first match of the same sumula';
 END IF;
 RETURN NEW;
END;
$body$;
DROP TRIGGER IF EXISTS football_benefit_first_match_guard ON football_benefit_sessions;
CREATE TRIGGER football_benefit_first_match_guard BEFORE INSERT OR UPDATE
 ON football_benefit_sessions FOR EACH ROW EXECUTE FUNCTION football_benefit_first_match_guard();
CREATE OR REPLACE FUNCTION football_benefit_match_parent_guard() RETURNS trigger
 LANGUAGE plpgsql SET search_path FROM CURRENT AS $body$
BEGIN
 IF (NEW.number<>1 OR NEW.sumula_id<>OLD.sumula_id) AND EXISTS(
  SELECT 1 FROM football_benefit_sessions WHERE first_match_id=OLD.id) THEN
  RAISE EXCEPTION 'Cannot change first match linked to a benefit session';
 END IF;
 RETURN NEW;
END;
$body$;
DROP TRIGGER IF EXISTS football_benefit_match_parent_guard ON football_matches;
CREATE TRIGGER football_benefit_match_parent_guard BEFORE UPDATE OF number,sumula_id
 ON football_matches FOR EACH ROW EXECUTE FUNCTION football_benefit_match_parent_guard();
CREATE OR REPLACE FUNCTION football_benefit_immutable() RETURNS trigger
 LANGUAGE plpgsql AS $body$
BEGIN
 RAISE EXCEPTION 'Benefit audit is append-only';
END;
$body$;
"""
        for table in ('football_benefit_events', 'football_presence_events'):
            sql += f"""DROP TRIGGER IF EXISTS {table}_immutable ON {table};
CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table}
 FOR EACH ROW EXECUTE FUNCTION football_benefit_immutable();
"""
        # SECURITY INVOKER; no anonymous/authenticated policies or privilege escalation.
        for table in ('football_directorships', 'football_presence_events', 'football_benefit_sessions',
                      'football_benefit_allocations', 'football_benefit_events'):
            sql += f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;\n'
            sql += f'REVOKE ALL ON {table} FROM PUBLIC;\n'
    else:
        for op in ('INSERT', 'UPDATE'):
            sql += f"""
CREATE TRIGGER IF NOT EXISTS football_directorships_overlap_{op.lower()}
 BEFORE {op} ON football_directorships WHEN EXISTS({overlap})
 BEGIN SELECT RAISE(ABORT,'Overlapping benefit membership'); END;
CREATE TRIGGER IF NOT EXISTS football_benefit_first_match_{op.lower()}
 BEFORE {op} ON football_benefit_sessions WHEN NOT EXISTS(
 SELECT 1 FROM football_matches WHERE id=NEW.first_match_id AND sumula_id=NEW.sumula_id AND number=1)
 BEGIN SELECT RAISE(ABORT,'Benefit requires first match of the same sumula'); END;
"""
        sql += """
CREATE TRIGGER IF NOT EXISTS football_benefit_match_parent_guard
 BEFORE UPDATE OF number,sumula_id ON football_matches
 WHEN (NEW.number<>1 OR NEW.sumula_id<>OLD.sumula_id) AND EXISTS(
 SELECT 1 FROM football_benefit_sessions WHERE first_match_id=OLD.id)
 BEGIN SELECT RAISE(ABORT,'Cannot change first match linked to a benefit session'); END;
CREATE TRIGGER IF NOT EXISTS football_directorships_revision BEFORE UPDATE ON football_directorships
 WHEN NEW.player_id<>OLD.player_id OR NEW.revision<>OLD.revision+1
 BEGIN SELECT RAISE(ABORT,'Invalid membership revision or player change'); END;
CREATE TRIGGER IF NOT EXISTS football_directorships_no_delete BEFORE DELETE ON football_directorships
 BEGIN SELECT RAISE(ABORT,'Benefit history cannot be deleted'); END;
"""
        for table in ('football_benefit_events', 'football_presence_events'):
            for op in ('UPDATE', 'DELETE'):
                sql += f"""CREATE TRIGGER IF NOT EXISTS {table}_{op.lower()} BEFORE {op} ON {table}
 BEGIN SELECT RAISE(ABORT,'Benefit audit is append-only'); END;
"""
    return sql
