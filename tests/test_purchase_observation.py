"""Same behavioral contract on SQLite and opt-in disposable PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import os
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier, Event
import unittest
from unittest.mock import patch
import uuid

from src.db import DbWrapper
from src.purchase_observation_schema import schema_sql
from src.services.purchase_forecast import purchase_forecast
from src.services.purchase_observation import activate_observation, process_observation
from src.services import purchase_observation as observation


BASE = """
CREATE TABLE products(id INTEGER PRIMARY KEY, name TEXT,category TEXT,units_per_case INTEGER DEFAULT 0,
 stock INTEGER DEFAULT 20,active INTEGER DEFAULT 1,created_at TEXT DEFAULT '2025-01-01');
CREATE TABLE sales(id INTEGER PRIMARY KEY,delivered_at TEXT,payment_status TEXT,payment_method TEXT);
CREATE TABLE sale_items(id INTEGER PRIMARY KEY,sale_id INTEGER REFERENCES sales(id) ON DELETE CASCADE,
 product_id INTEGER REFERENCES products(id),quantity INTEGER);
CREATE TABLE sale_item_deliveries(id INTEGER PRIMARY KEY,
 sale_item_id INTEGER REFERENCES sale_items(id) ON DELETE CASCADE,quantity INTEGER,delivered_at TEXT);
CREATE TABLE sale_item_delivery_restorations(id INTEGER PRIMARY KEY,
 delivery_id INTEGER UNIQUE REFERENCES sale_item_deliveries(id),quantity INTEGER);
