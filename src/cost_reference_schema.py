"""Explicit DDL for append-only managerial cost references and invalidation.

No valuation/stock calculations in triggers. Historical references are never
updated or deleted. Revision changes conservatively invalidate old approvals.
"""


def table_sql(postgres=False):
    identity = 'BIGSERIAL PRIMARY KEY' if postgres else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    return f"""
CREATE TABLE IF NOT EXISTS stock_cost_references (
 id {identity}, product_id INTEGER NOT NULL, product_name TEXT NOT NULL,
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
"""


def schema_sql(postgres=False):
    sql = table_sql(postgres)
    if postgres:
        sql = ('ALTER TABLE products ADD COLUMN IF NOT EXISTS cost_reference_revision BIGINT NOT NULL DEFAULT 0;\n'
               + sql)
        sql += """
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
"""
    else:
        sql += """
CREATE TRIGGER IF NOT EXISTS products_cost_reference_revision
 AFTER UPDATE OF stock,active,category ON products FOR EACH ROW
 WHEN NEW.stock IS NOT OLD.stock OR NEW.active IS NOT OLD.active OR NEW.category IS NOT OLD.category
BEGIN
 UPDATE products SET cost_reference_revision=OLD.cost_reference_revision+1 WHERE id=NEW.id;
END;
CREATE TRIGGER IF NOT EXISTS stock_cost_references_no_update BEFORE UPDATE ON stock_cost_references
BEGIN SELECT RAISE(ABORT,'Cost reference history is immutable'); END;
CREATE TRIGGER IF NOT EXISTS stock_cost_references_no_delete BEFORE DELETE ON stock_cost_references
BEGIN SELECT RAISE(ABORT,'Cost reference history is immutable'); END;
"""
    for table in ('restocks', 'restock_corrections', 'stock_adjustments'):
        for operation, timing, row in (('INSERT', 'AFTER', 'NEW'), ('UPDATE', 'BEFORE', 'OLD'),
                                       ('UPDATE', 'AFTER', 'NEW'), ('DELETE', 'BEFORE', 'OLD')):
            name = f'cr_{table}_{operation.lower()}_{row.lower()}'
            selection = (f'SELECT product_id FROM restocks WHERE id={row}.restock_id'
                         if table == 'restock_corrections' else f'SELECT {row}.product_id AS product_id')
            update = ('UPDATE products SET cost_reference_revision=cost_reference_revision+1 '
                      f'WHERE id IN ({selection});')
            if postgres:
                sql += f"""
CREATE OR REPLACE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $cost$
BEGIN
 {update}
 RETURN {'OLD' if operation == 'DELETE' else 'NEW'};
END;
$cost$;
DROP TRIGGER IF EXISTS {name} ON {table};
CREATE TRIGGER {name} {timing} {operation} ON {table}
 FOR EACH ROW EXECUTE FUNCTION {name}();
"""
            else:
                sql += f"""
CREATE TRIGGER IF NOT EXISTS {name} {timing} {operation} ON {table} FOR EACH ROW
BEGIN {update} END;
"""
    return sql
