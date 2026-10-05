import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import test_bar_installment_payments as bar
import test_sports_installment_reconciliation as sports
from src.services.pix_checkout_closures import (
    request_closure, process_closure, is_abandonment_due, has_confirmed_payment,
)
from src.services.mercadopago import MercadoPagoError, cancel_order


def install_schema(db):
    sql = Path('supabase/migrations/20261005010000_pix_checkout_closures.sql').read_text()
    db.conn.executescript(sql.replace('BIGSERIAL PRIMARY KEY', 'INTEGER PRIMARY KEY').replace('TIMESTAMPTZ', 'TEXT'))


class ClosureTest(bar.BarInstallmentPaymentsTest):
    # Reuse fixture operations, without inheriting the existing test methods.
    __unittest_skip__ = False

    def setUp(self):
        super().setUp()
        self.setup_payments()
        install_schema(self.db)
        self.db.execute("UPDATE sales SET created_at='2026-10-04 12:00:00' WHERE id=?", (self.sale_id,))
        self.db.execute('UPDATE products SET stock=76 WHERE id=?', (self.product,))
        self.db.commit()
        self.attempt = self.attempts()[0]
        self.db.execute('UPDATE sale_items SET quantity=24 WHERE sale_id=?', (self.sale_id,))
        self.db.commit()

    def request(self, reason='timeout', **kwargs):
        return request_closure(self.db, self.sale_id, reason,
                               now=datetime(2026,10,4,12,5,tzinfo=timezone.utc), **kwargs)

    def remote(self, status='canceled'):
        return dict(id=self.attempt['mercado_pago_order_id'], external_reference=self.attempt['external_reference'],status=status)

    def run_closure(self, response=None):
        def remote(token, order_id):
            row=self.db.execute('SELECT external_reference FROM bar_installment_payment_attempts WHERE mercado_pago_order_id=?',(order_id,)).fetchone()
            return dict(id=order_id,external_reference=row[0],status='canceled')
        with patch('src.services.pix_checkout_closures.get_order', side_effect=remote):
            return process_closure(self.db, self.sale_id, 'fake')

    def test_request_idempotent_unique(self):
        self.assertEqual(self.request()['id'], self.request()['id'])
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db:
                self.db.execute("INSERT INTO pix_checkout_closures(sale_id,reason) VALUES(?,'timeout')", (self.sale_id,))

    def test_deadline_and_retry_not_reset(self):
        sale=self.state()[0]
        self.assertFalse(is_abandonment_due(sale, '2026-10-04T12:04:59Z'))
        self.assertTrue(is_abandonment_due(sale, '2026-10-04T12:05:00Z'))
        self.db.execute("UPDATE bar_installment_payment_attempts SET created_at='2026-10-04 12:04:59'")
        self.assertTrue(is_abandonment_due(self.state()[0], '2026-10-04T12:05:00Z'))
        with self.assertRaises(ValueError):
            request_closure(self.db,self.sale_id,'timeout',now=datetime(2026,10,4,12,4,tzinfo=timezone.utc))

    def test_client_author_and_shared_engine(self):
        user=self.db.execute("INSERT INTO users(username,name,password_hash,role,player_id) VALUES('owner','Owner','hash','client',?)",(self.player,)).lastrowid
        self.db.commit()
        record=self.request('client_cancel',requested_by=user,player_id=self.player)
        self.assertEqual(record['requested_by'],user)
        self.assertEqual(self.run_closure()['status'],'completed')
        self.assertEqual(self.state()[0]['payment_status'],'canceled')

    def test_wrong_owner_blocked(self):
        with self.assertRaises(ValueError):
            self.request('client_cancel',requested_by=1,player_id=999)

    def test_paid_sale_and_financial_evidence(self):
        for statement in ("UPDATE sales SET paid=1", "UPDATE sales SET payment_status='approved'",
                          "INSERT INTO sale_payment_parts(sale_id,method,amount_cents,status) SELECT id,'Pix',100,'approved' FROM sales"):
            with self.subTest(statement=statement):
                self.db.execute(statement)
                self.assertTrue(has_confirmed_payment(self.db,self.state()[0]))
                self.db.rollback()

    def test_either_bar_installment_blocks(self):
        for number in (1,2):
            with self.subTest(number=number):
                self.db.execute("UPDATE bar_installments SET status='paid' WHERE installment_number=?",(number,))
                self.assertTrue(has_confirmed_payment(self.db,self.state()[0]))
                self.db.rollback()
        self.db.execute("UPDATE bar_installments SET status='paid' WHERE installment_number=2")
        self.db.commit()
        self.assertEqual(self.request()['status'],'aborted_payment')
        with patch('src.services.pix_checkout_closures.get_order') as api:
            self.assertEqual(process_closure(self.db,self.sale_id,'fake')['status'],'aborted_payment')
        api.assert_not_called()
        self.assertEqual(self.db.execute('SELECT stock FROM products WHERE id=?',(self.product,)).fetchone()[0],76)

    def test_approved_attempt_blocks(self):
        self.db.execute("UPDATE bar_installment_payment_attempts SET status='approved' WHERE id=?",(self.attempt['id'],))
        self.db.commit()
        self.assertEqual(self.request()['status'],'aborted_payment')

    def test_confirmed_remote_and_stock_once_history_preserved(self):
        before=[self.db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ('sales','sale_items','bar_installment_plans','bar_installments','bar_installment_payment_attempts')]
        self.request()
        self.assertEqual(self.run_closure()['status'],'completed')
        self.assertEqual(self.run_closure()['status'],'completed')
        self.assertEqual(self.db.execute('SELECT stock FROM products WHERE id=?',(self.product,)).fetchone()[0],100)
        after=[self.db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ('sales','sale_items','bar_installment_plans','bar_installments','bar_installment_payment_attempts')]
        self.assertEqual(before,after)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM sale_cancellations').fetchone()[0],1)
        self.assertEqual(self.state()[0]['payment_status'],'expired')

    def test_timeout_and_unknown_no_release(self):
        self.request()
        for error in (MercadoPagoError('timeout'), ValueError('unknown')):
            with patch('src.services.pix_checkout_closures.get_order',side_effect=error):
                self.assertEqual(process_closure(self.db,self.sale_id,'fake')['status'],'retryable')
        self.assertEqual(self.state()[0]['payment_status'],'pending')
        self.assertEqual(self.db.execute('SELECT stock FROM products WHERE id=?',(self.product,)).fetchone()[0],76)

    def test_cancel_timeout_and_idempotent_retry(self):
        self.request()
        with patch('src.services.pix_checkout_closures.get_order',return_value=self.remote('action_required')), patch('src.services.pix_checkout_closures.cancel_order',side_effect=MercadoPagoError('timeout')) as cancel:
            for _ in range(2):
                self.assertEqual(process_closure(self.db,self.sale_id,'fake')['status'],'retryable')
            self.assertEqual(cancel.call_args_list[0],cancel.call_args_list[1])
        self.assertEqual(self.db.execute('SELECT stock FROM products WHERE id=?',(self.product,)).fetchone()[0],76)

    def test_remote_cancel_confirmed(self):
        self.request()
        # Both installment charges need confirmed cancellation.
        second=dict(self.remote(),id='BAR-ORDER-2',external_reference=self.db.execute('SELECT external_reference FROM bar_installment_payment_attempts WHERE mercado_pago_order_id=?',('BAR-ORDER-2',)).fetchone()[0])
        with patch('src.services.pix_checkout_closures.get_order',side_effect=[self.remote('action_required'),self.remote(),second]), patch('src.services.pix_checkout_closures.cancel_order') as api:
            self.assertEqual(process_closure(self.db,self.sale_id,'fake')['status'],'completed')
            api.assert_called_once()

    def test_payment_during_network_wins(self):
        self.request()
        def response(*args):
            self.db.execute("UPDATE bar_installments SET status='paid' WHERE installment_number=2")
            self.db.commit()
            return dict(id=args[1],external_reference=self.db.execute('SELECT external_reference FROM bar_installment_payment_attempts WHERE mercado_pago_order_id=?',(args[1],)).fetchone()[0],status='canceled')
        with patch('src.services.pix_checkout_closures.get_order',side_effect=response):
            self.assertEqual(process_closure(self.db,self.sale_id,'fake')['status'],'aborted_payment')
        self.assertEqual(self.db.execute('SELECT stock FROM products WHERE id=?',(self.product,)).fetchone()[0],76)

    def test_closing_blocks_new_charge(self):
        self.request()
        with self.assertRaises(ValueError):
            self.charge(2)

    def test_missing_provider_identity_no_release(self):
        self.request()
        self.db.execute('UPDATE bar_installment_payment_attempts SET mercado_pago_order_id=NULL WHERE id=?',(self.attempt['id'],))
        self.db.commit()
        self.assertEqual(self.run_closure()['status'],'retryable')