CREATE TABLE sale_cancellations(id INTEGER PRIMARY KEY,sale_id INTEGER UNIQUE REFERENCES sales(id) ON DELETE CASCADE);
"""


class ObservationContract:
    def prepare(self):
        clock = patch('src.services.purchase_observation.local_today', return_value=date(2026, 4, 18))
        clock.start()
        self.addCleanup(clock.stop)
        self.db = self.connect()
        self.addCleanup(self.db.close)
        self.db.execute("INSERT INTO products(id,name,category) VALUES(1,'Água','Bebidas'),(2,'Suco','Bebidas')")
        self.db.execute("INSERT INTO sales(id,delivered_at,payment_status) VALUES(1,'2026-03-15 15:00:00','paid')")
        self.db.execute("INSERT INTO sale_items(id,sale_id,product_id,quantity) VALUES(1,1,1,60),(2,1,2,60)")
        self.db.commit()

    def process(self):
        db = self.connect()
        try:
            return process_observation(db)
        finally:
            db.close()

    def activate(self):
        db = self.connect()
        try:
            return activate_observation(db)
        finally:
            db.close()

    def drain(self):
        for _ in range(100):
            result = self.process()
            if not result['processed']:
                return
        self.fail('Queue did not drain')

    def query(self, sql, params=()):
        db = self.connect()
        try:
            return [dict(row) for row in db.execute(sql, params).fetchall()]
        finally:
            db.close()

    def count(self, table, where='1=1'):
        return self.query(f'SELECT COUNT(*) AS n FROM {table} WHERE {where}')[0]['n']

    def stock(self, amount):
        with self.db:
            self.db.execute('UPDATE products SET stock=? WHERE id=1', (amount,))

    def test_disabled_and_activation_baseline_preserve_stock_and_history(self):
        self.stock(17)
        self.assertEqual(self.count('purchase_observation_events'), 0)
        self.assertEqual(self.process()['processed'], 0)
        self.assertTrue(self.activate()['activated'])
        self.assertFalse(self.activate()['activated'])
        self.drain()
        self.assertEqual(self.query('SELECT stock FROM products WHERE id=1')[0]['stock'], 17)
        self.assertEqual(self.count('sale_items'), 2)
        self.assertEqual(self.count('purchase_replenishment_episodes'), 2)
        self.assertEqual(self.count('purchase_replenishment_episodes', "origin='baseline' AND notifiable=0"), 2)
        self.assertEqual(self.count('purchase_replenishment_transitions', "action='baseline'"), 2)
        self.assertEqual(self.process()['processed'], 0)

    def test_open_update_close_reopen_20_17_19(self):
        self.stock(30)
        self.activate(); self.drain()
        self.stock(20); self.drain()  # gross ceil((60/3)*1.10)=22 => need 2
        self.stock(17); self.drain()
        self.stock(19); self.drain()
        state = self.query('SELECT * FROM purchase_replenishment_state WHERE product_id=1')[0]
        self.assertEqual((state['stock_units'], state['needed_units'], state['episode_number']), (19, 3, 1))
        self.assertEqual(self.count('purchase_replenishment_episodes', 'product_id=1'), 1)
        self.stock(30); self.drain()
        self.stock(17); self.drain()
        self.assertEqual(self.count('purchase_replenishment_episodes', 'product_id=1'), 2)
        self.assertEqual(self.count('purchase_replenishment_episodes', 'product_id=1 AND closed_at IS NULL'), 1)
        self.assertEqual(self.count('purchase_replenishment_episodes', 'notifiable<>0'), 0)
        actions = [r['action'] for r in self.query('SELECT action FROM purchase_replenishment_transitions WHERE product_id=1 ORDER BY id')]
        self.assertEqual(actions, ['baseline', 'opened', 'updated', 'updated', 'closed', 'opened'])

    def test_source_rollback_and_failed_worker_remain_recoverable(self):
        self.activate(); self.drain()
        with self.assertRaises(RuntimeError):
            with self.db:
                self.db.execute('UPDATE products SET stock=1 WHERE id=1')
                raise RuntimeError('Abort operational transaction')
        self.assertEqual(self.count('purchase_observation_events', 'processed_at IS NULL'), 0)
        self.assertEqual(self.query('SELECT stock FROM products WHERE id=1')[0]['stock'], 20)
        self.stock(17)
        before = self.count('purchase_observation_events', 'processed_at IS NULL')
        insert = observation._insert
        def fail_after_partial_writes(db, table, *args):
            if table == 'purchase_replenishment_state':
                raise RuntimeError('Worker aborted after episode and transition writes')
            return insert(db, table, *args)
        with patch('src.services.purchase_observation._insert', side_effect=fail_after_partial_writes):
            with self.assertRaises(RuntimeError):
                self.process()
        self.assertEqual(self.count('purchase_observation_events', 'processed_at IS NULL'), before)
        self.assertEqual(self.query('SELECT stock_units FROM purchase_replenishment_state WHERE product_id=1')[0]['stock_units'], 20)
        self.assertEqual(self.query('SELECT needed_units FROM purchase_replenishment_episodes WHERE product_id=1')[0]['needed_units'], 2)
        self.assertEqual(self.count('purchase_replenishment_transitions'), 2)
        self.drain()
        self.assertEqual(self.query('SELECT stock_units FROM purchase_replenishment_state WHERE product_id=1')[0]['stock_units'], 17)

    def test_duplicates_do_not_duplicate_episodes_or_transitions(self):
        self.activate(); self.drain()
        before = self.count('purchase_replenishment_transitions')
        with self.db:
            self.db.execute("INSERT INTO purchase_observation_events(product_id,source_table,operation) VALUES(1,'test','DUPLICATE'),(1,'test','DUPLICATE')")
        self.drain(); self.drain()
        self.assertEqual(self.count('purchase_replenishment_episodes'), 2)
        self.assertEqual(self.count('purchase_replenishment_transitions'), before)

    def test_delivery_switches_all_sale_siblings_and_restoration_closes_need(self):
        self.activate(); self.drain()
        with self.db:
            self.db.execute("INSERT INTO sale_item_deliveries(id,sale_item_id,quantity,delivered_at) VALUES(1,1,90,'2026-03-15 15:00:00')")
        self.assertEqual({r['product_id'] for r in self.query('SELECT product_id FROM purchase_observation_events WHERE processed_at IS NULL')}, {1, 2})
        self.drain()
        states = {r['product_id']:r for r in self.query('SELECT * FROM purchase_replenishment_state')}
        self.assertEqual(states[1]['needed_units'], 13)
        self.assertEqual(states[2]['needs_restock'], 0)  # sale-wide legacy exclusion
        with self.db:
            self.db.execute('INSERT INTO sale_item_delivery_restorations(id,delivery_id,quantity) VALUES(1,1,90)')
        self.drain()
        self.assertEqual(self.count('purchase_replenishment_episodes', 'closed_at IS NULL'), 0)
        with self.db:
            self.db.execute('DELETE FROM sale_item_delivery_restorations WHERE id=1')
        self.drain()
        self.assertEqual(self.count('purchase_replenishment_episodes', 'product_id=1 AND closed_at IS NULL'), 1)

    def test_cancellation_status_item_reassignment_and_cascading_deletion(self):
        self.activate(); self.drain()
        with self.db:
            self.db.execute("UPDATE sales SET payment_status='expired' WHERE id=1")
        self.drain()
        self.assertEqual(self.count('purchase_replenishment_episodes', 'closed_at IS NULL'), 0)
        with self.db:
            self.db.execute("UPDATE sales SET payment_status='paid' WHERE id=1")
            self.db.execute('UPDATE sale_items SET product_id=2 WHERE id=1')
        self.drain()
        self.assertEqual(self.query('SELECT needs_restock FROM purchase_replenishment_state WHERE product_id=1')[0]['needs_restock'], 0)
        with self.db:
            self.db.execute('INSERT INTO sale_cancellations(id,sale_id) VALUES(1,1)')
        self.drain()
        self.assertEqual(self.count('purchase_replenishment_episodes', 'closed_at IS NULL'), 0)
        with self.db:
            self.db.execute('DELETE FROM sale_cancellations WHERE id=1')
        self.drain()
        with self.db:
            self.db.execute('DELETE FROM sales WHERE id=1')
        self.drain()
        self.assertEqual(self.count('sale_items'), 0)
        self.assertEqual(self.count('purchase_replenishment_episodes', 'closed_at IS NULL'), 0)

    def test_inactive_sports_packaging_new_products_and_noop(self):
        self.activate(); self.drain()
        with self.db:
            self.db.execute('UPDATE products SET units_per_case=6 WHERE id=1')
        self.drain()
        self.assertEqual(self.query('SELECT suggested_units FROM purchase_replenishment_state WHERE product_id=1')[0]['suggested_units'], 6)
        with self.db:
            self.db.execute('UPDATE products SET active=0 WHERE id=1')
            self.db.execute("UPDATE products SET category='Material Esportivo' WHERE id=2")
        self.drain()
        self.assertEqual(self.count('purchase_replenishment_episodes', 'closed_at IS NULL'), 0)
        with self.db:
            self.db.execute('UPDATE products SET stock=stock WHERE id=1')
        self.assertEqual(self.count('purchase_observation_events', 'processed_at IS NULL'), 0)
        with self.db:
            self.db.execute("INSERT INTO products(id,name,category) VALUES(3,'Novo','Bebidas')")
        self.drain()
        self.assertEqual(self.count('purchase_replenishment_state'), 3)

    def test_month_rollover_enqueues_once_and_retains_page_policy(self):
        self.activate(); self.drain()
        with patch('src.services.purchase_observation.local_today', return_value=date(2026, 7, 1)):
            self.drain()
            count = self.count('purchase_observation_events', "operation='MONTH_ROLLOVER'")
            self.drain()
        self.assertEqual(count, 2)
        self.assertEqual(self.count('purchase_replenishment_episodes', 'closed_at IS NULL'), 0)

    def test_simultaneous_workers_only_one_episode_and_reconnect_persistence(self):
        self.stock(30)
        self.activate(); self.drain()
        self.stock(17)
        barrier = Barrier(2)
        def worker(_):
            barrier.wait()
            return self.process()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, range(2)))
        self.drain()
        self.assertEqual(sum(r['processed'] for r in results), 2)  # OLD/NEW invalidations
        self.assertEqual(self.count('purchase_replenishment_episodes', 'product_id=1'), 1)
        self.assertEqual(self.query('SELECT stock_units FROM purchase_replenishment_state WHERE product_id=1')[0]['stock_units'], 17)

    def test_unique_open_episode_enforced_in_database(self):
        self.activate(); self.drain()
        with self.assertRaises(Exception):
            with self.db:
                self.db.execute("INSERT INTO purchase_replenishment_episodes(product_id,episode_number,origin,opened_at,updated_at,needed_units,suggested_units) VALUES(1,99,'transition','now','now',1,1)")
        self.assertEqual(self.count('purchase_replenishment_episodes', 'product_id=1 AND closed_at IS NULL'), 1)

    def test_notifiable_constraint_and_operational_transaction_guard(self):
        self.activate(); self.drain()
        with self.assertRaises(Exception):
            with self.db:
                self.db.execute('UPDATE purchase_replenishment_episodes SET notifiable=1')
        self.db.execute('UPDATE products SET stock=17 WHERE id=1')
        with self.assertRaisesRegex(RuntimeError, 'transação operacional'):
            process_observation(self.db)
        self.db.rollback()
        self.assertEqual(self.query('SELECT stock FROM products WHERE id=1')[0]['stock'], 20)


class SQLiteObservationTest(ObservationContract, unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='gpcta-observation-', dir='/tmp')
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / 'test.db')
        db = self.connect()
        db.conn.executescript(BASE + schema_sql())
        db.close()
        self.prepare()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        return DbWrapper(conn)

    def test_scoped_batch_queries_have_constant_count_and_no_full_forecast(self):
        with self.db:
            for pid in range(3, 83):
                self.db.execute("INSERT INTO products(id,name,category) VALUES(?,'Produto','Bebidas')", (pid,))
        self.activate()
        db = self.connect()
        try:
            calls = []
            db.conn.set_trace_callback(calls.append)
            with patch('src.services.purchase_observation.purchase_forecast', wraps=purchase_forecast) as forecast:
                result = process_observation(db)
            self.assertEqual(result['products'], 82)
            self.assertEqual(forecast.call_args.kwargs['product_ids'], list(range(1, 83)))
            self.assertLess(len(calls), 18)
        finally:
            db.close()

    def test_sqlite_explicit_initialization_idempotent(self):
        self.activate(); self.drain()
        count = self.count('purchase_replenishment_episodes')
        self.db.conn.executescript(schema_sql())
        self.assertEqual(self.count('purchase_replenishment_episodes'), count)


@unittest.skipUnless(os.environ.get('GPCTA_TEST_POSTGRES_SOCKET'), 'requires disposable local PostgreSQL socket')
class PostgresObservationTest(ObservationContract, unittest.TestCase):
    def setUp(self):
        import psycopg2
        from psycopg2.extras import DictCursor
        socket = os.environ['GPCTA_TEST_POSTGRES_SOCKET']
        if not socket.startswith('/tmp/gpcta-pg.'):
            raise RuntimeError('Only disposable local sockets are allowed')
        self.args = dict(host=socket, port=55439, dbname='postgres', cursor_factory=DictCursor)
        self.schema = 'observation_' + uuid.uuid4().hex
        self.admin = psycopg2.connect(**self.args)
        self.admin.autocommit = True
        with self.admin.cursor() as cursor:
            cursor.execute('CREATE SCHEMA ' + self.schema)
        self.addCleanup(self.cleanup)
        db = self.connect()
        with db.conn.cursor() as cursor:
            cursor.execute(BASE)
            migration = Path('supabase/migrations/20261018010000_purchase_observation.sql').read_text()
            self.assertTrue(migration.endswith(schema_sql(True)))
            cursor.execute(migration)
            cursor.execute(migration)
        db.commit(); db.close()
        self.prepare()

    def connect(self):
        import psycopg2
        conn = psycopg2.connect(**self.args)
        with conn.cursor() as cursor:
            cursor.execute('SET search_path TO ' + self.schema)
        conn.commit()
        return DbWrapper(conn, True)

    def cleanup(self):
        with self.admin.cursor() as cursor:
            cursor.execute('DROP SCHEMA ' + self.schema + ' CASCADE')
        self.admin.close()

    def test_stock_commit_during_forecast_survives_as_pending_event(self):
        self.activate(); self.drain()
        self.stock(17)
        reading, written = Event(), Event()
        def forecast(*args, **kwargs):
            reading.set()
            if not written.wait(10):
                raise RuntimeError('Writer blocked by observation')
            return purchase_forecast(*args, **kwargs)
        def writer():
            self.assertTrue(reading.wait(10))
            db = self.connect()
            try:
                with db:
                    db.execute('UPDATE products SET stock=19 WHERE id=1')
                written.set()
            finally:
                db.close()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(writer)
            with patch('src.services.purchase_observation.purchase_forecast', side_effect=forecast):
                self.process()
            future.result(timeout=10)
        self.assertEqual(self.query('SELECT stock_units FROM purchase_replenishment_state WHERE product_id=1')[0]['stock_units'], 17)
        self.assertGreater(self.count('purchase_observation_events', 'processed_at IS NULL'), 0)
        self.drain()
        self.assertEqual(self.query('SELECT stock_units FROM purchase_replenishment_state WHERE product_id=1')[0]['stock_units'], 19)

    def test_late_commit_with_lower_event_id_is_not_skipped(self):
        self.activate(); self.drain()
        early = self.connect()
        try:
            early.execute('UPDATE products SET stock=17 WHERE id=1')
            with self.db:
                self.db.execute('UPDATE products SET stock=19 WHERE id=2')
            self.drain()  # Larger event IDs commit first and are consumed.
            self.assertEqual(self.query('SELECT stock_units FROM purchase_replenishment_state WHERE product_id=1')[0]['stock_units'], 20)
            early.commit()
            self.assertGreater(self.count('purchase_observation_events', 'processed_at IS NULL'), 0)
            self.drain()
            self.assertEqual(self.query('SELECT stock_units FROM purchase_replenishment_state WHERE product_id=1')[0]['stock_units'], 17)
        finally:
            early.close()


if __name__ == '__main__':
    unittest.main()
