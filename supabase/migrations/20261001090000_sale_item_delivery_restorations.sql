CREATE TABLE IF NOT EXISTS sale_item_delivery_restorations (
    id BIGSERIAL PRIMARY KEY,
    delivery_id BIGINT NOT NULL UNIQUE
        REFERENCES sale_item_deliveries(id) ON DELETE RESTRICT,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    restored_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
    restored_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reason TEXT NOT NULL CHECK (length(btrim(reason)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_sale_item_delivery_restorations_delivery
    ON sale_item_delivery_restorations(delivery_id);
