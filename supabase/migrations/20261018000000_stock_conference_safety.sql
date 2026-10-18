-- Additive only: historical conferences and balances are not modified.
ALTER TABLE products ADD COLUMN IF NOT EXISTS stock_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE stock_conference_items ADD COLUMN IF NOT EXISTS expected_revision INTEGER;
ALTER TABLE stock_adjustments ADD COLUMN IF NOT EXISTS conference_id INTEGER REFERENCES stock_conferences(id);
CREATE TABLE IF NOT EXISTS stock_conference_applications (
    id SERIAL PRIMARY KEY,
    conference_id INTEGER NOT NULL UNIQUE REFERENCES stock_conferences(id),
    user_id INTEGER REFERENCES users(id),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE OR REPLACE FUNCTION bump_product_stock_revision() RETURNS trigger AS $$
BEGIN
    IF NEW.stock IS DISTINCT FROM OLD.stock OR NEW.active IS DISTINCT FROM OLD.active OR NEW.category IS DISTINCT FROM OLD.category THEN
        NEW.stock_revision := OLD.stock_revision + 1;
    ELSE
        NEW.stock_revision := OLD.stock_revision;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS products_stock_revision ON products;
CREATE TRIGGER products_stock_revision BEFORE UPDATE ON products
FOR EACH ROW EXECUTE FUNCTION bump_product_stock_revision();
