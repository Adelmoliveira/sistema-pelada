"""Bounded operational entry points. No notifications and no operational writes."""
from datetime import datetime, timezone, timedelta
import time
import uuid

from src.services.purchase_observation import _transaction, _now, process_observation
from src.utils import local_today
from src.catalog import SPORTS_MATERIAL_CATEGORY


class BudgetDatabase:
    """Bound every PostgreSQL statement by the remaining invocation budget."""
    def __init__(self, db, seconds=15):
        self.db = db
        self.deadline = time.monotonic() + seconds

    def __getattr__(self, key):
        return getattr(self.db, key)

    def __enter__(self):
        self.db.__enter__()
        return self

    def __exit__(self, *args):
        return self.db.__exit__(*args)

    def execute(self, sql, params=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('monitor budget')
        if self.db.is_postgres and not sql.startswith('BEGIN'):
            self.db.execute("SET LOCAL statement_timeout = '" + str(max(1, int(remaining * 1000))) + "ms'")
            self.db.execute("SET LOCAL lock_timeout = '1000ms'")
        return self.db.execute(sql, params)


def deactivate_observation(db):
    with _transaction(db, snapshot=False) as locked:
        if locked:
            db.execute('UPDATE purchase_observation_control SET enabled=0 WHERE id=1')
        return {'deactivated': bool(locked), 'busy': not locked}


def run_monitor(db, *, source, requested_by=None, request_key=None):
    if source not in ('manual', 'periodic'):
        raise ValueError('Invalid source')
    now = datetime.now(timezone.utc)
    slot = now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0)
    key = request_key or ('periodic:' + slot.isoformat() if source == 'periodic' else 'manual:' + uuid.uuid4().hex)
    if not db.is_postgres:
        db.execute("PRAGMA busy_timeout=1000")
    limited = BudgetDatabase(db)
    with _transaction(limited, snapshot=False) as locked:
        if not locked:
            return {'busy': True, 'processed': 0}
        old = db.execute('SELECT outcome FROM purchase_monitor_runs WHERE request_key=?', (key,)).fetchone()
        if old:
            return {'duplicate': True, 'failed': old['outcome'] == 'failed', 'processed': 0}
        db.execute('INSERT INTO purchase_monitor_runs(request_key,source,requested_by,started_at,outcome) '
                   "VALUES(?,?,?,?,'running')", (key, source, requested_by, _now()))
    try:
        if not db.is_postgres:
            db.conn.set_progress_handler(lambda: int(time.monotonic() >= limited.deadline), 1000)
        try:
            result = process_observation(limited)
        finally:
            if not db.is_postgres:
                db.conn.set_progress_handler(None, 0)
        outcome = 'busy' if result.get('busy') else 'success'
        error = None
    except Exception as exc:
        db.rollback()
        result = {'failed': True, 'processed': 0}
        outcome = 'failed'
        # Persist only the exception category; messages may contain credentials/SQL.
        error = type(exc).__name__
    # A fresh short transaction records success/failure even after processing rollback.
    try:
        # Telemetry updates only this invocation's row, not observation state.
        # Do not contend for the consumer mutex just to record a busy result.
        with db:
            if db.is_postgres:
                db.execute("SET LOCAL statement_timeout = '2000ms'")
                db.execute("SET LOCAL lock_timeout = '500ms'")
            pending = db.execute('SELECT COUNT(*) FROM purchase_observation_events WHERE processed_at IS NULL').fetchone()[0]
            db.execute('UPDATE purchase_monitor_runs SET finished_at=?,outcome=?,processed_events=?,pending_events=?,products=?,error_code=? '
                       'WHERE request_key=?', (_now(), outcome, result.get('processed', 0), pending,
                                               result.get('products', 0), error, key))
        return dict(result, pending=pending)
    except Exception:
        db.rollback()
        return dict(result, telemetry_pending=True)


def monitor_status(db, before=None):
    control = dict(db.execute('SELECT * FROM purchase_observation_control WHERE id=1').fetchone())
    pending = db.execute('SELECT COUNT(*) FROM purchase_observation_events WHERE processed_at IS NULL').fetchone()[0]
    monitored = db.execute('SELECT COUNT(*) FROM purchase_replenishment_state s JOIN products p ON p.id=s.product_id '
                           "WHERE p.active=1 AND COALESCE(p.category,'') <> ?",
                           (SPORTS_MATERIAL_CATEGORY,)).fetchone()[0]
    runs = [dict(row) for row in db.execute('SELECT * FROM purchase_monitor_runs ORDER BY id DESC LIMIT 10').fetchall()]
    last = runs[0] if runs else None
    stale = bool(last and last['outcome'] == 'running' and
                 datetime.fromisoformat(last['started_at']) < datetime.now(timezone.utc) - timedelta(minutes=5))
    state = 'desativado' if not control['enabled'] else (
        'falha' if stale or (last and last['outcome'] == 'failed') else
        'processamento pendente' if pending or not last or last['outcome'] == 'running' or
        control['policy_month'] != local_today().strftime('%Y-%m') else 'ativo')
    episodes = [dict(row) for row in db.execute(
        'SELECT e.*,p.name FROM purchase_replenishment_episodes e LEFT JOIN products p ON p.id=e.product_id '
        'WHERE e.id < ? ORDER BY e.id DESC LIMIT 21', (before or 9223372036854775807,)).fetchall()]
    return dict(control=control, pending=pending, monitored=monitored, runs=runs, last=last,
                state=state, episodes=episodes[:20], next_cursor=episodes[19]['id'] if len(episodes)>20 else None)
