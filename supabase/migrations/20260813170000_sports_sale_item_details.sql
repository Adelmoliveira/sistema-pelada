-- Phase C1: immutable sports choices and per-item fulfillment state.
CREATE TABLE IF NOT EXISTS sports_sale_item_details (
    sale_item_id BIGINT PRIMARY KEY REFERENCES sale_items(id) ON DELETE CASCADE,
    variant_id BIGINT NOT NULL REFERENCES sports_product_variants(id),
    variant_size TEXT NOT NULL,
    custom_name TEXT NOT NULL DEFAULT '',
    custom_number TEXT NOT NULL DEFAULT '',
    order_mode TEXT NOT NULL CHECK(order_mode IN ('ready','backorder')),
    fulfillment_status TEXT NOT NULL CHECK(fulfillment_status IN ('reserved','requested','in_production','available','delivered')),
    stock_released_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sports_sale_details_variant ON sports_sale_item_details(variant_id);
CREATE INDEX IF NOT EXISTS idx_sports_sale_details_status ON sports_sale_item_details(fulfillment_status,order_mode);
