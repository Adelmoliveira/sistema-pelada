"""Same alert contract on disposable SQLite/PostgreSQL. Never contacts push providers."""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import json
import os
from pathlib import Path
from threading import Event
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_purchase_observation as fixtures
import test_purchase_planning as pages
from src.purchase_alert_schema import schema_sql
from src.services.purchase_observation import activate_observation, process_observation
from src.services.purchase_alerts import set_enabled, classify, dispatch, process_alerts, _claim, status
from src.services.manager_push import register, revoke, validate_subscription, vapid_configuration_error


def subscription(suffix='one'):
    from cryptography.hazmat.primitives.asymmetric.ec import derive_private_key, SECP256R1
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    public=derive_private_key(1,SECP256R1()).public_key().public_bytes(Encoding.X962,PublicFormat.UncompressedPoint)
    return {'endpoint':'https://fcm.googleapis.com/fcm/send/'+suffix,
            'keys':{'p256dh':base64.urlsafe_b64encode(public).decode().rstrip('='),
                    'auth':base64.urlsafe_b64encode(b'b'*16).decode().rstrip('=')},'consent':True}


AUX='''CREATE TABLE users(id INTEGER PRIMARY KEY,role TEXT,active INTEGER DEFAULT 1);
CREATE TABLE push_subscriptions(id INTEGER PRIMARY KEY,player_id INTEGER,endpoint TEXT UNIQUE);
INSERT INTO users(id,role) VALUES(1,'manager'),(2,'staff'),(3,'client'),(4,'manager');'''