# Remove inherited test methods: regressions run separately against original fixtures.
for name in dir(bar.BarInstallmentPaymentsTest):
    if name.startswith('test_') and name not in ClosureTest.__dict__:
        setattr(ClosureTest,name,None)


class SportsClosureTest(sports.InstallmentReconciliationTest):
    def setUp(self):
        super().setUp()
        self.setup_payments()
        # The inherited minimal fixture omits this real catalog column
        # (20260813153000_sports_material_catalog.sql).
        self.db.execute('ALTER TABLE sports_product_variants ADD COLUMN updated_at TEXT')
        self.db.execute('UPDATE sports_product_variants SET updated_at=CURRENT_TIMESTAMP')
        install_schema(self.db)
        self.db.execute("UPDATE sales SET created_at='2026-10-04 12:00:00' WHERE id=?",(self.sale_id,))
        self.db.commit()

    def request(self):
        return request_closure(self.db,self.sale_id,'timeout',now=datetime(2026,10,4,12,5,tzinfo=timezone.utc))

    def test_each_sports_installment_blocks(self):
        for number in (1,2,3):
            self.db.execute("UPDATE sports_installments SET status='paid' WHERE installment_number=?",(number,))
            self.assertTrue(has_confirmed_payment(self.db,self.state()[0]))
            self.db.rollback()
        self.approve(3)
        self.assertEqual(self.request()['status'],'aborted_payment')

    def test_sports_stock_once_and_history(self):
        stock=self.db.execute('SELECT stock FROM sports_product_variants').fetchone()[0]
        self.request()
        def remote(token,order_id):
            a=next(a for a in self.attempts if a['mercado_pago_order_id']==order_id)
            return dict(id=order_id,external_reference=a['external_reference'],status='canceled')
        with patch('src.services.pix_checkout_closures.get_order',side_effect=remote):
            self.assertEqual(process_closure(self.db,self.sale_id,'fake')['status'],'completed')
            self.assertEqual(process_closure(self.db,self.sale_id,'fake')['status'],'completed')
        self.assertEqual(self.db.execute('SELECT stock FROM sports_product_variants').fetchone()[0],stock+1)
        self.assertEqual(self.db.execute('SELECT status FROM sports_stock_reservations').fetchone()[0],'released')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM sports_installments').fetchone()[0],3)

for name in dir(sports.InstallmentReconciliationTest):
    if name.startswith('test_') and name not in SportsClosureTest.__dict__:
        setattr(SportsClosureTest,name,None)


class ClientTest(unittest.TestCase):
    def test_cancel_client_request_and_timeout(self):
        with patch('src.services.mercadopago._request',return_value={'status':'canceled'}) as api:
            self.assertEqual(cancel_order('fake','ORD-1','stable')['status'],'canceled')
            self.assertEqual(api.call_args.args[:3],('POST','/v1/orders/ORD-1/cancel','fake'))
            self.assertEqual(api.call_args.kwargs['idempotency_key'],'stable')
        with patch('src.services.mercadopago._request',side_effect=MercadoPagoError('timeout')):
            with self.assertRaises(MercadoPagoError):
                cancel_order('fake','ORD-1','stable')
