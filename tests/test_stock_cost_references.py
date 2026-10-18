"""One service contract on SQLite and a disposable opt-in PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch
import uuid

from src.db import DbWrapper
from src.cost_reference_schema import schema_sql
from src.services.stock_cost_references import parse_reference_cost, reference_snapshot, register_cost_reference
from src.services.stock_financial import stock_financial_data


BASE = '''
CREATE TABLE users(id INTEGER PRIMARY KEY,name TEXT,role TEXT,active INTEGER DEFAULT 1);
CREATE TABLE products(id INTEGER PRIMARY KEY,name TEXT,category TEXT,active INTEGER DEFAULT 1,
 stock INTEGER DEFAULT 10,price_cents INTEGER DEFAULT 500,cost_cents INTEGER DEFAULT 175,
 cost_reference_revision BIGINT NOT NULL DEFAULT 0);
CREATE TABLE restocks(id INTEGER PRIMARY KEY,product_id INTEGER,quantity INTEGER,unit_cost_cents INTEGER,notes TEXT);
CREATE TABLE restock_corrections(id INTEGER PRIMARY KEY,restock_id INTEGER,corrected_quantity INTEGER,corrected_unit_cost_cents INTEGER);
CREATE TABLE stock_adjustments(id INTEGER PRIMARY KEY,product_id INTEGER,previous_stock INTEGER,new_stock INTEGER,difference INTEGER,reason TEXT);
'''


class CostReferenceContract:
    def prepare(self):
        db = self.connect()
        try:
            with db:
                db.execute("INSERT INTO users(id,name,role) VALUES(1,'Gestor','manager'),(2,'Staff','staff'),(3,'Cliente','client')")
                db.execute("INSERT INTO products(id,name,category) VALUES(1,'Sem histórico','Bar'),(2,'Outro','Bar')")
        finally:
            db.close()

    def read(self, sql, params=()):
        db = self.connect()
        try:
            return [dict(row) for row in db.execute(sql, params).fetchall()]
        finally:
            db.close()

    def write(self, sql, params=()):
        db = self.connect()
        try:
            with db:
                return db.execute(sql, params).lastrowid
        finally:
            db.close()

    def report(self):
        db = self.connect()
        try:
            return stock_financial_data(db)
        finally:
            db.close()

    def product(self, pid=1):
        return next(p for p in self.report()['products'] if p['id'] == pid)

    def approve(self, expected=None, **options):
        if expected is None:
            expected = reference_snapshot(self.product())
        arguments = dict(unit_cost_cents=175, justification='Nota conferida pelo gestor',
                         value_origin='catalog_confirmed', confirmed=True)
        arguments.update(options)
        user = arguments.pop('user_id', 1)
        db = self.connect()
        try:
            return register_cost_reference(db, 1, user, expected, **arguments)
        finally:
            db.close()

    def test_explicit_confirmation_and_no_business_mutation(self):
        before = {t:self.read('SELECT * FROM '+t+' ORDER BY id')
                  for t in ('products','restocks','restock_corrections','stock_adjustments')}
        self.assertEqual(self.product()['cost_status'], 'unavailable')
        with self.assertRaises(ValueError):
            self.approve(confirmed=False)
        self.approve(support_document='Nota 123 / arquivo do gestor')
        product = self.product()
        self.assertEqual((product['cost_status'], product['unit_cost_cents'], product['stock_value_cents']),
                         ('manager_reference',175,1750))
        self.assertEqual(before, {t:self.read('SELECT * FROM '+t+' ORDER BY id') for t in before})
        row = self.read('SELECT * FROM stock_cost_references')[0]
        self.assertEqual((row['responsible_id'],row['responsible_name'],row['reference_stock']), (1,'Gestor',10))
        self.assertEqual(row['support_document'],'Nota 123 / arquivo do gestor')
        self.assertTrue(row['created_at'])

    def test_append_only_and_previous_reference_concurrency_guard(self):
        first_snapshot = reference_snapshot(self.product())
        first = self.approve(first_snapshot)
        with self.assertRaises(ValueError):
            self.approve(first_snapshot, unit_cost_cents=200, value_origin='manager_entered')
        second = self.approve(unit_cost_cents=200, value_origin='manager_entered')
        rows = self.read('SELECT * FROM stock_cost_references ORDER BY id')
        self.assertEqual([r['unit_cost_cents'] for r in rows], [175,200])
        self.assertEqual(rows[1]['previous_reference_id'],first)
        self.assertGreater(second,first)
        for sql in ('UPDATE stock_cost_references SET unit_cost_cents=1', 'DELETE FROM stock_cost_references'):
            with self.assertRaises(Exception):
                self.write(sql)
        self.assertEqual(self.product()['unit_cost_cents'],200)

    def test_authorization_validation_and_catalog_origin(self):
        for user in (2,3,999):
            with self.subTest(user=user), self.assertRaises(PermissionError):
                self.approve(user_id=user)
        self.write('UPDATE users SET active=0 WHERE id=1')
        with self.assertRaises(PermissionError):
            self.approve()
        self.write('UPDATE users SET active=1 WHERE id=1')
        for options in ({'unit_cost_cents':0},{'unit_cost_cents':-1},{'unit_cost_cents':True},
                        {'unit_cost_cents':2147483648},{'justification':' '},
                        {'justification':'x'*2001},{'value_origin':'fake'},
                        {'unit_cost_cents':200}, {'support_document':'x'*501}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.approve(**options)
        self.assertEqual(self.read('SELECT COUNT(*) n FROM stock_cost_references')[0]['n'],0)

    def test_stock_movement_round_trip_and_catalog_suggestion_change(self):
        stale = reference_snapshot(self.product())
        self.write('UPDATE products SET cost_cents=200 WHERE id=1')
        with self.assertRaises(ValueError):
            self.approve(stale)
        self.approve(unit_cost_cents=200)
        self.write('UPDATE products SET stock=17 WHERE id=1')
        self.write('UPDATE products SET stock=10 WHERE id=1')
        product = self.product()
        self.assertFalse(product['reference_valid'])
        self.assertEqual(product['cost_status'],'unavailable')
        self.assertEqual(self.read('SELECT unit_cost_cents FROM stock_cost_references')[0]['unit_cost_cents'],200)

    def test_new_acquisition_correction_adjustment_and_reliable_history_priority(self):
        self.approve()
        self.write("INSERT INTO restocks VALUES(1,1,20,0,'Reposição via solicitação #1')")
        self.assertFalse(self.product()['reference_valid'])
        self.approve()
        self.write('INSERT INTO restock_corrections VALUES(1,1,20,300)')
        self.assertFalse(self.product()['reference_valid'])
        self.approve()
        self.write("INSERT INTO stock_adjustments VALUES(1,1,10,10,0,'Histórico verificado')")
        self.assertFalse(self.product()['reference_valid'])
        self.approve()
        self.write("UPDATE restocks SET notes='Individual comprovada' WHERE id=1")
        row = self.product()
        self.assertEqual((row['cost_status'],row['unit_cost_cents']),('historical_reference',300))
        with self.assertRaises(ValueError):
            self.approve()

    def test_existing_correction_flow_takes_priority(self):
        self.write("INSERT INTO restocks VALUES(1,1,20,0,'Compra individual')")
        self.assertEqual(self.product()['correctable_restock_ids'],[1])
        self.assertFalse(self.product()['can_regularize'])
        with self.assertRaisesRegex(ValueError,'Corrija primeiro'):
            self.approve()
        self.write('INSERT INTO restock_corrections VALUES(1,1,20,120)')
        self.assertEqual(self.product()['cost_status'],'historical_reference')

    def test_inactive_reactivation_category_and_deleted_product(self):
        self.approve()
        self.write('UPDATE products SET active=0 WHERE id=1')
        self.assertFalse(self.product()['reference_valid'])
        with self.assertRaises(ValueError):
            self.approve()
        self.write('UPDATE products SET active=1 WHERE id=1')
        self.assertFalse(self.product()['reference_valid'])
        snapshot = reference_snapshot(self.product())
        self.write("UPDATE products SET category='Material Esportivo' WHERE id=1")
        with self.assertRaises(ValueError):
            self.approve(snapshot)
        self.write('DELETE FROM products WHERE id=1')
        with self.assertRaises(ValueError):
            self.approve(snapshot)
        self.assertEqual(len(self.read('SELECT * FROM stock_cost_references')),1)

    def test_noop_price_and_catalog_cost_do_not_invalidate_explicit_reference(self):
        self.approve()
        self.write('UPDATE products SET stock=stock,price_cents=600,cost_cents=999 WHERE id=1')
        self.assertTrue(self.product()['reference_valid'])
        self.assertEqual(self.product()['unit_cost_cents'],175)

    def test_rollback_does_not_invalidate_and_failed_insert_changes_nothing(self):
        self.approve()
        db = self.connect()
        try:
            with self.assertRaises(RuntimeError):
                with db:
                    db.execute('UPDATE products SET stock=17 WHERE id=1')
                    raise RuntimeError('Rollback stock')
        finally:
            db.close()
        self.assertTrue(self.product()['reference_valid'])
        with patch('src.services.stock_cost_references.datetime') as clock:
            clock.now.side_effect=RuntimeError('Failure before insert')
            with self.assertRaises(RuntimeError):
                self.approve()
        self.assertEqual(len(self.read('SELECT * FROM stock_cost_references')),1)

    def test_summary_separates_sources_and_coverage(self):
        self.approve()
        self.write("INSERT INTO restocks VALUES(1,2,20,100,'Individual')")
        summary = self.report()['summary']
        self.assertEqual((summary['historical_value_cents'],summary['regularized_value_cents'],
                          summary['stock_value_cents'],summary['covered_units'],summary['coverage_percent']),
                         (1000,1750,2750,20,100))
        self.write("INSERT INTO products(id,name,category) VALUES(3,'Sem custo','Bar')")
        summary = self.report()['summary']
        self.assertEqual(summary['covered_units'],20)
        self.assertEqual(summary['missing_units'],10)
        self.assertEqual(str(summary['coverage_percent']),'66.67')
        self.assertTrue(summary['partial'])

    def test_concurrent_approvals_only_one_succeeds(self):
        expected = reference_snapshot(self.product())
        barrier = Barrier(2)
        def worker(_):
            barrier.wait()
            try:
                self.approve(expected)
                return 'saved'
            except ValueError:
                return 'stale'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(worker,range(2)))
        self.assertCountEqual(results,['saved','stale'])
        self.assertEqual(len(self.read('SELECT * FROM stock_cost_references')),1)

    def test_reassignment_and_deletion_invalidate_both_product_histories(self):
        self.write("INSERT INTO restocks VALUES(1,1,20,100,'Reposição via solicitação #1')")
        self.approve()
        self.write('UPDATE restocks SET product_id=2 WHERE id=1')
        self.assertFalse(self.product()['reference_valid'])
        revision = self.product(2)['reference_revision']
        self.write('DELETE FROM restocks WHERE id=1')
        self.assertGreater(self.product(2)['reference_revision'],revision)


class SQLiteCostReferenceTest(CostReferenceContract, unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory(prefix='gpcta-cost-',dir='/tmp')
        self.addCleanup(directory.cleanup)
        self.path=str(Path(directory.name)/'test.db')
        db=self.connect()
        db.conn.executescript(BASE+schema_sql())
        db.close()
        self.prepare()

    def connect(self):
        conn=sqlite3.connect(self.path,timeout=10)
        conn.row_factory=sqlite3.Row
        return DbWrapper(conn)

    def test_round_half_up_and_invalid_monetary_inputs(self):
        for value,expected in (('1,005',101),('0,005',1),('1.234,56',123456),('175',17500)):
            self.assertEqual(parse_reference_cost(value),expected)
        for value in ('0','0,004','-1','NaN','Infinity','1e3','1.50','1,00001','9'*25):
            with self.subTest(value=value),self.assertRaises(ValueError):
                parse_reference_cost(value)

    def test_reapply_schema_preserves_approvals(self):
        self.approve()
        db=self.connect()
        try:
            db.conn.executescript(schema_sql())
        finally:
            db.close()
        self.assertTrue(self.product()['reference_valid'])


@unittest.skipUnless(os.environ.get('GPCTA_TEST_POSTGRES_SOCKET'),'requires disposable local PostgreSQL socket')
class PostgresCostReferenceTest(CostReferenceContract,unittest.TestCase):
    def setUp(self):
        import psycopg2
        from psycopg2.extras import DictCursor
        socket=os.environ['GPCTA_TEST_POSTGRES_SOCKET']
        if not socket.startswith('/tmp/gpcta-pg.'):
            raise RuntimeError('Only disposable local test sockets are allowed')
        self.args=dict(host=socket,port=55439,dbname='postgres',cursor_factory=DictCursor)
        self.schema='cost_test_'+uuid.uuid4().hex
        self.admin=psycopg2.connect(**self.args)
        self.admin.autocommit=True
        with self.admin.cursor() as cursor:
            cursor.execute('CREATE SCHEMA '+self.schema)
        self.addCleanup(self.cleanup)
        db=self.connect()
        with db.conn.cursor() as cursor:
            cursor.execute(BASE.replace(',\n cost_reference_revision BIGINT NOT NULL DEFAULT 0',''))
            migration=Path('supabase/migrations/20261018020000_stock_cost_references.sql').read_text()
            self.assertTrue(migration.endswith(schema_sql(True)))
            cursor.execute(migration)
            cursor.execute(migration)
        db.commit();db.close()
        self.prepare()

    def connect(self):
        import psycopg2
        conn=psycopg2.connect(**self.args)
        with conn.cursor() as cursor:
            cursor.execute('SET search_path TO '+self.schema)
        conn.commit()
        return DbWrapper(conn,True)

    def cleanup(self):
        with self.admin.cursor() as cursor:
            cursor.execute('DROP SCHEMA '+self.schema+' CASCADE')
        self.admin.close()

    def test_truncate_is_rejected(self):
        self.approve()
        with self.assertRaises(Exception):
            self.write('TRUNCATE stock_cost_references')
        self.assertTrue(self.product()['reference_valid'])

    def test_reapply_migration_after_approval_preserves_history_and_revision(self):
        self.approve()
        before=self.read('SELECT * FROM stock_cost_references')
        db=self.connect()
        try:
            with db.conn.cursor() as cursor:
                cursor.execute(Path('supabase/migrations/20261018020000_stock_cost_references.sql').read_text())
            db.commit()
        finally:
            db.close()
        self.assertEqual(self.read('SELECT * FROM stock_cost_references'),before)
        self.assertTrue(self.product()['reference_valid'])


from tests import test_stock_conferences_postgres as stock_fixtures
from tests.stock_write_scenarios import StockWriteScenarios


@unittest.skipUnless(os.environ.get('GPCTA_TEST_POSTGRES_SOCKET'),'requires disposable local PostgreSQL socket')
class PostgresCostInvalidationStockWritesTest(StockWriteScenarios,unittest.TestCase):
    connect=stock_fixtures.PostgresStockConferenceTest.connect
    open_db=stock_fixtures.PostgresStockConferenceTest.connect
    cleanup=stock_fixtures.PostgresStockConferenceTest.cleanup

    def setUp(self):
        stock_fixtures.PostgresStockConferenceTest.setUp(self)
        db=self.connect()
        try:
            with db.conn.cursor() as cursor:
                cursor.execute('''CREATE TABLE restocks(id SERIAL PRIMARY KEY,product_id INTEGER,quantity INTEGER,unit_cost_cents INTEGER);
                    CREATE TABLE restock_corrections(id SERIAL PRIMARY KEY,restock_id INTEGER,previous_quantity INTEGER,
                    corrected_quantity INTEGER,previous_unit_cost_cents INTEGER,corrected_unit_cost_cents INTEGER,
                    reason TEXT,created_by INTEGER);''')
                cursor.execute(Path('supabase/migrations/20261018020000_stock_cost_references.sql').read_text())
            db.commit()
        finally:
            db.close()
