import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import test_mercadopago as fixtures
from app import app
from src.db import get_db
from src.services.bar_installment_reconciliation import reconcile_installment_order


class BarInstallmentClientTest(unittest.TestCase):
    setUp = fixtures.MercadoPagoFlowTest.setUp
    tearDown = fixtures.MercadoPagoFlowTest.tearDown
    create_sports_schema = fixtures.MercadoPagoFlowTest.create_sports_schema
    login_manager = fixtures.MercadoPagoFlowTest.login_manager
    headers = fixtures.MercadoPagoFlowTest.headers

    def setup_client(self):
        app.config['EXTERNAL_PAYMENTS_ENABLED']=True
        with app.app_context():
            db=get_db()
            for name in ('20261004220000_bar_installment_plans.sql','20261004230000_bar_installment_payment_attempts.sql'):
                sql=Path('supabase/migrations/'+name).read_text()
                db.conn.executescript(sql.replace('BIGSERIAL PRIMARY KEY','INTEGER PRIMARY KEY').replace('TIMESTAMPTZ','TEXT'))
            db.execute('UPDATE products SET case_sale_enabled=1,units_per_case=24,price_cents=1500,stock=100 WHERE id=?',(self.product_id,))
            self.client_id=db.execute("INSERT INTO users(username,name,password_hash,role,player_id) VALUES('barclient','Cliente','hash','client',?)",(self.player_id,)).lastrowid
            db.commit()
        with self.client.session_transaction() as session:session['user_id']=self.client_id
        self.items=[dict(product_id=str(self.product_id),quantity='1',sale_mode='case')]
        self.key=str(uuid4())

    def provider(self, number=1):
        return dict(id=f'CLIENT-ORDER-{number}',transactions={'payments':[dict(id=f'CLIENT-PAY-{number}',payment_method=dict(qr_code=f'QR-{number}',qr_code_base64='encoded'))]})

    def checkout(self, **kwargs):
        body=dict(items=self.items,idempotency_key=self.key,amount_cents=1,price_cents=1,units_per_case=1)
        body.update(kwargs)
        with patch('src.services.bar_installment_payments.create_pix_order',return_value=self.provider()) as api:
            response=self.client.post('/bar/parcelamento/checkout',json=body)
        return response,api

    def buy(self):
        response,api=self.checkout()
        self.assertEqual(response.status_code,200)
        self.sale_id=response.json['installment']['sale_id']
        self.first=response.json['attempt']
        with app.app_context():
            self.second_id=get_db().execute('SELECT i.id FROM bar_installments i JOIN bar_installment_plans p ON p.id=i.plan_id WHERE p.sale_id=? AND i.installment_number=2',(self.sale_id,)).fetchone()[0]
        return response

    def approve(self, attempt, status='processed'):
        with app.app_context():
            reconcile_installment_order(get_db(),dict(id=attempt['mercado_pago_order_id'],external_reference=attempt['external_reference'],status=status,status_detail='accredited',total_paid_amount='180.00',transactions={'payments':[{'id':attempt['mercado_pago_payment_id']}]}))

    def test_choice_and_server_preview(self):
        self.setup_client()
        page=self.client.get('/sale')
        self.assertEqual(page.status_code,200)
        self.assertIn(b'bar-pix-choice',page.data)
        self.assertIn(b'Pix em 2x',page.data)
        for mode,expected in (('case',200),('unit',400)):
            items=[dict(self.items[0],sale_mode=mode)]
            self.assertEqual(self.client.post('/bar/parcelamento/preview',json=dict(items=items)).status_code,expected)
        mixed=self.items+[dict(self.items[0],sale_mode='unit')]
        self.assertEqual(self.client.post('/bar/parcelamento/preview',json=dict(items=mixed)).status_code,400)
        script=Path('static/bar_installments.js').read_text()
        self.assertIn("cart.every(item=>item.sale_mode==='case')",script)
        self.assertIn("choice.classList.remove('d-none')",script)

    def test_manual_ineligible_requests_have_no_effect(self):
        self.setup_client()
        for items,credit in (([dict(self.items[0],sale_mode='unit')],False),(self.items+[dict(self.items[0],sale_mode='unit')],False),(self.items,True)):
            response,api=self.checkout(items=items,use_bar_credit=credit)
            self.assertEqual(response.status_code,409)
            api.assert_not_called()
        with app.app_context():
            db=get_db();db.execute('UPDATE products SET case_sale_enabled=0 WHERE id=?',(self.product_id,));db.commit()
        response,api=self.checkout()
        self.assertEqual(response.status_code,409);api.assert_not_called()
        with app.app_context():
            db=get_db()
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sales').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT stock FROM products WHERE id=?',(self.product_id,)).fetchone()[0],100)

    def test_checkout_qr_price_units_and_idempotence(self):
        self.setup_client();response=self.buy()
        self.assertEqual(response.json['payload'],'QR-1')
        self.assertEqual(response.json['plan']['total_cents'],36000)
        repeat,api=self.checkout();self.assertEqual(repeat.status_code,200);api.assert_not_called()
        with app.app_context():
            db=get_db();rows=db.execute('SELECT * FROM sale_items WHERE sale_id=?',(self.sale_id,)).fetchall()
            self.assertEqual([(r['quantity'],r['unit_price_cents']) for r in rows],[(24,1500)])
            self.assertEqual(db.execute('SELECT stock FROM products WHERE id=?',(self.product_id,)).fetchone()[0],76)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sales').fetchone()[0],1)

    def test_insufficient_stock_atomic(self):
        self.setup_client()
        response,api=self.checkout(items=[dict(self.items[0],quantity='5')])
        self.assertEqual(response.status_code,409);api.assert_not_called()
        with app.app_context():self.assertEqual(get_db().execute('SELECT COUNT(*) FROM sales').fetchone()[0],0)

    def test_first_pending_blocks_and_paid_unlocks_filters_and_delivery(self):
        self.setup_client();self.buy();self.login_manager()
        self.assertEqual(self.client.post(f'/orders/{self.sale_id}/deliver',json={}).status_code,409)
        self.approve(self.first)
        feed=self.client.get('/orders/feed')
        self.assertEqual(feed.status_code,200)
        self.assertIn(self.sale_id,[r['id'] for r in feed.json['pending']])
        self.assertEqual(self.client.post(f'/orders/{self.sale_id}/deliver',json={}).status_code,200)
        with app.app_context():
            self.assertEqual(get_db().execute('SELECT paid FROM sales WHERE id=?',(self.sale_id,)).fetchone()[0],0)
        with self.client.session_transaction() as session:session['user_id']=self.client_id
        page=self.client.get('/minhas-compras');self.assertEqual(page.status_code,200)
        self.assertIn(b'Pix 2x',page.data)

    def test_later_failure_preserves_withdrawal_and_stock(self):
        self.setup_client();self.buy();self.approve(self.first)
        with patch('src.services.bar_installment_payments.create_pix_order',return_value=self.provider(2)):
            response=self.client.post(f'/bar/parcelas/{self.second_id}/pix',json={})
        self.assertEqual(response.status_code,200)
        self.approve(response.json['attempt'],'failed')
        self.login_manager()
        self.assertEqual(self.client.post(f'/orders/{self.sale_id}/deliver',json={}).status_code,200)
        with app.app_context():self.assertEqual(get_db().execute('SELECT stock FROM products WHERE id=?',(self.product_id,)).fetchone()[0],76)

    def test_advance_second_and_settlement(self):
        self.setup_client();self.buy()
        with patch('src.services.bar_installment_payments.create_pix_order',return_value=self.provider(2)):
            second=self.client.post(f'/bar/parcelas/{self.second_id}/pix',json={})
        self.assertEqual(second.status_code,200);self.approve(second.json['attempt'])
        self.login_manager()
        self.assertEqual(self.client.post(f'/orders/{self.sale_id}/deliver',json={}).status_code,409)
        self.approve(self.first)
        with app.app_context():
            sale=get_db().execute('SELECT * FROM sales WHERE id=?',(self.sale_id,)).fetchone()
            self.assertEqual((sale['paid'],sale['payment_status']),(1,'approved'))

    def test_ownership_and_client_cancel_blocked(self):
        self.setup_client();self.buy();self.approve(self.first)
        self.login_manager()
        with app.app_context():
            item_id=get_db().execute('SELECT id FROM sale_items WHERE sale_id=?',(self.sale_id,)).fetchone()[0]
        self.assertEqual(self.client.post(f'/orders/{self.sale_id}/deliver',json=dict(sale_item_id=item_id,quantity=1)).status_code,200)
        with self.client.session_transaction() as session:session['user_id']=self.client_id
        def snapshot():
            with app.app_context():
                db=get_db()
                return {table:[tuple(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY 1').fetchall()]
                        for table in ('sales','bar_installment_plans','bar_installments','bar_installment_payment_attempts',
                                      'products','sale_item_deliveries','sale_item_delivery_restorations','sale_cancellations')}
        before=snapshot()
        for path in (f'/orders/{self.sale_id}/cancel',f'/sales/{self.sale_id}/delete'):
            response=self.client.post(path,json={})
            self.assertEqual(response.status_code,302)
            self.assertEqual(response.headers['Location'],'/sale')
            self.assertEqual(snapshot(),before,'CLIENT não pode cancelar, restaurar estoque ou desfazer retirada.')
        response=self.client.post(f'/orders/{self.sale_id}/cancel',json={},headers={'Accept':'application/json'})
        self.assertEqual(response.status_code,403)
        self.assertEqual(snapshot(),before)
        with app.app_context():
            db=get_db();other=db.execute("INSERT INTO players(name) VALUES('Outro')").lastrowid
            db.execute('UPDATE users SET player_id=? WHERE id=?',(other,self.client_id));db.commit()
        self.assertEqual(self.client.get(f"/bar/parcelas/{self.first['installment_id']}/status").status_code,404)
        self.assertEqual(self.client.post(f'/bar/parcelas/{self.second_id}/pix',json={}).status_code,404)
        page=self.client.get('/minhas-compras');self.assertNotIn(b'bar-installment-plan',page.data)
