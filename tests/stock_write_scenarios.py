"""The same real-connection stock-write scenarios run against both databases."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Barrier
from unittest.mock import patch

from src.services.stock_conferences import apply_conference
from src.services.stock_writes import correct_restock_stock, update_product_stock
from src.db import DbWrapper


class StockWriteScenarios:
    def prepare_writers(self):
        db = self.open_db()
        with db:
            db.execute('''CREATE TABLE IF NOT EXISTS restocks (
                id INTEGER PRIMARY KEY,product_id INTEGER,quantity INTEGER,unit_cost_cents INTEGER)''')
            db.execute('''CREATE TABLE IF NOT EXISTS restock_corrections (
                id SERIAL PRIMARY KEY,restock_id INTEGER,previous_quantity INTEGER,
                corrected_quantity INTEGER,previous_unit_cost_cents INTEGER,
                corrected_unit_cost_cents INTEGER,reason TEXT,created_by INTEGER)'''.replace(
                    'SERIAL PRIMARY KEY', 'SERIAL PRIMARY KEY' if db.is_postgres else 'INTEGER PRIMARY KEY'))
            db.execute('UPDATE products SET stock=20 WHERE id=1')
            revision = db.execute('SELECT stock_revision FROM products WHERE id=1').fetchone()[0]
            if not db.execute('SELECT id FROM stock_conferences WHERE id=1').fetchone():
                db.execute("INSERT INTO stock_conferences(conference_month) VALUES('2026-01')")
                db.execute("INSERT INTO stock_conference_items(conference_id,product_id,expected_stock,physical_stock,difference,reason,expected_revision) VALUES(1,1,20,17,-3,'Count',?)", (revision,))
            else:
                db.execute('UPDATE stock_conference_items SET expected_revision=?', (revision,))
            db.execute('INSERT INTO restocks(id,product_id,quantity,unit_cost_cents) VALUES(1,1,10,100)')
        db.close()

    def read(self, sql):
        db = self.open_db()
        try:
            return [tuple(row) for row in db.execute(sql).fetchall()]
        finally:
            db.close()

    def test_concurrent_restock_20_17_19_and_persistent_audit(self):
        self.prepare_writers()
        read_old = Event()
        applied = Event()

        def correction():
            db = self.open_db()
            try:
                self.assertEqual(db.execute('SELECT stock FROM products WHERE id=1').fetchone()[0], 20)
                db.rollback()  # release only the page's read transaction
                read_old.set()
                self.assertTrue(applied.wait(5))
                correct_restock_stock(db, 1, 12, 150, 'Contagem corrigida', 1)
            finally:
                db.close()

        def conference():
            self.assertTrue(read_old.wait(5))
            db = self.open_db()
            try:
                apply_conference(db, 1, 1)
                self.assertEqual(db.execute('SELECT stock FROM products WHERE id=1').fetchone()[0], 17)
            finally:
                db.close()
                applied.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(correction)
            b = pool.submit(conference)
            a.result(timeout=10)
            b.result(timeout=10)
        self.assertEqual(self.read('SELECT stock FROM products WHERE id=1'), [(19,)])
        self.assertEqual(self.read('SELECT previous_stock,new_stock,difference FROM stock_adjustments ORDER BY id'), [(20,17,-3),(17,19,2)])
        self.assertEqual(self.read('SELECT COUNT(*) FROM stock_conference_applications'), [(1,)])

    def test_manual_read_before_conference_is_rejected_then_fresh_17_to_19(self):
        self.prepare_writers()
        old_read = Event()
        applied = Event()

        def manual():
            db = self.open_db()
            try:
                revision = db.execute('SELECT stock_revision FROM products WHERE id=1').fetchone()[0]
                db.rollback()
                old_read.set()
                self.assertTrue(applied.wait(5))
                with self.assertRaisesRegex(ValueError, 'mudou'):
                    update_product_stock(db, 1, revision, {'stock':22}, 1, 'Manual antigo')
            finally:
                db.close()

        def conference():
            self.assertTrue(old_read.wait(5))
            db = self.open_db()
            try:
                apply_conference(db, 1, 1)
            finally:
                db.close()
                applied.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(manual)
            b = pool.submit(conference)
            a.result(timeout=10)
            b.result(timeout=10)
        self.assertEqual(self.read('SELECT stock FROM products WHERE id=1'), [(17,)])
        db = self.open_db()
        try:
            revision = db.execute('SELECT stock_revision FROM products WHERE id=1').fetchone()[0]
            db.rollback()
            update_product_stock(db, 1, revision, {'stock':19}, 1, 'Manual revisado')
        finally:
            db.close()
        self.assertEqual(self.read('SELECT previous_stock,new_stock,difference FROM stock_adjustments ORDER BY id'), [(20,17,-3),(17,19,2)])

    def test_simultaneous_restock_corrections_do_not_double_count_delta(self):
        self.prepare_writers()
        barrier = Barrier(2)
        def worker(quantity):
            db = self.open_db()
            try:
                barrier.wait(timeout=5)
                correct_restock_stock(db, 1, quantity, 100, 'Quantidade revisada', 1)
            finally:
                db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(worker, q) for q in (12,14)]
            for job in jobs:
                job.result(timeout=10)
        final_quantity = self.read('SELECT corrected_quantity FROM restock_corrections ORDER BY id DESC LIMIT 1')[0][0]
        self.assertEqual(self.read('SELECT stock FROM products WHERE id=1'), [(20+final_quantity-10,)])
        self.assertEqual(sum(r[0] for r in self.read('SELECT difference FROM stock_adjustments')), final_quantity-10)

    def test_round_trip_invalidates_manual_and_errors_rollback_writers(self):
        self.prepare_writers()
        db = self.open_db()
        try:
            revision = db.execute('SELECT stock_revision FROM products WHERE id=1').fetchone()[0]
            with db:
                db.execute('UPDATE products SET stock=19 WHERE id=1')
                db.execute('UPDATE products SET stock=20 WHERE id=1')
            with self.assertRaises(ValueError):
                update_product_stock(db,1,revision,{'stock':22},1,'Manual antigo')
            revision = db.execute('SELECT stock_revision FROM products WHERE id=1').fetchone()[0]
            db.rollback()
            execute = DbWrapper.execute
            def fail_audit(connection, sql, params=None):
                if 'INSERT INTO stock_adjustments' in sql:
                    raise RuntimeError('simulated audit failure')
                return execute(connection, sql, params)
            with patch.object(DbWrapper, 'execute', fail_audit):
                with self.assertRaises(RuntimeError):
                    correct_restock_stock(db,1,12,150,'Quantidade corrigida',1)
                with self.assertRaises(RuntimeError):
                    update_product_stock(db,1,revision,{'stock':22},1,'Manual corrigido')
        finally:
            db.close()
        self.assertEqual(self.read('SELECT stock FROM products WHERE id=1'), [(20,)])
        self.assertEqual(self.read('SELECT COUNT(*) FROM restock_corrections'), [(0,)])
        self.assertEqual(self.read('SELECT COUNT(*) FROM stock_adjustments'), [(0,)])

    def test_simultaneous_manual_writes_accept_only_one_version(self):
        self.prepare_writers()
        barrier = Barrier(2)
        def worker(stock):
            db = self.open_db()
            try:
                revision = db.execute('SELECT stock_revision FROM products WHERE id=1').fetchone()[0]
                db.rollback()
                barrier.wait(timeout=5)
                try:
                    update_product_stock(db, 1, revision, {'stock':stock}, 1, 'Contagem manual')
                    return 'applied'
                except ValueError:
                    return 'blocked'
            finally:
                db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(worker, stock) for stock in (21,22)]
            results = [job.result(timeout=10) for job in jobs]
        self.assertCountEqual(results, ['applied','blocked'])
        history = self.read('SELECT previous_stock,new_stock,difference FROM stock_adjustments')
        self.assertEqual(len(history),1)
        self.assertEqual(history[0][0],20)
        self.assertEqual(self.read('SELECT stock FROM products WHERE id=1')[0][0],history[0][1])

    def test_restock_insufficient_balance_and_old_purchase_cost(self):
        self.prepare_writers()
        db = self.open_db()
        try:
            with db:
                db.execute('UPDATE products SET stock=1,cost_cents=200 WHERE id=1')
                db.execute('INSERT INTO restocks(id,product_id,quantity,unit_cost_cents) VALUES(2,1,1,200)')
            with self.assertRaisesRegex(ValueError,'parte do estoque'):
                correct_restock_stock(db,1,0,50,'Quantidade corrigida',1)
            correct_restock_stock(db,1,12,50,'Quantidade corrigida',1)
        finally:
            db.close()
        self.assertEqual(self.read('SELECT stock,cost_cents FROM products WHERE id=1'),[(3,200)])
        self.assertEqual(self.read('SELECT COUNT(*) FROM restock_corrections'),[(1,)])
