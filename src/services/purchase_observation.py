"""Durable invalidations and replenishment episodes, strictly observation-only.

Entry points OWN a fresh transaction/connection: never call from stock writes.
One transaction consumes a bounded batch using a consistent snapshot. Failure
leaves events pending; no process-local queues, leases or notification side effects.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json

from src.services.purchase_forecast import purchase_forecast
from src.utils import local_today


def _now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _transaction(db, snapshot=True):
    if db.is_postgres:
        from psycopg2.extensions import TRANSACTION_STATUS_IDLE
        if db.conn.get_transaction_status() != TRANSACTION_STATUS_IDLE:
            raise RuntimeError("Use uma conexão sem transação operacional aberta")
        db.execute("BEGIN ISOLATION LEVEL " + ("REPEATABLE READ" if snapshot else "READ COMMITTED"))
    else:
        if db.conn.in_transaction:
            raise RuntimeError("Use uma conexão sem transação operacional aberta")
        db.execute("BEGIN IMMEDIATE")
    with db:
        locked = not db.is_postgres or db.execute(
            "SELECT pg_try_advisory_xact_lock(1196442452,2)"
        ).fetchone()[0]
        yield locked


def _insert(db, table, columns, rows, conflict=""):
    if not rows:
        return
    # Also fits older SQLite builds with a 999-variable statement limit.
    chunk_size = 900 // len(columns)
    if len(rows) > chunk_size:
        for offset in range(0, len(rows), chunk_size):
            _insert(db, table, columns, rows[offset:offset + chunk_size], conflict)
        return
    placeholders = "(" + ",".join("?" for _ in columns) + ")"
    db.execute(
        f"INSERT INTO {table}({','.join(columns)}) VALUES "
        + ",".join(placeholders for _ in rows) + " " + conflict,
        tuple(value for row in rows for value in row),
    )


def activate_observation(db):
    """Explicit, idempotent activation; existing products start silently.

    Serializes activation with database writes just once to establish an exact
    baseline boundary. Forecasts are NOT run here; the baseline is queued.
    """
    with _transaction(db, snapshot=False) as locked:
        if not locked:
            return {"busy": True}
        if db.is_postgres:
            db.execute("SET LOCAL lock_timeout = '1000ms'")
            db.execute("SET LOCAL statement_timeout = '10000ms'")
            # Wait for pre-activation source writes to commit, then block new
            # writes until baseline invalidations and enabled flag commit.
            db.execute("LOCK TABLE products,sales,sale_items,sale_item_deliveries,"
                       "sale_item_delivery_restorations,sale_cancellations IN SHARE MODE")
        control = db.execute("SELECT enabled FROM purchase_observation_control WHERE id=1").fetchone()
        if control["enabled"]:
            return {"activated": False}
        # On reactivation invalidate everything, retaining prior episodes.
        db.execute("INSERT INTO purchase_observation_events(product_id,source_table,operation) "
                   "SELECT id,'baseline','ACTIVATE' FROM products")
        db.execute("UPDATE purchase_observation_control SET enabled=1,activated_at=? WHERE id=1", (_now(),))
        return {"activated": True}


def process_observation(db, batch_size=100):
    """Process at most 100 invalidations, not the full catalog.

    Current-month rollover queues one durable invalidation per product once.
    Duplicate invalidations coalesce; transient states between runs are not
    reconstructed. All episodes are permanently non-notifiable in this stage.
    """
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 100:
        raise ValueError("batch_size deve estar entre 1 e 100")
    with _transaction(db) as locked:
        if not locked:
            return {"busy": True, "processed": 0}
        control = db.execute("SELECT enabled,policy_month FROM purchase_observation_control WHERE id=1").fetchone()
        if not control["enabled"]:
            return {"enabled": False, "processed": 0}
        month = local_today().strftime("%Y-%m")
        if control["policy_month"] != month:
            if control["policy_month"] is not None:
                db.execute("INSERT INTO purchase_observation_events(product_id,source_table,operation) "
                           "SELECT id,'policy','MONTH_ROLLOVER' FROM products")
            db.execute("UPDATE purchase_observation_control SET policy_month=? WHERE id=1", (month,))
        events = db.execute(
            "SELECT id,product_id FROM purchase_observation_events WHERE processed_at IS NULL ORDER BY id LIMIT ?",
            (batch_size,),
        ).fetchall()
        if not events:
            return {"enabled": True, "processed": 0, "products": 0}
        latest = {}
        for event in events:
            latest[event["product_id"]] = event["id"]
        ids = sorted(latest)
        marks = ",".join("?" for _ in ids)
        previous = {row["product_id"]: dict(row) for row in db.execute(
            f"SELECT * FROM purchase_replenishment_state WHERE product_id IN ({marks})", tuple(ids)
        ).fetchall()}
        forecast = purchase_forecast(db, month, 3, 10, product_ids=ids)
        products = {p["product_id"]: p for p in forecast["products"]}
        states, episodes, transitions, closing = [], [], [], []
        now = _now()
        for pid in ids:
            product = products.get(pid)
            # Deleted / sports products close their former need; inactive
            # products already have zero suggestion under the existing rules.
            snapshot = product or {"product_id": pid, "excluded_or_deleted": True}
            fingerprint = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
            needed = product["net_required_units"] if product else 0
            suggested = product["suggested_purchase_units"] if product else 0
            stock = product["current_stock_units"] if product else 0
            needs = int(suggested > 0)
            old = previous.get(pid)
            was_needed = bool(old and old["needs_restock"])
            number = old["episode_number"] if old else 0
            action = None
            if not old:
                action = "baseline"
                number = needs
            elif needs and not was_needed:
                number += 1
                action = "opened"
            elif was_needed and not needs:
                closing.append(pid)
                action = "closed"
            elif needs and (old["fingerprint"] != fingerprint or old["policy_month"] != month):
                action = "updated"
            if needs:
                episodes.append((pid, number, "baseline" if not old else "transition", now, now, needed, suggested))
            if action:
                transitions.append((pid, number, action, latest[pid], fingerprint, now))
            states.append((pid, needs, number, month, stock, needed, suggested, fingerprint, latest[pid], now))
        if closing:
            db.execute("UPDATE purchase_replenishment_episodes SET closed_at=?,updated_at=? "
                       "WHERE closed_at IS NULL AND product_id IN (" + ",".join("?" for _ in closing) + ")",
                       (now, now, *closing))
        _insert(db, "purchase_replenishment_episodes",
                ("product_id", "episode_number", "origin", "opened_at", "updated_at", "needed_units", "suggested_units"),
                episodes, "ON CONFLICT(product_id,episode_number) DO UPDATE SET "
                "updated_at=excluded.updated_at,needed_units=excluded.needed_units,suggested_units=excluded.suggested_units")
        _insert(db, "purchase_replenishment_transitions",
                ("product_id", "episode_number", "action", "source_event_id", "snapshot", "created_at"), transitions)
        columns = ("product_id", "needs_restock", "episode_number", "policy_month", "stock_units", "needed_units",
                   "suggested_units", "fingerprint", "last_event_id", "updated_at")
        _insert(db, "purchase_replenishment_state", columns, states,
                "ON CONFLICT(product_id) DO UPDATE SET " + ",".join(f"{c}=excluded.{c}" for c in columns[1:]))
        event_ids = [e["id"] for e in events]
        db.execute("UPDATE purchase_observation_events SET processed_at=? WHERE id IN ("
                   + ",".join("?" for _ in event_ids) + ")", (now, *event_ids))
        return {"enabled": True, "processed": len(events), "products": len(ids), "transitions": len(transitions)}
