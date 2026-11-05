-- Persistent manager messages for eligible intelligent purchase alerts.
-- Apply before deploying the classifier integration. No backfill is performed.
SET LOCAL lock_timeout = '2000ms';
SET LOCAL statement_timeout = '30000ms';

CREATE TABLE IF NOT EXISTS purchase_alert_messages (
 id BIGSERIAL PRIMARY KEY, alert_id BIGINT NOT NULL REFERENCES purchase_stock_alerts(id) ON DELETE RESTRICT,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 title TEXT NOT NULL, body TEXT NOT NULL, url TEXT NOT NULL,
 read_at TEXT, deleted_at TEXT, created_at TEXT NOT NULL,
 UNIQUE(alert_id,user_id)
);
CREATE INDEX IF NOT EXISTS purchase_alert_messages_user
 ON purchase_alert_messages(user_id,deleted_at,created_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS purchase_alert_messages_unread
 ON purchase_alert_messages(user_id,deleted_at,read_at);
ALTER TABLE purchase_alert_messages ENABLE ROW LEVEL SECURITY;
