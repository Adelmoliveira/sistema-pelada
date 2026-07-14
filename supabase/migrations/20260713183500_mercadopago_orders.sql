ALTER TABLE sales ADD COLUMN IF NOT EXISTS payment_status TEXT NOT NULL DEFAULT 'approved';
ALTER TABLE sales ADD COLUMN IF NOT EXISTS mercadopago_order_id TEXT;
ALTER TABLE sales ADD COLUMN IF NOT EXISTS mercadopago_payment_id TEXT;
ALTER TABLE sales ADD COLUMN IF NOT EXISTS external_reference TEXT;
ALTER TABLE sales ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
ALTER TABLE sales ADD COLUMN IF NOT EXISTS paid_at TIMESTAMP;

CREATE UNIQUE INDEX IF NOT EXISTS idx_sales_mp_order
ON sales(mercadopago_order_id)
WHERE mercadopago_order_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_sales_external_reference
ON sales(external_reference)
WHERE external_reference IS NOT NULL;
