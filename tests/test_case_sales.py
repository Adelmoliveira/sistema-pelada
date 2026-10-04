import unittest
import json
from unittest.mock import patch
import test_mercadopago as fixtures
from app import app
from src.db import get_db


class CaseSalesTest(unittest.TestCase):
    # Reuse only the isolated database fixture, not its full test suite.
    setUp = fixtures.MercadoPagoFlowTest.setUp
    tearDown = fixtures.MercadoPagoFlowTest.tearDown
    create_sports_schema = fixtures.MercadoPagoFlowTest.create_sports_schema
    login_manager = fixtures.MercadoPagoFlowTest.login_manager
    headers = fixtures.MercadoPagoFlowTest.headers

    def configure(self, stock=100, enabled=1, price=1500):
        self.login_manager()
        self.previous_config = dict(app.config)
        self.addCleanup(lambda: app.config.update(self.previous_config))
        app.config.update(EXTERNAL_PAYMENTS_ENABLED=True, APP_ENV='local')
        with app.app_context():
            db = get_db()
            db.execute('UPDATE products SET category=?,price_cents=?,units_per_case=24,case_sale_enabled=?,stock=? WHERE id=?', ('Cerveja', price, enabled, stock, self.product_id))
            db.commit()

    def buy(self, quantity=1, mode='case', method='Dinheiro'):
        return self.client.post('/sale', data={'department':'bar', 'player_id':self.player_id, 'payment_method':method, 'product_id':[self.product_id], 'quantity':[quantity], 'sale_mode':[mode], 'price_cents':'1','units_per_case':'1','total_cents':'1'})

    def assert_sale(self, quantity, total, stock):
        with app.app_context():
            db=get_db()
            sale=db.execute('SELECT * FROM sales ORDER BY id DESC LIMIT 1').fetchone()
            self.assertIsNotNone(sale)
            rows=db.execute('SELECT * FROM sale_items WHERE sale_id=?',(sale['id'],)).fetchall()
            self.assertEqual(len(rows),1)
            self.assertEqual(sum(row['quantity'] for row in rows),quantity)
            self.assertEqual(sum(row['quantity']*row['unit_price_cents'] for row in rows),total)
            self.assertEqual(sale['total_cents'],total)
            self.assertEqual(db.execute('SELECT stock FROM products WHERE id=?',(self.product_id,)).fetchone()['stock'],stock)
            return sale['id']

    def test_unit_sale_unchanged(self):
        self.configure(enabled=0)
        self.assertEqual(self.buy(2,'unit').status_code,303)
        self.assert_sale(2,3000,98)

    def test_one_case_price_and_units(self):
        self.configure()
        self.assertEqual(self.buy().status_code,303)
        self.assert_sale(24,36000,76)

    def test_two_cases_price_and_units(self):
        self.configure()
        self.buy(2)
        self.assert_sale(48,72000,52)

    def test_case_uses_current_unit_price(self):
        self.configure(price=1500)
        self.buy()
        self.assert_sale(24,36000,76)
        with app.app_context():
            self.assertEqual(get_db().execute('SELECT quantity FROM sale_items').fetchone()['quantity'],24)

    def test_insufficient_stock_no_sale(self):
        self.configure(stock=23)
        self.buy()
        with app.app_context():
            self.assertEqual(get_db().execute('SELECT COUNT(*) FROM sales').fetchone()[0],0)
            self.assertEqual(get_db().execute('SELECT stock FROM products WHERE id=?',(self.product_id,)).fetchone()['stock'],23)

    def test_disabled_case_blocked(self):
        self.configure(enabled=0)
        self.buy()
        with app.app_context():
            self.assertEqual(get_db().execute('SELECT COUNT(*) FROM sales').fetchone()[0],0)

    def test_pix_case_trusted_price(self):
        self.configure()
        order={'id':'ORD-CASE','status':'created','transactions':{'payments':[{'id':'PAY-CASE','payment_method':{'qr_code':'000201TEST'}}]}}
        with patch('src.routes.sales.create_pix_order',return_value=order) as create:
            response=self.client.post('/pix/mercadopago/orders',headers=self.headers(),json={'player_id':self.player_id,'items':[{'product_id':self.product_id,'quantity':2,'sale_mode':'case','price_cents':1,'units_per_case':1}],'total_cents':1})
        self.assertEqual(response.status_code,201,response.get_json())
        self.assert_sale(48,72000,52)
        self.assertTrue(create.called)

    def test_delivery_and_remaining_stay_units(self):
        self.configure(price=1500)
        self.buy(method='Débito')
        sale_id=self.assert_sale(24,36000,76)
        with app.app_context():
            item=get_db().execute('SELECT id FROM sale_items WHERE sale_id=?',(sale_id,)).fetchone()
        response=self.client.post(f'/orders/{sale_id}/deliver',json={'sale_item_id':item['id'],'quantity':5})
        self.assertEqual(response.status_code,200)
        self.assertEqual(sum(i['quantity'] for i in response.get_json()['remaining_items']),19)
        self.assert_sale(24,36000,76)

    def test_case_delivery_and_statement(self):
        self.configure()
        self.buy(method='Débito')
        sale_id=self.assert_sale(24,36000,76)
        response=self.client.post(f'/orders/{sale_id}/deliver',json={})
        self.assertEqual(response.status_code,200)
        self.assertFalse(response.get_json()['partial'])
        with app.app_context():
            db=get_db()
            self.assertEqual(db.execute('SELECT SUM(quantity) FROM sale_item_deliveries').fetchone()[0],24)
        restored=self.client.post(f'/orders/{sale_id}/restore-delivery',json={'reason':'Retirada registrada por engano'})
        self.assertEqual(restored.status_code,200)
        self.assert_sale(24,36000,76)
        statement=self.client.get(f'/orders/player-statement?player_id={self.player_id}')
        self.assertEqual(statement.status_code,200)

    def test_credit_and_mixed_cash_use_case_total(self):
        for method,partial in [('Créditos',False),('Dinheiro',True)]:
            with self.subTest(method=method):
                self.configure()
                with app.app_context():
                    db=get_db()
                    db.execute('INSERT INTO bar_credit_accounts(player_id,balance_cents) VALUES(?,?) ON CONFLICT(player_id) DO UPDATE SET balance_cents=excluded.balance_cents',(self.player_id,4000 if partial else 40000))
                    db.commit()
                response=self.client.post('/sale',data={'player_id':self.player_id,'payment_method':method,'product_id':[self.product_id],'quantity':[1],'sale_mode':['case'],'use_bar_credit':'1' if partial else '0'})
                self.assertEqual(response.status_code,303)
                sale_id=self.assert_sale(24,36000,76)
                with app.app_context():
                    parts=get_db().execute('SELECT method,amount_cents,status FROM sale_payment_parts WHERE sale_id=? ORDER BY method',(sale_id,)).fetchall()
                    self.assertEqual([(r['method'],r['amount_cents'],r['status']) for r in parts], [('Créditos',4000,'reserved'),('Dinheiro',32000,'pending')] if partial else [('Créditos',36000,'approved')])

    def test_legacy_pix_case_recalculates_amount(self):
        self.configure()
        response=self.client.get('/pix/qrcode',headers=self.headers(),query_string={'amount_cents':1,'items':json.dumps([{'product_id':self.product_id,'quantity':2,'sale_mode':'case'}])})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.get_json()['amount'],'R$ 720,00')

    def test_pix_case_insufficient_stock_does_not_call_provider(self):
        self.configure(stock=47)
        with patch('src.routes.sales.create_pix_order') as create:
            response=self.client.post('/pix/mercadopago/orders',headers=self.headers(),json={'player_id':self.player_id,'items':[{'product_id':self.product_id,'quantity':2,'sale_mode':'case'}]})
        self.assertEqual(response.status_code,409)
        create.assert_not_called()

    def test_price_change_affects_only_new_sales(self):
        self.configure()
        self.buy()
        first=self.assert_sale(24,36000,76)
        with app.app_context():
            db=get_db()
            db.execute('UPDATE products SET price_cents=1600 WHERE id=?',(self.product_id,))
            db.commit()
        self.buy()
        self.assert_sale(24,38400,52)
        with app.app_context():
            first_item=get_db().execute('SELECT quantity,unit_price_cents FROM sale_items WHERE sale_id=?',(first,)).fetchone()
            self.assertEqual((first_item['quantity'],first_item['unit_price_cents']),(24,1500))

    def test_mixed_unit_and_case_same_product_one_line(self):
        self.configure()
        response=self.client.post('/sale',data={'player_id':self.player_id,'payment_method':'Dinheiro','product_id':[self.product_id,self.product_id],'quantity':[1,2],'sale_mode':['case','unit']})
        self.assertEqual(response.status_code,303)
        self.assert_sale(26,39000,74)

    def test_product_configuration_and_quick_sale_render(self):
        self.configure()
        response=self.client.post(f'/products/{self.product_id}/edit', data={'name':'Heineken','category':'Cerveja','units_per_case':'24','case_sale_enabled':'1','price':'15,00','cost':'1,00','stock':'100','min_stock':'5'})
        self.assertIn(response.status_code,(302,303))
        with app.app_context():
            product=get_db().execute('SELECT * FROM products WHERE id=?',(self.product_id,)).fetchone()
            self.assertEqual((product['case_sale_enabled'],product['price_cents']),(1,1500))
        html=self.client.get('/sale').get_data(as_text=True)
        self.assertIn('Quantidade de caixas',html)
        self.assertIn('"price_cents": 1500',html)


if __name__=='__main__':
    unittest.main()
