import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import test_bar_installment_client as fixtures
from app import app
from src.db import get_db
from src.services.bar_installments import create_installment_plan
from src.services.bar_receivables import receivables, receivable_detail


class BarReceivablesTest(unittest.TestCase):
    setUp = fixtures.BarInstallmentClientTest.setUp
    tearDown = fixtures.BarInstallmentClientTest.tearDown
    create_sports_schema = fixtures.BarInstallmentClientTest.create_sports_schema
    setup_client = fixtures.BarInstallmentClientTest.setup_client

    def fixture(self):
        self.setup_client()
        clock=patch('src.services.bar_receivables.local_today',return_value=date(2026,11,4))
        clock.start();self.addCleanup(clock.stop)
        with app.app_context():
            db=get_db()
            sql=Path('supabase/migrations/20261005010000_pix_checkout_closures.sql').read_text()
            db.conn.executescript(sql.replace('BIGSERIAL PRIMARY KEY','INTEGER PRIMARY KEY').replace('TIMESTAMPTZ','TEXT'))
            other=db.execute("INSERT INTO players(name,email) VALUES('Outro','other@example.com')").lastrowid
            self.other=other
            self.plans={}
            for situation,player in (('overdue',self.player_id),('open',other),('paid',self.player_id)):
                sale=db.execute("INSERT INTO sales(player_id,payment_method,total_cents,paid,payment_status) VALUES(?,'Pix',36000,0,'pending')",(player,)).lastrowid
                db.execute('INSERT INTO sale_items(sale_id,product_id,quantity,unit_price_cents) VALUES(?,?,24,1500)',(sale,self.product_id));db.commit()
                day='2026-12-01' if situation=='open' else '2026-10-04'
                result=create_installment_plan(db,sale,36000,day,[dict(product_id=self.product_id,quantity=1,sale_mode='case')])
                plan=result['plan']['id'];self.plans[situation]=plan
                if situation in {'overdue','open'}:
                    db.execute("UPDATE bar_installments SET status='paid',paid_at='2026-10-04' WHERE plan_id=? AND installment_number=1",(plan,))
                    db.execute("UPDATE bar_installment_plans SET status='active',first_installment_paid_at='2026-10-04' WHERE id=?",(plan,))
                if situation=='paid':
                    db.execute("UPDATE bar_installments SET status='paid',paid_at='2026-10-04' WHERE plan_id=?",(plan,))
                    db.execute("UPDATE bar_installment_plans SET status='paid',first_installment_paid_at='2026-10-04',fully_paid_at='2026-10-04' WHERE id=?",(plan,))
                db.commit()
            self.staff=db.execute("INSERT INTO users(username,name,password_hash,role) VALUES('staff-receivable','Staff','hash','staff')").lastrowid;db.commit()

    def view(self,args=None):
        with app.app_context():return receivables(get_db(),args or {})

    def login(self,user):
        with self.client.session_transaction() as session:session['user_id']=user

    def test_staff_and_manager_access_list_and_detail(self):
        self.fixture()
        for user in (self.staff,self.user_id):
            self.login(user)
            for path in ('/bar/installments/receivables',f"/bar/installments/receivables/{self.plans['overdue']}"):
                response=self.client.get(path)
                self.assertEqual(response.status_code,200)
                self.assertIn(b'Material Esportivo',response.data)
                self.assertIn(b'Bar',response.data)

    def test_client_blocked_list_and_detail(self):
        self.fixture()
        for path in ('/bar/installments/receivables',f"/bar/installments/receivables/{self.plans['overdue']}"):
            self.assertEqual(self.client.get(path,headers={'Accept':'application/json'}).status_code,403)

    def test_received(self):
        self.fixture();self.assertEqual(self.view()['summary']['received_cents'],72000)

    def test_balance(self):
        self.fixture();self.assertEqual(self.view()['summary']['receivable_cents'],36000)

    def test_overdue(self):
        self.fixture();self.assertEqual(self.view()['summary']['overdue_cents'],18000)

    def test_upcoming(self):
        self.fixture();self.assertEqual(self.view()['summary']['upcoming_cents'],18000)

    def test_overdue_count(self):
        self.fixture();self.assertEqual(self.view()['summary']['overdue_count'],1)

    def test_distinct_players_with_balance(self):
        self.fixture();self.assertEqual(self.view()['summary']['players_with_balance'],2)

    def test_classifications_and_next_due(self):
        self.fixture()
        plans={p['id']:p for p in self.view()['plans']}
        for situation,label in (('paid','Quitado'),('overdue','Vencido'),('open','Em aberto')):
            self.assertEqual(plans[self.plans[situation]]['situation_label'],label)
        self.assertEqual(plans[self.plans['overdue']]['next_due_date'],'2026-11-03')
        self.assertIsNone(plans[self.plans['paid']]['next_due_date'])

    def test_detail_exactly_two_and_paid_date(self):
        self.fixture()
        with app.app_context():plan=receivable_detail(get_db(),self.plans['overdue'])
        self.assertEqual([i['installment_number'] for i in plan['installments']],[1,2])
        self.assertEqual(plan['installments'][0]['paid_at'],'2026-10-04')
        self.assertEqual(plan['balance_cents'],18000)
        self.login(self.user_id)
        response=self.client.get(f"/bar/installments/receivables/{plan['id']}")
        self.assertIn(b'1/2',response.data);self.assertIn(b'2/2',response.data)

    def test_attempts_never_duplicate_financials(self):
        self.fixture();before=self.view()['summary']
        with app.app_context():
            db=get_db();installment=db.execute('SELECT id FROM bar_installments WHERE plan_id=? LIMIT 1',(self.plans['overdue'],)).fetchone()[0]
            for number in range(3):
                db.execute("INSERT INTO bar_installment_payment_attempts(installment_id,amount_cents,status,external_reference,idempotency_key) VALUES(?,18000,'approved',?,?)",(installment,f'audit-{number}',f'key-{number}'))
            db.commit()
        self.assertEqual(self.view()['summary'],before)

    def test_filters(self):
        self.fixture()
        for situation in ('open','overdue','paid'):
            self.assertEqual([p['id'] for p in self.view({'situation':situation})['plans']],[self.plans[situation]])
        self.assertEqual([p['id'] for p in self.view({'player_id':str(self.other)})['plans']],[self.plans['open']])
        self.assertEqual([p['id'] for p in self.view({'due_from':'2026-12-01','due_to':'2026-12-31'})['plans']],[self.plans['open']])
        self.assertEqual(self.view({'situation':'open','player_id':str(self.player_id)})['plans'],[])

    def test_invalid_filters_and_missing_detail(self):
        self.fixture()
        for args in ({'situation':'bad'},{'player_id':'-1'},{'due_from':'invalid'},{'due_from':'2026-12-31','due_to':'2026-12-01'}):
            with self.assertRaises(ValueError):self.view(args)
        self.login(self.user_id)
        self.assertEqual(self.client.get('/bar/installments/receivables/99999').status_code,404)

    def test_read_only_service_and_pages(self):
        self.fixture();self.login(self.user_id)
        with app.app_context():
            db=get_db()
            original=db.execute
            def select_only(sql,*args):
                self.assertTrue(sql.lstrip().upper().startswith('SELECT'),sql)
                self.assertNotIn('payment_attempts',sql)
                return original(sql,*args)
            with patch.object(db,'execute',side_effect=select_only):
                receivables(db,{});receivable_detail(db,self.plans['overdue'])
            tables=('sales','products','bar_installment_plans','bar_installments','bar_installment_payment_attempts','sale_item_deliveries')
            before={t:[tuple(r) for r in original('SELECT * FROM '+t).fetchall()] for t in tables}
        for path in ('/bar/installments/receivables',f"/bar/installments/receivables/{self.plans['overdue']}"):
            page=self.client.get(path);self.assertEqual(page.status_code,200)
            self.assertNotIn(b'Pagar agora',page.data)
            self.assertEqual(self.client.post(path,json={}).status_code,405)
        with app.app_context():
            db=get_db();after={t:[tuple(r) for r in db.execute('SELECT * FROM '+t).fetchall()] for t in tables}
        self.assertEqual(before,after)

    def test_unconfirmed_plan_excluded_everywhere(self):
        self.fixture()
        with app.app_context():
            db=get_db();plan=self.plans['open']
            db.execute("UPDATE bar_installments SET status='pending',paid_at=NULL WHERE plan_id=?",(plan,));db.commit()
            result=receivables(db,{})
            self.assertNotIn(plan,{p['id'] for p in result['plans']})
            self.assertNotIn(self.other,{p['id'] for p in result['players']})
            self.assertIsNone(receivable_detail(db,plan))
            self.assertEqual(result['summary']['receivable_cents'],18000)

    def test_advance_second_included(self):
        self.fixture()
        with app.app_context():
            db=get_db();plan=self.plans['open']
            db.execute("UPDATE bar_installments SET status=CASE WHEN installment_number=2 THEN 'paid' ELSE 'pending' END WHERE plan_id=?",(plan,));db.commit()
            self.assertIsNotNone(receivable_detail(db,plan))
            self.assertIn(plan,{p['id'] for p in receivables(db,{})['plans']})

    def test_terminal_or_completed_closure_excluded(self):
        self.fixture()
        with app.app_context():
            db=get_db();plan=self.plans['overdue']
            sale=db.execute('SELECT sale_id FROM bar_installment_plans WHERE id=?',(plan,)).fetchone()[0]
            for status in ('canceled','expired'):
                db.execute('UPDATE sales SET payment_status=? WHERE id=?',(status,sale));db.commit()
                self.assertIsNone(receivable_detail(db,plan))
            db.execute("UPDATE sales SET payment_status='pending' WHERE id=?",(sale,))
            db.execute("INSERT INTO pix_checkout_closures(sale_id,reason,status) VALUES(?,'timeout','completed')",(sale,));db.commit()
            self.assertIsNone(receivable_detail(db,plan))
            self.assertNotIn(plan,{p['id'] for p in receivables(db,{})['plans']})
