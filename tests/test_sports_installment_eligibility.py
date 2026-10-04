import unittest
from datetime import date
from uuid import uuid4
from unittest.mock import patch

import test_mercadopago as fixtures
from app import app
from src.db import get_db
from src.services.sports_installments import create_installment_plan
from src.services.sports_installment_reconciliation import reconcile_installment_order


class SportsEligibilityTest(unittest.TestCase):
    setUp=fixtures.MercadoPagoFlowTest.setUp
    tearDown=fixtures.MercadoPagoFlowTest.tearDown
    create_sports_schema=fixtures.MercadoPagoFlowTest.create_sports_schema
    create_sports_product=fixtures.MercadoPagoFlowTest.create_sports_product
    login_manager=fixtures.MercadoPagoFlowTest.login_manager

    def material(self,enabled=False):
        pid,vid=self.create_sports_product('Material '+str(uuid4()))
        with app.app_context():
            db=get_db();db.execute('UPDATE sports_product_config SET installment_pix_enabled=? WHERE product_id=?',(enabled,pid));db.commit()
        return dict(product_id=pid,variant_id=vid,quantity=1,order_mode='ready')

    def login_client(self):
        with app.app_context():
            db=get_db();uid=db.execute("INSERT INTO users(username,name,password_hash,role,player_id) VALUES('eligible-client','Cliente','hash','client',?)",(self.player_id,)).lastrowid;db.commit()
        with self.client.session_transaction() as session:session['user_id']=uid
        app.config['EXTERNAL_PAYMENTS_ENABLED']=True

    def form(self,enabled=None):
        result={'name':'Camisa','type_id':'1','price':'20,00','cost':'10,00','active':'1','sale_mode':'ready','variant_size':'M','variant_stock':'5','variant_min_stock':'0','variant_active':'1'}
        if enabled:result['installment_pix_enabled']='1'
        return result

    def checkout(self,items):
        response={'id':'ORD-'+str(uuid4()),'transactions':{'payments':[{'id':'PAY-'+str(uuid4()),'payment_method':{'qr_code':'test','qr_code_base64':'encoded'}}]}}
        with patch('src.services.sports_installment_payments.create_pix_order',return_value=response) as provider:
            result=self.client.post('/material-esportivo/pix-3x/comprar',json={'items':items,'idempotency_key':str(uuid4())})
        return result,provider

    def test_new_material_defaults_false(self):
        import sqlite3
        from pathlib import Path
        connection=sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.execute('CREATE TABLE sports_product_config(product_id INTEGER PRIMARY KEY)')
        migration=Path('supabase/migrations/20261004210000_sports_installment_pix_eligibility.sql').read_text()
        connection.executescript(migration.replace('ADD COLUMN IF NOT EXISTS','ADD COLUMN'))
        connection.execute('INSERT INTO sports_product_config(product_id) VALUES(1)')
        self.assertEqual(connection.execute('SELECT installment_pix_enabled FROM sports_product_config').fetchone()[0],0)
        self.login_manager()
        self.assertEqual(self.client.post('/material-esportivo',data=self.form()).status_code,303)
        with app.app_context():self.assertEqual(get_db().execute('SELECT installment_pix_enabled FROM sports_product_config').fetchone()[0],0)

    def test_staff_and_manager_enable(self):
        for role in ('staff','manager'):
            item=self.material()
            with app.app_context():get_db().execute('UPDATE users SET role=? WHERE id=?',(role,self.user_id));get_db().commit()
            self.login_manager()
            response=self.client.post(f"/material-esportivo/{item['product_id']}/editar",data=dict(self.form(True),name='Camisa '+str(item['product_id'])))
            self.assertEqual(response.status_code,303)
            with app.app_context():self.assertEqual(get_db().execute('SELECT installment_pix_enabled FROM sports_product_config WHERE product_id=?',(item['product_id'],)).fetchone()[0],1)

    def test_staff_and_manager_disable(self):
        for role in ('staff','manager'):
            item=self.material(True)
            with app.app_context():get_db().execute('UPDATE users SET role=? WHERE id=?',(role,self.user_id));get_db().commit()
            self.login_manager()
            self.assertEqual(self.client.post(f"/material-esportivo/{item['product_id']}/editar",data=dict(self.form(),name='Camisa '+str(item['product_id']))).status_code,303)
            with app.app_context():self.assertEqual(get_db().execute('SELECT installment_pix_enabled FROM sports_product_config WHERE product_id=?',(item['product_id'],)).fetchone()[0],0)

    def test_client_cannot_configure(self):
        item=self.material();self.login_client()
        self.assertEqual(self.client.post(f"/material-esportivo/{item['product_id']}/editar",data=self.form(True),headers={'Accept':'application/json'}).status_code,403)
        with app.app_context():self.assertEqual(get_db().execute('SELECT installment_pix_enabled FROM sports_product_config').fetchone()[0],0)

    def test_disabled_has_no_preview(self):
        item=self.material();self.login_client()
        self.assertEqual(self.client.post('/material-esportivo/pix-3x/preview',json={'items':[item]}).status_code,400)

    def test_enabled_has_preview(self):
        item=self.material(True);self.login_client()
        self.assertEqual(self.client.post('/material-esportivo/pix-3x/preview',json={'items':[item]}).status_code,200)

    def test_manual_request_disabled_rejected_without_side_effects(self):
        item=self.material();self.login_client()
        result,provider=self.checkout([dict(item,installment_pix_enabled=True)])
        self.assertEqual(result.status_code,409);provider.assert_not_called()
        with app.app_context():
            db=get_db()
            for table in ('sales','sports_installment_plans','sports_installments','sports_installment_payment_attempts'):self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT stock FROM sports_product_variants').fetchone()[0],5)
            # A direct service call must enforce the same rule on persisted items.
            sid=db.execute("INSERT INTO sales(player_id,payment_method,total_cents,paid,payment_status) VALUES(?,'Pix',2000,0,'pending')",(self.player_id,)).lastrowid
            iid=db.execute('INSERT INTO sale_items(sale_id,product_id,quantity,unit_price_cents) VALUES(?,?,1,2000)',(sid,item['product_id'])).lastrowid
            db.execute("INSERT INTO sports_sale_item_details(sale_item_id,variant_id,variant_size,order_mode,fulfillment_status) VALUES(?,?,'M','ready','reserved')",(iid,item['variant_id']));db.commit()
            with self.assertRaises(ValueError):create_installment_plan(db,sid,2000,date.today())
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sports_installment_plans').fetchone()[0],0)

    def test_enabled_creates_plan(self):
        item=self.material(True);self.login_client();result,provider=self.checkout([item])
        self.assertEqual(result.status_code,200,result.get_json());provider.assert_called_once()
        with app.app_context():self.assertEqual(get_db().execute('SELECT COUNT(*) FROM sports_installments').fetchone()[0],3)

    def test_two_enabled_products_allow(self):
        items=[self.material(True),self.material(True)];self.login_client();result,_=self.checkout(items)
        self.assertEqual(result.status_code,200,result.get_json())
        self.assertEqual(result.get_json()['plan']['total_cents'],4000)

    def test_mixed_cart_blocks_all(self):
        items=[self.material(True),self.material(False)];self.login_client();result,provider=self.checkout(items)
        self.assertEqual(result.status_code,409);provider.assert_not_called()
        with app.app_context():
            db=get_db();self.assertEqual(db.execute('SELECT COUNT(*) FROM sales').fetchone()[0],0)
            self.assertEqual([r[0] for r in db.execute('SELECT stock FROM sports_product_variants').fetchall()],[5,5])

    def test_backorder_still_rejected(self):
        item=self.material(True);item['order_mode']='backorder';self.login_client()
        result,provider=self.checkout([item]);self.assertEqual(result.status_code,409);provider.assert_not_called()

    def test_cash_pix_independent_of_flag(self):
        item=self.material(False);self.login_manager();app.config['EXTERNAL_PAYMENTS_ENABLED']=True
        response={'id':'ORD-CASH','transactions':{'payments':[{'id':'PAY-CASH','payment_method':{'qr_code':'test'}}]}}
        with patch('src.routes.sales.create_pix_order',return_value=response):
            result=self.client.post('/pix/mercadopago/orders',headers={'X-Pix-Token':self.token},json={'player_id':self.player_id,'department':'sports','items':[item]})
        self.assertEqual(result.status_code,201,result.get_json())

    def test_existing_plan_survives_disable(self):
        item=self.material(True);self.login_client();result,_=self.checkout([item]);self.assertEqual(result.status_code,200)
        sid=result.get_json()['plan']['sale_id']
        with app.app_context():
            db=get_db();db.execute('UPDATE sports_product_config SET installment_pix_enabled=0 WHERE product_id=?',(item['product_id'],));db.commit()
            first=db.execute('SELECT * FROM sports_installment_payment_attempts').fetchone()
            reconcile_installment_order(db,{'id':first['mercado_pago_order_id'],'external_reference':first['external_reference'],'status':'processed','status_detail':'accredited','total_paid_amount':'6.66'})
            plan=db.execute('SELECT * FROM sports_installment_plans WHERE sale_id=?',(sid,)).fetchone()
            self.assertEqual(plan['status'],'active')
            create_installment_plan(db,sid,2000,str(db.execute('SELECT due_date FROM sports_installments WHERE plan_id=? AND installment_number=1',(plan['id'],)).fetchone()[0]))
            second=db.execute('SELECT id FROM sports_installments WHERE plan_id=? AND installment_number=2',(plan['id'],)).fetchone()[0]
        response={'id':'ORD-SECOND','transactions':{'payments':[{'id':'PAY-SECOND','payment_method':{'qr_code':'test','qr_code_base64':'encoded'}}]}}
        with patch('src.services.sports_installment_payments.create_pix_order',return_value=response):
            self.assertEqual(self.client.post(f'/material-esportivo/parcelas/{second}/pix',json={}).status_code,200)
        self.login_manager();self.assertEqual(self.client.get('/sports/installments/receivables').status_code,200)


if __name__=='__main__':unittest.main()
