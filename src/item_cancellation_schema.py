"""Explicit setup only; no DDL in request connections."""


def schema_sql(postgres=False):
    identity = 'BIGSERIAL PRIMARY KEY' if postgres else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    sql = f"""
CREATE TABLE IF NOT EXISTS sale_item_cancellations (
 id {identity},
 sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE RESTRICT,
 sale_item_id INTEGER NOT NULL REFERENCES sale_items(id) ON DELETE RESTRICT,
 quantity INTEGER NOT NULL CHECK(quantity>0),
 amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
 stock_restored_quantity INTEGER NOT NULL CHECK(stock_restored_quantity=quantity),
 credit_transaction_id INTEGER NOT NULL UNIQUE REFERENCES bar_credit_transactions(id) ON DELETE RESTRICT,
 canceled_by INTEGER NOT NULL REFERENCES users(id),
 reason TEXT NOT NULL CHECK(length(reason)>=5),
 idempotency_key TEXT NOT NULL UNIQUE,
 canceled_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_item_cancellations_sale ON sale_item_cancellations(sale_id,sale_item_id);
CREATE VIEW sale_items_net AS
 SELECT i.id,i.sale_id,i.product_id,
        i.quantity-COALESCE(c.quantity,0) quantity,
        i.quantity original_quantity,COALESCE(c.quantity,0) canceled_quantity,
        i.unit_price_cents,i.unit_cost_cents
 FROM sale_items i LEFT JOIN (
   SELECT sale_item_id,SUM(quantity) quantity FROM sale_item_cancellations GROUP BY sale_item_id
 ) c ON c.sale_item_id=i.id;
CREATE VIEW sales_net AS
 SELECT s.*,COALESCE(c.amount_cents,0) canceled_cents,
        s.total_cents-COALESCE(c.amount_cents,0) net_total_cents
 FROM sales s LEFT JOIN (
   SELECT sale_id,SUM(amount_cents) amount_cents FROM sale_item_cancellations GROUP BY sale_id
 ) c ON c.sale_id=s.id;
"""
    if postgres:
        sql = sql.replace('CREATE VIEW ', 'CREATE OR REPLACE VIEW ')
        sql += 'ALTER TABLE sale_item_cancellations ENABLE ROW LEVEL SECURITY;\n'
        sql += "REVOKE ALL ON sale_items_net,sales_net FROM PUBLIC;\n"
        sql += """DO $$ DECLARE app_role TEXT; BEGIN
 FOR app_role IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon','authenticated') LOOP
  EXECUTE format('REVOKE ALL ON sale_items_net,sales_net FROM %I', app_role);
 END LOOP;
END $$;
"""
    return sql
