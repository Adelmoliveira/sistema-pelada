-- Additive managerial estimates only; no balance or acquisition backfill.
ALTER TABLE products ADD COLUMN IF NOT EXISTS cost_reference_revision BIGINT NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS stock_cost_references (
 id BIGSERIAL PRIMARY KEY, product_id INTEGER NOT NULL, product_name TEXT NOT NULL,
 unit_cost_cents INTEGER NOT NULL CHECK(unit_cost_cents>0),
 reference_stock INTEGER NOT NULL CHECK(reference_stock>=0),
 reference_revision BIGINT NOT NULL CHECK(reference_revision>=0),
 previous_reference_id BIGINT NOT NULL DEFAULT 0 CHECK(previous_reference_id>=0),
 responsible_id INTEGER NOT NULL, responsible_name TEXT NOT NULL,
 justification TEXT NOT NULL CHECK(length(trim(justification))>=5),
 value_origin TEXT NOT NULL CHECK(value_origin IN ('catalog_confirmed','manager_entered')),
 support_document TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
 UNIQUE(product_id,previous_reference_id)
);
CREATE INDEX IF NOT EXISTS idx_stock_cost_references_product
 ON stock_cost_references(product_id,id);

CREATE OR REPLACE FUNCTION invalidate_product_cost_reference() RETURNS trigger
LANGUAGE plpgsql AS $cost$
BEGIN
 IF NEW.stock IS DISTINCT FROM OLD.stock OR NEW.active IS DISTINCT FROM OLD.active
    OR NEW.category IS DISTINCT FROM OLD.category THEN
  NEW.cost_reference_revision := OLD.cost_reference_revision+1;
 END IF;
 RETURN NEW;
END;
$cost$;
DROP TRIGGER IF EXISTS products_cost_reference_revision ON products;
CREATE TRIGGER products_cost_reference_revision BEFORE UPDATE OF stock,active,category ON products
 FOR EACH ROW EXECUTE FUNCTION invalidate_product_cost_reference();
CREATE OR REPLACE FUNCTION reject_stock_cost_reference_mutation() RETURNS trigger
LANGUAGE plpgsql AS $cost$
BEGIN
 RAISE EXCEPTION 'Cost reference history is immutable';
END;
$cost$;
DROP TRIGGER IF EXISTS stock_cost_references_immutable ON stock_cost_references;
CREATE TRIGGER stock_cost_references_immutable BEFORE UPDATE OR DELETE ON stock_cost_references
 FOR EACH ROW EXECUTE FUNCTION reject_stock_cost_reference_mutation();
DROP TRIGGER IF EXISTS stock_cost_references_no_truncate ON stock_cost_references;
CREATE TRIGGER stock_cost_references_no_truncate BEFORE TRUNCATE ON stock_cost_references
 FOR EACH STATEMENT EXECUTE FUNCTION reject_stock_cost_reference_mutation();
ALTER TABLE stock_cost_references ENABLE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION cr_restocks_insert_new() RETURNS trigger LANGUAGE plpgsql AS $cost$
BEGIN
 UPDATE products SET cost_reference_revision=cost_reference_revision+1 WHERE id IN (SELECT NEW.product_id AS product_id);
 RETURN NEW;
END;
$cost$;
DROP TRIGGER IF EXISTS cr_restocks_insert_new ON restocks;
CREATE TRIGGER cr_restocks_insert_new AFTER INSERT ON restocks
 FOR EACH ROW EXECUTE FUNCTION cr_restocks_insert_new();

CREATE OR REPLACE FUNCTION cr_restocks_update_old() RETURNS trigger LANGUAGE plpgsql AS $cost$
BEGIN
 UPDATE products SET cost_reference_revision=cost_reference_revision+1 WHERE id IN (SELECT OLD.product_id AS product_id);
 RETURN NEW;
END;
$cost$;
DROP TRIGGER IF EXISTS cr_restocks_update_old ON restocks;
CREATE TRIGGER cr_restocks_update_old BEFORE UPDATE ON restocks
 FOR EACH ROW EXECUTE FUNCTION cr_restocks_update_old();

CREATE OR REPLACE FUNCTION cr_restocks_update_new() RETURNS trigger LANGUAGE plpgsql AS $cost$
BEGIN
 UPDATE products SET cost_reference_revision=cost_reference_revision+1 WHERE id IN (SELECT NEW.product_id AS product_id);
 RETURN NEW;
END;
$cost$;
DROP TRIGGER IF EXISTS cr_restocks_update_new ON restocks;
CREATE TRIGGER cr_restocks_update_new AFTER UPDATE ON restocks
 FOR EACH ROW EXECUTE FUNCTION cr_restocks_update_new();

CREATE OR REPLACE FUNCTION cr_restocks_delete_old() RETURNS trigger LANGUAGE plpgsql AS $cost$
BEGIN
 UPDATE products SET cost_reference_revision=cost_reference_revision+1 WHERE id IN (SELECT OLD.product_id AS product_id);
 RETURN OLD;
END;
$cost$;
DROP TRIGGER IF EXISTS cr_restocks_delete_old ON restocks;
CREATE TRIGGER cr_restocks_delete_old BEFORE DELETE ON restocks
 FOR EACH ROW EXECUTE FUNCTION cr_restocks_delete_old();

