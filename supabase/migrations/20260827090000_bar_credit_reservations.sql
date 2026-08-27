CREATE TABLE IF NOT EXISTS bar_credit_reservations (
    id BIGSERIAL PRIMARY KEY,
    sale_id BIGINT NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    status TEXT NOT NULL DEFAULT 'reserved'
        CHECK (status IN ('reserved', 'consumed', 'released')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NULL,
    consumed_at TIMESTAMPTZ NULL,
    released_at TIMESTAMPTZ NULL,
    CONSTRAINT bar_credit_reservations_sale_id_key UNIQUE (sale_id)
);

CREATE INDEX IF NOT EXISTS idx_bar_credit_reservations_player_active
    ON bar_credit_reservations (player_id, created_at)
    WHERE status = 'reserved';

CREATE INDEX IF NOT EXISTS idx_bar_credit_reservations_status_expires
    ON bar_credit_reservations (status, expires_at)
    WHERE status = 'reserved';
