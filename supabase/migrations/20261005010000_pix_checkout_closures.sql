-- Durable abandonment workflow; financial records remain untouched.
CREATE TABLE IF NOT EXISTS pix_checkout_closures (
    id BIGSERIAL PRIMARY KEY,
    sale_id BIGINT NOT NULL UNIQUE REFERENCES sales(id) ON DELETE RESTRICT,
    reason TEXT NOT NULL CHECK(reason IN ('client_cancel','timeout')),
    status TEXT NOT NULL DEFAULT 'requested' CHECK(status IN ('requested','awaiting_provider','completed','aborted_payment','retryable')),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    requested_by BIGINT REFERENCES users(id) ON DELETE RESTRICT,
    provider_cancel_requested_at TIMESTAMPTZ,
    provider_cancel_confirmed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_pix_checkout_closures_pending ON pix_checkout_closures(status,requested_at);
