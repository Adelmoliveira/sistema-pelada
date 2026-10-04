import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.db import connect_sqlite, initialize_sqlite_database
from src.services.sports_installments import create_installment_plan


class SportsInstallmentsTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        path = str(Path(self.tempdir.name) / 'installments.db')
        initialize_sqlite_database(path)
        self.db = connect_sqlite(path)
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE sports_product_variants(id INTEGER PRIMARY KEY, product_id INTEGER REFERENCES products(id))')
        schema = Path('supabase/migrations/20260813170000_sports_sale_item_details.sql').read_text()
        self.db.conn.executescript(schema)
        self.db.execute('CREATE TABLE sports_product_config(product_id INTEGER PRIMARY KEY,installment_pix_enabled INTEGER NOT NULL DEFAULT 0)')
        self.base_date = date(2026, 10, 4)

    def sale(self, total=10000, category='Material Esportivo', mode='ready', details=True):
        db = self.db
        product_id = db.execute('INSERT INTO products(name,category,price_cents) VALUES(?,?,?)',
                                (f'Produto {db.execute("SELECT COUNT(*) FROM products").fetchone()[0]}', category, total)).lastrowid
        if category == 'Material Esportivo':
            db.execute('INSERT INTO sports_product_config(product_id,installment_pix_enabled) VALUES(?,1)',(product_id,))
        sale_id = db.execute("INSERT INTO sales(payment_method,total_cents,paid,payment_status) VALUES('Pix',?,0,'pending')", (total,)).lastrowid
        item_id = db.execute('INSERT INTO sale_items(sale_id,product_id,quantity,unit_price_cents) VALUES(?,?,1,?)', (sale_id, product_id, total)).lastrowid
        if details:
            variant_id = db.execute('INSERT INTO sports_product_variants(product_id) VALUES(?)', (product_id,)).lastrowid
            db.execute("INSERT INTO sports_sale_item_details(sale_item_id,variant_id,variant_size,order_mode,fulfillment_status) VALUES(?,?,'M',?,?)", (item_id, variant_id, mode, 'reserved' if mode == 'ready' else 'requested'))
        db.commit()
        return sale_id

    def plan(self, sale_id, total=10000, day=None):
        return create_installment_plan(self.db, sale_id, total, day or self.base_date)

    def test_300_reais_splits_100_each(self):
        result = self.plan(self.sale(30000), 30000)
        self.assertEqual([r['amount_cents'] for r in result['installments']], [10000]*3)

    def test_100_reais_exactly_three_with_remainder_last(self):
        result = self.plan(self.sale())
        self.assertEqual([r['amount_cents'] for r in result['installments']], [3333,3333,3334])
        self.assertEqual([r['installment_number'] for r in result['installments']], [1,2,3])
        self.assertEqual(sum(r['amount_cents'] for r in result['installments']), 10000)
        self.assertEqual(result['plan']['status'], 'pending')
        self.assertTrue(all(r['status']=='pending' for r in result['installments']))
        self.assertIsNone(result['plan']['first_installment_paid_at'])
        self.assertIsNone(result['plan']['fully_paid_at'])

    def test_due_dates_zero_30_60_calendar_days(self):
        result = self.plan(self.sale())
        self.assertEqual([r['due_date'] for r in result['installments']], ['2026-10-04','2026-11-03','2026-12-03'])

    def test_repeat_does_not_duplicate_and_preserves_sales_and_stock(self):
        sale_id = self.sale()
        before = dict(self.db.execute('SELECT * FROM sales WHERE id=?', (sale_id,)).fetchone())
        first = self.plan(sale_id)
        second = self.plan(sale_id, day='2026-10-04')
        self.assertEqual(first, second)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM sports_installment_plans').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM sports_installments').fetchone()[0],3)
        self.assertEqual(dict(self.db.execute('SELECT * FROM sales WHERE id=?',(sale_id,)).fetchone()),before)
        self.assertEqual(self.db.execute('SELECT stock FROM products').fetchone()[0],0)

    def test_bar_sale_rejected(self):
        with self.assertRaises(ValueError):
            self.plan(self.sale(category='Cerveja', details=False))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM sports_installment_plans').fetchone()[0],0)

    def test_backorder_rejected(self):
        with self.assertRaises(ValueError):
            self.plan(self.sale(mode='backorder'))

    def test_mixed_sale_or_missing_sports_details_rejected(self):
        for details in (False, True):
            with self.subTest(details=details):
                sale_id=self.sale(details=details)
                if details:
                    bar_id=self.db.execute("INSERT INTO products(name,category,price_cents) VALUES('Bar','Cerveja',1)").lastrowid
                    self.db.execute('INSERT INTO sale_items(sale_id,product_id,quantity,unit_price_cents) VALUES(?,?,1,1)',(sale_id,bar_id))
                    self.db.commit()
                with self.assertRaises(ValueError):
                    self.plan(sale_id)

    def test_invalid_total_and_nonmatching_sale_total_rejected(self):
        sale_id = self.sale()
        for total in (0,-1,1,2,True,10000.0,'10000',None,9999,2147483648):
            with self.subTest(total=total), self.assertRaises(ValueError):
                self.plan(sale_id,total)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM sports_installment_plans').fetchone()[0],0)

    def test_date_change_and_incomplete_existing_plan_rejected(self):
        sale_id=self.sale()
        self.plan(sale_id)
        with self.assertRaises(ValueError):
            self.plan(sale_id, day=date(2026,10,5))
        self.db.execute('DELETE FROM sports_installments WHERE installment_number=3')
        self.db.commit()
        with self.assertRaises(ValueError):
            self.plan(sale_id)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM sports_installments').fetchone()[0],2)

    def test_invalid_dates_rejected(self):
        sale_id=self.sale()
        for day in ('not-a-date',42,date.max):
            with self.subTest(day=day), self.assertRaises(ValueError):
                self.plan(sale_id,day=day)

    def test_atomic_failure_rolls_back_whole_plan(self):
        sale_id=self.sale()
        self.db.execute("CREATE TRIGGER fail_third BEFORE INSERT ON sports_installments WHEN NEW.installment_number=3 BEGIN SELECT RAISE(ABORT,'simulated failure'); END")
        self.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.plan(sale_id)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM sports_installment_plans').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM sports_installments').fetchone()[0],0)

    def test_essential_constraints(self):
        sale_id=self.sale()
        plan_id=self.plan(sale_id)['plan']['id']
        cases=[
            ('INSERT INTO sports_installment_plans(sale_id,total_cents) VALUES(?,?)',(sale_id,10000)),
            ('INSERT INTO sports_installment_plans(sale_id,total_cents) VALUES(?,?)',(999999,10000)),
            ('INSERT INTO sports_installments(plan_id,installment_number,amount_cents,due_date) VALUES(?,?,?,?)',(plan_id,1,1,'2026-10-04')),
            ('INSERT INTO sports_installments(plan_id,installment_number,amount_cents,due_date) VALUES(?,?,?,?)',(plan_id,4,1,'2026-10-04')),
            ('INSERT INTO sports_installments(plan_id,installment_number,amount_cents,due_date) VALUES(?,?,?,?)',(plan_id,0,1,'2026-10-04')),
            ('INSERT INTO sports_installments(plan_id,installment_number,amount_cents,due_date) VALUES(?,?,?,?)',(999999,1,1,'2026-10-04')),
            ("UPDATE sports_installments SET amount_cents=0 WHERE plan_id=?",(plan_id,)),
            ("UPDATE sports_installments SET status='invalid' WHERE plan_id=?",(plan_id,)),
            ("UPDATE sports_installment_plans SET status='invalid' WHERE id=?",(plan_id,)),
            ('DELETE FROM sales WHERE id=?',(sale_id,)),
        ]
        for sql,params in cases:
            with self.subTest(sql=sql,params=params), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql,params)
            self.db.rollback()

    def test_paid_cancelled_or_delivered_sales_cannot_get_new_plan(self):
        for paid,status,delivered in ((1,'approved',None),(0,'canceled',None),(0,'pending','2026-10-04')):
            with self.subTest(status=status,delivered=delivered):
                sale_id=self.sale()
                self.db.execute('UPDATE sales SET paid=?,payment_status=?,delivered_at=? WHERE id=?',(paid,status,delivered,sale_id))
                self.db.commit()
                with self.assertRaises(ValueError):
                    self.plan(sale_id)

    def test_minimum_total_produces_three_positive_cents(self):
        result=self.plan(self.sale(3),3)
        self.assertEqual([row['amount_cents'] for row in result['installments']],[1,1,1])

    def test_migration_constraints_match_sqlite_model(self):
        sql=Path('supabase/migrations/20261004130000_sports_installment_plans.sql').read_text()
        sql=sql.replace('BIGSERIAL PRIMARY KEY','INTEGER PRIMARY KEY AUTOINCREMENT')
        connection=sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.execute('CREATE TABLE sales(id INTEGER PRIMARY KEY)')
        connection.executescript(sql)
        for table in ('sports_installment_plans','sports_installments'):
            expected=[(r[1],r[3],r[4]) for r in self.db.execute(f'PRAGMA table_info({table})').fetchall()]
            actual=[(r[1],r[3],r[4]) for r in connection.execute(f'PRAGMA table_info({table})').fetchall()]
            self.assertEqual(actual,expected)


if __name__=='__main__':
    unittest.main()
