import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.db import connect_sqlite, initialize_sqlite_database
from src.services.bar_installments import create_installment_plan, validate_checkout


class BarInstallmentsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = str(Path(directory.name) / 'bar.db')
        initialize_sqlite_database(path)
        self.db = connect_sqlite(path)
        self.addCleanup(self.db.close)
        sql = Path('supabase/migrations/20261004220000_bar_installment_plans.sql').read_text()
        self.db.conn.executescript(sql.replace('BIGSERIAL PRIMARY KEY', 'INTEGER PRIMARY KEY').replace('TIMESTAMPTZ', 'TEXT'))
        self.player = self.db.execute("INSERT INTO players(name) VALUES('Peladeiro')").lastrowid
        self.product = self.product_row()

    def product_row(self, price=10000, size=1, enabled=1, category='Cerveja'):
        return self.db.execute('''INSERT INTO products(name,category,price_cents,units_per_case,case_sale_enabled,stock)
                                 VALUES(?,?,?,?,?,100)''',
                               (f'Produto {self.db.execute("SELECT COUNT(*) FROM products").fetchone()[0]}', category, price, size, enabled)).lastrowid

    def cart(self, quantity=1, mode='case', product=None):
        return [{'product_id': product or self.product, 'quantity': quantity, 'sale_mode': mode}]

    def sale(self, cart=None):
        cart = cart or self.cart()
        validated = validate_checkout(self.db, cart)
        sale = self.db.execute("INSERT INTO sales(player_id,payment_method,total_cents,paid,payment_status) VALUES(?,'Pix',?,0,'pending')",
                               (self.player, validated['total_cents'])).lastrowid
        for item in validated['items']:
            self.db.execute('INSERT INTO sale_items(sale_id,product_id,quantity,unit_price_cents) VALUES(?,?,?,?)',
                            (sale, item['product_id'], item['quantity'], item['unit_price_cents']))
        self.db.commit()
        return sale, validated['total_cents'], cart

    def plan(self, sale=None, day='2026-10-04', credit=False):
        sale = sale or self.sale()
        return create_installment_plan(self.db, sale[0], sale[1], day, sale[2], credit)

    def test_even_split(self):
        self.assertEqual([i['amount_cents'] for i in self.plan()['installments']], [5000, 5000])

    def test_odd_split_and_total(self):
        self.db.execute('UPDATE products SET price_cents=10001 WHERE id=?', (self.product,))
        result = self.plan()
        self.assertEqual([i['amount_cents'] for i in result['installments']], [5000, 5001])
        self.assertEqual(sum(i['amount_cents'] for i in result['installments']), 10001)

    def test_dates_and_exactly_two(self):
        rows = self.plan()['installments']
        self.assertEqual([i['installment_number'] for i in rows], [1, 2])
        self.assertEqual([i['due_date'] for i in rows], ['2026-10-04', '2026-11-03'])

    def test_minimum_and_invalid_totals(self):
        self.db.execute('UPDATE products SET price_cents=2 WHERE id=?', (self.product,))
        sale = self.sale()
        self.assertEqual([i['amount_cents'] for i in self.plan(sale)['installments']], [1, 1])
        for total in (0, 1, -1, True, 2.0, 2147483648):
            with self.subTest(total=total), self.assertRaises(ValueError):
                create_installment_plan(self.db, sale[0], total, '2026-10-04', sale[2])

    def test_single_case_existing_price_rule_and_browser_values_ignored(self):
        self.db.execute('UPDATE products SET price_cents=1500,units_per_case=24 WHERE id=?', (self.product,))
        cart = self.cart()
        cart[0].update(price_cents=1, units_per_case=1, total_cents=1)
        sale = self.sale(cart)
        result = self.plan(sale)
        self.assertEqual(result['plan']['total_cents'], 36000)
        snapshot = json.loads(result['plan']['checkout_snapshot'])
        self.assertEqual(result['plan']['checkout_origin'], 'case_only')
        self.assertEqual(snapshot[0]['quantity'], 24)
        self.assertEqual(snapshot[0]['unit_price_cents'], 1500)

    def test_multiple_cases_and_products(self):
        cart = self.cart(2) + self.cart(3, product=self.product_row(price=2500, size=2))
        self.assertEqual(self.plan(self.sale(cart))['plan']['total_cents'], 35000)

    def test_unit_and_mixed_blocked(self):
        for cart in (self.cart(mode='unit'), self.cart()+self.cart(mode='unit')):
            with self.subTest(cart=cart), self.assertRaises(ValueError):
                validate_checkout(self.db, cart)

    def test_credits_blocked_in_request_and_database(self):
        sale = self.sale()
        with self.assertRaises(ValueError):
            self.plan(sale, credit=True)
        self.db.execute("INSERT INTO sale_payment_parts(sale_id,method,amount_cents,status) VALUES(?,'Créditos',1,'reserved')", (sale[0],))
        self.db.commit()
        with self.assertRaises(ValueError):
            self.plan(sale)

    def test_disabled_invalid_size_inactive_and_sports_blocked(self):
        for change in ('case_sale_enabled=0', 'units_per_case=0', 'active=0', "category='Material Esportivo'"):
            with self.subTest(change=change):
                self.db.execute('UPDATE products SET '+change+' WHERE id=?', (self.product,))
                with self.assertRaises(ValueError):
                    validate_checkout(self.db, self.cart())
                self.db.execute("UPDATE products SET case_sale_enabled=1,units_per_case=1,active=1,category='Cerveja' WHERE id=?", (self.product,))

    def test_total_or_sale_items_mismatch_blocked(self):
        sale = self.sale()
        with self.assertRaises(ValueError):
            create_installment_plan(self.db, sale[0], 10001, '2026-10-04', sale[2])
        self.db.execute('UPDATE sale_items SET quantity=24 WHERE sale_id=?', (sale[0],))
        self.db.commit()
        with self.assertRaises(ValueError):
            self.plan(sale)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM bar_installment_plans').fetchone()[0], 0)

    def test_idempotence_preserves_history_after_product_changes(self):
        sale = self.sale()
        first = self.plan(sale)
        self.db.execute('UPDATE products SET price_cents=20000,case_sale_enabled=0,units_per_case=24 WHERE id=?', (self.product,))
        self.db.commit()
        self.assertEqual(self.plan(sale), first)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM bar_installments').fetchone()[0], 2)
        with self.assertRaises(ValueError):
            self.plan(sale, day='2026-10-05')
        with self.assertRaises(ValueError):
            self.plan((sale[0], sale[1], self.cart(2)))

    def test_sale_stock_payment_and_delivery_unchanged(self):
        sale = self.sale()
        tables = ('sales', 'sale_items', 'products', 'sale_payment_parts', 'sale_item_deliveries', 'bar_credit_reservations')
        before = {t: [tuple(r) for r in self.db.execute('SELECT * FROM '+t).fetchall()] for t in tables}
        self.plan(sale)
        self.assertEqual(before, {t: [tuple(r) for r in self.db.execute('SELECT * FROM '+t).fetchall()] for t in tables})

    def test_atomic_failure(self):
        sale = self.sale()
        original = self.db.execute
        def fail(sql, *args):
            if 'INSERT INTO bar_installments' in sql:
                raise RuntimeError('falha simulada')
            return original(sql, *args)
        with patch.object(self.db, 'execute', side_effect=fail), self.assertRaises(RuntimeError):
            self.plan(sale)
        self.assertEqual(original('SELECT COUNT(*) FROM bar_installment_plans').fetchone()[0], 0)

    def test_constraints(self):
        result = self.plan()
        plan_id = result['plan']['id']
        statements = [
            ("INSERT INTO bar_installments(plan_id,installment_number,amount_cents,due_date) VALUES(?,?,?,'2026-10-04')", (plan_id, 3, 1)),
            ("INSERT INTO bar_installments(plan_id,installment_number,amount_cents,due_date) VALUES(?,?,?,'2026-10-04')", (plan_id, 1, 1)),
            ("UPDATE bar_installments SET amount_cents=0 WHERE plan_id=?", (plan_id,)),
            ("UPDATE bar_installments SET status='expired' WHERE plan_id=?", (plan_id,)),
            ("UPDATE bar_installment_plans SET checkout_origin='unit' WHERE id=?", (plan_id,)),
            ("UPDATE bar_installment_plans SET status='canceled' WHERE id=?", (plan_id,)),
            ("DELETE FROM sales WHERE id=?", (result['plan']['sale_id'],)),
            ("DELETE FROM bar_installment_plans WHERE id=?", (plan_id,)),
        ]
        for sql, args in statements:
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                with self.db:
                    self.db.execute(sql, args)

    def test_invalid_cart_date_and_sale_state(self):
        for cart in ([], self.cart(0), self.cart(True), [{'product_id': self.product, 'quantity': 1}], self.cart(product=999)):
            with self.subTest(cart=cart), self.assertRaises(ValueError):
                validate_checkout(self.db, cart)
        sale = self.sale()
        with self.assertRaises(ValueError):
            self.plan(sale, day='invalid')
        self.db.execute('UPDATE sales SET paid=1 WHERE id=?', (sale[0],))
        self.db.commit()
        with self.assertRaises(ValueError):
            self.plan(sale)
