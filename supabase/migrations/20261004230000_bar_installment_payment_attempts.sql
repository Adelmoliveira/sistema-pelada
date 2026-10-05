-- Independent Pix attempts for Bar case installment plans.
CREATE TABLE IF NOT EXISTS bar_installment_payment_attempts (
    id BIGSERIAL PRIMARY KEY,
    installment_id BIGINT NOT NULL REFERENCES bar_installments(id) ON DELETE RESTRICT,
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    status TEXT NOT NULL DEFAULT 'creating' CHECK(status IN ('creating','pending','approved','expired','failed','canceled')),
    mercado_pago_order_id TEXT UNIQUE,
    mercado_pago_payment_id TEXT UNIQUE,
    external_reference TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE,
    qr_code TEXT,
    qr_code_base64 TEXT,
    ticket_url TEXT,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_bar_installment_attempts_installment
    ON bar_installment_payment_attempts(installment_id,id);
