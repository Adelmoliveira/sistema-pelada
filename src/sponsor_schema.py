"""Small public logos; no filesystem or Supabase administrative key required."""


def schema_sql(postgres=False):
    identity = 'BIGSERIAL' if postgres else 'INTEGER'
    binary = 'BYTEA' if postgres else 'BLOB'
    sql = f"""
CREATE TABLE IF NOT EXISTS sponsor_catalog (
 id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL DEFAULT 1
);
INSERT INTO sponsor_catalog(id) VALUES(1) ON CONFLICT(id) DO NOTHING;
CREATE TABLE IF NOT EXISTS sponsors (
 id {identity} PRIMARY KEY, name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 100),
 primary_slot INTEGER UNIQUE CHECK(primary_slot IN (1,2)),
 sort_order INTEGER NOT NULL DEFAULT 0 CHECK(sort_order>=0),
 active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0,1)),
 link_url TEXT NOT NULL DEFAULT '', logo_data {binary}, logo_version TEXT,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_by INTEGER REFERENCES users(id),
 CHECK((logo_data IS NULL AND logo_version IS NULL) OR
       (logo_data IS NOT NULL AND logo_version IS NOT NULL AND length(logo_data)<=131072))
);
CREATE INDEX IF NOT EXISTS sponsors_display ON sponsors(active,primary_slot,sort_order,id);
INSERT INTO sponsors(name,primary_slot) VALUES('VINAC',1) ON CONFLICT(primary_slot) DO NOTHING;
INSERT INTO sponsors(name,primary_slot) VALUES('Prudention',2) ON CONFLICT(primary_slot) DO NOTHING;
"""
    if postgres:
        sql += 'ALTER TABLE sponsor_catalog ENABLE ROW LEVEL SECURITY;\nALTER TABLE sponsors ENABLE ROW LEVEL SECURITY;\n'
    return sql
