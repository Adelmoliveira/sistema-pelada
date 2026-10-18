"""DDL for explicit initialization only; never called by request connections.

PostgreSQL's checked-in migration is generated from ``schema_sql(True)``.
Capture only appends invalidations: forecasts never run in these triggers.
"""


SOURCES = {
    "products": ("stock,active,category,units_per_case,created_at", "direct"),
    "sales": ("delivered_at,payment_status", "sale"),
    "sale_items": ("sale_id,product_id,quantity", "item"),
    "sale_item_deliveries": ("sale_item_id,quantity,delivered_at", "delivery"),
    "sale_item_delivery_restorations": ("delivery_id,quantity", "restoration"),
    "sale_cancellations": ("sale_id", "cancellation"),
}


def _selection(kind, row):
    if kind == "direct":
        return f"SELECT {row}.id AS product_id"
    if kind == "sale":
        sale = f"{row}.id"
    elif kind in ("item", "cancellation"):
        sale = f"{row}.sale_id"
    elif kind == "delivery":
        sale = f"(SELECT sale_id FROM sale_items WHERE id={row}.sale_item_id)"
    else:
        sale = (f"(SELECT i.sale_id FROM sale_items i JOIN sale_item_deliveries d "
                f"ON d.sale_item_id=i.id WHERE d.id={row}.delivery_id)")
    # Delivery existence switches the legacy fallback for the ENTIRE sale.
    selected = f"SELECT product_id FROM sale_items WHERE sale_id={sale}"
    if kind == "item":
        selected += f" UNION SELECT {row}.product_id"
    return selected


def schema_sql(postgres=False):
    identity = "BIGSERIAL PRIMARY KEY" if postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    sql = f"""
CREATE TABLE IF NOT EXISTS purchase_observation_control (
 id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
 activated_at TEXT, policy_month TEXT
);
INSERT INTO purchase_observation_control(id) VALUES(1) ON CONFLICT(id) DO NOTHING;
CREATE TABLE IF NOT EXISTS purchase_observation_events (
 id {identity}, product_id INTEGER NOT NULL, source_table TEXT NOT NULL,
 source_id INTEGER, operation TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_purchase_events_pending ON purchase_observation_events(id) WHERE processed_at IS NULL;
CREATE TABLE IF NOT EXISTS purchase_replenishment_state (
 id {identity}, product_id INTEGER NOT NULL UNIQUE,
 needs_restock INTEGER NOT NULL CHECK(needs_restock IN (0,1)), episode_number INTEGER NOT NULL CHECK(episode_number>=0),
 policy_month TEXT NOT NULL, stock_units INTEGER NOT NULL, needed_units INTEGER NOT NULL,
 suggested_units INTEGER NOT NULL, fingerprint TEXT NOT NULL, last_event_id BIGINT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS purchase_replenishment_episodes (
 id {identity}, product_id INTEGER NOT NULL, episode_number INTEGER NOT NULL CHECK(episode_number>0),
 origin TEXT NOT NULL CHECK(origin IN ('baseline','transition')),
 notifiable INTEGER NOT NULL DEFAULT 0 CHECK(notifiable=0),
 opened_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT,
 needed_units INTEGER NOT NULL, suggested_units INTEGER NOT NULL,
 UNIQUE(product_id,episode_number)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_purchase_one_open_episode
 ON purchase_replenishment_episodes(product_id) WHERE closed_at IS NULL;
CREATE TABLE IF NOT EXISTS purchase_replenishment_transitions (
 id {identity}, product_id INTEGER NOT NULL, episode_number INTEGER NOT NULL,
 action TEXT NOT NULL CHECK(action IN ('baseline','opened','updated','closed')),
 source_event_id BIGINT NOT NULL, snapshot TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(product_id,source_event_id)
);
"""
    # OLD invalidation must precede cascading deletes / FK reassignment. NEW
    # invalidation follows the write so newly assigned sale items are included.
    for table, (columns, kind) in SOURCES.items():
        for operation, timing, row in (("INSERT", "AFTER", "NEW"),
                                       ("UPDATE", "BEFORE", "OLD"),
                                       ("UPDATE", "AFTER", "NEW"),
                                       ("DELETE", "BEFORE", "OLD")):
            name = f"po_{table}_{operation.lower()}_{row.lower()}"
            event = f"UPDATE OF {columns}" if operation == "UPDATE" else operation
            changed = " OR ".join(
                f"NEW.{c} IS DISTINCT FROM OLD.{c}" if postgres else f"NEW.{c} IS NOT OLD.{c}"
                for c in columns.split(",")
            )
            condition = (f"({changed}) AND " if operation == "UPDATE" else "")
            condition += "(SELECT enabled FROM purchase_observation_control WHERE id=1)=1"
            selection = _selection(kind, row)
            insert = ("INSERT INTO purchase_observation_events(product_id,source_table,source_id,operation) "
                      f"SELECT DISTINCT affected.product_id,'{table}',{row}.id,'{operation}' "
                      f"FROM ({selection}) AS affected WHERE affected.product_id IS NOT NULL;")
            if postgres:
                sql += f"""
CREATE OR REPLACE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $po$
BEGIN
 IF {condition} THEN
  {insert}
 END IF;
 RETURN {"OLD" if operation == "DELETE" else "NEW"};
END;
$po$;
DROP TRIGGER IF EXISTS {name} ON {table};
CREATE TRIGGER {name} {timing} {event} ON {table}
 FOR EACH ROW EXECUTE FUNCTION {name}();
"""
            else:
                sql += f"""
CREATE TRIGGER IF NOT EXISTS {name} {timing} {event} ON {table}
 FOR EACH ROW WHEN {condition}
BEGIN
 {insert}
END;
"""
    if postgres:
        # Direct owner/BYPASSRLS connection, as in the existing application.
        # Never expose observation/audit data through Supabase's public API.
        for table in ("purchase_observation_control", "purchase_observation_events",
                      "purchase_replenishment_state", "purchase_replenishment_episodes",
                      "purchase_replenishment_transitions"):
            sql += f"\nALTER TABLE {table} ENABLE ROW LEVEL SECURITY;\n"
    return sql
