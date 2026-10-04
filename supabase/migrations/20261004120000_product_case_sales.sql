-- Reuse products.units_per_case; stock and sale quantities remain in units.
ALTER TABLE products ADD COLUMN IF NOT EXISTS case_sale_enabled INTEGER NOT NULL DEFAULT 0 CHECK(case_sale_enabled IN (0,1));
