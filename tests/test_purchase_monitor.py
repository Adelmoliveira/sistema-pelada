"""Operational monitoring contract on disposable databases only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_purchase_observation as fixtures
import test_purchase_planning as pages
from src.purchase_monitor_schema import schema_sql
from src.services.purchase_monitor import run_monitor, monitor_status, deactivate_observation, BudgetDatabase


class MonitorContract:
    def setup_monitor(self):
        if self.db.is_postgres:
            self.db.execute(schema_sql(True))
            self.db.execute(schema_sql(True))
            self.db.commit()
        else:
            self.db.conn.executescript(schema_sql())
            self.db.conn.executescript(schema_sql())

    def run_monitor(self, **kwargs):
        db = self.connect()
        try:
            return run_monitor(db, source='periodic', **kwargs)
        finally:
            db.close()

    def test_silent_baseline_and_periodic_idempotency(self):
        self.activate()
        result = self.run_monitor(request_key='one')
        self.assertEqual(result['processed'], 2)
        self.assertEqual(result['pending'], 0)
        self.assertTrue(self.run_monitor(request_key='one')['duplicate'])
        self.assertEqual(self.count('purchase_replenishment_episodes', 'notifiable <> 0'), 0)
        self.assertEqual(self.count('purchase_replenishment_episodes', "origin='baseline'"), 2)
        self.assertEqual(self.count('purchase_monitor_runs'), 1)

    def test_failure_rollback_and_next_execution_recovers(self):
        self.activate()
        with patch('src.services.purchase_observation.purchase_forecast', side_effect=RuntimeError('sensitive text')):
            self.assertTrue(self.run_monitor(request_key='fail')['failed'])
        self.assertEqual(self.count('purchase_observation_events','processed_at IS NULL'), 2)
        self.assertEqual(self.count('purchase_replenishment_state'), 0)
        self.assertEqual(self.query('SELECT error_code FROM purchase_monitor_runs')[0]['error_code'], 'RuntimeError')
        self.assertEqual(self.run_monitor(request_key='retry')['processed'], 2)

    def test_disabled_does_not_process_and_reactivation_preserves_history(self):
        self.activate(); self.run_monitor(request_key='initial')
        db = self.connect()
        try:
            self.assertTrue(deactivate_observation(db)['deactivated'])
        finally:
            db.close()
        before = self.count('purchase_replenishment_episodes')
        self.stock(0)
        self.assertEqual(self.run_monitor(request_key='disabled')['processed'], 0)
        self.activate(); self.run_monitor(request_key='reactivate')
        self.assertEqual(self.count('purchase_replenishment_episodes'), before)
        self.assertEqual(self.count('purchase_replenishment_episodes','notifiable <> 0'), 0)

    def test_concurrent_duplicate_requests(self):
        self.activate()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.run_monitor(request_key='shared'), range(2)))
        self.assertEqual(self.count('purchase_monitor_runs'), 1)
        self.assertEqual(sum(r.get('processed',0) for r in results), 2)
        self.assertEqual(self.count('purchase_replenishment_episodes'), 2)

    def test_budget_expiry_leaves_events_pending(self):
        self.activate()
        with patch('src.services.purchase_monitor.process_observation', side_effect=TimeoutError):
            result = self.run_monitor(request_key='timeout')
        self.assertTrue(result['failed'])
        self.assertEqual(self.count('purchase_observation_events','processed_at IS NULL'), 2)

    def test_real_statement_budget_rolls_back_and_is_recorded(self):
        self.activate()
        def slow(db, *args, **kwargs):
            if db.is_postgres:
                db.execute('SELECT pg_sleep(0.5)').fetchone()
            else:
                db.execute('WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<5000000) SELECT SUM(x) FROM n').fetchone()
            raise AssertionError('timeout was not enforced')
        with patch('src.services.purchase_monitor.BudgetDatabase', side_effect=lambda db: BudgetDatabase(db, seconds=0.1)):
            with patch('src.services.purchase_observation.purchase_forecast', side_effect=slow):
                self.assertTrue(self.run_monitor(request_key='real-timeout')['failed'])
        self.assertEqual(self.count('purchase_observation_events','processed_at IS NULL'),2)
        self.assertEqual(self.count('purchase_replenishment_state'),0)
        self.assertEqual(self.run_monitor(request_key='recovered')['processed'],2)

    def test_abandoned_run_is_visible_and_next_slot_recovers(self):
        self.activate()
        with self.db:
            self.db.execute("INSERT INTO purchase_monitor_runs(request_key,source,started_at,outcome) VALUES('abandoned','periodic','2020-01-01T00:00:00+00:00','running')")
        db = self.connect()
        try:
            self.assertEqual(monitor_status(db)['state'],'falha')
        finally:
            db.close()
        self.assertEqual(self.run_monitor(request_key='next-slot')['processed'],2)

    def test_status_enabled_pending_failed_and_disabled(self):
        self.activate()
        def state():
            db = self.connect()
            try:
                with patch('src.services.purchase_monitor.local_today',return_value=date(2026,4,18)):
                    return monitor_status(db)['state']
            finally:
                db.close()
        self.assertEqual(state(),'processamento pendente')
        self.run_monitor(request_key='status')
        self.assertEqual(state(),'ativo')
        with patch('src.services.purchase_monitor.process_observation',side_effect=RuntimeError):
            self.run_monitor(request_key='failed-status')
        self.assertEqual(state(),'falha')
        db = self.connect()
        try:
            deactivate_observation(db)
        finally:
            db.close()
        self.assertEqual(state(),'desativado')

    def test_status_and_month_rollover(self):
        self.activate(); self.run_monitor(request_key='first')
        db = self.connect()
        try:
            self.assertEqual(monitor_status(db)['monitored'],2)
        finally:
            db.close()
        self.stock(17)
        self.assertGreater(self.run_monitor(request_key='stock')['processed'],0)
        self.stock(19)
        self.run_monitor(request_key='stock2')
        self.assertEqual(self.query('SELECT stock_units FROM purchase_replenishment_state WHERE product_id=1')[0]['stock_units'],19)


class SQLiteMonitorTest(MonitorContract, unittest.TestCase):
    connect = fixtures.SQLiteObservationTest.connect
    prepare = fixtures.ObservationContract.prepare
    activate = fixtures.ObservationContract.activate
    stock = fixtures.ObservationContract.stock
    query = fixtures.ObservationContract.query
    count = fixtures.ObservationContract.count

    def setUp(self):
        fixtures.SQLiteObservationTest.setUp(self)
        self.setup_monitor()


@unittest.skipUnless(os.environ.get('GPCTA_TEST_POSTGRES_SOCKET'), 'disposable PostgreSQL required')
class PostgresMonitorTest(SQLiteMonitorTest):
    connect = fixtures.PostgresObservationTest.connect
    cleanup = fixtures.PostgresObservationTest.cleanup

    def setUp(self):
        fixtures.PostgresObservationTest.setUp(self)
        self.setup_monitor()

    def test_migration_matches_generator(self):
        self.assertTrue(Path('supabase/migrations/20261018030000_purchase_monitoring.sql').read_text().endswith(schema_sql(True)))


class MonitorPageTest(unittest.TestCase):
    setUp = pages.PurchasePlanningPageTest.setUp
    login = pages.PurchasePlanningPageTest.login

    def test_manager_controls_baseline_episodes_and_no_push(self):
        self.login(self.manager)
        self.assertIn('desativado', self.client.get('/stock/purchase-monitoring').get_data(as_text=True))
        self.assertEqual(self.client.post('/stock/purchase-monitoring',data={'action':'activate'}).status_code,400)
        self.assertEqual(self.client.post('/stock/purchase-monitoring',data={'action':'activate','confirm':'yes'}).status_code,302)
        with patch('src.services.purchase_observation.local_today',return_value=date(2026,4,18)):
            self.assertEqual(self.client.post('/stock/purchase-monitoring',data={'action':'process'}).status_code,302)
        page = self.client.get('/stock/purchase-monitoring').get_data(as_text=True)
        self.assertIn('Eventos pendentes:',page)
        self.assertIn('Água Planejada',page)
        self.assertIn('Baseline silencioso',page)
        self.assertIn('Desativados',page)
        self.assertEqual(self.client.post('/stock/purchase-monitoring',data={'action':'deactivate'}).status_code,302)

    def test_staff_client_and_csrf(self):
        for user in (self.staff,self.client_user):
            self.login(user)
            self.assertEqual(self.client.get('/stock/purchase-monitoring').status_code,302)
            self.assertEqual(self.client.post('/stock/purchase-monitoring',data={'action':'process'}).status_code,302)
        self.login(self.manager)
        pages.app.config['WTF_CSRF_ENABLED'] = True
        self.assertEqual(self.client.post('/stock/purchase-monitoring',data={'action':'activate','confirm':'yes'}).status_code,303)

    def test_cron_dedicated_secret_post_and_idempotency(self):
        pages.app.config.update(PURCHASE_MONITOR_SECRET='exclusive-test',CRON_SECRET='general-test',WTF_CSRF_ENABLED=True)
        path = '/cron/process-purchase-observation'
        self.assertEqual(self.client.get(path).status_code,405)
        for token in ('','general-test','wrong'):
            self.assertEqual(self.client.post(path,headers={'Authorization':'Bearer '+token}).status_code,401)
        response = self.client.post(path,headers={'Authorization':'Bearer exclusive-test'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['processed'],0)
        self.assertTrue(self.client.post(path,headers={'Authorization':'Bearer exclusive-test'}).json['duplicate'])
