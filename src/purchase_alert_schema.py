"""Additive intelligent alerts schema. Legacy observation notifiable=0 is untouched."""
def schema_sql(postgres=False):
    identity = 'BIGSERIAL PRIMARY KEY' if postgres else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    sql = f"""
CREATE TABLE IF NOT EXISTS manager_push_subscriptions (
 id {identity}, user_id INTEGER NOT NULL REFERENCES users(id), endpoint TEXT NOT NULL UNIQUE,
 p256dh TEXT NOT NULL, auth TEXT NOT NULL, version TEXT NOT NULL, binding_hash TEXT NOT NULL, device_hash TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), consented_at TEXT NOT NULL,
 revoked_at TEXT, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS manager_push_user ON manager_push_subscriptions(user_id,active);
CREATE TABLE IF NOT EXISTS manager_push_revocations (
 id {identity}, binding_hash TEXT NOT NULL UNIQUE, user_id INTEGER, revoked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS purchase_alert_control (
 id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
 generation INTEGER NOT NULL DEFAULT 0, monitor_activation TEXT, last_run_at TEXT, last_error TEXT, updated_at TEXT, updated_by INTEGER
);
INSERT INTO purchase_alert_control(id) VALUES(1) ON CONFLICT(id) DO NOTHING;
CREATE INDEX IF NOT EXISTS purchase_events_product_pending ON purchase_observation_events(product_id) WHERE processed_at IS NULL;
CREATE TABLE IF NOT EXISTS purchase_alert_product_state (
 id {identity}, product_id INTEGER NOT NULL UNIQUE, generation INTEGER NOT NULL,
 episode_number INTEGER NOT NULL, eligible INTEGER NOT NULL CHECK(eligible IN (0,1)),
 high_water INTEGER NOT NULL DEFAULT 0, level INTEGER NOT NULL DEFAULT 0,
 last_event_id BIGINT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS purchase_stock_alerts (
 id {identity}, episode_id BIGINT NOT NULL REFERENCES purchase_replenishment_episodes(id),
 generation INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('opened','worsened')),
 level INTEGER NOT NULL, suggested_units INTEGER NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(episode_id,kind,level)
);
CREATE TABLE IF NOT EXISTS purchase_alert_deliveries (
 id {identity}, alert_id BIGINT NOT NULL REFERENCES purchase_stock_alerts(id),
 user_id INTEGER NOT NULL, subscription_id BIGINT NOT NULL REFERENCES manager_push_subscriptions(id),
 subscription_version TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','processing','accepted','failed','cancelled')),
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 5), available_at TEXT NOT NULL,
 lease_token TEXT, lease_until TEXT, finished_at TEXT, error_code TEXT,
 UNIQUE(alert_id,user_id,subscription_id,subscription_version)
);
CREATE INDEX IF NOT EXISTS purchase_alert_ready ON purchase_alert_deliveries(status,available_at,id);
CREATE TABLE IF NOT EXISTS purchase_alert_attempts (
 id {identity}, delivery_id BIGINT NOT NULL REFERENCES purchase_alert_deliveries(id),
 attempt_number INTEGER NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
 outcome TEXT NOT NULL CHECK(outcome IN ('started','accepted','failed','unknown','cancelled')),
 http_status INTEGER, error_code TEXT,
 UNIQUE(delivery_id,attempt_number)
);
"""
    if postgres:
        sql += """
CREATE OR REPLACE FUNCTION purchase_manager_push_revoke() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
 EXECUTE format('UPDATE %I.manager_push_subscriptions SET active=0,revoked_at=CURRENT_TIMESTAMP::text,updated_at=CURRENT_TIMESTAMP::text WHERE user_id=$1 AND active=1', TG_TABLE_SCHEMA) USING NEW.id;
 RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS purchase_manager_push_revoke ON users;
CREATE TRIGGER purchase_manager_push_revoke AFTER UPDATE OF role,active ON users
 FOR EACH ROW WHEN (OLD.role='manager' AND (NEW.role IS DISTINCT FROM 'manager' OR NEW.active IS DISTINCT FROM 1))
 EXECUTE FUNCTION purchase_manager_push_revoke();
"""
    else:
        sql += """
CREATE TRIGGER IF NOT EXISTS purchase_manager_push_revoke AFTER UPDATE OF role,active ON users
 WHEN OLD.role='manager' AND (NEW.role IS NOT 'manager' OR NEW.active IS NOT 1)
BEGIN
 UPDATE manager_push_subscriptions SET active=0,revoked_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
 WHERE user_id=NEW.id AND active=1;
END;
"""
    if postgres:
        for table in ('manager_push_subscriptions','manager_push_revocations','purchase_alert_control','purchase_alert_product_state',
                      'purchase_stock_alerts','purchase_alert_deliveries','purchase_alert_attempts'):
            sql += f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;\n'
    return sql
