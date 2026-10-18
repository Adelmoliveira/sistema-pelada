-- Monitoring telemetry only. Runtime requires owner/BYPASSRLS and table/sequence privileges.

CREATE TABLE IF NOT EXISTS purchase_monitor_runs (
 id BIGSERIAL PRIMARY KEY, request_key TEXT NOT NULL UNIQUE,
 source TEXT NOT NULL CHECK(source IN ('manual','periodic')),
 requested_by INTEGER, started_at TEXT NOT NULL, finished_at TEXT,
 outcome TEXT NOT NULL CHECK(outcome IN ('running','success','failed','busy')),
 processed_events INTEGER NOT NULL DEFAULT 0,
 pending_events INTEGER, products INTEGER NOT NULL DEFAULT 0, error_code TEXT
);
ALTER TABLE purchase_monitor_runs ENABLE ROW LEVEL SECURITY;
