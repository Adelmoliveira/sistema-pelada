-- Observation only. No stock backfill or notification; disabled until explicit activation.

CREATE TABLE IF NOT EXISTS purchase_observation_control (
 id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
 activated_at TEXT, policy_month TEXT
);
INSERT INTO purchase_observation_control(id) VALUES(1) ON CONFLICT(id) DO NOTHING;
CREATE TABLE IF NOT EXISTS purchase_observation_events (
 id BIGSERIAL PRIMARY KEY, product_id INTEGER NOT NULL, source_table TEXT NOT NULL,
 source_id INTEGER, operation TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_purchase_events_pending ON purchase_observation_events(id) WHERE processed_at IS NULL;
CREATE TABLE IF NOT EXISTS purchase_replenishment_state (
 id BIGSERIAL PRIMARY KEY, product_id INTEGER NOT NULL UNIQUE,
 needs_restock INTEGER NOT NULL CHECK(needs_restock IN (0,1)), episode_number INTEGER NOT NULL CHECK(episode_number>=0),
 policy_month TEXT NOT NULL, stock_units INTEGER NOT NULL, needed_units INTEGER NOT NULL,
 suggested_units INTEGER NOT NULL, fingerprint TEXT NOT NULL, last_event_id BIGINT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS purchase_replenishment_episodes (
 id BIGSERIAL PRIMARY KEY, product_id INTEGER NOT NULL, episode_number INTEGER NOT NULL CHECK(episode_number>0),
 origin TEXT NOT NULL CHECK(origin IN ('baseline','transition')),
 notifiable INTEGER NOT NULL DEFAULT 0 CHECK(notifiable=0),
 opened_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT,
 needed_units INTEGER NOT NULL, suggested_units INTEGER NOT NULL,
 UNIQUE(product_id,episode_number)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_purchase_one_open_episode
 ON purchase_replenishment_episodes(product_id) WHERE closed_at IS NULL;
CREATE TABLE IF NOT EXISTS purchase_replenishment_transitions (
 id BIGSERIAL PRIMARY KEY, product_id INTEGER NOT NULL, episode_number INTEGER NOT NULL,
 action TEXT NOT NULL CHECK(action IN ('baseline','opened','updated','closed')),
 source_event_id BIGINT NOT NULL, snapshot TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(product_id,source_event_id)
);

CREATE OR REPLACE FUNCTION po_products_insert_new() RETURNS trigger LANGUAGE plpgsql AS $po$
BEGIN
 IF (SELECT enabled FROM purchase_observation_control WHERE id=1)=1 THEN
  INSERT INTO purchase_observation_events(product_id,source_table,source_id,operation) SELECT DISTINCT affected.product_id,'products',NEW.id,'INSERT' FROM (SELECT NEW.id AS product_id) AS affected WHERE affected.product_id IS NOT NULL;
 END IF;
 RETURN NEW;
END;
$po$;
DROP TRIGGER IF EXISTS po_products_insert_new ON products;
CREATE TRIGGER po_products_insert_new AFTER INSERT ON products
 FOR EACH ROW EXECUTE FUNCTION po_products_insert_new();

CREATE OR REPLACE FUNCTION po_products_update_old() RETURNS trigger LANGUAGE plpgsql AS $po$
BEGIN
 IF (NEW.stock IS DISTINCT FROM OLD.stock OR NEW.active IS DISTINCT FROM OLD.active OR NEW.category IS DISTINCT FROM OLD.category OR NEW.units_per_case IS DISTINCT FROM OLD.units_per_case OR NEW.created_at IS DISTINCT FROM OLD.created_at) AND (SELECT enabled FROM purchase_observation_control WHERE id=1)=1 THEN
  INSERT INTO purchase_observation_events(product_id,source_table,source_id,operation) SELECT DISTINCT affected.product_id,'products',OLD.id,'UPDATE' FROM (SELECT OLD.id AS product_id) AS affected WHERE affected.product_id IS NOT NULL;
 END IF;
 RETURN NEW;
END;
$po$;
DROP TRIGGER IF EXISTS po_products_update_old ON products;
CREATE TRIGGER po_products_update_old BEFORE UPDATE OF stock,active,category,units_per_case,created_at ON products
 FOR EACH ROW EXECUTE FUNCTION po_products_update_old();

CREATE OR REPLACE FUNCTION po_products_update_new() RETURNS trigger LANGUAGE plpgsql AS $po$
BEGIN
 IF (NEW.stock IS DISTINCT FROM OLD.stock OR NEW.active IS DISTINCT FROM OLD.active OR NEW.category IS DISTINCT FROM OLD.category OR NEW.units_per_case IS DISTINCT FROM OLD.units_per_case OR NEW.created_at IS DISTINCT FROM OLD.created_at) AND (SELECT enabled FROM purchase_observation_control WHERE id=1)=1 THEN
  INSERT INTO purchase_observation_events(product_id,source_table,source_id,operation) SELECT DISTINCT affected.product_id,'products',NEW.id,'UPDATE' FROM (SELECT NEW.id AS product_id) AS affected WHERE affected.product_id IS NOT NULL;
 END IF;
 RETURN NEW;
END;
$po$;
DROP TRIGGER IF EXISTS po_products_update_new ON products;
CREATE TRIGGER po_products_update_new AFTER UPDATE OF stock,active,category,units_per_case,created_at ON products
 FOR EACH ROW EXECUTE FUNCTION po_products_update_new();

CREATE OR REPLACE FUNCTION po_products_delete_old() RETURNS trigger LANGUAGE plpgsql AS $po$
BEGIN
 IF (SELECT enabled FROM purchase_observation_control WHERE id=1)=1 THEN
  INSERT INTO purchase_observation_events(product_id,source_table,source_id,operation) SELECT DISTINCT affected.product_id,'products',OLD.id,'DELETE' FROM (SELECT OLD.id AS product_id) AS affected WHERE affected.product_id IS NOT NULL;
 END IF;
 RETURN OLD;
END;
$po$;
DROP TRIGGER IF EXISTS po_products_delete_old ON products;
CREATE TRIGGER po_products_delete_old BEFORE DELETE ON products
 FOR EACH ROW EXECUTE FUNCTION po_products_delete_old();

CREATE OR REPLACE FUNCTION po_sales_insert_new() RETURNS trigger LANGUAGE plpgsql AS $po$
BEGIN
 IF (SELECT enabled FROM purchase_observation_control WHERE id=1)=1 THEN
  INSERT INTO purchase_observation_events(product_id,source_table,source_id,operation) SELECT DISTINCT affected.product_id,'sales',NEW.id,'INSERT' FROM (SELECT product_id FROM sale_items WHERE sale_id=NEW.id) AS affected WHERE affected.product_id IS NOT NULL;
 END IF;
 RETURN NEW;
END;
$po$;
DROP TRIGGER IF EXISTS po_sales_insert_new ON sales;
CREATE TRIGGER po_sales_insert_new AFTER INSERT ON sales
 FOR EACH ROW EXECUTE FUNCTION po_sales_insert_new();

CREATE OR REPLACE FUNCTION po_sales_update_old() RETURNS trigger LANGUAGE plpgsql AS $po$
BEGIN
 IF (NEW.delivered_at IS DISTINCT FROM OLD.delivered_at OR NEW.payment_status IS DISTINCT FROM OLD.payment_status) AND (SELECT enabled FROM purchase_observation_control WHERE id=1)=1 THEN
  INSERT INTO purchase_observation_events(product_id,source_table,source_id,operation) SELECT DISTINCT affected.product_id,'sales',OLD.id,'UPDATE' FROM (SELECT product_id FROM sale_items WHERE sale_id=OLD.id) AS affected WHERE affected.product_id IS NOT NULL;
 END IF;
 RETURN NEW;
END;
$po$;
DROP TRIGGER IF EXISTS po_sales_update_old ON sales;
CREATE TRIGGER po_sales_update_old BEFORE UPDATE OF delivered_at,payment_status ON sales
 FOR EACH ROW EXECUTE FUNCTION po_sales_update_old();

CREATE OR REPLACE FUNCTION po_sales_update_new() RETURNS trigger LANGUAGE plpgsql AS $po$
BEGIN
 IF (NEW.delivered_at IS DISTINCT FROM OLD.delivered_at OR NEW.payment_status IS DISTINCT FROM OLD.payment_status) AND (SELECT enabled FROM purchase_observation_control WHERE id=1)=1 THEN
  INSERT INTO purchase_observation_events(product_id,source_table,source_id,operation) SELECT DISTINCT affected.product_id,'sales',NEW.id,'UPDATE' FROM (SELECT product_id FROM sale_items WHERE sale_id=NEW.id) AS affected WHERE affected.product_id IS NOT NULL;
 END IF;
 RETURN NEW;
END;
$po$;
DROP TRIGGER IF EXISTS po_sales_update_new ON sales;
CREATE TRIGGER po_sales_update_new AFTER UPDATE OF delivered_at,payment_status ON sales
 FOR EACH ROW EXECUTE FUNCTION po_sales_update_new();

CREATE OR REPLACE FUNCTION po_sales_delete_old() RETURNS trigger LANGUAGE plpgsql AS $po$
BEGIN
 IF (SELECT enabled FROM purchase_observation_control WHERE id=1)=1 THEN
  INSERT INTO purchase_observation_events(product_id,source_table,source_id,operation) SELECT DISTINCT affected.product_id,'sales',OLD.id,'DELETE' FROM (SELECT product_id FROM sale_items WHERE sale_id=OLD.id) AS affected WHERE affected.product_id IS NOT NULL;
 END IF;
 RETURN OLD;
END;
$po$;
DROP TRIGGER IF EXISTS po_sales_delete_old ON sales;
CREATE TRIGGER po_sales_delete_old BEFORE DELETE ON sales
 FOR EACH ROW EXECUTE FUNCTION po_sales_delete_old();

CREATE OR REPLACE FUNCTION po_sale_items_insert_new() RETURNS trigger LANGUAGE plpgsql AS $po$
BEGIN
 IF (SELECT enabled FROM purchase_observation_control WHERE id=1)=1 THEN
  INSERT INTO purchase_observation_events(product_id,source_table,source_id,operation) SELECT DISTINCT affected.product_id,'sale_items',NEW.id,'INSERT' FROM (SELECT product_id FROM sale_items WHERE sale_id=NEW.sale_id UNION SELECT NEW.product_id) AS affected WHERE affected.product_id IS NOT NULL;
 END IF;
 RETURN NEW;
END;
$po$;
DROP TRIGGER IF EXISTS po_sale_items_insert_new ON sale_items;
CREATE TRIGGER po_sale_items_insert_new AFTER INSERT ON sale_items
 FOR EACH ROW EXECUTE FUNCTION po_sale_items_insert_new();

CREATE OR REPLACE FUNCTION po_sale_items_update_old() RETURNS trigger LANGUAGE plpgsql AS $po$
BEGIN
 IF (NEW.sale_id IS DISTINCT FROM OLD.sale_id OR NEW.product_id IS DISTINCT FROM OLD.product_id OR NEW.quantity IS DISTINCT FROM OLD.quantity) AND (SELECT enabled FROM purchase_observation_control WHERE id=1)=1 THEN
  INSERT INTO purchase_observation_events(product_id,source_table,source_id,operation) SELECT DISTINCT affected.product_id,'sale_items',OLD.id,'UPDATE' FROM (SELECT product_id FROM sale_items WHERE sale_id=OLD.sale_id UNION SELECT OLD.product_id) AS affected WHERE affected.product_id IS NOT NULL;
 END IF;
 RETURN NEW;
END;
$po$;
DROP TRIGGER IF EXISTS po_sale_items_update_old ON sale_items;
CREATE TRIGGER po_sale_items_update_old BEFORE UPDATE OF sale_id,product_id,quantity ON sale_items
 FOR EACH ROW EXECUTE FUNCTION po_sale_items_update_old();

CREATE OR REPLACE FUNCTION po_sale_items_update_new() RETURNS trigger LANGUAGE plpgsql AS $po$
BEGIN
 IF (NEW.sale_id IS DISTINCT FROM OLD.sale_id OR NEW.product_id IS DISTINCT FROM OLD.product_id OR NEW.quantity IS DISTINCT FROM OLD.quantity) AND (SELECT enabled FROM purchase_observation_control WHERE id=1)=1 THEN
  INSERT INTO purchase_observation_events(product_id,source_table,source_id,operation) SELECT DISTINCT affected.product_id,'sale_items',NEW.id,'UPDATE' FROM (SELECT product_id FROM sale_items WHERE sale_id=NEW.sale_id UNION SELECT NEW.product_id) AS affected WHERE affected.product_id IS NOT NULL;
 END IF;
 RETURN NEW;
END;
$po$;
DROP TRIGGER IF EXISTS po_sale_items_update_new ON sale_items;
CREATE TRIGGER po_sale_items_update_new AFTER UPDATE OF sale_id,product_id,quantity ON sale_items
 FOR EACH ROW EXECUTE FUNCTION po_sale_items_update_new();

CREATE OR REPLACE FUNCTION po_sale_items_delete_old() RETURNS trigger LANGUAGE plpgsql AS $po$
BEGIN
 IF (SELECT enabled FROM purchase_observation_control WHERE id=1)=1 THEN
  INSERT INTO purchase_observation_events(product_id,source_table,source_id,operation) SELECT DISTINCT affected.product_id,'sale_items',OLD.id,'DELETE' FROM (SELECT product_id FROM sale_items WHERE sale_id=OLD.sale_id UNION SELECT OLD.product_id) AS affected WHERE affected.product_id IS NOT NULL;
 END IF;
 RETURN OLD;
END;
$po$;
DROP TRIGGER IF EXISTS po_sale_items_delete_old ON sale_items;
CREATE TRIGGER po_sale_items_delete_old BEFORE DELETE ON sale_items
 FOR EACH ROW EXECUTE FUNCTION po_sale_items_delete_old();

CREATE OR REPLACE FUNCTION po_sale_item_deliveries_insert_new() RETURNS trigger LANGUAGE plpgsql AS $po$
BEGIN
 IF (SELECT enabled FROM purchase_observation_control WHERE id=1)=1 THEN
  INSERT INTO purchase_observation_events(product_id,source_table,source_id,operation) SELECT DISTINCT affected.product_id,'sale_item_deliveries',NEW.id,'INSERT' FROM (SELECT product_id FROM sale_items WHERE sale_id=(SELECT sale_id FROM sale_items WHERE id=NEW.sale_item_id)) AS affected WHERE affected.product_id IS NOT NULL;
 END IF;
 RETURN NEW;
END;
$po$;
DROP TRIGGER IF EXISTS po_sale_item_deliveries_insert_new ON sale_item_deliveries;
CREATE TRIGGER po_sale_item_deliveries_insert_new AFTER INSERT ON sale_item_deliveries
 FOR EACH ROW EXECUTE FUNCTION po_sale_item_deliveries_insert_new();

CREATE OR REPLACE FUNCTION po_sale_item_deliveries_update_old() RETURNS trigger LANGUAGE plpgsql AS $po$
BEGIN
 IF (NEW.sale_item_id IS DISTINCT FROM OLD.sale_item_id OR NEW.quantity IS DISTINCT FROM OLD.quantity OR NEW.delivered_at IS DISTINCT FROM OLD.delivered_at) AND (SELECT enabled FROM purchase_observation_control WHERE id=1)=1 THEN
  INSERT INTO purchase_observation_events(product_id,source_table,source_id,operation) SELECT DISTINCT affected.product_id,'sale_item_deliveries',OLD.id,'UPDATE' FROM (SELECT product_id FROM sale_items WHERE sale_id=(SELECT sale_id FROM sale_items WHERE id=OLD.sale_item_id)) AS affected WHERE affected.product_id IS NOT NULL;
 END IF;
 RETURN NEW;
END;
$po$;
DROP TRIGGER IF EXISTS po_sale_item_deliveries_update_old ON sale_item_deliveries;
CREATE TRIGGER po_sale_item_deliveries_update_old BEFORE UPDATE OF sale_item_id,quantity,delivered_at ON sale_item_deliveries
 FOR EACH ROW EXECUTE FUNCTION po_sale_item_deliveries_update_old();

CREATE OR REPLACE FUNCTION po_sale_item_deliveries_update_new() RETURNS trigger LANGUAGE plpgsql AS $po$
BEGIN
 IF (NEW.sale_item_id IS DISTINCT FROM OLD.sale_item_id OR NEW.quantity IS DISTINCT FROM OLD.quantity OR NEW.delivered_at IS DISTINCT FROM OLD.delivered_at) AND (SELECT enabled FROM purchase_observation_control WHERE id=1)=1 THEN
  INSERT INTO purchase_observation_events(product_id,source_table,source_id,operation) SELECT DISTINCT affected.product_id,'sale_item_deliveries',NEW.id,'UPDATE' FROM (SELECT product_id FROM sale_items WHERE sale_id=(SELECT sale_id FROM sale_items WHERE id=NEW.sale_item_id)) AS affected WHERE affected.product_id IS NOT NULL;
 END IF;
 RETURN NEW;
END;
$po$;
DROP TRIGGER IF EXISTS po_sale_item_deliveries_update_new ON sale_item_deliveries;
CREATE TRIGGER po_sale_item_deliveries_update_new AFTER UPDATE OF sale_item_id,quantity,delivered_at ON sale_item_deliveries
 FOR EACH ROW EXECUTE FUNCTION po_sale_item_deliveries_update_new();

CREATE OR REPLACE FUNCTION po_sale_item_deliveries_delete_old() RETURNS trigger LANGUAGE plpgsql AS $po$
BEGIN
 IF (SELECT enabled FROM purchase_observation_control WHERE id=1)=1 THEN
  INSERT INTO purchase_observation_events(product_id,source_table,source_id,operation) SELECT DISTINCT affected.product_id,'sale_item_deliveries',OLD.id,'DELETE' FROM (SELECT product_id FROM sale_items WHERE sale_id=(SELECT sale_id FROM sale_items WHERE id=OLD.sale_item_id)) AS affected WHERE affected.product_id IS NOT NULL;
 END IF;
 RETURN OLD;
END;
$po$;
DROP TRIGGER IF EXISTS po_sale_item_deliveries_delete_old ON sale_item_deliveries;
CREATE TRIGGER po_sale_item_deliveries_delete_old BEFORE DELETE ON sale_item_deliveries
 FOR EACH ROW EXECUTE FUNCTION po_sale_item_deliveries_delete_old();

CREATE OR REPLACE FUNCTION po_sale_item_delivery_restorations_insert_new() RETURNS trigger LANGUAGE plpgsql AS $po$
BEGIN
 IF (SELECT enabled FROM purchase_observation_control WHERE id=1)=1 THEN
  INSERT INTO purchase_observation_events(product_id,source_table,source_id,operation) SELECT DISTINCT affected.product_id,'sale_item_delivery_restorations',NEW.id,'INSERT' FROM (SELECT product_id FROM sale_items WHERE sale_id=(SELECT i.sale_id FROM sale_items i JOIN sale_item_deliveries d ON d.sale_item_id=i.id WHERE d.id=NEW.delivery_id)) AS affected WHERE affected.product_id IS NOT NULL;
 END IF;
 RETURN NEW;
END;
$po$;
DROP TRIGGER IF EXISTS po_sale_item_delivery_restorations_insert_new ON sale_item_delivery_restorations;
CREATE TRIGGER po_sale_item_delivery_restorations_insert_new AFTER INSERT ON sale_item_delivery_restorations
 FOR EACH ROW EXECUTE FUNCTION po_sale_item_delivery_restorations_insert_new();

CREATE OR REPLACE FUNCTION po_sale_item_delivery_restorations_update_old() RETURNS trigger LANGUAGE plpgsql AS $po$
BEGIN
 IF (NEW.delivery_id IS DISTINCT FROM OLD.delivery_id OR NEW.quantity IS DISTINCT FROM OLD.quantity) AND (SELECT enabled FROM purchase_observation_control WHERE id=1)=1 THEN
  INSERT INTO purchase_observation_events(product_id,source_table,source_id,operation) SELECT DISTINCT affected.product_id,'sale_item_delivery_restorations',OLD.id,'UPDATE' FROM (SELECT product_id FROM sale_items WHERE sale_id=(SELECT i.sale_id FROM sale_items i JOIN sale_item_deliveries d ON d.sale_item_id=i.id WHERE d.id=OLD.delivery_id)) AS affected WHERE affected.product_id IS NOT NULL;
 END IF;
 RETURN NEW;
END;
$po$;
DROP TRIGGER IF EXISTS po_sale_item_delivery_restorations_update_old ON sale_item_delivery_restorations;
CREATE TRIGGER po_sale_item_delivery_restorations_update_old BEFORE UPDATE OF delivery_id,quantity ON sale_item_delivery_restorations
 FOR EACH ROW EXECUTE FUNCTION po_sale_item_delivery_restorations_update_old();

CREATE OR REPLACE FUNCTION po_sale_item_delivery_restorations_update_new() RETURNS trigger LANGUAGE plpgsql AS $po$
BEGIN
 IF (NEW.delivery_id IS DISTINCT FROM OLD.delivery_id OR NEW.quantity IS DISTINCT FROM OLD.quantity) AND (SELECT enabled FROM purchase_observation_control WHERE id=1)=1 THEN
  INSERT INTO purchase_observation_events(product_id,source_table,source_id,operation) SELECT DISTINCT affected.product_id,'sale_item_delivery_restorations',NEW.id,'UPDATE' FROM (SELECT product_id FROM sale_items WHERE sale_id=(SELECT i.sale_id FROM sale_items i JOIN sale_item_deliveries d ON d.sale_item_id=i.id WHERE d.id=NEW.delivery_id)) AS affected WHERE affected.product_id IS NOT NULL;
 END IF;
 RETURN NEW;
END;
$po$;
DROP TRIGGER IF EXISTS po_sale_item_delivery_restorations_update_new ON sale_item_delivery_restorations;
CREATE TRIGGER po_sale_item_delivery_restorations_update_new AFTER UPDATE OF delivery_id,quantity ON sale_item_delivery_restorations
 FOR EACH ROW EXECUTE FUNCTION po_sale_item_delivery_restorations_update_new();

CREATE OR REPLACE FUNCTION po_sale_item_delivery_restorations_delete_old() RETURNS trigger LANGUAGE plpgsql AS $po$
BEGIN
 IF (SELECT enabled FROM purchase_observation_control WHERE id=1)=1 THEN
  INSERT INTO purchase_observation_events(product_id,source_table,source_id,operation) SELECT DISTINCT affected.product_id,'sale_item_delivery_restorations',OLD.id,'DELETE' FROM (SELECT product_id FROM sale_items WHERE sale_id=(SELECT i.sale_id FROM sale_items i JOIN sale_item_deliveries d ON d.sale_item_id=i.id WHERE d.id=OLD.delivery_id)) AS affected WHERE affected.product_id IS NOT NULL;
 END IF;
 RETURN OLD;
END;
$po$;
DROP TRIGGER IF EXISTS po_sale_item_delivery_restorations_delete_old ON sale_item_delivery_restorations;
CREATE TRIGGER po_sale_item_delivery_restorations_delete_old BEFORE DELETE ON sale_item_delivery_restorations
 FOR EACH ROW EXECUTE FUNCTION po_sale_item_delivery_restorations_delete_old();

CREATE OR REPLACE FUNCTION po_sale_cancellations_insert_new() RETURNS trigger LANGUAGE plpgsql AS $po$
BEGIN
 IF (SELECT enabled FROM purchase_observation_control WHERE id=1)=1 THEN
  INSERT INTO purchase_observation_events(product_id,source_table,source_id,operation) SELECT DISTINCT affected.product_id,'sale_cancellations',NEW.id,'INSERT' FROM (SELECT product_id FROM sale_items WHERE sale_id=NEW.sale_id) AS affected WHERE affected.product_id IS NOT NULL;
 END IF;
 RETURN NEW;
END;
$po$;
DROP TRIGGER IF EXISTS po_sale_cancellations_insert_new ON sale_cancellations;
CREATE TRIGGER po_sale_cancellations_insert_new AFTER INSERT ON sale_cancellations
 FOR EACH ROW EXECUTE FUNCTION po_sale_cancellations_insert_new();

CREATE OR REPLACE FUNCTION po_sale_cancellations_update_old() RETURNS trigger LANGUAGE plpgsql AS $po$
BEGIN
 IF (NEW.sale_id IS DISTINCT FROM OLD.sale_id) AND (SELECT enabled FROM purchase_observation_control WHERE id=1)=1 THEN
  INSERT INTO purchase_observation_events(product_id,source_table,source_id,operation) SELECT DISTINCT affected.product_id,'sale_cancellations',OLD.id,'UPDATE' FROM (SELECT product_id FROM sale_items WHERE sale_id=OLD.sale_id) AS affected WHERE affected.product_id IS NOT NULL;
 END IF;
 RETURN NEW;
END;
$po$;
DROP TRIGGER IF EXISTS po_sale_cancellations_update_old ON sale_cancellations;
CREATE TRIGGER po_sale_cancellations_update_old BEFORE UPDATE OF sale_id ON sale_cancellations
 FOR EACH ROW EXECUTE FUNCTION po_sale_cancellations_update_old();

CREATE OR REPLACE FUNCTION po_sale_cancellations_update_new() RETURNS trigger LANGUAGE plpgsql AS $po$
BEGIN
 IF (NEW.sale_id IS DISTINCT FROM OLD.sale_id) AND (SELECT enabled FROM purchase_observation_control WHERE id=1)=1 THEN
  INSERT INTO purchase_observation_events(product_id,source_table,source_id,operation) SELECT DISTINCT affected.product_id,'sale_cancellations',NEW.id,'UPDATE' FROM (SELECT product_id FROM sale_items WHERE sale_id=NEW.sale_id) AS affected WHERE affected.product_id IS NOT NULL;
 END IF;
 RETURN NEW;
END;
$po$;
DROP TRIGGER IF EXISTS po_sale_cancellations_update_new ON sale_cancellations;
CREATE TRIGGER po_sale_cancellations_update_new AFTER UPDATE OF sale_id ON sale_cancellations
 FOR EACH ROW EXECUTE FUNCTION po_sale_cancellations_update_new();

CREATE OR REPLACE FUNCTION po_sale_cancellations_delete_old() RETURNS trigger LANGUAGE plpgsql AS $po$
BEGIN
 IF (SELECT enabled FROM purchase_observation_control WHERE id=1)=1 THEN
  INSERT INTO purchase_observation_events(product_id,source_table,source_id,operation) SELECT DISTINCT affected.product_id,'sale_cancellations',OLD.id,'DELETE' FROM (SELECT product_id FROM sale_items WHERE sale_id=OLD.sale_id) AS affected WHERE affected.product_id IS NOT NULL;
 END IF;
 RETURN OLD;
END;
$po$;
DROP TRIGGER IF EXISTS po_sale_cancellations_delete_old ON sale_cancellations;
CREATE TRIGGER po_sale_cancellations_delete_old BEFORE DELETE ON sale_cancellations
 FOR EACH ROW EXECUTE FUNCTION po_sale_cancellations_delete_old();

ALTER TABLE purchase_observation_control ENABLE ROW LEVEL SECURITY;

ALTER TABLE purchase_observation_events ENABLE ROW LEVEL SECURITY;

ALTER TABLE purchase_replenishment_state ENABLE ROW LEVEL SECURITY;

ALTER TABLE purchase_replenishment_episodes ENABLE ROW LEVEL SECURITY;

ALTER TABLE purchase_replenishment_transitions ENABLE ROW LEVEL SECURITY;
