import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from flask import Flask,g,jsonify
from flask_wtf.csrf import CSRFProtect,generate_csrf
from jinja2 import ChoiceLoader,DictLoader

from test_pix_closure_payment_reconciliation_postgres import ClosurePaymentReconciliationPostgresTest as Fixture,DSN,connect
from src.services.pix_checkout_abandonment import expire_recent_checkouts,decorate_purchase_closure
from src.routes import auth,finance,sales

@unittest.skipUnless(DSN,'PostgreSQL temporário não configurado')
class AbandonmentPostgresTest(unittest.TestCase):
    fixture=Fixture.fixture
    remote=Fixture.remote
    stock=Fixture.stock
    event=Fixture.event
    approve=Fixture.approve
    request=Fixture.request

    def setUp(self):
        self.db=connect();self.addCleanup(self.db.close)
        # Prior fixtures in this disposable database are outside this test's deploy cutoff.
        self.db.execute("UPDATE sales SET created_at='2026-10-04 12:00:00'");self.db.commit()
        self.app=Flask(__name__,template_folder='../templates',static_folder='../static')
        self.app.config.update(SECRET_KEY='isolated-test',EXTERNAL_PAYMENTS_ENABLED=True,CRON_ENABLED=True,
                               MERCADOPAGO_ACCESS_TOKEN='fake',CRON_SECRET='test-cron',TESTING=True)
        self.app.register_blueprint(auth.bp);self.app.register_blueprint(finance.bp);self.app.register_blueprint(sales.bp)
        self.app.jinja_loader=ChoiceLoader([DictLoader({'base.html':'{% block content %}{% endblock %}'}),self.app.jinja_loader])
        self.app.jinja_env.filters['money']=str
        self.app.jinja_env.filters['brdate']=str
        CSRFProtect(self.app)
        self.user=None
        @self.app.before_request
        def user():g.user=self.user
        @self.app.get('/_test/csrf')
        def token():return jsonify(token=generate_csrf())
        self.client=self.app.test_client()
        self.headers={'Accept':'application/json','X-CSRFToken':self.client.get('/_test/csrf').json['token']}
        for module in (auth,finance,sales):
            patcher=patch.object(module,'get_db',return_value=self.db);patcher.start();self.addCleanup(patcher.stop)

    def buy(self,domain='bar',plan=True):
        self.fixture(domain,plan)
        sale=self.db.execute('SELECT player_id FROM sales WHERE id=?',(self.sale,)).fetchone()
        uid=self.db.execute("INSERT INTO users(username,name,password_hash,role,player_id) VALUES(?,?,'hash','client',?)",(f'owner-{self.sale}','Owner',sale['player_id'])).lastrowid
        self.db.commit()
        self.user={'id':uid,'player_id':sale['player_id'],'role':'client'}
        self.now=datetime.now(timezone.utc)
        self.cutoff=self.now-timedelta(minutes=10)
        self.app.config['PIX_ABANDONMENT_NOT_BEFORE']=self.cutoff.isoformat()
        self.db.execute('UPDATE sales SET created_at=? WHERE id=?',((self.now-timedelta(minutes=6)).replace(tzinfo=None),self.sale));self.db.commit()

    def cancel(self):
        return self.client.post(f'/minhas-compras/{self.sale}/cancelar',headers=self.headers)

    def cron(self):
        return self.client.get('/cron/expire-pix-checkouts',headers={'Authorization':'Bearer test-cron'})

    def test_owner_cancel_and_csrf(self):
        self.buy(plan=False)
        self.assertEqual(self.client.post(f'/minhas-compras/{self.sale}/cancelar').status_code,400)
        with patch('src.services.pix_checkout_closures.get_order',side_effect=self.remote):
            self.assertEqual(self.cancel().json['status'],'completed')
            self.assertEqual(self.cancel().json['status'],'completed')
        self.assertEqual(self.stock(),100)
        self.assertEqual(self.db.execute('SELECT requested_by FROM pix_checkout_closures WHERE sale_id=?',(self.sale,)).fetchone()[0],self.user['id'])

    def test_other_owner_forbidden(self):
        self.buy();self.user['player_id']=999999
        with patch('src.services.pix_checkout_closures.get_order') as api:
            self.assertEqual(self.cancel().status_code,403);api.assert_not_called()
        self.assertEqual(self.stock(),76)

    def test_paid_and_advance_block(self):
        for domain,numbers in [('bar',(1,2)),('sports',(1,2,3))]:
            for number in numbers:
                self.buy(domain);self.approve(number)
                with patch('src.services.pix_checkout_closures.get_order') as api:
                    self.assertEqual(self.cancel().status_code,409);api.assert_not_called()
                self.assertEqual(self.stock(),76)
        self.buy(plan=False)
        self.db.execute("UPDATE sales SET paid=1,payment_status='approved' WHERE id=?",(self.sale,));self.db.commit()
        self.assertEqual(self.cancel().status_code,409)

    def test_deadline_and_old_sales_excluded(self):
        self.buy()
        def run():return expire_recent_checkouts(self.db,'fake',self.cutoff,self.now,limit=100)
        self.db.execute('UPDATE sales SET created_at=? WHERE id=?',((self.now-timedelta(minutes=4,seconds=59)).replace(tzinfo=None),self.sale));self.db.commit()
        with patch('src.services.pix_checkout_closures.get_order') as api:
            self.assertEqual(run()['processed'],0);api.assert_not_called()
        self.db.execute('UPDATE sales SET created_at=? WHERE id=?',((self.cutoff-timedelta(seconds=1)).replace(tzinfo=None),self.sale));self.db.commit()
        with patch('src.services.pix_checkout_closures.get_order') as api:
            self.assertEqual(run()['processed'],0);api.assert_not_called()
        self.db.execute('UPDATE sales SET created_at=? WHERE id=?',((self.now-timedelta(minutes=5)).replace(tzinfo=None),self.sale))
        self.db.execute('UPDATE bar_installment_payment_attempts SET created_at=? WHERE installment_id IN (?,?)',(self.now, *self.installments));self.db.commit()
        with patch('src.services.pix_checkout_closures.get_order',side_effect=self.remote):
            self.assertEqual(run()['completed'],1)
        self.assertEqual(self.stock(),100)

    def test_cron_auth_cutoff_and_manual_retry_same_engine(self):
        self.buy()
        self.assertEqual(self.client.get('/cron/expire-pix-checkouts').status_code,401)
        self.app.config['PIX_ABANDONMENT_NOT_BEFORE']=None
        self.assertEqual(self.cron().status_code,503)
        self.app.config['PIX_ABANDONMENT_NOT_BEFORE']=self.cutoff.isoformat()
        with patch('src.services.pix_checkout_closures.get_order',side_effect=TimeoutError('simulated timeout')):
            self.assertEqual(self.cancel().status_code,202)
        self.assertEqual(self.stock(),76)
        with patch('src.services.pix_checkout_closures.get_order',side_effect=self.remote):
            self.assertEqual(self.cron().json['completed'],1)
            self.assertEqual(self.cancel().json['status'],'completed')
            self.assertEqual(self.cron().json['processed'],0)
        self.assertEqual(self.stock(),100)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM pix_checkout_closures WHERE sale_id=?',(self.sale,)).fetchone()[0],1)

    def test_payment_during_cancel_wins(self):
        self.buy('sports')
        called=False
        def remote(token,oid):
            nonlocal called
            if not called:called=True;self.approve(3)
            return self.remote(token,oid)
        with patch('src.services.pix_checkout_closures.get_order',side_effect=remote):
            self.assertEqual(self.cancel().json['status'],'aborted_payment')
        self.assertEqual(self.stock(),76)

    def test_purchase_page_terminal_and_processing(self):
        self.buy()
        page=self.client.get('/minhas-compras')
        self.assertEqual(page.status_code,200)
        self.assertIn(b'Cancelar compra',page.data)
        with patch('src.services.pix_checkout_closures.get_order',side_effect=TimeoutError('simulated')):
            self.assertEqual(self.cancel().status_code,202)
        page=self.client.get('/minhas-compras')
        self.assertIn(b'Cancelamento em processamento',page.data)
        self.assertNotIn(b'bar-installment-pay"',page.data)
        with patch('src.services.pix_checkout_closures.get_order',side_effect=self.remote):self.cancel()
        page=self.client.get('/minhas-compras')
        self.assertIn(b'Compra cancelada',page.data)
        self.assertIn(b'Compras encerradas',page.data)
        self.assertNotIn(b'Aguardando primeira parcela',page.data)
        self.assertNotIn(b'Cancelar compra</button>',page.data)

    def test_button_and_job_concurrent_restore_once(self):
        import threading
        from src.services.pix_checkout_abandonment import cancel_client_checkout
        self.buy()
        barrier=threading.Barrier(2);local=threading.local();errors=[]
        def remote(token,oid):
            if not getattr(local,'seen',False):
                local.seen=True;barrier.wait(timeout=10)
            return self.remote(token,oid)
        def worker(manual):
            db=connect()
            try:
                if manual:cancel_client_checkout(db,self.sale,self.user['player_id'],self.user['id'],'fake')
                else:expire_recent_checkouts(db,'fake',self.cutoff,self.now)
            except Exception as exc:errors.append(exc)
            finally:db.close()
        with patch('src.services.pix_checkout_closures.get_order',side_effect=remote):
            threads=[threading.Thread(target=worker,args=(manual,)) for manual in (True,False)]
            for thread in threads:thread.start()
            for thread in threads:thread.join(timeout=15)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertFalse(errors)
        self.assertEqual(self.stock(),100)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM sale_cancellations WHERE sale_id=?',(self.sale,)).fetchone()[0],1)
