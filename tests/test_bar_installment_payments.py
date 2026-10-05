import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import test_bar_installments as fixtures
from src.services.bar_installment_payments import create_installment_payment_attempt
from src.services.bar_installment_reconciliation import find_installment_attempt, reconcile_installment_order
from src.services.mercadopago import MercadoPagoError


class BarInstallmentPaymentsTest(unittest.TestCase):
    setUp = fixtures.BarInstallmentsTest.setUp
    product_row = fixtures.BarInstallmentsTest.product_row
    cart = fixtures.BarInstallmentsTest.cart
    sale = fixtures.BarInstallmentsTest.sale
    plan = fixtures.BarInstallmentsTest.plan

    def setup_payments(self):
        sql = Path('supabase/migrations/20261004230000_bar_installment_payment_attempts.sql').read_text()
        self.db.conn.executescript(sql.replace('BIGSERIAL PRIMARY KEY','INTEGER PRIMARY KEY').replace('TIMESTAMPTZ','TEXT'))
        self.db.execute("UPDATE players SET email='client@example.com' WHERE id=?", (self.player,))
        self.sale_data = self.sale()
        self.result = self.plan(self.sale_data)
        self.installments = self.result['installments']
        self.sale_id = self.sale_data[0]

    def order(self, number=1):
        return {'id':f'BAR-ORDER-{number}', 'transactions':{'payments':[{'id':f'BAR-PAY-{number}',
                'payment_method':{'qr_code':f'QR-{number}', 'qr_code_base64':'encoded'}}]}}

    def charge(self, number=1, key=None):
        return create_installment_payment_attempt(self.db, self.installments[number-1]['id'], 'fake', key)

    def event(self, attempt, status='processed', amount='50.00'):
        return dict(id=attempt['mercado_pago_order_id'],external_reference=attempt['external_reference'],
                    status=status,status_detail='accredited',total_paid_amount=amount,
                    transactions={'payments':[{'id':attempt['mercado_pago_payment_id']}]})

    def attempts(self):
        with patch('src.services.bar_installment_payments.create_pix_order', side_effect=[self.order(1),self.order(2)]):
            return self.charge(1), self.charge(2)

    def state(self):
        return (dict(self.db.execute('SELECT * FROM sales WHERE id=?',(self.sale_id,)).fetchone()),
                dict(self.db.execute('SELECT * FROM bar_installment_plans WHERE sale_id=?',(self.sale_id,)).fetchone()))

    def test_correct_amounts_and_advance_second(self):
        self.setup_payments()
        self.db.execute('UPDATE bar_installments SET amount_cents=5001 WHERE id=?',(self.installments[1]['id'],))
        # Inconsistent schedule must fail before any provider request.
        self.db.commit()
        with patch('src.services.bar_installment_payments.create_pix_order') as api, self.assertRaises(ValueError):
            self.charge(2)
        api.assert_not_called()
        self.db.execute('UPDATE bar_installments SET amount_cents=5000 WHERE id=?',(self.installments[1]['id'],))
        self.db.commit()
        for number in (1,2):
            with patch('src.services.bar_installment_payments.create_pix_order',return_value=self.order(number)) as api:
                attempt = self.charge(number)
            self.assertEqual(api.call_args.args[2],5000)
            self.assertEqual(attempt['amount_cents'],5000)
            self.assertTrue(attempt['external_reference'].startswith(f"bar2x_p{self.result['plan']['id']}_i{number}_a"))

    def test_idempotency_and_live_qr_reuse(self):
        self.setup_payments()
        key=str(uuid4())
        with patch('src.services.bar_installment_payments.create_pix_order',return_value=self.order()) as api:
            first=self.charge(key=key)
            self.assertEqual(first,self.charge(key=key))
            self.assertEqual(first['id'],self.charge()['id'])
        self.assertEqual(api.call_count,1)
        with self.assertRaises(ValueError):
            self.charge(2,key)

    def test_expiration_preserves_attempt_and_installment_due_date(self):
        self.setup_payments()
        with patch('src.services.bar_installment_payments.create_pix_order',side_effect=[self.order(1),self.order(3)]):
            first=self.charge()
            self.db.execute('UPDATE bar_installment_payment_attempts SET expires_at=? WHERE id=?',
                            ((datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(),first['id']))
            self.db.commit()
            second=self.charge()
        self.assertNotEqual(first['id'],second['id'])
        self.assertEqual(self.db.execute('SELECT status FROM bar_installment_payment_attempts WHERE id=?',(first['id'],)).fetchone()[0],'expired')
        self.assertEqual(self.db.execute('SELECT due_date FROM bar_installments WHERE id=?',(first['installment_id'],)).fetchone()[0],'2026-10-04')

    def test_first_does_not_pay_sale_but_unlocks_delivery(self):
        self.setup_payments(); first,second=self.attempts()
        reconcile_installment_order(self.db,self.event(first))
        sale,plan=self.state()
        self.assertEqual((sale['paid'],sale['payment_status'],sale['ready_for_delivery'],plan['status']),(0,'pending',1,'active'))
        self.assertIsNotNone(plan['first_installment_paid_at'])
        self.assertIsNone(plan['fully_paid_at'])
        with patch('src.services.bar_installment_payments.create_pix_order') as api,self.assertRaises(ValueError):
            self.charge()
        api.assert_not_called()

    def test_order_first_second(self):
        self.setup_payments(); first,second=self.attempts()
        reconcile_installment_order(self.db,self.event(first))
        reconcile_installment_order(self.db,self.event(second))
        sale,plan=self.state()
        self.assertEqual((sale['paid'],sale['payment_status'],plan['status']),(1,'approved','paid'))
        self.assertIsNotNone(plan['fully_paid_at'])
        before=self.state()
        reconcile_installment_order(self.db,self.event(second))
        reconcile_installment_order(self.db,self.event(first,'expired'))
        self.assertEqual(before,self.state())

    def test_order_second_first(self):
        self.setup_payments(); first,second=self.attempts()
        reconcile_installment_order(self.db,self.event(second))
        sale,plan=self.state()
        self.assertEqual((sale['paid'],plan['status']),(0,'pending'))
        self.assertIsNone(plan['first_installment_paid_at'])
        reconcile_installment_order(self.db,self.event(first))
        self.assertEqual((self.state()[0]['paid'],self.state()[1]['status']),(1,'paid'))

    def test_terminal_second_preserves_sale_stock_and_first(self):
        self.setup_payments(); first,second=self.attempts()
        reconcile_installment_order(self.db,self.event(first))
        before=self.state()
        stock=self.db.execute('SELECT stock FROM products WHERE id=?',(self.product,)).fetchone()[0]
        for status in ('failed','expired','canceled'):
            reconcile_installment_order(self.db,self.event(second,status))
            self.assertEqual(before,self.state())
            self.assertEqual(self.db.execute('SELECT stock FROM products WHERE id=?',(self.product,)).fetchone()[0],stock)
        self.assertEqual(self.db.execute('SELECT status FROM bar_installments WHERE id=?',(first['installment_id'],)).fetchone()[0],'paid')

    def test_wrong_amount_and_conflicting_ids_rejected(self):
        self.setup_payments(); first,second=self.attempts()
        for amount in ('49.99','50.001','NaN'):
            with self.assertRaises(ValueError):
                reconcile_installment_order(self.db,self.event(first,amount=amount))
        event=self.event(first);event['id']=second['mercado_pago_order_id']
        with self.assertRaises(ValueError):find_installment_attempt(self.db,event)
        self.assertEqual(self.state()[1]['status'],'pending')

    def test_failure_and_timeout_are_durable_without_sale_mutation(self):
        self.setup_payments()
        before=self.state()
        key=str(uuid4())
        with patch('src.services.bar_installment_payments.create_pix_order',side_effect=MercadoPagoError('mock')):
            with self.assertRaises(MercadoPagoError):self.charge(key=key)
            self.assertEqual(self.charge(key=key)['status'],'failed')
        error=MercadoPagoError('timeout');error.__cause__=TimeoutError()
        with patch('src.services.bar_installment_payments.create_pix_order',side_effect=error) as api:
            with self.assertRaises(MercadoPagoError):self.charge()
            self.assertEqual(self.charge()['status'],'creating')
        self.assertEqual(api.call_count,1)
        self.assertEqual(before,self.state())

    def test_unknown_bar_reference_never_falls_back(self):
        self.setup_payments()
        with self.assertRaises(ValueError):
            find_installment_attempt(self.db,{'external_reference':'bar2x_unknown'})
        self.assertIsNone(find_installment_attempt(self.db,{'external_reference':'pelada_unknown'}))
        from src.routes.sales import apply_mercadopago_status
        with self.assertRaises(ValueError):apply_mercadopago_status(self.db,self.state()[0],{})

    def test_client_ownership(self):
        self.setup_payments()
        with patch('src.services.bar_installment_payments.create_pix_order') as api,self.assertRaises(ValueError):
            create_installment_payment_attempt(self.db,self.installments[0]['id'],'fake',player_id=self.player+1)
        api.assert_not_called()


import test_mercadopago as route_fixtures
from app import app
from src.db import get_db
from src.services.bar_installments import create_installment_plan


class BarInstallmentRoutesTest(unittest.TestCase):
    setUp = route_fixtures.MercadoPagoFlowTest.setUp
    tearDown = route_fixtures.MercadoPagoFlowTest.tearDown
    create_sports_schema = route_fixtures.MercadoPagoFlowTest.create_sports_schema
    login_manager = route_fixtures.MercadoPagoFlowTest.login_manager
    headers = route_fixtures.MercadoPagoFlowTest.headers

    def setup_bar(self):
        self.login_manager()
        app.config['EXTERNAL_PAYMENTS_ENABLED']=True
        with app.app_context():
            db=get_db()
            for name in ('20261004220000_bar_installment_plans.sql','20261004230000_bar_installment_payment_attempts.sql'):
                sql=Path('supabase/migrations/'+name).read_text()
                db.conn.executescript(sql.replace('BIGSERIAL PRIMARY KEY','INTEGER PRIMARY KEY').replace('TIMESTAMPTZ','TEXT'))
            db.execute('UPDATE products SET case_sale_enabled=1,units_per_case=24,price_cents=1500 WHERE id=?',(self.product_id,))
            self.sale_id=db.execute("INSERT INTO sales(player_id,payment_method,total_cents,paid,payment_status) VALUES(?,'Pix',36000,0,'pending')",(self.player_id,)).lastrowid
            db.execute('INSERT INTO sale_items(sale_id,product_id,quantity,unit_price_cents) VALUES(?,?,24,1500)',(self.sale_id,self.product_id))
            db.commit()
            result=create_installment_plan(db,self.sale_id,36000,'2026-10-04',[dict(product_id=self.product_id,quantity=1,sale_mode='case')])
            self.installment_id=result['installments'][0]['id']
        with patch('src.services.bar_installment_payments.create_pix_order',return_value={'id':'BAR-ROUTE-1','transactions':{'payments':[{'id':'BAR-P-1','payment_method':{'qr_code':'QR','qr_code_base64':'encoded'}}]}}) as api:
            response=self.client.post(f'/bar/parcelas/{self.installment_id}/pix',json={'amount_cents':1})
        self.assertEqual(response.status_code,200)
        self.assertEqual(api.call_args.args[2],18000)
        self.attempt=response.json['attempt']
        self.event=dict(id='BAR-ROUTE-1',external_reference=self.attempt['external_reference'],status='processed',status_detail='accredited',total_paid_amount='180.00',transactions={'payments':[{'id':'BAR-P-1'}]})

    def test_webhook_authenticated_response_routes_bar_before_whole_sale(self):
        self.setup_bar()
        with patch('src.routes.sales.validate_webhook_signature',return_value=True), patch('src.routes.sales.get_order',return_value=self.event),patch('src.routes.sales.apply_mercadopago_status') as legacy:
            response=self.client.post('/webhooks/mercadopago',json={'data':{'id':'BAR-ROUTE-1'}})
            duplicate=self.client.post('/webhooks/mercadopago',json={'data':{'id':'BAR-ROUTE-1'}})
        self.assertEqual(response.status_code,200)
        self.assertEqual(duplicate.status_code,200)
        legacy.assert_not_called()
        with app.app_context():
            self.assertEqual(get_db().execute('SELECT status FROM bar_installment_plans WHERE sale_id=?',(self.sale_id,)).fetchone()[0],'active')
            self.assertEqual(get_db().execute('SELECT paid FROM sales WHERE id=?',(self.sale_id,)).fetchone()[0],0)

    def test_polling_routes_bar_and_blocks_whole_sale_polling(self):
        self.setup_bar()
        with patch('src.routes.sales.get_order',return_value=self.event),patch('src.routes.sales.apply_mercadopago_status') as legacy:
            response=self.client.get(f'/bar/parcelas/{self.installment_id}/status')
            whole=self.client.get(f'/pix/mercadopago/orders/{self.sale_id}/status',headers=self.headers())
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['installment']['status'],'paid')
        self.assertEqual(whole.status_code,409)
        legacy.assert_not_called()

    def test_unknown_bar_webhook_does_not_fall_through(self):
        self.setup_bar()
        with patch('src.routes.sales.validate_webhook_signature',return_value=True),patch('src.routes.sales.apply_mercadopago_status') as legacy:
            response=self.client.post('/webhooks/mercadopago',json={'data':{'id':'unknown','external_reference':'bar2x_unknown'}})
        self.assertEqual(response.status_code,500)
        legacy.assert_not_called()

    def test_provider_mismatch_does_not_approve(self):
        self.setup_bar()
        wrong=dict(self.event,external_reference='bar2x_unknown')
        with patch('src.routes.sales.validate_webhook_signature',return_value=True),patch('src.routes.sales.get_order',return_value=wrong):
            response=self.client.post('/webhooks/mercadopago',json={'data':{'id':'BAR-ROUTE-1'}})
        self.assertEqual(response.status_code,500)
        with app.app_context():
            self.assertEqual(get_db().execute('SELECT status FROM bar_installments WHERE id=?',(self.installment_id,)).fetchone()[0],'pending')

    def test_client_cannot_access_another_player_and_local_block(self):
        self.setup_bar()
        app.config['EXTERNAL_PAYMENTS_ENABLED']=False
        self.assertEqual(self.client.post(f'/bar/parcelas/{self.installment_id}/pix',json={}).status_code,403)
        with app.app_context():
            db=get_db()
            other=db.execute("INSERT INTO players(name) VALUES('Outro')").lastrowid
            user=db.execute("INSERT INTO users(username,name,password_hash,role,player_id) VALUES('other','Outro','hash','client',?)",(other,)).lastrowid
            db.commit()
        with self.client.session_transaction() as session:session['user_id']=user
        app.config['EXTERNAL_PAYMENTS_ENABLED']=True
        self.assertEqual(self.client.get(f'/bar/parcelas/{self.installment_id}/status').status_code,404)
        self.assertEqual(self.client.post(f'/bar/parcelas/{self.installment_id}/pix',json={}).status_code,404)
