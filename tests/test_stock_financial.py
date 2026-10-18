import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.db import SCHEMA, DbWrapper
from src.catalog import SPORTS_MATERIAL_CATEGORY
from src.services.stock_financial import stock_financial_data


class StockFinancialDataTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.db = DbWrapper(self.connection)
        self.addCleanup(self.connection.close)

    def product(self, name='Água', stock=10, price=500, active=1, category='Bar'):
        return self.db.execute('''INSERT INTO products(name,category,stock,price_cents,cost_cents,active)
            VALUES(?,?,?,?,9999,?)''', (name,category,stock,price,active)).lastrowid

    def buy(self, product, quantity, cost, notes='Compra individual'):
        return self.db.execute('INSERT INTO restocks(product_id,quantity,unit_cost_cents,notes) VALUES(?,?,?,?)',
                               (product,quantity,cost,notes)).lastrowid

    def correct(self, restock, quantity, cost):
        self.db.execute('''INSERT INTO restock_corrections(restock_id,previous_quantity,corrected_quantity,
            previous_unit_cost_cents,corrected_unit_cost_cents,reason) VALUES(?,10,?,100,?,'Correção')''',
            (restock,quantity,cost))

    def test_weighted_reference_and_consolidated_margin(self):
        product = self.product()
        self.buy(product,10,100)
        self.buy(product,30,300)
        report = stock_financial_data(self.db)
        row = report['products'][0]
        self.assertEqual((row['unit_cost_cents'],row['stock_value_cents'],row['revenue_cents'],row['margin_cents']), (250,2500,5000,2500))
        self.assertEqual(row['margin_percent'],50)
        self.assertEqual(report['summary']['stock_value_cents'],2500)
        self.assertEqual(row['cost_status'],'historical_reference')

    def test_latest_correction_replaces_original_and_zero_quantity_is_ignored(self):
        product = self.product()
        first = self.buy(product,10,100)
        self.correct(first,20,200)
        self.correct(first,10,300)
        removed = self.buy(product,15,0,'Reposição via solicitação #99')
        self.correct(removed,0,0)
        row = stock_financial_data(self.db)['products'][0]
        self.assertEqual((row['unit_cost_cents'],row['purchase_units'],row['purchase_count']),(300,10,1))

    def test_missing_history_does_not_use_catalog_cost_or_fake_zero(self):
        self.product()
        report = stock_financial_data(self.db)
        self.assertIsNone(report['products'][0]['unit_cost_cents'])
        self.assertIsNone(report['summary']['stock_value_cents'])
        self.assertIsNone(report['summary']['margin_cents'])
        self.assertEqual(report['summary']['missing_revenue_cents'],5000)
        self.assertTrue(report['summary']['partial'])

    def test_unknown_cost_contaminates_whole_product_not_just_unknown_purchase(self):
        product = self.product()
        self.buy(product,10,100)
        self.buy(product,2,0)
        row = stock_financial_data(self.db)['products'][0]
        self.assertIn('missing_cost',row['reason_codes'])
        self.assertIsNone(row['stock_value_cents'])

    def test_allocated_cost_is_not_an_individually_verified_acquisition(self):
        product = self.product()
        restock = self.buy(product,10,100,'Reposição via solicitação #4 — Fornecedor')
        self.correct(restock,10,250)
        row = stock_financial_data(self.db)['products'][0]
        self.assertIn('allocated_cost',row['reason_codes'])
        self.assertIsNone(row['unit_cost_cents'])

    def test_unvalued_increases_and_uncovered_opening_balance(self):
        first = self.product()
        self.buy(first,10,100)
        self.db.execute('''INSERT INTO stock_adjustments(product_id,previous_stock,new_stock,difference,reason)
            VALUES(?,8,10,2,'Conferência mensal')''',(first,))
        second = self.product('Saldo inicial',stock=20)
        self.buy(second,10,100)
        rows = {r['id']:r for r in stock_financial_data(self.db)['products']}
        self.assertIn('unvalued_increase',rows[first]['reason_codes'])
        self.assertIn('uncovered_stock',rows[second]['reason_codes'])

    def test_restock_correction_audit_not_counted_as_unvalued_increase(self):
        product = self.product()
        self.buy(product,10,100)
        self.db.execute('''INSERT INTO stock_adjustments(product_id,previous_stock,new_stock,difference,reason)
            VALUES(?,8,10,2,'Correção da reposição #1: quantidade')''',(product,))
        self.assertEqual(stock_financial_data(self.db)['products'][0]['cost_status'],'historical_reference')

    def test_partial_margin_uses_only_covered_revenue_and_inactive_stock_is_included(self):
        product = self.product(stock=2,price=500,active=0)
        self.buy(product,10,100)
        self.product('Sem custo',stock=3,price=1000)
        self.product('Inativo vazio',stock=0,active=0)
        self.product('Esportivo',stock=100,category=SPORTS_MATERIAL_CATEGORY)
        report = stock_financial_data(self.db)
        summary = report['summary']
        self.assertEqual(len(report['products']),2)
        self.assertEqual((summary['revenue_cents'],summary['covered_revenue_cents'],summary['missing_revenue_cents']), (4000,1000,3000))
        self.assertEqual((summary['stock_value_cents'],summary['margin_cents'],summary['margin_percent']), (200,800,80))
        self.assertEqual((summary['missing_units'],summary['inactive_units']), (3,2))

    def test_rounding_uses_exact_average_for_line_and_sums_rounded_lines(self):
        product = self.product(stock=3)
        self.buy(product,2,100)
        self.buy(product,1,101)
        row = stock_financial_data(self.db)['products'][0]
        self.assertEqual(row['unit_cost_cents'],100)
        self.assertEqual(row['stock_value_cents'],301)
        self.assertEqual(stock_financial_data(self.db)['summary']['stock_value_cents'],301)

    def test_zero_price_negative_margin_and_empty_inventory(self):
        self.assertEqual(stock_financial_data(self.db)['summary']['stock_value_cents'],0)
        product = self.product(stock=1,price=0)
        self.buy(product,1,100)
        row = stock_financial_data(self.db)['products'][0]
        self.assertEqual(row['margin_cents'],-100)
        self.assertIsNone(row['margin_percent'])

    def test_invalid_history_and_zero_inventory(self):
        product = self.product(stock=0)
        self.buy(product,1,-1)
        report = stock_financial_data(self.db)
        self.assertIn('invalid_history',report['products'][0]['reason_codes'])
        self.assertEqual(report['summary']['stock_value_cents'],0)
        self.assertFalse(report['summary']['partial'])

    def test_service_executes_only_one_select_and_changes_nothing(self):
        product = self.product()
        self.buy(product,10,100)
        before = self.connection.total_changes
        statements = []
        self.connection.set_trace_callback(statements.append)
        stock_financial_data(self.db)
        self.assertEqual(self.connection.total_changes,before)
        self.assertEqual(len(statements),1)
        self.assertTrue(statements[0].lstrip().upper().startswith('SELECT'))


