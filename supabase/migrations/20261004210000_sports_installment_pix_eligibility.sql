-- Eligibility controls new plans only; existing installments are unchanged.
ALTER TABLE sports_product_config
    ADD COLUMN IF NOT EXISTS installment_pix_enabled BOOLEAN NOT NULL DEFAULT FALSE;
