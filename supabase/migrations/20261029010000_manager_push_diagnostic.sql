-- Controle persistente de testes Web Push para gestores.
-- Não altera estoque, episódios ou filas de alertas.

SET LOCAL lock_timeout = '2000ms';
SET LOCAL statement_timeout = '30000ms';

CREATE TABLE IF NOT EXISTS manager_push_diagnostic_control (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    last_attempt_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE manager_push_diagnostic_control ENABLE ROW LEVEL SECURITY;