CREATE OR REPLACE FUNCTION cr_restock_corrections_insert_new() RETURNS trigger LANGUAGE plpgsql AS $cost$
BEGIN
 UPDATE products SET cost_reference_revision=cost_reference_revision+1 WHERE id IN (SELECT product_id FROM restocks WHERE id=NEW.restock_id);
 RETURN NEW;
END;
$cost$;
DROP TRIGGER IF EXISTS cr_restock_corrections_insert_new ON restock_corrections;
CREATE TRIGGER cr_restock_corrections_insert_new AFTER INSERT ON restock_corrections
 FOR EACH ROW EXECUTE FUNCTION cr_restock_corrections_insert_new();

CREATE OR REPLACE FUNCTION cr_restock_corrections_update_old() RETURNS trigger LANGUAGE plpgsql AS $cost$
BEGIN
 UPDATE products SET cost_reference_revision=cost_reference_revision+1 WHERE id IN (SELECT product_id FROM restocks WHERE id=OLD.restock_id);
 RETURN NEW;
END;
$cost$;
DROP TRIGGER IF EXISTS cr_restock_corrections_update_old ON restock_corrections;
CREATE TRIGGER cr_restock_corrections_update_old BEFORE UPDATE ON restock_corrections
 FOR EACH ROW EXECUTE FUNCTION cr_restock_corrections_update_old();

CREATE OR REPLACE FUNCTION cr_restock_corrections_update_new() RETURNS trigger LANGUAGE plpgsql AS $cost$
BEGIN
 UPDATE products SET cost_reference_revision=cost_reference_revision+1 WHERE id IN (SELECT product_id FROM restocks WHERE id=NEW.restock_id);
 RETURN NEW;
END;
$cost$;
DROP TRIGGER IF EXISTS cr_restock_corrections_update_new ON restock_corrections;
CREATE TRIGGER cr_restock_corrections_update_new AFTER UPDATE ON restock_corrections
 FOR EACH ROW EXECUTE FUNCTION cr_restock_corrections_update_new();

CREATE OR REPLACE FUNCTION cr_restock_corrections_delete_old() RETURNS trigger LANGUAGE plpgsql AS $cost$
BEGIN
 UPDATE products SET cost_reference_revision=cost_reference_revision+1 WHERE id IN (SELECT product_id FROM restocks WHERE id=OLD.restock_id);
 RETURN OLD;
END;
$cost$;
DROP TRIGGER IF EXISTS cr_restock_corrections_delete_old ON restock_corrections;
CREATE TRIGGER cr_restock_corrections_delete_old BEFORE DELETE ON restock_corrections
 FOR EACH ROW EXECUTE FUNCTION cr_restock_corrections_delete_old();

CREATE OR REPLACE FUNCTION cr_stock_adjustments_insert_new() RETURNS trigger LANGUAGE plpgsql AS $cost$
BEGIN
 UPDATE products SET cost_reference_revision=cost_reference_revision+1 WHERE id IN (SELECT NEW.product_id AS product_id);
 RETURN NEW;
END;
$cost$;
DROP TRIGGER IF EXISTS cr_stock_adjustments_insert_new ON stock_adjustments;
CREATE TRIGGER cr_stock_adjustments_insert_new AFTER INSERT ON stock_adjustments
 FOR EACH ROW EXECUTE FUNCTION cr_stock_adjustments_insert_new();

CREATE OR REPLACE FUNCTION cr_stock_adjustments_update_old() RETURNS trigger LANGUAGE plpgsql AS $cost$
BEGIN
 UPDATE products SET cost_reference_revision=cost_reference_revision+1 WHERE id IN (SELECT OLD.product_id AS product_id);
 RETURN NEW;
END;
$cost$;
DROP TRIGGER IF EXISTS cr_stock_adjustments_update_old ON stock_adjustments;
CREATE TRIGGER cr_stock_adjustments_update_old BEFORE UPDATE ON stock_adjustments
 FOR EACH ROW EXECUTE FUNCTION cr_stock_adjustments_update_old();

CREATE OR REPLACE FUNCTION cr_stock_adjustments_update_new() RETURNS trigger LANGUAGE plpgsql AS $cost$
BEGIN
 UPDATE products SET cost_reference_revision=cost_reference_revision+1 WHERE id IN (SELECT NEW.product_id AS product_id);
 RETURN NEW;
END;
$cost$;
DROP TRIGGER IF EXISTS cr_stock_adjustments_update_new ON stock_adjustments;
CREATE TRIGGER cr_stock_adjustments_update_new AFTER UPDATE ON stock_adjustments
 FOR EACH ROW EXECUTE FUNCTION cr_stock_adjustments_update_new();

CREATE OR REPLACE FUNCTION cr_stock_adjustments_delete_old() RETURNS trigger LANGUAGE plpgsql AS $cost$
BEGIN
 UPDATE products SET cost_reference_revision=cost_reference_revision+1 WHERE id IN (SELECT OLD.product_id AS product_id);
 RETURN OLD;
END;
$cost$;
DROP TRIGGER IF EXISTS cr_stock_adjustments_delete_old ON stock_adjustments;
CREATE TRIGGER cr_stock_adjustments_delete_old BEFORE DELETE ON stock_adjustments
 FOR EACH ROW EXECUTE FUNCTION cr_stock_adjustments_delete_old();