class StockFinancialPageTest(unittest.TestCase):
    def setUp(self):
        # No initializer/migration is run: only a disposable base-schema fixture.
        with patch.dict(os.environ, {'APP_ENV':'local','DATABASE_URL':'','SUPABASE_DB_URL':'','VERCEL':'','NOW_REGION':''}), patch('dotenv.load_dotenv'):
            from app import app
        self.app = app
        original = dict(app.config)
        self.addCleanup(lambda: (app.config.clear(),app.config.update(original)))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name)/'financial.db')
        with sqlite3.connect(self.path) as db:
            db.executescript(SCHEMA.replace('password_required INTEGER NOT NULL DEFAULT 1,',
                                           'password_required INTEGER NOT NULL DEFAULT 1,player_id INTEGER,'))
            for role in ('manager','staff','client'):
                db.execute('INSERT INTO users(username,name,password_hash,role) VALUES(?,?,?,?)',(role,role,'hash',role))
            db.execute("INSERT INTO products(name,category,stock,price_cents,cost_cents) VALUES('Água teste','Bar',10,500,900)")
            db.execute('INSERT INTO restocks(product_id,quantity,unit_cost_cents) VALUES(1,10,100)')
        app.config.update(TESTING=True,APP_ENV='local',DATABASE=self.path,DATABASE_URL=None,SECRET_KEY='financial-test',WTF_CSRF_ENABLED=False)
        self.client = app.test_client()

    def login(self, user):
        with self.client.session_transaction() as session:
            session['user_id'] = user

    def test_manager_page_menu_disclosures_and_cost_values(self):
        self.login(1)
        response = self.client.get('/stock/financial')
        self.assertEqual(response.status_code,200)
        page = response.get_data(as_text=True)
        for value in ('Visão Financeira do Estoque','Estimativa, não valor contábil','pedidos pendentes','R$ 1,00','R$ 10,00','R$ 50,00','R$ 40,00','Referência histórica estimada','class="active" href="/stock/financial"','class="collapse show" id="sidebar-stock"'):
            self.assertIn(value,page)
        self.assertNotIn('<form',page.split('<main',1)[1].split('</main>',1)[0])

    def test_anonymous_staff_and_client_cannot_access(self):
        self.assertEqual(self.client.get('/stock/financial').status_code,302)
        for user in (2,3):
            self.login(user)
            self.assertEqual(self.client.get('/stock/financial').status_code,302)
        self.login(2)
        self.assertNotIn('/stock/financial',self.client.get('/stock').get_data(as_text=True))

    def test_get_and_post_do_not_write_financial_data(self):
        self.login(1)
        with sqlite3.connect(self.path) as db:
            before = '\n'.join(db.iterdump())
        self.client.get('/stock/financial')
        response = self.client.post('/stock/financial',data={'stock':999})
        self.assertIn(response.status_code,(302,303,405))
        with sqlite3.connect(self.path) as db:
            self.assertEqual('\n'.join(db.iterdump()),before)

    def test_partial_and_empty_states_and_escaped_product_names(self):
        self.login(1)
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO products(name,category,stock,price_cents) VALUES('<script>alert(1)</script>','Bar',2,100)")
        page = self.client.get('/stock/financial').get_data(as_text=True)
        self.assertIn('Totais de custo e margem parciais',page)
        self.assertIn('Sem custo apurado',page)
        self.assertIn('&lt;script&gt;',page)
        self.assertNotIn('<script>alert(1)</script>',page)
        with sqlite3.connect(self.path) as db:
            db.execute('DELETE FROM restocks')
            db.execute('DELETE FROM products')
        self.assertIn('Nenhum produto do bar',self.client.get('/stock/financial').get_data(as_text=True))

    def test_existing_stock_and_planning_views_keep_manager_navigation(self):
        self.login(1)
        for path in ('/stock', '/stock/purchase-planning'):
            page = self.client.get(path)
            self.assertEqual(page.status_code,200)
            self.assertIn('/stock/financial',page.get_data(as_text=True))

    def test_stock_submenu_order_active_state_and_responsive_controls(self):
        import re
        self.login(1)
        expected = [('/stock','Controle de Estoque'),('/stock/conference','Conferência Mensal'),
                    ('/stock/purchase-planning','Planejamento de Compras'),('/stock/financial','Visão Financeira')]
        for path, label in expected:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code,200)
                page = response.get_data(as_text=True)
                submenu = page.split('id="sidebar-stock">',1)[1].split('</div>',1)[0]
                links = re.findall(r'<a class="([^"]*)" href="([^"]+)"([^>]*)>([^<]+)</a>',submenu)
                self.assertEqual([(link[1],link[3]) for link in links],expected)
                active = [link for link in links if link[0]=='active']
                self.assertEqual([(link[1],link[3]) for link in active],[(path,label)])
                self.assertIn('aria-current="page"',active[0][2])
                self.assertIn('class="collapse show" id="sidebar-stock"',page)
                self.assertIn('aria-expanded="true" aria-controls="sidebar-stock"',page)
                self.assertIn('data-bs-toggle="collapse" data-bs-target="#sidebar-stock"',page)
                self.assertIn('class="offcanvas-lg offcanvas-start app-sidebar"',page)
                self.assertIn('bootstrap.Offcanvas.getInstance(sidebar)?.hide()',page)

    def test_staff_stock_menu_and_permissions_are_preserved(self):
        self.login(2)
        for path in ('/stock','/stock/conference'):
            page = self.client.get(path)
            self.assertEqual(page.status_code,200)
            submenu = page.get_data(as_text=True).split('id="sidebar-stock">',1)[1].split('</div>',1)[0]
            self.assertIn('Controle de Estoque',submenu)
            self.assertIn('Conferência Mensal',submenu)
            self.assertNotIn('Planejamento de Compras',submenu)
            self.assertNotIn('Visão Financeira',submenu)
        for path in ('/stock/purchase-planning','/stock/financial'):
            self.assertEqual(self.client.get(path).status_code,302)

    def test_conference_shortcut_removed_only_from_stock_content(self):
        self.login(1)
        page = self.client.get('/stock').get_data(as_text=True)
        main = page.split('<main',1)[1].split('</main>',1)[0]
        self.assertNotIn('/stock/conference',main)
        self.assertIn('/stock/conference',page)
        self.assertIn('Registrar entrada',main)
        self.assertIn('Gerar relatório PDF',main)
