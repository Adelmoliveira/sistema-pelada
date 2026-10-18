"""Additive monitoring telemetry; no stock or historical event modifications."""
def schema_sql(postgres=False):
    identity = "BIGSERIAL PRIMARY KEY" if postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    sql = f"""
CREATE TABLE IF NOT EXISTS purchase_monitor_runs (
 id {identity}, request_key TEXT NOT NULL UNIQUE,
 source TEXT NOT NULL CHECK(source IN ('manual','periodic')),
 requested_by INTEGER, started_at TEXT NOT NULL, finished_at TEXT,
 outcome TEXT NOT NULL CHECK(outcome IN ('running','success','failed','busy')),
 processed_events INTEGER NOT NULL DEFAULT 0,
 pending_events INTEGER, products INTEGER NOT NULL DEFAULT 0, error_code TEXT
);
"""
    if postgres:
        sql += "ALTER TABLE purchase_monitor_runs ENABLE ROW LEVEL SECURITY;\n"
    return sql