class AlertContract:
    prepare=fixtures.ObservationContract.prepare
    stock=fixtures.ObservationContract.stock
    query=fixtures.ObservationContract.query
    count=fixtures.ObservationContract.count

    def setup_alerts(self):
        if self.db.is_postgres:
            self.db.execute(AUX); self.db.execute(schema_sql(True)); self.db.commit()
        else: self.db.conn.executescript(AUX+schema_sql())
        mocked=patch('src.services.manager_push.vapid_configuration_error',return_value=None); mocked.start(); self.addCleanup(mocked.stop)
        self.send_mock=patch('src.services.manager_push.send',return_value=SimpleNamespace(status_code=201)).start()
        self.addCleanup(patch.stopall)
        self.call(activate_observation); self.drain()

    def call(self,fn,*args,**kwargs):
        db=self.connect()
        try: return fn(db,*args,**kwargs)
        finally: db.close()

    def drain(self):
        for _ in range(30):
            if not self.call(process_observation)['processed']: break

    def register(self,user=1,suffix='one'):
        db=self.connect()
        try:
            with db: register(db,user,'binding-'+str(user),subscription(suffix))
        finally: db.close()

    def baseline(self):
        self.call(set_enabled,True,1); self.drain(); self.call(classify)

    def change(self,n):
        self.stock(n); self.drain(); return self.call(classify)

    def opening(self):
        self.register(); self.baseline(); self.change(30); self.change(20)

    def dispatch(self):
        return self.call(dispatch,deadline=time.monotonic()+25)

    def test_multiple_products_consolidated_per_device(self):
        self.register()
        with self.db:
            self.db.execute(
                "UPDATE products SET units_per_case=12 WHERE id=1"
            )

        self.baseline()

        with self.db:
            self.db.execute(
                "UPDATE products SET stock=30 WHERE id IN (1,2)"
            )
        self.drain()
        self.call(classify)

        with self.db:
            self.db.execute(
                "UPDATE products SET stock=20 WHERE id IN (1,2)"
            )
        self.drain()
        self.call(classify)

        pending = self.count(
            'purchase_alert_deliveries', "status='pending'"
        )
        self.assertEqual(pending, 2)

        self.dispatch()

        self.assertEqual(self.send_mock.call_count, 1)
        self.assertEqual(
            self.count('purchase_alert_deliveries', "status='accepted'"),
            1
        )
        self.assertEqual(
            self.count('purchase_alert_deliveries', "status='cancelled'"),
            1
        )

        kwargs = self.send_mock.call_args.kwargs
        items = kwargs['items']

        self.assertEqual(len(items), 2)
        self.assertEqual(
            {item['name'] for item in items},
            {'\u00c1gua', 'Suco'}
        )

        self.dispatch()
        self.assertEqual(self.send_mock.call_count, 1)
    def test_consolidation_preserves_new_delivery_during_send(self):
        self.opening()

        original = self.query(
            "SELECT id, alert_id, subscription_id, user_id, "
            "subscription_version FROM purchase_alert_deliveries "
            "WHERE status='pending'"
        )
        self.assertEqual(len(original), 1)

        created = []

        def send_and_enqueue(*args, **kwargs):
            db = self.connect()
            try:
                with db:
                    source = original[0]

                    alert = db.execute(
                        "SELECT episode_id, generation, kind, level, "
                        "suggested_units, created_at "
                        "FROM purchase_stock_alerts WHERE id=?",
                        (source['alert_id'],)
                    ).fetchone()

                    cursor = db.execute(
                        "INSERT INTO purchase_stock_alerts "
                        "(episode_id, generation, kind, level, "
                        "suggested_units, created_at) "
                        "VALUES (?, ?, 'worsened', ?, ?, ?)",
                        (
                            alert['episode_id'],
                            alert['generation'],
                            alert['level'] + 1,
                            alert['suggested_units'] + 1,
                            alert['created_at']
                        )
                    )

                    new_alert_id = cursor.lastrowid

                    cursor = db.execute(
                        "INSERT INTO purchase_alert_deliveries "
                        "(alert_id, user_id, subscription_id, "
                        "subscription_version, status, attempts, available_at) "
                        "SELECT ?, user_id, subscription_id, "
                        "subscription_version, 'pending', 0, available_at "
                        "FROM purchase_alert_deliveries WHERE id=?",
                        (new_alert_id, source['id'])
                    )

                    created.append(cursor.lastrowid)
            finally:
                db.close()

            return SimpleNamespace(status_code=201)

        self.send_mock.side_effect = send_and_enqueue

        # Process only the original delivery in this invocation.
        result = self.call(
            dispatch,
            deadline=time.monotonic() + 25,
            max_messages=1
        )

        self.assertEqual(result['accepted'], 1)
        self.assertEqual(self.send_mock.call_count, 1)
        self.assertEqual(len(created), 1)

        self.assertEqual(
            self.query(
                "SELECT status FROM purchase_alert_deliveries WHERE id=?",
                (created[0],)
            )[0]['status'],
            'pending'
        )

    def test_default_off_migration_reapply_and_legacy_constraint(self):
        self.assertEqual(self.query('SELECT enabled FROM purchase_alert_control')[0]['enabled'],0)
        self.assertEqual(self.count('purchase_stock_alerts'),0)
        if self.db.is_postgres:
            self.db.execute(schema_sql(True)); self.db.commit()
        else: self.db.conn.executescript(schema_sql())
        self.assertEqual(self.count('purchase_replenishment_episodes','notifiable<>0'),0)
        self.assertTrue(Path('supabase/migrations/20261018040000_purchase_intelligent_alerts.sql').read_text().endswith(schema_sql(True)))

    def test_baseline_and_baseline_worsening_are_always_silent(self):
        self.register(); self.baseline()
        self.change(0); self.dispatch()
        self.assertEqual(self.count('purchase_stock_alerts'),0); self.send_mock.assert_not_called()
        self.assertEqual(self.count('purchase_replenishment_episodes','notifiable<>0'),0)

    def test_opening_repeated_cron_and_oscillation(self):
        self.opening(); self.assertEqual(self.count('purchase_stock_alerts'),1)
        self.assertEqual(self.call(status)['history'][0]['product_name'],'Água')
        self.dispatch(); self.dispatch(); self.call(classify)
        self.assertEqual(self.send_mock.call_count,1)
        self.change(19); self.dispatch()
        self.assertEqual(self.count('purchase_stock_alerts'),2)
        self.change(20); self.change(19); self.dispatch()
        self.assertEqual(self.count('purchase_stock_alerts'),2)
        self.assertEqual(self.send_mock.call_count,2)

    def test_twenty_five_percent_and_one_full_case(self):
        with self.db: self.db.execute('UPDATE products SET units_per_case=6 WHERE id=1')
        self.drain(); self.register(); self.baseline(); self.change(30); self.change(10)
        self.assertEqual(self.query('SELECT suggested_units FROM purchase_stock_alerts')[0]['suggested_units'],12)
        self.change(9); self.assertEqual(self.count('purchase_stock_alerts'),2) # ceil to next case = 18
        self.change(10); self.change(9)
        self.assertEqual(self.count('purchase_stock_alerts'),2)
        # high=18: +6 qualifies; packaging and 25% both required.
        self.change(-1); self.assertEqual(self.count('purchase_stock_alerts'),3)

    def test_only_active_managers_and_no_late_subscription_retroactivity(self):
        for uid in (2,3):
            with self.assertRaises(PermissionError): self.register(uid,str(uid))
            with self.assertRaises(PermissionError): self.call(set_enabled,True,uid)
        self.baseline(); self.change(30); self.change(20)
        self.assertEqual(self.count('purchase_stock_alerts'),1)
        self.assertEqual(self.count('purchase_alert_deliveries'),0)
        self.register(); self.dispatch(); self.send_mock.assert_not_called()

    def test_revoked_role_inactive_and_consent_checked_before_send(self):
        self.opening()
        with self.db: self.db.execute("UPDATE users SET role='staff' WHERE id=1")
        self.dispatch(); self.send_mock.assert_not_called()
        self.assertEqual(self.count('purchase_alert_deliveries',"status='cancelled'"),1)

    def test_disable_reenable_and_closure_cancel_pending_without_push(self):
        self.opening(); self.call(set_enabled,False,1); self.dispatch()
        self.send_mock.assert_not_called()
        self.baseline(); self.change(0); self.dispatch(); self.send_mock.assert_not_called()
        self.change(30); self.change(20); self.change(30); self.dispatch()
        self.send_mock.assert_not_called()

    def test_pending_stock_event_defers_obsolete_alert(self):
        self.opening(); self.stock(30)
        self.dispatch(); self.send_mock.assert_not_called()
        self.drain(); self.call(classify); self.dispatch(); self.send_mock.assert_not_called()

    def test_five_failures_backoff_and_no_financial_payload(self):
        self.opening(); self.send_mock.side_effect=TimeoutError('secret must not be persisted')
        for n in range(5):
            self.dispatch()
            with self.db: self.db.execute("UPDATE purchase_alert_deliveries SET available_at='2000-01-01' WHERE status='pending'")
        self.dispatch()
        self.assertEqual(self.send_mock.call_count,5)
        self.assertEqual(self.count('purchase_alert_attempts'),5)
        self.assertEqual(self.count('purchase_alert_deliveries',"status='failed'"),1)
        self.assertEqual(self.query('SELECT error_code FROM purchase_alert_attempts LIMIT 1')[0]['error_code'],'TimeoutError')

    def test_gone_endpoint_revoked_and_no_retry(self):
        self.opening()
        error=RuntimeError('gone'); error.response=SimpleNamespace(status_code=410)
        self.send_mock.side_effect=error
        self.dispatch(); self.dispatch()
        self.assertEqual(self.send_mock.call_count,1)
        self.assertEqual(self.count('manager_push_subscriptions','active=1'),0)

    def test_unknown_http_400_is_sanitized_and_retried(self):
        self.opening()
        error=type('WebPushException',(Exception,),{})('must not be persisted')
        error.response=SimpleNamespace(status_code=400,text='unknown provider error endpoint=https://secret.invalid/token')
        self.send_mock.side_effect=error
        self.dispatch()
        self.assertEqual(self.count('purchase_alert_deliveries',"status='pending'"),1)
        self.assertEqual(self.query('SELECT error_code FROM purchase_alert_attempts')[0]['error_code'],'webpush_http_400')
        self.assertEqual(self.count('manager_push_subscriptions','active=1'),1)
        with self.db: self.db.execute("UPDATE purchase_alert_deliveries SET available_at='2000-01-01'")
        self.dispatch()
        self.assertEqual(self.send_mock.call_count,2)

    def test_proven_permanent_http_400_revokes_without_retry_or_raw_body(self):
        self.opening()
        error=type('WebPushException',(Exception,),{})('secret exception')
        error.response=SimpleNamespace(status_code=400,text='UnauthorizedRegistration endpoint=https://secret.invalid/token')
        self.send_mock.side_effect=error
        self.dispatch(); self.dispatch()
        self.assertEqual(self.send_mock.call_count,1)
        self.assertEqual(self.count('purchase_alert_deliveries',"status='failed'"),1)
        self.assertEqual(self.count('manager_push_subscriptions','active=1'),0)
        code=self.query('SELECT error_code FROM purchase_alert_attempts')[0]['error_code']
        self.assertEqual(code,'push_subscription_unauthorized')
        self.assertNotIn('secret',code)

    def test_crashed_lease_recovers_and_accepted_devices_do_not_repeat(self):
        self.opening(); self.register(4,'four') # new manager must not join the old audience
        row=self.call(_claim); self.assertIsNotNone(row)
        with self.db: self.db.execute("UPDATE purchase_alert_deliveries SET lease_until='2000-01-01'")
        self.dispatch(); self.dispatch()
        self.assertEqual(self.send_mock.call_count,1)
        self.assertEqual(self.count('purchase_alert_attempts',"outcome='unknown'"),1)
        self.assertEqual(self.count('purchase_alert_attempts',"outcome='accepted'"),1)

    def test_concurrent_dispatch_has_one_owner(self):
        self.opening(); started,release=Event(),Event()
        def send(*args, **kwargs):
            started.set(); self.assertTrue(release.wait(5)); return SimpleNamespace(status_code=201)
        self.send_mock.side_effect=send
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.dispatch)
            self.assertTrue(started.wait(5))
            try: self.assertEqual(self.dispatch()['accepted'],0)
            finally: release.set()
            future.result(timeout=5)
        self.assertEqual(self.send_mock.call_count,1)

    def test_parallel_classification_does_not_duplicate(self):
        self.register(); self.baseline(); self.change(30)
        self.stock(20); self.drain()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: self.call(classify),range(2)))
        self.assertEqual(self.count('purchase_stock_alerts'),1)
        self.assertEqual(self.count('purchase_alert_deliveries'),1)

    def test_reassociation_and_revoke_invalidate_old_deliveries(self):
        self.opening(); self.register(4,'one'); self.dispatch()
        self.send_mock.assert_not_called()
        self.assertEqual(self.query('SELECT user_id FROM manager_push_subscriptions')[0]['user_id'],4)
        self.call(lambda db: (revoke(db,user_id=4,binding='binding-4'),db.commit()))
        self.assertEqual(self.count('manager_push_subscriptions','active=1'),0)

    def test_queue_failure_does_not_rollback_monitor_or_stock(self):
        self.register(); self.baseline(); self.change(30); self.stock(20); self.drain()
        with patch('src.services.purchase_alerts._insert',side_effect=RuntimeError('simulated queue failure')):
            self.assertTrue(self.call(process_alerts,deadline=time.monotonic()+25)['failed'])
        self.assertEqual(self.query('SELECT stock FROM products WHERE id=1')[0]['stock'],20)
        self.assertEqual(self.count('purchase_observation_events','processed_at IS NULL'),0)
        self.assertEqual(self.count('purchase_stock_alerts'),0)
        self.call(classify); self.assertEqual(self.count('purchase_stock_alerts'),1)

    def test_database_role_trigger_revokes_and_does_not_restore_consent(self):
        self.register()
        with self.db: self.db.execute('UPDATE users SET active=0 WHERE id=1')
        self.assertEqual(self.count('manager_push_subscriptions','active=1'),0)
        with self.db: self.db.execute('UPDATE users SET active=1 WHERE id=1')
        self.assertEqual(self.count('manager_push_subscriptions','active=1'),0)
        self.register()
        with self.db: self.db.execute("UPDATE users SET role='client' WHERE id=1")
        self.assertEqual(self.count('manager_push_subscriptions','active=1'),0)

    def test_each_device_revalidates_role_after_previous_device(self):
        self.register(1,'two'); self.opening()
        def accepted_then_revoked(*args, **kwargs):
            db=self.connect()
            try:
                with db: db.execute("UPDATE users SET active=0 WHERE id=1")
            finally: db.close()
            return SimpleNamespace(status_code=201)
        self.send_mock.side_effect=accepted_then_revoked
        self.dispatch()
        self.assertEqual(self.send_mock.call_count,1)
        self.assertEqual(self.count('purchase_alert_deliveries',"status='accepted'"),1)
        self.assertEqual(self.count('purchase_alert_deliveries',"status='cancelled'"),1)

    def test_partial_acceptance_retries_only_failed_device(self):
        self.register(1,'two'); self.opening()
        self.send_mock.side_effect=[SimpleNamespace(status_code=201),TimeoutError('network')]
        self.dispatch()
        self.assertEqual(self.count('purchase_alert_deliveries',"status='accepted'"),1)
        with self.db: self.db.execute("UPDATE purchase_alert_deliveries SET available_at='2000-01-01' WHERE status='pending'")
        self.send_mock.side_effect=None
        self.dispatch()
        self.assertEqual(self.send_mock.call_count,3)
        self.assertEqual(self.count('purchase_alert_deliveries',"status='accepted'"),2)

    def test_migration_reapply_keeps_enabled_state_and_audit(self):
        self.opening(); self.dispatch()
        if self.db.is_postgres:
            self.db.execute(schema_sql(True)); self.db.commit()
            protected=self.query("SELECT relrowsecurity FROM pg_class WHERE relnamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema()) AND relname IN ('manager_push_subscriptions','manager_push_revocations','purchase_alert_control','purchase_alert_product_state','purchase_stock_alerts','purchase_alert_deliveries','purchase_alert_attempts')")
            self.assertEqual(len(protected),7)
            self.assertTrue(all(row['relrowsecurity'] for row in protected))
        else: self.db.conn.executescript(schema_sql())
        self.assertEqual(self.query('SELECT enabled FROM purchase_alert_control')[0]['enabled'],1)
        self.assertEqual(self.count('purchase_alert_attempts',"outcome='accepted'"),1)

    def test_disabled_monitor_blocks_even_without_panel_toggle(self):
        self.opening()
        with self.db: self.db.execute('UPDATE purchase_observation_control SET enabled=0 WHERE id=1')
        self.dispatch(); self.send_mock.assert_not_called()
        self.call(activate_observation); self.drain(); self.call(classify)
        self.assertEqual(self.query('SELECT enabled FROM purchase_alert_control')[0]['enabled'],0)

    def test_accepted_provider_but_database_failure_is_explicitly_unknown(self):
        self.opening()
        db=self.connect(); original=db.execute
        def fail(sql,params=None):
            if sql.startswith('UPDATE purchase_alert_attempts SET finished_at'):
                raise RuntimeError('simulated post-send failure')
            return original(sql,params)
        db.execute=fail
        try:
            with self.assertRaises(RuntimeError): dispatch(db,deadline=time.monotonic()+25)
        finally: db.close()
        self.assertEqual(self.send_mock.call_count,1)
        with self.db: self.db.execute("UPDATE purchase_alert_deliveries SET lease_until='2000-01-01'")
        self.dispatch()
        self.assertEqual(self.count('purchase_alert_attempts',"outcome='unknown'"),1)
        self.assertEqual(self.send_mock.call_count,2) # same alert id: device deduplicates presentation
        self.assertEqual(self.send_mock.call_args_list[0].args[1],self.send_mock.call_args_list[1].args[1])

    def test_logout_tombstone_rejects_inflight_enrollment(self):
        from src.services.manager_push import revoke_binding
        self.call(lambda db: (revoke_binding(db,1,'binding-1'),db.commit()))
        with self.assertRaises(PermissionError): self.register()
        self.assertEqual(self.count('manager_push_subscriptions'),0)
        db=self.connect()
        try:
            with db: register(db,1,'fresh-binding',subscription())
        finally: db.close()
        self.assertEqual(self.count('manager_push_subscriptions','active=1'),1)

    def test_device_logout_blocks_inflight_and_preserves_other_device(self):
        from src.services.manager_push import revoke_device
        self.register(4,'other')
        self.call(lambda db: (revoke_device(db,'expired-browser',None),db.commit()))
        db=self.connect()
        try:
            with self.assertRaises(PermissionError):
                with db: register(db,1,'unknown-binding',subscription(),device='expired-browser')
        finally: db.close()
        self.assertEqual(self.count('manager_push_subscriptions','active=1'),1)

    def test_legacy_owner_from_previous_deployment_blocks_delivery(self):
        self.opening()
        with self.db:
            self.db.execute('INSERT INTO push_subscriptions(id,player_id,endpoint) VALUES(1,3,?)',(subscription()['endpoint'],))
        self.dispatch(); self.send_mock.assert_not_called()
        self.assertEqual(self.count('purchase_alert_deliveries',"status='cancelled'"),1)

    def test_no_vapid_holds_pending_without_fake_acceptance(self):
        self.opening()
        with patch('src.services.manager_push.vapid_configuration_error',return_value='vapid_unavailable'): self.dispatch()
        self.send_mock.assert_not_called()
        self.assertEqual(self.count('purchase_alert_attempts'),0)
        self.assertEqual(self.count('purchase_alert_deliveries',"status='pending'"),1)


