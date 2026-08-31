-- Separate sports backorder requests from immediate sales.
ALTER TABLE sports_product_config
    ADD COLUMN IF NOT EXISTS ready_sale_enabled BOOLEAN NOT NULL DEFAULT TRUE;

ALTER TABLE sports_product_config
    DROP CONSTRAINT IF EXISTS sports_product_config_sale_mode_check;
ALTER TABLE sports_product_config
    ADD CONSTRAINT sports_product_config_sale_mode_check
    CHECK (ready_sale_enabled OR allow_backorder);

ALTER TABLE sports_sale_item_details
    ADD COLUMN IF NOT EXISTS canceled_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS canceled_by BIGINT REFERENCES users(id),
    ADD COLUMN IF NOT EXISTS cancellation_reason TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS cancellation_resolution TEXT NOT NULL DEFAULT 'none';

ALTER TABLE sports_sale_item_details
    DROP CONSTRAINT IF EXISTS sports_sale_item_details_fulfillment_status_check;
ALTER TABLE sports_sale_item_details
    ADD CONSTRAINT sports_sale_item_details_fulfillment_status_check
    CHECK (fulfillment_status IN ('reserved','requested','in_production','available','delivered','cancelled'));
ALTER TABLE sports_sale_item_details
    ADD CONSTRAINT sports_sale_item_details_cancellation_resolution_check
    CHECK (cancellation_resolution IN ('none','awaiting_arrival','stocked','reassigned','admin_pending'));

ALTER TABLE sports_order_status_history
    DROP CONSTRAINT IF EXISTS sports_order_status_history_from_status_check;
ALTER TABLE sports_order_status_history
    DROP CONSTRAINT IF EXISTS sports_order_status_history_to_status_check;
ALTER TABLE sports_order_status_history
    ADD CONSTRAINT sports_order_status_history_from_status_check
    CHECK (from_status IN ('reserved','requested','in_production','available','delivered','cancelled'));
ALTER TABLE sports_order_status_history
    ADD CONSTRAINT sports_order_status_history_to_status_check
    CHECK (to_status IN ('reserved','requested','in_production','available','delivered','cancelled'));

ALTER TABLE notification_outbox
    DROP CONSTRAINT IF EXISTS notification_outbox_event_type_check;
ALTER TABLE notification_outbox
    ADD CONSTRAINT notification_outbox_event_type_check
    CHECK (event_type IN ('delivery_push','delivery_update_email','purchase_receipt_email','sports_order_available_push'));

CREATE INDEX IF NOT EXISTS idx_sports_cancel_resolution
    ON sports_sale_item_details(cancellation_resolution,variant_id);
