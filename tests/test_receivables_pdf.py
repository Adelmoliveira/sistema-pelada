import subprocess
import unittest
from datetime import date
from unittest.mock import patch

import test_bar_receivables as fixtures
from app import app
from src.db import get_db
from src.services.receivables_pdf import report_context, build_receivables_pdf
from src.services.bar_receivables import receivables as bar
from src.services.sports_receivables import receivables as sports


class ReceivablesPdfTest(unittest.TestCase):
    setUp=fixtures.BarReceivablesTest.setUp
    tearDown=fixtures.BarReceivablesTest.tearDown
    create_sports_schema=fixtures.BarReceivablesTest.create_sports_schema
    setup_client=fixtures.BarReceivablesTest.setup_client
    fixture=fixtures.BarReceivablesTest.fixture
    login=fixtures.BarReceivablesTest.login

    def prepare(self):
        self.fixture()
        clock=patch('src.services.sports_receivables.today',return_value=date(2026,11,4))
        clock.start();self.addCleanup(clock.stop)
        with app.app_context():
            db=get_db()
            product=db.execute("INSERT INTO products(name,category,price_cents) VALUES('Camisa PDF','Material Esportivo',9000)").lastrowid
            variant=db.execute("INSERT INTO sports_product_variants(product_id,size,stock) VALUES(?,'M',1)",(product,)).lastrowid
            sale=db.execute("INSERT INTO sales(player_id,payment_method,total_cents,paid,payment_status) VALUES(?,'Pix',9000,0,'pending')",(self.player_id,)).lastrowid
            item=db.execute('INSERT INTO sale_items(sale_id,product_id,quantity,unit_price_cents) VALUES(?,?,1,9000)',(sale,product)).lastrowid
            db.execute("INSERT INTO sports_sale_item_details(sale_item_id,variant_id,variant_size,order_mode,fulfillment_status) VALUES(?,?,'M','ready','reserved')",(item,variant))
            plan=db.execute("INSERT INTO sports_installment_plans(sale_id,total_cents,status,first_installment_paid_at) VALUES(?,9000,'active','2026-10-04')",(sale,)).lastrowid
            for number,due in ((1,'2026-10-04'),(2,'2026-11-03'),(3,'2026-12-03')):
                db.execute('INSERT INTO sports_installments(plan_id,installment_number,amount_cents,due_date,status) VALUES(?,?,3000,?,?)',(plan,number,due,'paid' if number==1 else 'pending'))
            db.commit()
        self.login(self.user_id)

    def text(self,response):
        return subprocess.run(['pdftotext','-','-'],input=response.data,stdout=subprocess.PIPE,check=True).stdout.decode()

    def test_domain_pdfs_and_permissions(self):
        self.prepare()
        for user in (self.staff,self.user_id):
            self.login(user)
            for path in ('/bar/installments/receivables/pdf','/sports/installments/receivables/pdf','/installments/receivables/pdf'):
                response=self.client.get(path)
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.mimetype,'application/pdf')
                self.assertTrue(response.data.startswith(b'%PDF'))
                self.assertIn('CONTAS A RECEBER',self.text(response))
        self.login(self.client_id)
        for path in ('/bar/installments/receivables/pdf','/sports/installments/receivables/pdf','/installments/receivables/pdf'):
            self.assertEqual(self.client.get(path,headers={'Accept':'application/json'}).status_code,403)

    def test_filters_and_indicators_match_sources(self):
        self.prepare()
        for domain,service in (('bar',bar),('sports',sports)):
            with app.app_context():
                db=get_db()
                for args in ({},{'situation':'overdue'},{'player_id':str(self.player_id),'due_from':'2026-11-01','due_to':'2026-11-30'}):
                    source=service(db,args);context=report_context(db,args,domain)
                    self.assertEqual(context['summary'],source['summary'])
                    self.assertEqual(context['sections'][0][1]['plans'],source['plans'])
            response=self.client.get(f'/{domain}/installments/receivables/pdf?situation=overdue')
            self.assertEqual(response.status_code,200)
            text=self.text(response)
            self.assertIn('Situação: Vencido',text)
            self.assertNotIn('Em aberto',text)
            self.assertNotIn('Quitado',text)

    def test_consolidated_sum_and_distinct_players(self):
        self.prepare()
        with app.app_context():context=report_context(get_db(),{},'all')
        self.assertEqual(context['summary'],dict(receivable_cents=42000,overdue_cents=21000,upcoming_cents=21000,received_cents=75000,overdue_count=2,players_with_balance=2))
        response=self.client.get('/installments/receivables/pdf')
        text=self.text(response)
        self.assertIn('BAR',text);self.assertIn('MATERIAL ESPORTIVO',text)
        self.assertIn('R$ 420,00',text);self.assertIn('R$ 750,00',text)

    def test_attempts_do_not_duplicate_and_read_only(self):
        self.prepare()
        with app.app_context():
            db=get_db()
            first=db.execute('SELECT id FROM bar_installments ORDER BY id LIMIT 1').fetchone()[0]
            for n in range(3):
                db.execute("INSERT INTO bar_installment_payment_attempts(installment_id,amount_cents,status,external_reference,idempotency_key) VALUES(?,18000,'approved',?,?)",(first,f'pdf-{n}',f'pdfkey-{n}'))
            db.commit()
            tables=('sales','products','bar_installment_plans','bar_installments','bar_installment_payment_attempts','sports_installment_plans','sports_installments','sale_item_deliveries')
            before={t:[tuple(r) for r in db.execute('SELECT * FROM '+t).fetchall()] for t in tables}
            self.assertEqual(report_context(db,{},'all')['summary']['received_cents'],75000)
        for path in ('/bar/installments/receivables/pdf','/sports/installments/receivables/pdf','/installments/receivables/pdf'):
            self.assertEqual(self.client.get(path).status_code,200)
            self.assertEqual(self.client.post(path,json={}).status_code,405)
        with app.app_context():
            db=get_db();after={t:[tuple(r) for r in db.execute('SELECT * FROM '+t).fetchall()] for t in tables}
        self.assertEqual(before,after)

    def test_empty_and_existing_pages_export_filters(self):
        self.prepare()
        for domain in ('bar','sports'):
            response=self.client.get(f'/{domain}/installments/receivables/pdf?player_id=99999')
            self.assertEqual(response.status_code,200)
            self.assertIn('Nenhum parcelamento',self.text(response))
            page=self.client.get(f'/{domain}/installments/receivables?situation=overdue')
            self.assertEqual(page.status_code,200)
            self.assertIn(b'Exportar PDF',page.data)
            self.assertIn(b'/pdf?situation=overdue',page.data)
        self.assertEqual(self.client.get('/installments/receivables/pdf?player_id=99999').status_code,200)

    def test_long_table_paginates_and_repeats_headers(self):
        self.prepare()
        with app.app_context():context=report_context(get_db(),{},'all')
        context['sections'][0][1]['plans']*=40
        output=build_receivables_pdf(context)
        # Artifact is temporary and contains only synthetic test data.
        from pathlib import Path
        Path('/private/tmp/pelada-receivables-review.pdf').write_bytes(output.getvalue())
        text=subprocess.run(['pdftotext','-','-'],input=output.getvalue(),stdout=subprocess.PIPE,check=True).stdout.decode()
        self.assertGreater(text.count('Peladeiro\n'),1)
        self.assertIn('Página 2',text)

    def test_unconfirmed_and_closed_excluded_from_pdf_and_totals(self):
        self.prepare()
        with app.app_context():
            db=get_db()
            db.execute("UPDATE bar_installments SET status='pending',paid_at=NULL WHERE plan_id=?",(self.plans['open'],))
            db.execute("UPDATE sports_installments SET status='pending',paid_at=NULL")
            db.commit()
            context=report_context(db,{},'all')
            self.assertEqual({p['id'] for p in context['sections'][0][1]['plans']},{self.plans['overdue'],self.plans['paid']})
            self.assertEqual(context['sections'][1][1]['plans'],[])
            self.assertEqual(context['summary']['receivable_cents'],18000)
            self.assertEqual(context['summary']['received_cents'],54000)
            db.execute("UPDATE sales SET payment_status='expired' WHERE id IN (SELECT sale_id FROM bar_installment_plans WHERE id=?)",(self.plans['overdue'],))
            sale=db.execute('SELECT sale_id FROM bar_installment_plans WHERE id=?',(self.plans['paid'],)).fetchone()[0]
            db.execute("INSERT INTO pix_checkout_closures(sale_id,reason,status) VALUES(?,'timeout','completed')",(sale,));db.commit()
            context=report_context(db,{},'all')
            self.assertEqual(context['summary']['receivable_cents'],0)
            self.assertTrue(all(not section['plans'] for _,section in context['sections']))
        response=self.client.get('/installments/receivables/pdf')
        self.assertEqual(response.status_code,200)
        self.assertIn('Nenhum parcelamento',self.text(response))