class SQLiteAlertTest(AlertContract,unittest.TestCase):
    connect=fixtures.SQLiteObservationTest.connect
    def setUp(self):
        fixtures.SQLiteObservationTest.setUp(self); self.setup_alerts()

    def test_baseline_queries_are_batched_not_per_product(self):
        with self.db:
            for pid in range(3,83):
                self.db.execute("INSERT INTO products(id,name,category) VALUES(?,'Batch','Bebidas')",(pid,))
        self.drain(); self.call(set_enabled,True,1); self.drain()
        db=self.connect(); queries=[]
        try:
            db.conn.set_trace_callback(queries.append)
            result=classify(db)
        finally: db.close()
        self.assertEqual(result['classified'],82)
        self.assertLess(len(queries),16)


@unittest.skipUnless(os.environ.get('GPCTA_TEST_POSTGRES_SOCKET'),'disposable local PostgreSQL required')
class PostgresAlertTest(AlertContract,unittest.TestCase):
    connect=fixtures.PostgresObservationTest.connect
    cleanup=fixtures.PostgresObservationTest.cleanup
    def setUp(self):
        fixtures.PostgresObservationTest.setUp(self); self.setup_alerts()


    def test_revocation_uses_users_schema_even_with_shadow_table(self):
        self.register()
        db=self.connect()
        try:
            # A caller-controlled temporary relation must never receive revocation.
            db.execute('CREATE TEMP TABLE manager_push_subscriptions (id BIGSERIAL PRIMARY KEY, user_id INTEGER, active INTEGER, revoked_at TEXT, updated_at TEXT)')
            db.execute('INSERT INTO pg_temp.manager_push_subscriptions(user_id,active) VALUES(1,1)')
            db.execute("UPDATE users SET role='staff' WHERE id=1")
            self.assertEqual(db.execute('SELECT active FROM pg_temp.manager_push_subscriptions').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT active FROM '+self.schema+'.manager_push_subscriptions').fetchone()[0],0)
            db.commit()
        finally: db.close()

    def test_revocation_rolls_back_with_user_update(self):
        self.register()
        db=self.connect()
        try:
            db.execute('UPDATE users SET active=0 WHERE id=1')
            self.assertEqual(db.execute('SELECT active FROM manager_push_subscriptions').fetchone()[0],0)
            db.rollback()
        finally: db.close()
        self.assertEqual(self.query('SELECT active FROM manager_push_subscriptions')[0]['active'],1)
        self.assertEqual(self.query('SELECT active FROM users WHERE id=1')[0]['active'],1)


class ManagerPushRoutesTest(unittest.TestCase):
    setUp=pages.PurchasePlanningPageTest.setUp
    def login(self,user_id):
        pages.PurchasePlanningPageTest.login(self,user_id)
        if user_id == self.manager:
            self.client.post('/notifications/manager-push/prepare',json={})

    def test_manager_consent_and_logout(self):
        self.login(self.manager)
        data=subscription()
        self.assertEqual(self.client.post('/notifications/manager-push/subscribe',json=['invalid']).status_code,400)
        self.assertEqual(self.client.post('/notifications/manager-push/subscribe',json={**data,'consent':False}).status_code,400)
        self.assertEqual(self.client.post('/notifications/manager-push/subscribe',json=data).status_code,200)
        # The private device identity still revokes consent after auth cookie expiry.
        with self.client.session_transaction() as session: session.clear()
        self.client.post('/logout')
        from src.db import connect_sqlite
        db=connect_sqlite(pages.app.config['DATABASE'])
        try: self.assertEqual(db.execute('SELECT active FROM manager_push_subscriptions').fetchone()[0],0)
        finally: db.close()

    def test_vapid_pair_validation_never_returns_key_material(self):
        from cryptography.hazmat.primitives.asymmetric.ec import derive_private_key, SECP256R1
        from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
        key=derive_private_key(1,SECP256R1())
        private=(1).to_bytes(32,'big')
        public=key.public_key().public_bytes(Encoding.X962,PublicFormat.UncompressedPoint)
        encode=lambda value: base64.urlsafe_b64encode(value).decode().rstrip('=')
        self.assertIsNone(vapid_configuration_error(encode(public),encode(private)))
        der=key.private_bytes(Encoding.DER,PrivateFormat.PKCS8,NoEncryption())
        self.assertIsNone(vapid_configuration_error(encode(public),encode(der)))
        mismatch=derive_private_key(2,SECP256R1()).public_key().public_bytes(Encoding.X962,PublicFormat.UncompressedPoint)
        self.assertEqual(vapid_configuration_error(encode(mismatch),encode(private)),'vapid_key_mismatch')
        self.assertEqual(vapid_configuration_error('not-a-key',encode(private)),'vapid_invalid')

    def test_other_roles_and_csrf_rejected(self):
        for uid in (self.staff,self.client_user):
            self.login(uid)
            self.assertEqual(self.client.post('/notifications/manager-push/subscribe',json=subscription(),headers={'Accept':'application/json'}).status_code,403)
        self.login(self.manager); pages.app.config['WTF_CSRF_ENABLED']=True
        self.assertEqual(self.client.post('/notifications/manager-push/subscribe',json=subscription()).status_code,303)

    def test_panel_independent_controls_default_off(self):
        self.login(self.manager)
        page=self.client.get('/stock/purchase-monitoring')
        self.assertEqual(page.status_code,200)
        self.assertIn('Desativados',page.get_data(as_text=True))
        self.client.post('/stock/purchase-monitoring',data={'action':'activate','confirm':'yes'})
        self.assertEqual(self.client.post('/stock/purchase-monitoring',data={'action':'enable_alerts','confirm_alerts':'yes'}).status_code,302)
        self.assertIn('Habilitados',self.client.get('/stock/purchase-monitoring').get_data(as_text=True))
        self.client.post('/stock/purchase-monitoring',data={'action':'disable_alerts'})
        self.assertIn('Desativados',self.client.get('/stock/purchase-monitoring').get_data(as_text=True))

    def test_account_switch_and_legacy_subscription_takeover(self):
        self.login(self.manager)
        self.client.post('/notifications/manager-push/subscribe',json=subscription())
        from src.db import connect_sqlite
        from werkzeug.security import generate_password_hash
        db=connect_sqlite(pages.app.config['DATABASE'])
        try:
            with db: db.execute('UPDATE users SET password_hash=? WHERE id=?',(generate_password_hash('test-pass',method='pbkdf2:sha256:1000'),self.staff))
            self.client.post('/logout')
            response=self.client.post('/login',data={'username':'planning.staff','password':'test-pass'})
            self.assertEqual(response.status_code,303)
            self.assertEqual(db.execute('SELECT active FROM manager_push_subscriptions').fetchone()[0],0)
            self.login(self.manager)
            self.client.post('/notifications/manager-push/subscribe',json=subscription())
            with db:
                player=db.execute("INSERT INTO players(name,war_name) VALUES('Push Client','Push Client')").lastrowid
                db.execute('UPDATE users SET player_id=? WHERE id=?',(player,self.client_user))
            self.login(self.client_user)
            self.assertEqual(self.client.post('/notifications/push/subscribe',json=subscription()).status_code,200)
            self.assertEqual(db.execute('SELECT active FROM manager_push_subscriptions').fetchone()[0],0)
        finally: db.close()

    def test_transport_payload_no_financial_data_and_explicit_limits(self):
        from src.services import manager_push
        row=dict(endpoint=subscription()['endpoint'],**subscription()['keys'])
        with patch.dict(os.environ,{'VAPID_PRIVATE_KEY':'test-secret','VAPID_SUBJECT':'mailto:test@example.invalid'}):
            with patch('pywebpush.webpush',return_value=SimpleNamespace(status_code=201)) as send:
                manager_push.send(row,123)
        args=send.call_args.kwargs
        payload=json.loads(args['data'])
        self.assertEqual(payload['url'],'/stock/purchase-planning')
        self.assertEqual(set(payload),{'title','body','url','alert_id','kind'})
        self.assertEqual(args['timeout'],2); self.assertEqual(args['ttl'],900)

    def test_endpoint_validation_blocks_ssrf_and_bad_keys(self):
        for endpoint in ('http://localhost/x','https://169.254.169.254/x','https://fcm.googleapis.com.evil.test/x','https://fcm.googleapis.com:999/x'):
            with self.assertRaises(ValueError): validate_subscription({**subscription(),'endpoint':endpoint})
        with self.assertRaises(ValueError): validate_subscription({**subscription(),'keys':{'p256dh':'abc','auth':'def'}})
