"""Final acceptance using real local sale/payment/delivery/cancel routes."""
import uuid
import unittest

import test_mercadopago as fixtures
from app import app
from src.db import get_db
from src.services.cash_register import session_summary
from src.services.monthly_sales_report import monthly_sales_data
from src.services.purchase_forecast import purchase_forecast
from src.utils import local_today


class FinalCancellationValidationTest(unittest.TestCase):
    setUp = fixtures.MercadoPagoFlowTest.setUp
    tearDown = fixtures.MercadoPagoFlowTest.tearDown
    create_sports_schema = fixtures.MercadoPagoFlowTest.create_sports_schema
    login_manager = fixtures.MercadoPagoFlowTest.login_manager

    def test_original_cash_delivered_beer_and_cancelled_snack(self):
        self.login_manager()
        app.config.update(EXTERNAL_PAYMENTS_ENABLED=False, APP_ENV='local')
        with app.app_context():
            db=get_db()
            db.execute("UPDATE products SET name='Batata sintética',price_cents=1200,stock=10 WHERE id=?",(self.product_id,))
            beer=db.execute("INSERT INTO products(name,category,price_cents,cost_cents,stock) VALUES('Cerveja sintética','Bebida',800,200,10)").lastrowid
            db.execute('UPDATE purchase_observation_control SET enabled=1 WHERE id=1')
            db.commit()
        response=self.client.post('/sale',data={'department':'bar','player_id':self.player_id,
            'payment_method':'Dinheiro','product_id':[str(beer),str(self.product_id)],'quantity':['1','1'],'sale_mode':['unit','unit']})
        self.assertEqual(response.status_code,303)
        with app.app_context():
            db=get_db()
            sale=db.execute('SELECT * FROM sales ORDER BY id DESC LIMIT 1').fetchone()
            sale_id=sale['id']
            items={row['product_id']:dict(row) for row in db.execute('SELECT * FROM sale_items WHERE sale_id=?',(sale_id,)).fetchall()}
            self.assertEqual(db.execute('SELECT stock FROM products WHERE id=?',(self.product_id,)).fetchone()[0],9)
            self.assertEqual((sale['paid'],sale['total_cents']), (0,2000))
        payment=self.client.post(f'/orders/{sale_id}/confirm-payment',json={'amount_received_cents':2000,'convert_change_to_credit':False})
        self.assertEqual(payment.status_code,200,payment.get_json())
        self.assertEqual(payment.get_json()['change_cents'],0)
        delivered=self.client.post(f'/orders/{sale_id}/deliver',json={'sale_item_id':items[beer]['id'],'quantity':1})
        self.assertEqual(delivered.status_code,200,delivered.get_json())
        with app.app_context():
            db=get_db()
            before={table:[tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()]
                    for table in ['sales','sale_items','sale_payment_parts','sale_item_deliveries']}
        body={'sale_item_id':items[self.product_id]['id'],'quantity':1,'reason':'Produto sintético não entregue',
              'confirmed':True,'idempotency_key':str(uuid.uuid4())}
        canceled=self.client.post(f'/orders/{sale_id}/cancel-item',json=body)
        self.assertEqual(canceled.status_code,200,canceled.get_json())
        self.assertEqual(self.client.post(f'/orders/{sale_id}/cancel-item',json=body).get_json(),canceled.get_json())
        self.assertEqual(self.client.post(f'/orders/{sale_id}/cancel-item',json=dict(body,idempotency_key=str(uuid.uuid4()))).status_code,409)
        with app.app_context():
            db=get_db()
            for table,rows in before.items():
                self.assertEqual(rows,[tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()],table)
            sale=db.execute('SELECT * FROM sales_net WHERE id=?',(sale_id,)).fetchone()
            self.assertEqual((sale['total_cents'],sale['canceled_cents'],sale['net_total_cents']), (2000,1200,800))
            self.assertEqual(db.execute('SELECT balance_cents FROM bar_credit_accounts WHERE player_id=?',(self.player_id,)).fetchone()[0],1200)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM bar_credit_transactions').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM bar_credit_audit').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sale_item_cancellations').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT stock FROM products WHERE id=?',(self.product_id,)).fetchone()[0],10)
            self.assertEqual(db.execute('SELECT stock FROM products WHERE id=?',(beer,)).fetchone()[0],9)
            business_date=local_today().isoformat()
            session_id=db.execute("INSERT INTO cash_sessions(business_date,opening_cash_cents,opening_bank_cents,status) VALUES(?,0,0,'open')",(business_date,)).lastrowid
            db.commit()
            cash=session_summary(db,db.execute('SELECT * FROM cash_sessions WHERE id=?',(session_id,)).fetchone())
            self.assertEqual((cash['cash_sales'],cash['expected_cash']), (2000,2000))
            monthly=monthly_sales_data(db,business_date[:7])['summary']
            self.assertEqual((monthly['gross_revenue'],monthly['canceled_cents'],monthly['revenue'],monthly['items_sold']), (2000,1200,800,1))
            month_end=local_today().replace(day=28)
            from datetime import timedelta
            planned=(month_end+timedelta(days=4)).replace(day=1).strftime('%Y-%m')
            forecast=purchase_forecast(db,planned,3,10)
            quantities={p['product_id']:sum(m['quantity'] for m in p['monthly_consumption']) for p in forecast['products']}
            self.assertEqual((quantities[beer],quantities[self.product_id]), (1,0))
            self.assertGreater(db.execute("SELECT COUNT(*) FROM purchase_observation_events WHERE product_id=? AND source_table='products'",(self.product_id,)).fetchone()[0],0)
        self.assertEqual(self.client.get('/orders/feed').get_json()['pending'],[])
        for url in ['/orders','/reports',f'/sales/{sale_id}/receipt',f'/relatorios/vendas/{sale_id}',f'/relatorios/vendas/{sale_id}.pdf']:
            self.assertEqual(self.client.get(url).status_code,200,url)

    def test_pwa_assets_and_protected_network_responses(self):
        self.login_manager()
        manifest=self.client.get('/static/manifest.webmanifest')
        self.assertEqual(manifest.status_code,200)
        self.assertEqual(manifest.get_json()['display'],'standalone')
        worker=self.client.get('/service-worker.js')
        self.assertEqual(worker.status_code,200)
        self.assertIn('no-store',worker.headers['Cache-Control'])
        source=worker.get_data(as_text=True)
        self.assertIn('if (request.method !== "GET") return;',source)
        self.assertIn('Authenticated pages are network-only',source)
        html=self.client.get('/orders').get_data(as_text=True)
        self.assertIn('name="viewport"',html)
        self.assertIn('id="cancel-item-dialog"',html)
        self.assertIn('id="cancel-item-reason"',html)
        self.assertIn('minlength="5"',html)
        self.assertIn('id="cancel-item-confirmed"',html)
        # This checks server/assets only. No viewport rendering or installed PWA claim.
