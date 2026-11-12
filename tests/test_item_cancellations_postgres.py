"""Real row-lock tests; accepts only a disposable local Unix socket."""
import os
import uuid
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from flask import Flask
from src.db import SCHEMA, DbWrapper
from src.services.item_cancellations import cancel_item


@unittest.skipUnless(os.environ.get('GPCTA_TEST_POSTGRES_SOCKET'), 'requires disposable local PostgreSQL socket')
class PostgresItemCancellationTest(unittest.TestCase):
    def setUp(self):
        import psycopg2
        import psycopg2.extras
        socket=os.environ['GPCTA_TEST_POSTGRES_SOCKET']
        if not socket.startswith('/tmp/gpcta-pg.'):
            raise RuntimeError('Only disposable local test sockets are allowed')
        self.args=dict(host=socket,port=55439,dbname='postgres',cursor_factory=psycopg2.extras.DictCursor)
        self.schema='item_cancel_'+uuid.uuid4().hex
        self.admin=psycopg2.connect(**self.args);self.admin.autocommit=True
        with self.admin.cursor() as cursor:
            cursor.execute('CREATE SCHEMA '+self.schema)
        self.addCleanup(self.cleanup)
        db=self.connect()
        try:
            with db.conn.cursor() as cursor:
                cursor.execute(';'.join(stmt for stmt in SCHEMA.replace('INTEGER PRIMARY KEY AUTOINCREMENT','SERIAL PRIMARY KEY').replace('COLLATE NOCASE','').split(';') if not stmt.strip().upper().startswith('INSERT')))
                migration=Path('supabase/migrations/20261111090000_sale_item_cancellations.sql').read_text()
                cursor.execute(migration);cursor.execute(migration)
            self.actor=db.execute("INSERT INTO users(username,name,password_hash,role) VALUES('test','Test','hash','staff')").lastrowid
            self.player=db.execute("INSERT INTO players(name) VALUES('Player')").lastrowid
            self.product=db.execute("INSERT INTO products(name,category,stock,price_cents) VALUES('Snack','Bar',4,1200)").lastrowid
            self.sale=db.execute("INSERT INTO sales(player_id,payment_method,total_cents,paid,payment_status,ready_for_delivery) VALUES(?,'Dinheiro',1200,1,'approved',1)",(self.player,)).lastrowid
            self.item=db.execute('INSERT INTO sale_items(sale_id,product_id,quantity,unit_price_cents) VALUES(?,?,1,1200)',(self.sale,self.product)).lastrowid
            db.execute("INSERT INTO sale_payment_parts(sale_id,method,amount_cents,status) VALUES(?,'Dinheiro',1200,'approved')",(self.sale,))
            db.commit()
        finally: db.close()

    def connect(self):
        import psycopg2
        conn=psycopg2.connect(**self.args)
        conn.set_client_encoding("UTF8")
        with conn.cursor() as cursor:
            cursor.execute('SET search_path TO '+self.schema)
        conn.commit()
        return DbWrapper(conn,True)

    def cleanup(self):
        with self.admin.cursor() as cursor: cursor.execute('DROP SCHEMA '+self.schema+' CASCADE')
        self.admin.close()

    def cancel(self,key=None):
        db=self.connect()
        try:
            with Flask(__name__).app_context():
                return cancel_item(db,self.sale,self.item,1,'Produto não entregue',key or str(uuid.uuid4()),self.actor)
        finally:db.close()

    def assert_balances(self,canceled):
        db=self.connect()
        try:
            self.assertEqual(db.execute('SELECT stock FROM products').fetchone()[0],5 if canceled else 4)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sale_item_cancellations').fetchone()[0],int(canceled))
            self.assertEqual(db.execute('SELECT COALESCE(SUM(amount_cents),0) FROM bar_credit_transactions').fetchone()[0],1200 if canceled else 0)
            self.assertEqual(db.execute('SELECT amount_cents FROM sale_payment_parts').fetchone()[0],1200)
        finally:db.close()

    def test_concurrent_different_keys(self):
        barrier=Barrier(2)
        def worker(_):
            barrier.wait()
            try:self.cancel();return True
            except ValueError:return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(worker,range(2))),[True,False])
        self.assert_balances(True)

    def test_concurrent_same_key(self):
        key=str(uuid.uuid4());barrier=Barrier(2)
        def worker(_):barrier.wait();return self.cancel(key)['id']
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(worker,range(2)))
        self.assertEqual(results[0],results[1]);self.assert_balances(True)

    def test_financial_failure_rolls_back_stock(self):
        with patch('src.services.item_cancellations._audit',side_effect=RuntimeError('financial failure')):
            with self.assertRaises(RuntimeError):self.cancel()
        self.assert_balances(False)

    def test_stock_failure_rolls_back_wallet(self):
        db=self.connect()
        with db.conn.cursor() as cursor:
            cursor.execute("CREATE FUNCTION reject_stock() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'stock failure'; END $$; CREATE TRIGGER reject_stock BEFORE UPDATE OF stock ON products FOR EACH ROW EXECUTE FUNCTION reject_stock()")
        db.commit();db.close()
        import psycopg2
        with self.assertRaises(psycopg2.Error):self.cancel()
        self.assert_balances(False)

    def test_concurrent_manual_credit_preserves_both_entries(self):
        from src.services.bar_credits import adjust_balance
        barrier=Barrier(2)
        def refund():barrier.wait();self.cancel()
        def adjustment():
            db=self.connect()
            try:
                barrier.wait()
                with Flask(__name__).app_context(),db:
                    adjust_balance(db,self.player,300,self.actor,'Crédito autorizado')
            finally:db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(refund);second=pool.submit(adjustment)
            first.result();second.result()
        db=self.connect()
        try:
            self.assertEqual(db.execute('SELECT balance_cents FROM bar_credit_accounts').fetchone()[0],1500)
            self.assertEqual(db.execute('SELECT SUM(amount_cents) FROM bar_credit_transactions').fetchone()[0],1500)
        finally:db.close()
