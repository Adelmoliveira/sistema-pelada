import os,json,unittest,threading
from pathlib import Path
from unittest.mock import patch
from datetime import date,datetime,timezone
import psycopg2,psycopg2.extras
from src.db import DbWrapper,init_postgres
from src.services.pix_checkout_closures import request_closure,process_closure,has_confirmed_payment
from src.services.bar_installment_payments import create_installment_payment_attempt as bar_charge
from src.services.sports_installment_payments import create_installment_payment_attempt as sports_charge
from src.services.bar_installment_reconciliation import reconcile_installment_order as bar_reconcile
from src.services.sports_installment_reconciliation import reconcile_installment_order as sports_reconcile
from src.services.mercadopago import MercadoPagoError
# Explicit DSN must point to a disposable PostgreSQL database with migrations applied.
DSN=os.environ.get('PIX_CLOSURE_TEST_POSTGRES_DSN')
def connect():
    return DbWrapper(psycopg2.connect(DSN,cursor_factory=psycopg2.extras.DictCursor),is_postgres=True)
@unittest.skipUnless(DSN, 'PostgreSQL temporário não configurado')
class ClosurePaymentReconciliationPostgresTest(unittest.TestCase):
    def setUp(self):
        self.db=connect();self.addCleanup(self.db.close)
    def fixture(self,domain='bar',plan=True):
        db=self.db
        player=db.execute('INSERT INTO players(name,email) VALUES(?,?)',('Player-'+str(threading.get_ident())+'-'+str(datetime.now().timestamp()),'player@example.com')).lastrowid
        product=db.execute('INSERT INTO products(name,category,price_cents,stock,case_sale_enabled,units_per_case) VALUES(?,?,1500,76,1,24)',('Product-'+str(player),'Material Esportivo' if domain=='sports' else 'Cerveja')).lastrowid
        sale=db.execute("INSERT INTO sales(player_id,payment_method,total_cents,paid,payment_status,created_at,external_reference,mercadopago_order_id) VALUES(?,'Pix',36000,0,'pending','2026-10-04 12:00:00',?,?)",(player,f'sale-{product}',f'order-{product}')).lastrowid
        item=db.execute('INSERT INTO sale_items(sale_id,product_id,quantity,unit_price_cents) VALUES(?,?,24,1500)',(sale,product)).lastrowid
        variant=None
        if domain=='sports':
            variant=db.execute("INSERT INTO sports_product_variants(product_id,size,stock) VALUES(?,'M',76)",(product,)).lastrowid
            db.execute("INSERT INTO sports_sale_item_details(sale_item_id,variant_id,variant_size,order_mode,fulfillment_status) VALUES(?,?,'M','ready','reserved') RETURNING sale_item_id",(item,variant))
            db.execute("INSERT INTO sports_stock_reservations(sale_item_id,variant_id,quantity) VALUES(?,?,24)",(item,variant))
            typeid=db.execute('SELECT id FROM sports_material_types LIMIT 1').fetchone()[0]
            db.execute('INSERT INTO sports_product_config(product_id,type_id,installment_pix_enabled) VALUES(?,?,TRUE) RETURNING product_id',(product,typeid))
        installments=[]
        if plan:
            if domain=='bar':
                snapshot=[dict(product_id=product,quantity=24,unit_price_cents=1500,sale_mode='case',case_quantity=1,units_per_case=24)]
                pid=db.execute("INSERT INTO bar_installment_plans(sale_id,total_cents,checkout_origin,checkout_snapshot) VALUES(?,36000,'case_only',?)",(sale,json.dumps(snapshot))).lastrowid
                amounts=[18000,18000]
            else:
                pid=db.execute('INSERT INTO sports_installment_plans(sale_id,total_cents) VALUES(?,36000)',(sale,)).lastrowid
                amounts=[12000,12000,12000]
            for number,amount in enumerate(amounts,1):
                installments.append(db.execute(f'INSERT INTO {domain}_installments(plan_id,installment_number,amount_cents,due_date) VALUES(?,?,?,?)',(pid,number,amount,date(2026,10,4))).lastrowid)
        db.commit()
        self.domain,self.sale,self.product,self.variant,self.installments=domain,sale,product,variant,installments
        self.attempts=[]
        for iid in installments:
            order=dict(id=f'order-{iid}-{domain}',transactions={'payments':[{'id':f'pay-{iid}-{domain}','payment_method':{'qr_code':'QR','qr_code_base64':'encoded'}}]})
            with patch(f'src.services.{domain}_installment_payments.create_pix_order',return_value=order):
                self.attempts.append((bar_charge if domain=='bar' else sports_charge)(db,iid,'fake'))
        return sale
    def request(self):
        return request_closure(self.db,self.sale,'timeout',now=datetime(2026,10,4,12,5,tzinfo=timezone.utc))
    def remote(self,token,oid):
        if self.attempts:
            a=next(a for a in self.attempts if a['mercado_pago_order_id']==oid)
            reference=a['external_reference']
        else: reference=f'sale-{self.product}'
        return dict(id=oid,external_reference=reference,status='canceled')
    def stock(self):
        if self.variant:return self.db.execute('SELECT stock FROM sports_product_variants WHERE id=?',(self.variant,)).fetchone()[0]
        return self.db.execute('SELECT stock FROM products WHERE id=?',(self.product,)).fetchone()[0]
    def event(self,n):
        a=self.attempts[n-1]
        return dict(id=a['mercado_pago_order_id'],external_reference=a['external_reference'],status='processed',status_detail='accredited',total_paid_amount=f"{a['amount_cents']/100:.2f}",transactions={'payments':[{'id':a['mercado_pago_payment_id']}]})
    def approve(self,n,db=None):
        return (bar_reconcile if self.domain=='bar' else sports_reconcile)(db or self.db,self.event(n))
    def assert_aborted_without_restore(self):
        first=dict(self.db.execute('SELECT * FROM pix_checkout_closures WHERE sale_id=?',(self.sale,)).fetchone())
        self.assertEqual(first['status'],'aborted_payment')
        self.assertIsNone(first['completed_at'])
        with patch('src.services.pix_checkout_closures.get_order') as api:
            for _ in range(2):
                self.assertEqual(process_closure(self.db,self.sale,'fake')['status'],'aborted_payment')
            api.assert_not_called()
        self.assertEqual(self.stock(),76)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM sale_cancellations WHERE sale_id=?',(self.sale,)).fetchone()[0],0)

    def test_whole_sale_payment_aborts_pending_closure(self):
        from src.routes.sales import apply_mercadopago_status
        for status in ('requested','awaiting_provider','retryable'):
            with self.subTest(status=status):
                self.fixture(plan=False);self.request()
                self.db.execute('UPDATE pix_checkout_closures SET status=? WHERE sale_id=?',(status,self.sale));self.db.commit()
                order=dict(id=f'order-{self.product}',external_reference=f'sale-{self.product}',status='processed',status_detail='accredited',total_paid_amount='360.00',transactions={'payments':[{'id':f'pay-{self.product}'}]})
                for _ in range(2):
                    sale=dict(self.db.execute('SELECT * FROM sales WHERE id=?',(self.sale,)).fetchone())
                    self.assertEqual(apply_mercadopago_status(self.db,sale,order),'approved')
                self.assert_aborted_without_restore()

    def test_any_bar_installment_aborts_pending_closure(self):
        for number in (1,2):
            with self.subTest(number=number):
                self.fixture();self.request()
                self.db.execute("UPDATE pix_checkout_closures SET status='awaiting_provider' WHERE sale_id=?",(self.sale,));self.db.commit()
                self.approve(number);self.approve(number)
                self.assert_aborted_without_restore()

    def test_any_sports_installment_aborts_pending_closure(self):
        for number in (1,2,3):
            with self.subTest(number=number):
                self.fixture('sports');self.request()
                self.db.execute("UPDATE pix_checkout_closures SET status='retryable' WHERE sale_id=?",(self.sale,));self.db.commit()
                self.approve(number);self.approve(number)
                self.assert_aborted_without_restore()

    def test_completed_closure_is_unchanged(self):
        from src.services.pix_checkout_closures import abort_closure_on_payment
        self.fixture();self.request()
        with patch('src.services.pix_checkout_closures.get_order',side_effect=self.remote):
            self.assertEqual(process_closure(self.db,self.sale,'fake')['status'],'completed')
        before=dict(self.db.execute('SELECT * FROM pix_checkout_closures WHERE sale_id=?',(self.sale,)).fetchone())
        with self.db:
            abort_closure_on_payment(self.db,self.sale)
            abort_closure_on_payment(self.db,self.sale)
        after=dict(self.db.execute('SELECT * FROM pix_checkout_closures WHERE sale_id=?',(self.sale,)).fetchone())
        self.assertEqual(before,after)
        self.assertEqual(self.stock(),100)
