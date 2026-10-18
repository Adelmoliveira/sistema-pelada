"""Opt-in integration tests. Only a disposable Unix socket is accepted."""
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from src.db import DbWrapper
from src.services.stock_conferences import apply_conference


@unittest.skipUnless(os.environ.get('GPCTA_TEST_POSTGRES_SOCKET'), 'requires disposable local PostgreSQL socket')
class PostgresStockConferenceTest(unittest.TestCase):
    def setUp(self):
        import psycopg2
        import psycopg2.extras
        socket = os.environ['GPCTA_TEST_POSTGRES_SOCKET']
        if not socket.startswith('/tmp/gpcta-pg.'):
            raise RuntimeError('Only a disposable local test socket is allowed')
        self.schema = 'conference_test_' + uuid.uuid4().hex
        self.args = dict(host=socket,port=55439,dbname='postgres',cursor_factory=psycopg2.extras.DictCursor)
        self.admin = psycopg2.connect(**self.args)
        self.admin.autocommit = True
        with self.admin.cursor() as c:
            c.execute('CREATE SCHEMA ' + self.schema)
        self.addCleanup(self.cleanup)
        db = self.connect()
        with db.conn.cursor() as c:
            # Deliberately emulate the pre-migration schema.
            c.execute('''
                CREATE TABLE users(id SERIAL PRIMARY KEY,name TEXT);
                CREATE TABLE products(id SERIAL PRIMARY KEY,name TEXT,category TEXT,cost_cents INTEGER DEFAULT 0,stock INTEGER,active INTEGER DEFAULT 1);
                CREATE TABLE stock_conferences(id SERIAL PRIMARY KEY,conference_month TEXT UNIQUE);
                CREATE TABLE stock_conference_items(id SERIAL PRIMARY KEY,conference_id INTEGER REFERENCES stock_conferences(id),product_id INTEGER REFERENCES products(id),expected_stock INTEGER,physical_stock INTEGER,difference INTEGER,reason TEXT);
                CREATE TABLE stock_conference_audit(id SERIAL PRIMARY KEY,conference_month TEXT,action TEXT,details TEXT,user_id INTEGER,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE stock_adjustments(id SERIAL PRIMARY KEY,product_id INTEGER,user_id INTEGER,previous_stock INTEGER,new_stock INTEGER,difference INTEGER,reason TEXT,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                INSERT INTO users(name) VALUES('Manager');
                INSERT INTO products(name,category,stock) VALUES('Water','Bar',20);
                INSERT INTO stock_conferences(conference_month) VALUES('2026-01');
            ''')
            migration=Path('supabase/migrations/20261018000000_stock_conference_safety.sql').read_text()
            c.execute(migration)
            c.execute(migration)  # idempotent schema setup; no data backfill
            c.execute("INSERT INTO stock_conference_items(conference_id,product_id,expected_stock,physical_stock,difference,reason,expected_revision) VALUES(1,1,20,17,-3,'Count',0)")
        db.commit();db.close()

    def connect(self):
        import psycopg2
        conn=psycopg2.connect(**self.args)
        with conn.cursor() as c:c.execute('SET search_path TO '+self.schema)
        conn.commit()
        return DbWrapper(conn,is_postgres=True)

    def cleanup(self):
        with self.admin.cursor() as c:c.execute('DROP SCHEMA '+self.schema+' CASCADE')
        self.admin.close()

    def test_simultaneous_application_and_persistence(self):
        barrier=Barrier(2)
        def worker(_):
            db=self.connect()
            try:
                barrier.wait()
                try:apply_conference(db,1,1);return 'applied'
                except ValueError:return 'blocked'
            finally:db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(worker,range(2)))
        self.assertCountEqual(results,['applied','blocked'])
        db=self.connect()
        try:
            self.assertEqual(db.execute('SELECT stock FROM products').fetchone()[0],17)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM stock_conference_applications').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM stock_adjustments WHERE conference_id=1').fetchone()[0],1)
        finally:db.close()

    def test_round_trip_and_concurrent_stock_writer(self):
        db=self.connect()
        with db:
            db.execute('UPDATE products SET stock=19')
            db.execute('UPDATE products SET stock=20')
        with self.assertRaises(ValueError):apply_conference(db,1,1)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM stock_conference_applications').fetchone()[0],0)
        db.rollback()
        with db:db.execute('UPDATE stock_conference_items SET expected_revision=2')
        db.close()
        barrier=Barrier(2)
        def apply():
            db=self.connect()
            try:
                barrier.wait()
                try:apply_conference(db,1,1);return True
                except ValueError:return False
            finally:db.close()
        def move():
            db=self.connect()
            try:
                barrier.wait()
                with db:db.execute('UPDATE products SET stock=stock-1 WHERE id=1')
            finally:db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            applied=pool.submit(apply);moved=pool.submit(move)
            success=applied.result();moved.result()
        db=self.connect()
        try:self.assertEqual(db.execute('SELECT stock FROM products').fetchone()[0],16 if success else 19)
        finally:db.close()

    def test_atomic_multi_product_rollback(self):
        db=self.connect()
        with db:
            db.execute("INSERT INTO products(name,category,stock) VALUES('Second','Bar',19)")
            db.execute("INSERT INTO stock_conference_items(conference_id,product_id,expected_stock,physical_stock,difference,reason,expected_revision) VALUES(1,2,20,18,-2,'Count',0)")
        with self.assertRaises(ValueError):apply_conference(db,1,1)
        db.close()
        db=self.connect()
        try:
            self.assertEqual(db.execute('SELECT stock FROM products WHERE id=1').fetchone()[0],20)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM stock_adjustments').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM stock_conference_applications').fetchone()[0],0)
        finally:db.close()


from tests.stock_write_scenarios import StockWriteScenarios


@unittest.skipUnless(os.environ.get('GPCTA_TEST_POSTGRES_SOCKET'), 'requires disposable local PostgreSQL socket')
class PostgresStockWritesTest(StockWriteScenarios, unittest.TestCase):
    setUp = PostgresStockConferenceTest.setUp
    cleanup = PostgresStockConferenceTest.cleanup
    connect = PostgresStockConferenceTest.connect
    open_db = PostgresStockConferenceTest.connect
