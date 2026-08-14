CREATE TABLE IF NOT EXISTS sale_delivery_operations (
    id BIGSERIAL PRIMARY KEY,
    sale_id BIGINT NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    delivered_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
    delivered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sale_delivery_operations_sale
    ON sale_delivery_operations (sale_id, delivered_at);

CREATE TABLE IF NOT EXISTS sale_item_deliveries (
    id BIGSERIAL PRIMARY KEY,
    delivery_operation_id BIGINT NOT NULL REFERENCES sale_delivery_operations(id) ON DELETE CASCADE,
    sale_item_id BIGINT NOT NULL REFERENCES sale_items(id) ON DELETE CASCADE,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    delivered_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
    delivered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sale_item_deliveries_item
    ON sale_item_deliveries (sale_item_id);
CREATE INDEX IF NOT EXISTS idx_sale_item_deliveries_operation
    ON sale_item_deliveries (delivery_operation_id);

CREATE TABLE IF NOT EXISTS notification_outbox (
    id BIGSERIAL PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL CHECK (event_type IN ('delivery_push', 'delivery_update_email', 'purchase_receipt_email')),
    sale_id BIGINT NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    delivery_id BIGINT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processing_started_at TIMESTAMPTZ,
    processed_at TIMESTAMPTZ,
    last_error TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_notification_outbox_status_ready
    ON notification_outbox (status, available_at, id);
CREATE INDEX IF NOT EXISTS idx_notification_outbox_sale
    ON notification_outbox (sale_id, delivery_id);
