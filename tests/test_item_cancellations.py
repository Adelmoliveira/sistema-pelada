import uuid
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import test_mercadopago as fixtures
from app import app
from src.db import get_db
from src.services.item_cancellations import cancel_item


class ItemCancellationTest(unittest.TestCase):
    setUp = fixtures.MercadoPagoFlowTest.setUp
    tearDown = fixtures.MercadoPagoFlowTest.tearDown
    create_sports_schema = fixtures.MercadoPagoFlowTest.create_sports_schema
    login_manager = fixtures.MercadoPagoFlowTest.login_manager

    def order(self, quantity=1, delivered_beer=True, mixed=False):
        self.login_manager()
        with app.app_context():
            db = get_db()
            beer = db.execute("INSERT INTO products(name,category,stock,price_cents) VALUES('Cerveja','Bebida',9,800)").lastrowid
            sale = db.execute("""INSERT INTO sales(player_id,payment_method,total_cents,paid,payment_status,ready_for_delivery)
                VALUES(?,'Dinheiro',?,1,'approved',1)""", (self.player_id, 800+1200*quantity)).lastrowid
            db.execute('INSERT INTO sale_items(sale_id,product_id,quantity,unit_price_cents) VALUES(?,?,1,800)', (sale,beer))
            beer_item = db.execute('SELECT id FROM sale_items WHERE sale_id=?', (sale,)).fetchone()['id']
            snack = db.execute('INSERT INTO sale_items(sale_id,product_id,quantity,unit_price_cents) VALUES(?,?,?,1200)', (sale,self.product_id,quantity)).lastrowid
            db.execute("INSERT INTO sale_payment_parts(sale_id,method,amount_cents,status) VALUES(?,'Dinheiro',?,'approved')", (sale,800+1200*quantity-(300 if mixed else 0)))
            if mixed:
                db.execute("INSERT INTO sale_payment_parts(sale_id,method,amount_cents,status) VALUES(?,'Créditos',300,'approved')", (sale,))
            if delivered_beer:
                op = db.execute('INSERT INTO sale_delivery_operations(sale_id,delivered_by) VALUES(?,?)', (sale,self.user_id)).lastrowid
                db.execute('INSERT INTO sale_item_deliveries(delivery_operation_id,sale_item_id,quantity,delivered_by) VALUES(?,?,1,?)', (op,beer_item,self.user_id))
            db.commit()
        self.sale,self.snack,self.beer_item = sale,snack,beer_item
        return sale

    def cancel(self, quantity=1, item=None, key=None, **extra):
        return self.client.post(f'/orders/{self.sale}/cancel-item', json={
            'sale_item_id': item or self.snack, 'quantity':quantity,'reason':'Produto não entregue',
            'idempotency_key':key or str(uuid.uuid4()),'confirmed':True,**extra})

    def snapshot(self):
        with app.app_context():
            db=get_db()
            return {t:[tuple(r) for r in db.execute(f'SELECT * FROM {t} ORDER BY id').fetchall()]
                    for t in ['sales','sale_items','sale_payment_parts','products','bar_credit_accounts',
                              'bar_credit_transactions','bar_credit_audit','sale_item_cancellations','sale_item_deliveries']}

    def test_cancel_preserves_delivered_item_and_original_receipt(self):
        self.order()
        before=self.snapshot()
        response=self.cancel()
        self.assertEqual(response.status_code,200,response.get_json())
        after=self.snapshot()
        for table in ['sales','sale_items','sale_payment_parts','sale_item_deliveries']:
            self.assertEqual(before[table],after[table])
        with app.app_context():
            db=get_db()
            self.assertEqual(db.execute('SELECT balance_cents FROM bar_credit_accounts').fetchone()[0],1200)
            self.assertEqual(db.execute('SELECT stock FROM products WHERE id=?',(self.product_id,)).fetchone()[0],6)
            self.assertEqual(db.execute('SELECT net_total_cents FROM sales_net WHERE id=?',(self.sale,)).fetchone()[0],800)
            self.assertEqual(db.execute('SELECT amount_cents FROM bar_credit_audit').fetchone()[0],1200)
        self.assertEqual(self.client.get('/orders/feed').get_json()['pending'],[])
        self.assertEqual(self.client.post(f'/orders/{self.sale}/deliver',json={}).status_code,409)
        self.assertEqual(self.client.post(f'/orders/{self.sale}/cancel',json={'reason':'Cancelamento'}).status_code,409)
        self.assertEqual(self.client.post(f'/sales/{self.sale}/delete').status_code,409)

    def test_partial_quantity_and_later_delivery(self):
        self.order(quantity=3)
        self.assertEqual(self.cancel(quantity=2).status_code,200)
        feed=self.client.get('/orders/feed').get_json()['pending'][0]
        item=next(i for i in feed['items'] if i['id']==self.snack)
        self.assertEqual((item['quantity'],item['pending_quantity'],item['canceled_quantity']), (1,1,2))
        self.assertEqual(self.client.post(f'/orders/{self.sale}/deliver',json={}).status_code,200)
        with app.app_context():
            self.assertEqual(get_db().execute('SELECT SUM(quantity) FROM sale_item_deliveries WHERE sale_item_id=?',(self.snack,)).fetchone()[0],1)

    def test_delivered_item_blocked(self):
        self.order()
        before=self.snapshot()
        self.assertEqual(self.cancel(item=self.beer_item).status_code,409)
        self.assertEqual(before,self.snapshot())

    def test_idempotency_and_second_key(self):
        self.order()
        key=str(uuid.uuid4())
        first=self.cancel(key=key)
        before=self.snapshot()
        self.assertEqual(self.cancel(key=key).get_json(),first.get_json())
        self.assertEqual(self.cancel().status_code,409)
        self.assertEqual(before,self.snapshot())
        self.assertEqual(self.cancel(quantity=2,key=key).status_code,409)

    def test_two_concurrent_requests(self):
        self.order()
        def run(key):
            with app.app_context():
                try:
                    cancel_item(get_db(),self.sale,self.snack,1,'Produto não entregue',key,self.user_id)
                    return True
                except ValueError:
                    return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(run,[str(uuid.uuid4()),str(uuid.uuid4())]))
        self.assertEqual(sorted(results),[False,True])
        self.assertEqual(len(self.snapshot()['bar_credit_transactions']),1)

    def test_concurrent_same_key(self):
        self.order()
        key=str(uuid.uuid4())
        def run(_):
            with app.app_context():
                return cancel_item(get_db(),self.sale,self.snack,1,'Produto não entregue',key,self.user_id)['id']
        with ThreadPoolExecutor(max_workers=2) as pool:
            result=list(pool.map(run,range(2)))
        self.assertEqual(result[0],result[1])
        self.assertEqual(len(self.snapshot()['bar_credit_transactions']),1)

    def test_financial_failure_rolls_back(self):
        self.order()
        before=self.snapshot()
        with patch('src.services.item_cancellations._audit',side_effect=RuntimeError('finance failure')):
            self.assertEqual(self.cancel().status_code,500)
        self.assertEqual(before,self.snapshot())

    def test_stock_failure_rolls_back_credit(self):
        self.order()
        with app.app_context():
            db=get_db()
            db.execute("CREATE TRIGGER reject_stock BEFORE UPDATE OF stock ON products BEGIN SELECT RAISE(ABORT,'stock failure'); END")
            db.commit()
        before=self.snapshot()
        self.assertEqual(self.cancel().status_code,500)
        self.assertEqual(before,self.snapshot())

    def test_original_price_used(self):
        self.order()
        with app.app_context():
            db=get_db(); db.execute('UPDATE products SET price_cents=9999 WHERE id=?',(self.product_id,)); db.commit()
        self.assertEqual(self.cancel().get_json()['operation']['amount_cents'],1200)

    def test_discount_without_proven_allocation_blocked(self):
        self.order()
        with app.app_context():
            db=get_db();db.execute('UPDATE sales SET total_cents=1800 WHERE id=?',(self.sale,))
            db.execute('UPDATE sale_payment_parts SET amount_cents=1800 WHERE sale_id=?',(self.sale,));db.commit()
        before=self.snapshot()
        self.assertIn('rateio',self.cancel().get_json()['error'])
        self.assertEqual(before,self.snapshot())

    def test_mixed_cash_credit_supported(self):
        self.order(mixed=True)
        self.assertEqual(self.cancel().status_code,200)

    def test_unsupported_mixed_and_unpaid_blocked(self):
        self.order(mixed=True)
        with app.app_context():
            db=get_db();db.execute("UPDATE sale_payment_parts SET method='Pix' WHERE sale_id=? AND method='Dinheiro'",(self.sale,));db.commit()
        self.assertEqual(self.cancel().status_code,409)
        with app.app_context():
            db=get_db();db.execute('UPDATE sales SET paid=0 WHERE id=?',(self.sale,));db.commit()
        self.assertEqual(self.cancel().status_code,409)

    def test_whole_cancel_unchanged_without_partial(self):
        self.order(delivered_beer=False)
        response=self.client.post(f'/orders/{self.sale}/cancel',json={'reason':'Pedido não retirado'})
        self.assertEqual(response.status_code,200,response.get_json())
        self.assertEqual(response.get_json()['credited_total_cents'],2000)
        self.assertEqual(self.cancel().status_code,409)

    def test_quantity_reason_confirmation_authorization(self):
        self.order()
        for quantity in [0,-1,1.5,True,'1',100]:
            self.assertNotEqual(self.cancel(quantity=quantity).status_code,200)
        self.assertEqual(self.cancel(reason='').status_code,409)
        self.assertEqual(self.cancel(confirmed=False).status_code,400)
        with self.client.session_transaction() as session:
            session.clear()
        self.assertNotEqual(self.cancel().status_code,200)

    def test_reports_cash_and_receipt_after_cancellation(self):
        from src.services.cash_register import payment_breakdown
        from src.services.monthly_sales_report import monthly_sales_data
        from src.utils import local_today
        self.order()
        self.assertEqual(self.cancel().status_code,200)
        with app.app_context():
            db=get_db()
            sale=dict(db.execute('SELECT * FROM sales WHERE id=?',(self.sale,)).fetchone())
            self.assertEqual(payment_breakdown(db,sale)['Dinheiro'],2000)
            data=monthly_sales_data(db,local_today().strftime('%Y-%m'))
            self.assertEqual((data['summary']['gross_revenue'],data['summary']['canceled_cents'],data['summary']['revenue']), (2000,1200,800))
            self.assertEqual(data['summary']['items_sold'],1)
        for path in [f'/sales/{self.sale}/receipt',f'/relatorios/vendas/{self.sale}','/reports','/orders']:
            response=self.client.get(path)
            self.assertEqual(response.status_code,200,(path,response.status_code))
        # Render the inline JS and check its syntax independently of browser availability.
        import re,subprocess
        from pathlib import Path
        html=self.client.get('/orders').get_data(as_text=True)
        scripts=re.findall(r'<script>(.*?)</script>',html,re.S)
        target=Path(self.tempdir.name)/'orders.js';target.write_text('\n'.join(scripts))
        result=subprocess.run(['node','--check',str(target)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_stock_invalidates_observation_without_fabricating_consumption(self):
        from src.services.purchase_forecast import purchase_forecast
        self.order()
        with app.app_context():
            db=get_db();db.execute('UPDATE purchase_observation_control SET enabled=1 WHERE id=1');db.commit()
        self.assertEqual(self.cancel().status_code,200)
        with app.app_context():
            db=get_db()
            self.assertGreater(db.execute("SELECT COUNT(*) FROM purchase_observation_events WHERE product_id=? AND source_table='products'",(self.product_id,)).fetchone()[0],0)
            data=purchase_forecast(db,'2026-12',3,10)
            product=next(p for p in data['products'] if p['product_id']==self.product_id)
            self.assertEqual(product['current_stock_units'],6)
            self.assertEqual(sum(m['quantity'] for m in product['monthly_consumption']),0)

    def test_legacy_delivery_and_missing_parts_blocked(self):
        self.order(delivered_beer=False)
        with app.app_context():
            db=get_db();db.execute('UPDATE sales SET delivered_at=CURRENT_TIMESTAMP WHERE id=?',(self.sale,));db.commit()
        self.assertEqual(self.cancel().status_code,409)
        with app.app_context():
            db=get_db();db.execute('UPDATE sales SET delivered_at=NULL WHERE id=?',(self.sale,));db.execute('DELETE FROM sale_payment_parts WHERE sale_id=?',(self.sale,));db.commit()
        self.assertEqual(self.cancel().status_code,409)

    def test_staff_authorized_client_forbidden(self):
        self.order()
        with app.app_context():
            db=get_db();db.execute("UPDATE users SET role='staff' WHERE id=?",(self.user_id,));db.commit()
        self.assertEqual(self.cancel().status_code,200)
        with app.app_context():
            db=get_db();db.execute("UPDATE users SET role='client',player_id=? WHERE id=?",(self.player_id,self.user_id));db.commit()
        self.assertIn(self.cancel().status_code,(302,403))

    def test_discount_recorded_in_original_line_is_supported(self):
        self.order()
        with app.app_context():
            db=get_db()
            db.execute('UPDATE sale_items SET unit_price_cents=1000 WHERE id=?',(self.snack,))
            db.execute('UPDATE sales SET total_cents=1800 WHERE id=?',(self.sale,))
            db.execute('UPDATE sale_payment_parts SET amount_cents=1800 WHERE sale_id=?',(self.sale,))
            db.commit()
        response=self.cancel()
        self.assertEqual(response.status_code,200,response.get_json())
        self.assertEqual(response.get_json()['operation']['amount_cents'],1000)
