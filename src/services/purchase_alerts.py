"""Persistent manager-only alerts. Never called inside an operational transaction."""
from datetime import datetime, timezone, timedelta
from math import ceil
import time
import uuid

from src.catalog import SPORTS_MATERIAL_CATEGORY
from src.services.purchase_observation import _transaction, _now, _insert
from src.services.purchase_monitor import BudgetDatabase
from src.services import manager_push


class AlertDatabase(BudgetDatabase):
    """Bound SQL without three round trips per statement in the alert worker."""
    def execute(self,sql,params=None):
        remaining=self.deadline-time.monotonic()
        if remaining<=0:
            raise TimeoutError('alert budget')
        if self.db.is_postgres and not sql.startswith('BEGIN'):
            milliseconds=max(1,int(remaining*1000))
            self.db.execute(f"SET LOCAL statement_timeout = '{milliseconds}ms'; SET LOCAL lock_timeout = '500ms'")
        return self.db.execute(sql,params)


def _after(seconds):
    return (datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()


def _limits(db):
    if isinstance(db,BudgetDatabase):
        return
    if db.is_postgres:
        db.execute("SET LOCAL statement_timeout = '2000ms'")
        db.execute("SET LOCAL lock_timeout = '500ms'")
    else:
        db.execute('PRAGMA busy_timeout=1000')


def set_enabled(db, enabled, actor):
    with _transaction(db, snapshot=False) as locked:
        if not locked:
            return {'busy':True}
        _limits(db)
        user=db.execute('SELECT role,active FROM users WHERE id=?',(actor,)).fetchone()
        if not user or user['role']!='manager' or not user['active']:
            raise PermissionError('Gestor não autorizado')
        old = db.execute('SELECT * FROM purchase_alert_control WHERE id=1').fetchone()
        monitor = db.execute('SELECT * FROM purchase_observation_control WHERE id=1').fetchone()
        if enabled and not monitor['enabled']:
            raise ValueError('Ative primeiro o monitoramento')
        if bool(old['enabled']) == bool(enabled) and (not enabled or old['monitor_activation'] == monitor['activated_at']):
            return {'enabled':bool(enabled)}
        db.execute('UPDATE purchase_alert_control SET enabled=?,generation=generation+1,monitor_activation=?,updated_at=?,updated_by=? WHERE id=1',
                   (int(enabled),monitor['activated_at'],_now(),actor))
        db.execute("UPDATE purchase_alert_deliveries SET status='cancelled',finished_at=?,error_code='policy_changed' WHERE status IN ('pending','processing')",(_now(),))
        if enabled:
            # Queue one reconciliation, no forecast here. Each first settled product
            # snapshot in this generation is silent, including existing episodes.
            db.execute("INSERT INTO purchase_observation_events(product_id,source_table,operation) SELECT id,'alert_baseline','ACTIVATE' FROM products")
        return {'enabled':bool(enabled)}


def classify(db, batch_size=100):
    with _transaction(db) as locked:
        if not locked:
            return {'busy':True,'classified':0}
        _limits(db)
        ctl = db.execute('SELECT * FROM purchase_alert_control WHERE id=1').fetchone()
        obs = db.execute('SELECT * FROM purchase_observation_control WHERE id=1').fetchone()
        if not obs['enabled'] or (ctl['enabled'] and ctl['monitor_activation'] != obs['activated_at']):
            db.execute('UPDATE purchase_alert_control SET enabled=0 WHERE id=1')
            ctl = dict(ctl); ctl['enabled'] = 0
        if not ctl['enabled']:
            db.execute("UPDATE purchase_alert_deliveries SET status='cancelled',finished_at=?,error_code='disabled' WHERE status IN ('pending','processing')",(_now(),))
            return {'enabled':False,'classified':0}
        rows = db.execute('''SELECT st.*,p.active,p.category,p.units_per_case,e.id AS episode_id,e.origin,
                    c.generation AS old_generation,c.episode_number AS old_episode,c.eligible,
                    c.high_water,c.level
             FROM purchase_replenishment_state st
             LEFT JOIN products p ON p.id=st.product_id
             LEFT JOIN purchase_replenishment_episodes e ON e.product_id=st.product_id AND e.episode_number=st.episode_number
             LEFT JOIN purchase_alert_product_state c ON c.product_id=st.product_id
             WHERE (c.product_id IS NULL OR c.generation<>? OR c.last_event_id<>st.last_event_id)
               AND NOT EXISTS(SELECT 1 FROM purchase_observation_events ev WHERE ev.product_id=st.product_id AND ev.processed_at IS NULL)
             ORDER BY st.product_id LIMIT ?''',(ctl['generation'],batch_size)).fetchall()
        # One batched audience query, no per-product user/catalog queries.
        audience = db.execute("SELECT s.id,s.user_id,s.version FROM manager_push_subscriptions s JOIN users u ON u.id=s.user_id WHERE s.active=1 AND u.active=1 AND u.role='manager'").fetchall() if rows else []
        emitted = 0
        new_alerts, states = [], []
        for r in rows:
            first = r['old_generation'] != ctl['generation']
            eligible = 0 if first else r['eligible']
            high, level = (0,0) if first else (r['high_water'],r['level'])
            kind = None
            valid = r['needs_restock'] and r['active'] and r['category'] != SPORTS_MATERIAL_CATEGORY
            if not first and valid and r['episode_number'] != r['old_episode']:
                eligible = int(r['origin'] == 'transition')
                high, level = r['suggested_units'],0
                if eligible: kind = 'opened'
            elif not first and valid and eligible:
                threshold = max((high+3)//4,max(1,int(r['units_per_case'] or 0)))
                if r['suggested_units']-high >= threshold:
                    high = r['suggested_units']; level += 1; kind = 'worsened'
            elif not valid:
                eligible = 0
            if kind:
                new_alerts.append((r['episode_id'],ctl['generation'],kind,level,high,_now()))
                emitted += 1
            states.append((r['product_id'],ctl['generation'],r['episode_number'],eligible,high,level,r['last_event_id'],_now()))
        _insert(db,"purchase_stock_alerts",("episode_id","generation","kind","level","suggested_units","created_at"),new_alerts,"ON CONFLICT DO NOTHING")
        deliveries=[]
        if new_alerts:
            ids=sorted({row[0] for row in new_alerts})
            marks=','.join('?' for _ in ids)
            known={(r['episode_id'],r['kind'],r['level']):r['id'] for r in db.execute(
                'SELECT id,episode_id,kind,level FROM purchase_stock_alerts WHERE generation=? AND episode_id IN ('+marks+')',
                (ctl['generation'],*ids)).fetchall()}
            alert_ids=[known[(row[0],row[2],row[3])] for row in new_alerts]
            for alert_id in alert_ids:
                deliveries.extend((alert_id,sub['user_id'],sub['id'],sub['version'],_now()) for sub in audience)
            # Supersede unsent older levels without repeating per-product queries.
            db.execute("UPDATE purchase_alert_deliveries SET status='cancelled',finished_at=?,error_code='superseded' WHERE status='pending' AND alert_id IN (SELECT older.id FROM purchase_stock_alerts older JOIN purchase_stock_alerts newer ON newer.episode_id=older.episode_id AND newer.id>older.id WHERE newer.id IN ("+','.join('?' for _ in alert_ids)+'))',(_now(),*alert_ids))
        _insert(db,"purchase_alert_deliveries",("alert_id","user_id","subscription_id","subscription_version","available_at"),deliveries,"ON CONFLICT DO NOTHING")
        columns=("product_id","generation","episode_number","eligible","high_water","level","last_event_id","updated_at")
        _insert(db,"purchase_alert_product_state",columns,states,"ON CONFLICT(product_id) DO UPDATE SET "+','.join(f'{column}=excluded.{column}' for column in columns[1:]))
        db.execute("UPDATE purchase_alert_deliveries SET status='cancelled',finished_at=?,error_code='episode_closed' WHERE status='pending' AND alert_id IN (SELECT a.id FROM purchase_stock_alerts a JOIN purchase_replenishment_episodes e ON e.id=a.episode_id WHERE e.closed_at IS NOT NULL)",(_now(),))
        return {'enabled':True,'classified':len(rows),'alerts':emitted}


JOIN = ''' FROM purchase_alert_deliveries d
 JOIN purchase_stock_alerts a ON a.id=d.alert_id
 JOIN purchase_replenishment_episodes e ON e.id=a.episode_id
 JOIN purchase_alert_product_state c ON c.product_id=e.product_id
 JOIN products p ON p.id=e.product_id
 JOIN manager_push_subscriptions s ON s.id=d.subscription_id
 JOIN users u ON u.id=d.user_id
 CROSS JOIN purchase_alert_control ctl CROSS JOIN purchase_observation_control obs '''
VALID = '''ctl.id=1 AND obs.id=1 AND ctl.enabled=1 AND obs.enabled=1
 AND ctl.monitor_activation=obs.activated_at AND a.generation=ctl.generation
 AND c.generation=ctl.generation AND c.eligible=1 AND c.episode_number=e.episode_number
 AND e.closed_at IS NULL AND p.active=1 AND COALESCE(p.category,'')<>?
 AND u.role='manager' AND u.active=1 AND s.active=1 AND s.user_id=u.id
 AND s.version=d.subscription_version
 AND NOT EXISTS(SELECT 1 FROM push_subscriptions legacy WHERE legacy.endpoint=s.endpoint)'''
FRESH = ' AND NOT EXISTS(SELECT 1 FROM purchase_observation_events ev WHERE ev.product_id=e.product_id AND ev.processed_at IS NULL)'


def _claim(db):
    with _transaction(db, snapshot=False) as locked:
        if not locked: return None
        _limits(db)
        now = _now()
        db.execute("UPDATE purchase_alert_attempts SET outcome='unknown',finished_at=?,error_code='lease_expired' WHERE outcome='started' AND delivery_id IN (SELECT id FROM purchase_alert_deliveries WHERE status='processing' AND lease_until<?)",(now,now))
        db.execute("UPDATE purchase_alert_deliveries SET status=CASE WHEN attempts>=5 THEN 'failed' ELSE 'pending' END,lease_token=NULL,error_code='lease_expired' WHERE status='processing' AND lease_until<?",(now,))
        # Role, consent, generation and closure changes cancel, rather than retry.
        db.execute("UPDATE purchase_alert_deliveries SET status='cancelled',finished_at=?,error_code='ineligible' WHERE status='pending' AND id NOT IN (SELECT d.id"+JOIN+' WHERE '+VALID+')',(now,SPORTS_MATERIAL_CATEGORY))
        db.execute("UPDATE purchase_alert_deliveries SET status='cancelled',finished_at=?,error_code='expired' WHERE status='pending' AND alert_id IN (SELECT id FROM purchase_stock_alerts WHERE created_at<?)",(now,_after(-86400)))
        row = db.execute('SELECT d.*,s.endpoint,s.p256dh,s.auth'+JOIN+" WHERE d.status='pending' AND d.attempts<5 AND d.available_at<=? AND "+VALID+FRESH+' AND NOT EXISTS (SELECT 1 FROM purchase_alert_deliveries busy WHERE busy.subscription_id=d.subscription_id AND busy.status=\'processing\') ORDER BY d.id LIMIT 1',(now,SPORTS_MATERIAL_CATEGORY)).fetchone()
        if not row: return None
        row = dict(row); token=uuid.uuid4().hex; attempt=row['attempts']+1
        # Capture the queue boundary. New events created during the network call
        # must never be swallowed by the consolidated notification.
        boundary = db.execute("SELECT MAX(id) FROM purchase_alert_deliveries WHERE subscription_id=? AND status='pending'", (row['subscription_id'],)).fetchone()[0]
        row['consolidation_boundary'] = boundary or row['id']
        db.execute("UPDATE purchase_alert_deliveries SET status='processing',attempts=?,lease_token=?,lease_until=? WHERE id=?",(attempt,token,_after(90),row['id']))
        db.execute("INSERT INTO purchase_alert_attempts(delivery_id,attempt_number,started_at,outcome) VALUES(?,?,?,'started')",(row['id'],attempt,now))
        row.update(token=token,attempt=attempt)
        return row


def _current_purchase_items(db, *, limit=1000):
    """Read active, eligible needs using the monitor's own suggested units.

    This is deliberately not the monthly forecast: the alert is driven by
    current replenishment episodes and the same state used for eligibility.
    """
    rows = db.execute("""SELECT p.name, p.units_per_case, st.suggested_units
        FROM purchase_replenishment_state st
        JOIN products p ON p.id=st.product_id
        JOIN purchase_replenishment_episodes e
          ON e.product_id=st.product_id AND e.episode_number=st.episode_number
        JOIN purchase_alert_product_state c ON c.product_id=st.product_id
        CROSS JOIN purchase_alert_control ctl
        CROSS JOIN purchase_observation_control obs
        WHERE ctl.id=1 AND obs.id=1 AND ctl.enabled=1 AND obs.enabled=1
          AND ctl.monitor_activation=obs.activated_at
          AND c.generation=ctl.generation AND c.eligible=1
          AND c.episode_number=e.episode_number
          AND e.closed_at IS NULL AND st.needs_restock=1
          AND p.active=1 AND COALESCE(p.category,'')<>?
          AND NOT EXISTS (SELECT 1 FROM purchase_observation_events ev
              WHERE ev.product_id=st.product_id AND ev.processed_at IS NULL)
        ORDER BY p.name, p.id LIMIT ?""", (SPORTS_MATERIAL_CATEGORY, limit)).fetchall()
    items = []
    for row in rows:
        units = max(0, int(row['suggested_units'] or 0))
        per_case = max(0, int(row['units_per_case'] or 0))
        if units <= 0:
            continue
        quantity = ceil(units / per_case) if per_case else units
        items.append({'name': row['name'], 'quantity': quantity,
                      'unit': 'caixa' if per_case and quantity == 1 else
                              'caixas' if per_case else
                              'unidade' if quantity == 1 else 'unidades'})
    return items


def dispatch(db, *, deadline, max_messages=5):
    configuration_error=manager_push.vapid_configuration_error()
    if configuration_error: return {'blocked':configuration_error,'accepted':0}
    accepted=failed=0
    for _ in range(max_messages):
        if time.monotonic()+10 >= deadline: break
        limited=AlertDatabase(db,seconds=min(4,deadline-time.monotonic()-6))
        row=_claim(limited)
        if not row: break
        # Prepare the current purchase list before the final authorization check.
        items=_current_purchase_items(limited)
        db.commit()
        # Fresh READ COMMITTED authorization immediately before external I/O.
        check=limited.execute('SELECT d.id'+JOIN+" WHERE d.id=? AND d.status='processing' AND d.lease_token=? AND "+VALID+FRESH,
                         (row['id'],row['token'],SPORTS_MATERIAL_CATEGORY)).fetchone()
        db.commit()
        outcome,error,code='cancelled','authorization_changed',None
        permanent=False
        if check:
            try:
                response=manager_push.send(row,row['alert_id'],items=items)
                code=getattr(response,'status_code',None)
                if not code or not 200<=code<300: raise RuntimeError('Push not accepted')
                outcome,error='accepted',None; accepted+=1
            except Exception as exc:
                code=getattr(getattr(exc,'response',None),'status_code',None)
                error,permanent=manager_push.error_details(exc); outcome='failed'; failed+=1
        # Network I/O is never within a database transaction or observer lock.
        finishing=AlertDatabase(db,seconds=min(2,max(0.1,deadline-time.monotonic())))
        with finishing:
            finishing.execute('UPDATE purchase_alert_attempts SET finished_at=?,outcome=?,http_status=?,error_code=? WHERE delivery_id=? AND attempt_number=?',
                       (_now(),outcome,code,error,row['id'],row['attempt']))
            terminal=outcome in ('accepted','cancelled') or row['attempt']>=5 or permanent
            status=outcome if terminal else 'pending'
            if permanent:
                finishing.execute('UPDATE manager_push_subscriptions SET active=0,revoked_at=? WHERE id=? AND version=?',(_now(),row['subscription_id'],row['subscription_version']))
            finishing.execute('UPDATE purchase_alert_deliveries SET status=?,available_at=?,finished_at=?,error_code=?,lease_token=NULL WHERE id=? AND lease_token=? AND status=\'processing\'',
                       (status,_after(min(900*2**(row['attempt']-1),7200)),_now() if terminal else None,error,row['id'],row['token']))
            if outcome == 'accepted':
                # This push already includes the active purchase list. Close
                # other queued deliveries for this same device that existed
                # when the list was prepared, but not newer events.
                finishing.execute(
                    "UPDATE purchase_alert_deliveries SET status='cancelled', "
                    "finished_at=?, error_code='consolidated' "
                    "WHERE subscription_id=? AND user_id=? AND status='pending' "
                    "AND id<=? AND alert_id IN ("
                    "SELECT a.id FROM purchase_stock_alerts a "
                    "JOIN purchase_replenishment_episodes e ON e.id=a.episode_id "
                    "JOIN purchase_replenishment_state st ON st.product_id=e.product_id "
                    "JOIN purchase_alert_product_state c ON c.product_id=e.product_id "
                    "WHERE e.closed_at IS NULL AND st.needs_restock=1 "
                    "AND st.episode_number=e.episode_number "
                    "AND c.eligible=1 AND c.episode_number=e.episode_number)",
                    (_now(), row['subscription_id'], row['user_id'],
                     row['consolidation_boundary']))
    return {'accepted':accepted,'failed':failed}


def process_alerts(db, *, deadline):
    """Failure-isolated orchestration, including recovery on duplicate cron calls."""
    if time.monotonic()+3>=deadline:
        return {'deferred':True}
    try:
        if time.monotonic()+12>=deadline:
            result={"deferred":True,"delivery":dispatch(db,deadline=deadline)}
        else:
            classified=classify(AlertDatabase(db,seconds=min(10,deadline-time.monotonic()-10)))
            delivery={"disabled":True,"accepted":0} if classified.get("enabled") is False else dispatch(db,deadline=deadline)
            result=dict(classified,delivery=delivery)
    except Exception as exc:
        db.rollback()
        result={'failed':True,'error_code':type(exc).__name__}
    try:
        with db:
            _limits(db)
            db.execute('UPDATE purchase_alert_control SET last_run_at=?,last_error=? WHERE id=1',
                       (_now(),result.get('error_code')))
    except Exception:
        db.rollback()
        result['telemetry_pending']=True
    return result


def status(db):
    control=dict(db.execute('SELECT * FROM purchase_alert_control WHERE id=1').fetchone())
    managers=db.execute("SELECT COUNT(DISTINCT s.user_id) FROM manager_push_subscriptions s JOIN users u ON u.id=s.user_id WHERE s.active=1 AND u.active=1 AND u.role='manager' AND NOT EXISTS(SELECT 1 FROM push_subscriptions legacy WHERE legacy.endpoint=s.endpoint)").fetchone()[0]
    initializing=db.execute("SELECT COUNT(*) FROM products p LEFT JOIN purchase_alert_product_state c ON c.product_id=p.id WHERE p.active=1 AND COALESCE(p.category,'')<>? AND (c.product_id IS NULL OR c.generation<>?)",(SPORTS_MATERIAL_CATEGORY,control['generation'])).fetchone()[0] if control['enabled'] else 0
    counts={r['status']:r['n'] for r in db.execute('SELECT status,COUNT(*) AS n FROM purchase_alert_deliveries GROUP BY status').fetchall()}
    history=[dict(r) for r in db.execute('''SELECT a.id,a.kind,a.suggested_units,a.created_at,e.product_id,p.name AS product_name,
        COUNT(d.id) AS devices,SUM(CASE WHEN d.status='accepted' THEN 1 ELSE 0 END) AS accepted,
        SUM(CASE WHEN d.status='failed' THEN 1 ELSE 0 END) AS failed
        FROM purchase_stock_alerts a JOIN purchase_replenishment_episodes e ON e.id=a.episode_id
        LEFT JOIN products p ON p.id=e.product_id
        LEFT JOIN purchase_alert_deliveries d ON d.alert_id=a.id
        GROUP BY a.id,a.kind,a.suggested_units,a.created_at,e.product_id,p.name ORDER BY a.id DESC LIMIT 30''').fetchall()]
    attempts=[dict(r) for r in db.execute('SELECT outcome,http_status,error_code,started_at,finished_at FROM purchase_alert_attempts ORDER BY id DESC LIMIT 20').fetchall()]
    return dict(control=control,managers=managers,initializing=initializing,counts=counts,history=history,attempts=attempts,vapid_ready=manager_push.ready())
