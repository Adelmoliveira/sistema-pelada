import unittest
from datetime import date
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

import test_sports_installment_client as fixtures
from src.services.sports_installments import create_installment_plan
from src.services.sports_receivables import receivables, receivable_detail


class SportsReceivablesTest(unittest.TestCase):
    setUp = fixtures.SportsInstallmentClientTest.setUp
    sale = fixtures.SportsInstallmentClientTest.sale
    setup_plan = fixtures.SportsInstallmentClientTest.setup_plan
    setup_payments = fixtures.SportsInstallmentClientTest.setup_payments
    charge = fixtures.SportsInstallmentClientTest.charge
    order = fixtures.SportsInstallmentClientTest.order
    event = fixtures.SportsInstallmentClientTest.event
    approve = fixtures.SportsInstallmentClientTest.approve
    prepare = fixtures.SportsInstallmentClientTest.prepare

    def fixture(self):
        self.prepare()
        sql=Path('supabase/migrations/20261005010000_pix_checkout_closures.sql').read_text()
        self.db.conn.executescript(sql.replace('BIGSERIAL PRIMARY KEY','INTEGER PRIMARY KEY').replace('TIMESTAMPTZ','TEXT'))
        self.addCleanup(patch.stopall)
        patch('src.services.sports_receivables.today',return_value=date(2026,11,4)).start()
        self.overdue_id=self.db.execute('SELECT id FROM sports_installment_plans WHERE sale_id=?',(self.sale_id,)).fetchone()[0]
        self.db.execute("UPDATE sports_installments SET status='paid',paid_at='2026-10-04' WHERE plan_id=? AND installment_number=1",(self.overdue_id,))
        self.db.execute("UPDATE sports_installment_plans SET status='active',first_installment_paid_at='2026-10-04' WHERE id=?",(self.overdue_id,))
        self.db.commit()
        self.open_sale=self.sale(total=30000)
        other_player=self.db.execute("INSERT INTO players(name,email) VALUES('Aberto','open@example.com')").lastrowid
        self.db.execute('UPDATE sales SET player_id=? WHERE id=?',(other_player,self.open_sale));self.db.commit()
        self.other_player=other_player
        self.open_id=create_installment_plan(self.db,self.open_sale,30000,date(2026,12,1))['plan']['id']
        self.db.execute("UPDATE sports_installments SET status='paid',paid_at='2026-10-04' WHERE plan_id=? AND installment_number=1",(self.open_id,))
        self.db.execute("UPDATE sports_installment_plans SET status='active',first_installment_paid_at='2026-10-04' WHERE id=?",(self.open_id,))
        self.db.commit()
        self.paid_sale=self.sale(total=9000)
        self.db.execute('UPDATE sales SET player_id=? WHERE id=?',(self.player_id,self.paid_sale));self.db.commit()
        self.paid_id=create_installment_plan(self.db,self.paid_sale,9000,date(2026,10,1))['plan']['id']
        self.db.execute("UPDATE sports_installments SET status='paid',paid_at='2026-10-01' WHERE plan_id=?",(self.paid_id,))
        self.db.execute("UPDATE sports_installment_plans SET status='paid',first_installment_paid_at='2026-10-01',fully_paid_at='2026-10-01' WHERE id=?",(self.paid_id,))
        self.db.commit()
        self.admins={}
        for role in ('staff','manager'):
            self.admins[role]=self.db.execute('INSERT INTO users(username,name,password_hash,role) VALUES(?,?,?,?)',(role,role,'hash',role)).lastrowid
        self.db.commit()

    def view(self,args=None):return receivables(self.db,args or {})

    def login(self,role):
        with self.client.session_transaction() as session:session['user_id']=self.admins[role] if role in self.admins else self.user

    def test_staff_access(self):
        self.fixture();self.login('staff')
        response=self.client.get('/sports/installments/receivables')
        self.assertEqual(response.status_code,200)
        self.assertIn('Contas a Receber',response.get_data(as_text=True))

    def test_manager_access(self):
        self.fixture();self.login('manager')
        self.assertEqual(self.client.get('/sports/installments/receivables').status_code,200)

    def test_client_blocked_list_and_detail(self):
        self.fixture()
        for path in ('/sports/installments/receivables',f'/sports/installments/receivables/{self.overdue_id}'):
            self.assertEqual(self.client.get(path,headers={'Accept':'application/json'}).status_code,403)

    def test_receivable_total(self):
        self.fixture();self.assertEqual(self.view()['summary']['receivable_cents'],26667)

    def test_received_total(self):
        self.fixture();self.assertEqual(self.view()['summary']['received_cents'],22333)

    def test_overdue_total_and_count(self):
        self.fixture();summary=self.view()['summary']
        self.assertEqual((summary['overdue_cents'],summary['overdue_count']),(3333,1))

    def test_upcoming_total(self):
        self.fixture();self.assertEqual(self.view()['summary']['upcoming_cents'],23334)

    def test_distinct_players_with_balance(self):
        self.fixture();self.assertEqual(self.view()['summary']['players_with_balance'],2)

    def test_filter_player(self):
        self.fixture();result=self.view({'player_id':str(self.player_id)})
        self.assertEqual({p['id'] for p in result['plans']},{self.overdue_id,self.paid_id})
        self.assertEqual(result['summary']['receivable_cents'],6667)

    def test_filter_open(self):
        self.fixture();self.assertEqual([p['id'] for p in self.view({'situation':'open'})['plans']],[self.open_id])

    def test_filter_overdue(self):
        self.fixture();self.assertEqual([p['id'] for p in self.view({'situation':'overdue'})['plans']],[self.overdue_id])

    def test_filter_paid(self):
        self.fixture();self.assertEqual([p['id'] for p in self.view({'situation':'paid'})['plans']],[self.paid_id])

    def test_due_period_filters_plans_keeps_complete_totals(self):
        self.fixture();result=self.view({'due_from':'2026-11-03','due_to':'2026-11-03'})
        self.assertEqual([p['id'] for p in result['plans']],[self.overdue_id])
        self.assertEqual(result['summary']['received_cents'],3333)
        self.assertEqual(result['summary']['receivable_cents'],6667)

    def test_combined_filters(self):
        self.fixture();result=self.view({'player_id':str(self.player_id),'situation':'overdue','due_from':'2026-11-01','due_to':'2026-11-30'})
        self.assertEqual([p['id'] for p in result['plans']],[self.overdue_id])

    def test_paid_classification_from_installments(self):
        self.fixture();self.assertEqual(receivable_detail(self.db,self.paid_id)['situation_label'],'Quitado')

    def test_overdue_classification_derived(self):
        self.fixture();plan=receivable_detail(self.db,self.overdue_id)
        self.assertEqual(plan['situation_label'],'Vencido')
        self.assertTrue(plan['withdrawal_allowed'])
        self.assertEqual(self.db.execute('SELECT status FROM sports_installments WHERE plan_id=? AND installment_number=2',(self.overdue_id,)).fetchone()[0],'pending')

    def test_open_classification(self):
        self.fixture();self.assertEqual(receivable_detail(self.db,self.open_id)['situation_label'],'Em aberto')

    def test_detail_three_parts_and_missing_plan(self):
        self.fixture();self.login('staff')
        response=self.client.get(f'/sports/installments/receivables/{self.overdue_id}')
        self.assertEqual(response.status_code,200)
        html=response.get_data(as_text=True)
        self.assertEqual(html.count('data-installment='),3)
        self.assertIn('Primeira parcela paga em',html)
        self.assertIn('Cliente',html)
        self.assertEqual(self.client.get('/sports/installments/receivables/999999').status_code,404)

    def test_attempts_never_duplicate_financial_totals(self):
        self.fixture();before=self.view()['summary']
        for _ in range(3):
            self.db.execute("INSERT INTO sports_installment_payment_attempts(installment_id,amount_cents,status,external_reference,idempotency_key) VALUES(?,3333,'approved',?,?)",(self.installments[0]['id'],str(uuid4()),str(uuid4())))
        self.db.commit()
        self.assertEqual(self.view()['summary'],before)

    def test_pages_read_only_no_financial_actions_or_api(self):
        self.fixture();self.login('manager')
        tables=('sales','sale_items','sports_installment_plans','sports_installments','sports_installment_payment_attempts','sports_product_variants','sports_stock_reservations','sale_item_deliveries')
        before={t:[tuple(r) for r in self.db.execute(f'SELECT * FROM {t}').fetchall()] for t in tables}
        with patch('src.routes.sales.get_order') as api:
            for path in ('/sports/installments/receivables',f'/sports/installments/receivables/{self.overdue_id}'):
                response=self.client.get(path)
                html=response.get_data(as_text=True)
                content=html.split('<main',1)[1].split('</main>',1)[0]
                self.assertNotIn('method="post"',content.lower())
                self.assertNotIn('Pagar agora',html)
                self.assertNotIn('idempotency_key',html)
                self.assertEqual(self.client.post(path,json={}).status_code,405)
        api.assert_not_called()
        after={t:[tuple(r) for r in self.db.execute(f'SELECT * FROM {t}').fetchall()] for t in tables}
        self.assertEqual(before,after)

    def test_invalid_filters_rejected_and_values_preserved(self):
        self.fixture();self.login('staff')
        for query in ('due_from=bad','due_from=2027-01-01&due_to=2026-01-01','player_id=1%20OR%201=1','situation=invalid'):
            self.assertEqual(self.client.get('/sports/installments/receivables?'+query).status_code,400)
        response=self.client.get(f'/sports/installments/receivables?player_id={self.player_id}&situation=overdue&due_from=2026-11-01')
        html=response.get_data(as_text=True)
        self.assertIn('value="2026-11-01"',html)
        self.assertIn('value="overdue" selected',html)

    def test_unconfirmed_plan_excluded_everywhere(self):
        self.fixture()
        self.db.execute("UPDATE sports_installments SET status='pending',paid_at=NULL WHERE plan_id=?",(self.open_id,));self.db.commit()
        result=self.view()
        self.assertNotIn(self.open_id,{p['id'] for p in result['plans']})
        self.assertNotIn(self.other_player,{p['id'] for p in result['players']})
        self.assertIsNone(receivable_detail(self.db,self.open_id))
        self.assertEqual(result['summary']['receivable_cents'],6667)

    def test_advance_second_or_third_included(self):
        self.fixture()
        for number in (2,3):
            self.db.execute("UPDATE sports_installments SET status=CASE WHEN installment_number=? THEN 'paid' ELSE 'pending' END WHERE plan_id=?",(number,self.open_id));self.db.commit()
            self.assertIsNotNone(receivable_detail(self.db,self.open_id))
            self.assertIn(self.open_id,{p['id'] for p in self.view()['plans']})

    def test_terminal_or_completed_closure_excluded(self):
        self.fixture()
        for status in ('canceled','expired'):
            self.db.execute('UPDATE sales SET payment_status=? WHERE id=?',(status,self.sale_id));self.db.commit()
            self.assertIsNone(receivable_detail(self.db,self.overdue_id))
        self.db.execute("UPDATE sales SET payment_status='pending' WHERE id=?",(self.sale_id,))
        self.db.execute("INSERT INTO pix_checkout_closures(sale_id,reason,status) VALUES(?,'timeout','completed')",(self.sale_id,));self.db.commit()
        self.assertIsNone(receivable_detail(self.db,self.overdue_id))
        self.assertNotIn(self.overdue_id,{p['id'] for p in self.view()['plans']})


if __name__=='__main__':unittest.main()
