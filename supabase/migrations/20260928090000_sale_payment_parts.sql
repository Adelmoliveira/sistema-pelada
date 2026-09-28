CREATE TABLE IF NOT EXISTS sale_payment_parts (
    id BIGSERIAL PRIMARY KEY,
    sale_id BIGINT NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    method TEXT NOT NULL CHECK (method IN ('Créditos', 'Pix', 'Dinheiro')),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    status TEXT NOT NULL CHECK (status IN ('reserved', 'pending', 'approved', 'canceled', 'refunded')),
    external_reference TEXT,
    payment_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_at TIMESTAMPTZ,
    canceled_at TIMESTAMPTZ,
    refunded_at TIMESTAMPTZ,
    UNIQUE (sale_id, method)
);

CREATE INDEX IF NOT EXISTS idx_sale_payment_parts_sale ON sale_payment_parts(sale_id);
CREATE INDEX IF NOT EXISTS idx_sale_payment_parts_status ON sale_payment_parts(status);
