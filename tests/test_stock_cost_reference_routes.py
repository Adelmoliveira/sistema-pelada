"""Manager confirmation, CSRF, escaping and finance UI regression."""
import re
import sqlite3
import unittest
from unittest.mock import patch

from tests import test_stock_financial as fixtures
from src.cost_reference_schema import schema_sql


class CostReferenceRoutesTest(unittest.TestCase):
    login = fixtures.StockFinancialPageTest.login

    def setUp(self):
        fixtures.StockFinancialPageTest.setUp(self)
        with sqlite3.connect(self.path) as db:
            db.executescript(schema_sql())
            db.execute('DELETE FROM restocks')
        self.url='/stock/financial/1/regularize'

    def fields(self):
        self.login(1)
        response=self.client.get(self.url)
        self.assertEqual(response.status_code,200)
        page=response.get_data(as_text=True)
        match=re.search(r'name="snapshot" value="([^"]+)"',page)
        snapshot=match.group(1) if match else ''
        data=dict(snapshot=snapshot,unit_cost='9,00',value_origin='catalog_confirmed',
                  justification='Confirmado com nota de suporte',confirmed='1')
        csrf=re.search(r'name="csrf_token" value="([^"]+)"',page)
        if csrf:
            data['csrf_token']=csrf.group(1)
        return data,page

    def count(self):
        with sqlite3.connect(self.path) as db:
            return db.execute('SELECT COUNT(*) FROM stock_cost_references').fetchone()[0]

    def test_manager_suggestion_confirmation_history_and_estimated_totals(self):
        data,page=self.fields()
        for text in ('value="9,00"','Justificativa obrigatória','Documento de suporte',
                     'Histórico imutável','name="confirmed"','Nenhuma regularização',
                     'Sem histórico de aquisição individual','aria-current="page"'):
            self.assertIn(text,page)
        self.assertNotIn('name="confirmed" value="1" required checked',page)
        with sqlite3.connect(self.path) as db:
            before={t:db.execute('SELECT * FROM '+t).fetchall() for t in
                    ('products','restocks','stock_adjustments','cash_movements','sales')}
        self.assertEqual(self.client.post(self.url,data=data).status_code,303)
        page=self.client.get('/stock/financial').get_data(as_text=True)
        for text in ('Referência regularizada pelo gestor','Valor com custo histórico',
                     'Valor com custo regularizado','Cobertura','R$ 90,00','Estimativa, não valor contábil'):
            self.assertIn(text,page)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(before,{t:db.execute('SELECT * FROM '+t).fetchall() for t in before})
        data,page=self.fields()
        self.assertIn('Referência vigente',page)
        data.update(unit_cost='8,00',value_origin='manager_entered',support_document='<script>bad()</script>')
        self.assertEqual(self.client.post(self.url,data=data).status_code,303)
        _,page=self.fields()
        self.assertEqual(self.count(),2)
        self.assertIn('&lt;script&gt;bad()',page)
        self.assertNotIn('<script>bad()',page)
        self.assertIn('R$ 9,00',page)
        self.assertIn('R$ 8,00',page)

    def test_explicit_confirmation_justification_origin_and_signed_snapshot(self):
        data,_=self.fields()
        invalids=({'confirmed':''},{'justification':' '},{'unit_cost':'0'},{'value_origin':'other'},
                  {'unit_cost':'8,00'},{'snapshot':'tampered'})
        for changes in invalids:
            with self.subTest(changes=changes):
                self.assertEqual(self.client.post(self.url,data=dict(data,**changes)).status_code,409)
        self.assertEqual(self.count(),0)

    def test_csrf_and_authorization_enforced(self):
        self.app.config['WTF_CSRF_ENABLED']=True
        data,_=self.fields()
        token=data.pop('csrf_token')
        # The application's existing CSRF handler redirects rather than 400.
        self.assertEqual(self.client.post(self.url,data=data).status_code,303)
        self.assertEqual(self.count(),0)
        data['csrf_token']=token
        self.assertEqual(self.client.post(self.url,data=data).status_code,303)
        self.app.config['WTF_CSRF_ENABLED']=False
        for user in (2,3):
            self.login(user)
            self.assertEqual(self.client.get(self.url,headers={'Accept':'application/json'}).status_code,403)
            self.assertEqual(self.client.post(self.url,data=data,headers={'Accept':'application/json'}).status_code,403)
        self.assertEqual(self.count(),1)

    def test_stale_stock_round_trip_duplicate_and_expired_form(self):
        data,_=self.fields()
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE products SET stock=9 WHERE id=1')
            db.execute('UPDATE products SET stock=10 WHERE id=1')
        response=self.client.post(self.url,data=data)
        self.assertEqual(response.status_code,409)
        self.assertIn('mudou',response.get_data(as_text=True))
        self.assertIn(data['snapshot'],response.get_data(as_text=True))
        data,_=self.fields()
        from itsdangerous import SignatureExpired
        with patch('src.routes.products.URLSafeTimedSerializer') as signer:
            signer.return_value.loads.side_effect=SignatureExpired('Expired')
            self.assertEqual(self.client.post(self.url,data=data).status_code,409)
        self.assertEqual(self.client.post(self.url,data=data).status_code,303)
        self.assertEqual(self.client.post(self.url,data=data).status_code,409)
        self.assertEqual(self.count(),1)

    def test_correctable_purchase_links_existing_flow_and_blocks_override(self):
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO restocks(product_id,quantity,unit_cost_cents) VALUES(1,10,0)')
            rid=db.execute('SELECT MAX(id) FROM restocks').fetchone()[0]
        data,page=self.fields()
        self.assertIn('Corrigir o histórico primeiro',page)
        self.assertIn(f'/stock/restocks/{rid}/correct',page)
        self.assertNotIn('name="confirmed"',page)
        # Obtain a signed snapshot explicitly: no form is exposed for this case.
        from itsdangerous import URLSafeTimedSerializer
        from src.db import connect_sqlite
        from src.services.stock_cost_references import reference_snapshot
        from src.services.stock_financial import stock_financial_data
        db=connect_sqlite(self.path)
        try:
            snapshot=reference_snapshot(stock_financial_data(db,1)['products'][0])
        finally:
            db.close()
        data['snapshot']=URLSafeTimedSerializer(self.app.secret_key,salt='stock-cost-reference').dumps(snapshot)
        self.assertEqual(self.client.post(self.url,data=data).status_code,409)
        self.assertEqual(self.count(),0)

    def test_inactive_missing_sports_and_revoked_manager_are_blocked(self):
        data,_=self.fields()
        self.assertEqual(self.client.post('/stock/financial/999/regularize',data=data).status_code,404)
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE products SET active=0 WHERE id=1')
        self.assertEqual(self.client.post(self.url,data=data).status_code,404)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE products SET active=1,category='Material Esportivo' WHERE id=1")
        self.assertEqual(self.client.post(self.url,data=data).status_code,404)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE products SET category='Bar' WHERE id=1")
            db.execute("UPDATE users SET role='staff' WHERE id=1")
        self.assertEqual(self.client.post(self.url,data=data,headers={'Accept':'application/json'}).status_code,403)
        self.assertEqual(self.count(),0)

    def test_anonymous_cannot_read_or_submit_reference(self):
        self.assertEqual(self.client.get(self.url).status_code,302)
        self.assertEqual(self.client.post(self.url,data={'confirmed':'1'}).status_code,302)
        self.assertEqual(self.count(),0)


if __name__=='__main__':
    unittest.main()
