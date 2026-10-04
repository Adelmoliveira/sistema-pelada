import unittest
from datetime import date
from uuid import uuid4
from unittest.mock import patch

import test_sports_installment_reconciliation as fixtures
from src.services.sports_installment_client import preview_checkout, plan_summary


class SportsInstallmentClientTest(unittest.TestCase):
    setUp = fixtures.InstallmentReconciliationTest.setUp
    sale = fixtures.InstallmentReconciliationTest.sale
    setup_plan = fixtures.InstallmentReconciliationTest.setup_plan
    setup_payments = fixtures.InstallmentReconciliationTest.setup_payments
    charge = fixtures.InstallmentReconciliationTest.charge
    order = fixtures.InstallmentReconciliationTest.order
    event = fixtures.InstallmentReconciliationTest.event
    approve = fixtures.InstallmentReconciliationTest.approve

    def prepare(self):
        from app import app
        self.setup_payments()
        self.db.execute('ALTER TABLE sports_product_variants ADD COLUMN size TEXT DEFAULT \'M\'')
        self.db.execute('ALTER TABLE sports_product_variants ADD COLUMN active INTEGER DEFAULT 1')
        self.db.execute('ALTER TABLE sports_product_variants ADD COLUMN updated_at TEXT')
        self.db.execute('ALTER TABLE sports_product_variants ADD COLUMN min_stock INTEGER DEFAULT 0')
        self.db.execute("CREATE TABLE sports_material_types(id INTEGER PRIMARY KEY,code TEXT,name TEXT DEFAULT 'Camisa')")
        self.db.execute('CREATE TABLE sports_product_config(product_id INTEGER PRIMARY KEY,type_id INTEGER,ready_sale_enabled INTEGER DEFAULT 1,allow_custom_name INTEGER DEFAULT 0,allow_custom_number INTEGER DEFAULT 0,allow_backorder INTEGER DEFAULT 0)')
        self.db.execute("INSERT INTO sports_material_types(id,code) VALUES(1,'shirt')")
        product=self.db.execute('SELECT product_id FROM sale_items WHERE id=?',(self.item_id,)).fetchone()[0]
        self.db.execute('INSERT INTO sports_product_config(product_id,type_id) VALUES(?,1)',(product,))
        self.product_id=product
        self.variant_id=self.db.execute('SELECT variant_id FROM sports_sale_item_details WHERE sale_item_id=?',(self.item_id,)).fetchone()[0]
        self.user=self.db.execute("INSERT INTO users(username,name,password_hash,role,player_id) VALUES('client3x','Cliente','hash','client',?)",(self.player_id,)).lastrowid
        other_player=self.db.execute("INSERT INTO players(name,email) VALUES('Outro','other@example.com')").lastrowid
        self.other_user=self.db.execute("INSERT INTO users(username,name,password_hash,role,player_id) VALUES('other3x','Outro','hash','client',?)",(other_player,)).lastrowid
        self.db.commit()
        saved=dict(app.config);self.addCleanup(lambda:app.config.update(saved))
        app.config.update(TESTING=True,WTF_CSRF_ENABLED=False,DATABASE=self.db.conn.execute('PRAGMA database_list').fetchone()[2],DATABASE_URL=None,APP_ENV='local',EXTERNAL_PAYMENTS_ENABLED=True,MERCADOPAGO_ACCESS_TOKEN='fake')
        self.app=app;self.client=app.test_client()
        with self.client.session_transaction() as session:session['user_id']=self.user
        self.cart=[dict(product_id=self.product_id,variant_id=self.variant_id,quantity=1,order_mode='ready')]

    def page(self):
        response=self.client.get('/minhas-compras')
        self.assertEqual(response.status_code,200)
        return response.get_data(as_text=True)

    def test_preview_server_total_and_exact_sum(self):
        self.prepare()
        response=self.client.post('/material-esportivo/pix-3x/preview',json={'items':self.cart,'total_cents':1})
        self.assertEqual(response.status_code,200)
        data=response.get_json()
        self.assertEqual(data['total_cents'],10000)
        self.assertEqual([i['amount_cents'] for i in data['installments']],[3333,3333,3334])
        self.assertEqual(sum(i['amount_cents'] for i in data['installments']),10000)

    def test_preview_rejects_backorder_bar_and_not_ready(self):
        self.prepare()
        for change in ('backorder','bar','not_ready'):
            cart=[dict(self.cart[0])]
            if change=='backorder':cart[0]['order_mode']='backorder'
            elif change=='bar':self.db.execute("UPDATE products SET category='Cerveja' WHERE id=?",(self.product_id,))
            else:
                self.db.execute("UPDATE products SET category='Material Esportivo' WHERE id=?",(self.product_id,))
                self.db.execute('UPDATE sports_product_config SET ready_sale_enabled=0')
            self.db.commit()
            self.assertEqual(self.client.post('/material-esportivo/pix-3x/preview',json={'items':cart}).status_code,400)

    def test_checkout_first_and_replay_do_not_duplicate(self):
        self.prepare()
        body={'items':self.cart,'idempotency_key':str(uuid4()),'player_id':999,'total_cents':1}
        with patch('src.services.sports_installment_payments.create_pix_order',return_value=self.order('checkout')) as provider:
            first=self.client.post('/material-esportivo/pix-3x/comprar',json=body)
            replay=self.client.post('/material-esportivo/pix-3x/comprar',json=body)
        self.assertEqual(first.status_code,200,first.get_json())
        self.assertEqual(replay.status_code,200,replay.get_json())
        self.assertEqual(first.get_json()['installment_id'],replay.get_json()['installment_id'])
        provider.assert_called_once()
        self.assertEqual(provider.call_args.args[2],3333)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM sports_installment_plans').fetchone()[0],2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM sports_installments').fetchone()[0],6)
        self.assertEqual(self.db.execute('SELECT stock FROM sports_product_variants').fetchone()[0],4)
        self.assertNotIn('mercado_pago_order_id',first.get_json()['attempt'])

    def test_client_page_only_own_plans(self):
        self.prepare()
        html=self.page()
        self.assertIn('Pix 3x',html)
        self.assertEqual(html.count('class="btn btn-sm btn-outline-success installment-pay"'),3)
        with self.client.session_transaction() as session:session['user_id']=self.other_user
        self.assertNotIn('installment-plan',self.page())

    def test_other_client_cannot_get_qr_or_poll(self):
        self.prepare()
        with self.client.session_transaction() as session:session['user_id']=self.other_user
        identifier=self.installments[0]['id']
        with patch('src.routes.sales.get_order') as provider:
            self.assertEqual(self.client.get(f'/material-esportivo/parcelas/{identifier}/status').status_code,404)
            self.assertEqual(self.client.post(f'/material-esportivo/parcelas/{identifier}/pix',json={}).status_code,404)
        provider.assert_not_called()

    def test_second_can_pay_early(self):
        self.prepare()
        response=self.client.post(f"/material-esportivo/parcelas/{self.installments[1]['id']}/pix",json={'amount_cents':1})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.get_json()['amount'],'R$ 33,33')
        self.assertFalse(response.get_json()['plan']['withdrawal_allowed'])

    def test_paid_no_button_and_endpoint_rejected(self):
        self.prepare();self.approve(1)
        identifier=self.installments[0]['id']
        self.assertNotIn(f'data-url="/material-esportivo/parcelas/{identifier}/pix"',self.page())
        self.assertEqual(self.client.post(f'/material-esportivo/parcelas/{identifier}/pix',json={}).status_code,409)

    def test_overdue_presentation_without_persistent_mutation(self):
        self.prepare()
        with patch('src.services.sports_installment_client.today',return_value=date(2027,1,1)):
            self.assertIn('Vencido',self.page())
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM sports_installments WHERE status='pending'").fetchone()[0],3)

    def test_balances_zero_one_two_three_paid(self):
        self.prepare()
        for n,paid,balance in ((0,0,10000),(1,3333,6667),(2,6666,3334),(3,10000,0)):
            if n:self.approve(n)
            summary=plan_summary(self.db,self.sale_id,self.player_id)
            self.assertEqual((summary['paid_cents'],summary['balance_cents']),(paid,balance))

    def test_second_only_keeps_withdrawal_blocked(self):
        self.prepare();self.approve(2)
        html=self.page()
        self.assertIn('Retirada bloqueada até confirmação',html)
        summary=plan_summary(self.db,self.sale_id,self.player_id)
        self.assertEqual(summary['balance_cents'],6667)
        self.assertFalse(summary['withdrawal_allowed'])

    def test_first_shows_release_but_not_quitado(self):
        self.prepare();self.approve(1)
        html=self.page()
        self.assertIn('Material liberado para retirada',html)
        self.assertIn('Saldo devedor: R$ 66,67',html)
        self.assertNotIn('Quitado',html)
        self.assertNotIn('Ver comprovante',html)

    def test_three_shows_quitado(self):
        self.prepare()
        for n in (1,2,3):self.approve(n)
        html=self.page()
        self.assertIn('Quitado',html)
        self.assertIn('Saldo devedor: R$ 0,00',html)

    def test_poll_reconciles_only_owned_installment(self):
        self.prepare()
        with patch('src.routes.sales.get_order',return_value=self.event(1)),patch('src.routes.sales.apply_mercadopago_status') as legacy:
            response=self.client.get(f"/material-esportivo/parcelas/{self.installments[0]['id']}/status")
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.get_json()['paid'])
        self.assertTrue(response.get_json()['plan']['withdrawal_allowed'])
        self.assertEqual(self.db.execute('SELECT paid FROM sales WHERE id=?',(self.sale_id,)).fetchone()[0],0)
        legacy.assert_not_called()

    def test_posts_require_csrf(self):
        self.prepare();self.app.config['WTF_CSRF_ENABLED']=True
        for url in ('/material-esportivo/pix-3x/preview','/material-esportivo/pix-3x/comprar',f"/material-esportivo/parcelas/{self.installments[0]['id']}/pix"):
            self.assertEqual(self.client.post(url,json={}).status_code,303)

    def test_template_keeps_cash_pix_and_scopes_choice_to_client(self):
        self.prepare()
        response=self.client.get('/sale?catalog=sports')
        self.assertEqual(response.status_code,200)
        html=response.get_data(as_text=True)
        self.assertIn('Pix à vista',html)
        self.assertIn('Pix em 3x',html)
        self.assertIn('sports_installments.js',html)
        self.assertIn('pixEndpoint',html)
        # Check the rendered inline JS, including the modified Jinja template.
        import re, subprocess, tempfile
        from pathlib import Path
        scripts=re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>',html,re.S)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'rendered-sale.js'
            path.write_text('\n'.join(scripts))
            subprocess.run(['node','--check',str(path)],check=True,capture_output=True)

    def test_checkout_key_cannot_be_reused_for_different_cart(self):
        self.prepare()
        body={'items':self.cart,'idempotency_key':str(uuid4())}
        with patch('src.services.sports_installment_payments.create_pix_order',return_value=self.order('unique')):
            self.assertEqual(self.client.post('/material-esportivo/pix-3x/comprar',json=body).status_code,200)
        body['items']=[dict(self.cart[0],quantity=2)]
        self.assertEqual(self.client.post('/material-esportivo/pix-3x/comprar',json=body).status_code,409)
        self.assertEqual(self.db.execute('SELECT stock FROM sports_product_variants').fetchone()[0],4)


if __name__=='__main__':unittest.main()
