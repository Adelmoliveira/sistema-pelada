import re
import unittest
from unittest.mock import patch
from tests import test_mercadopago as fixtures
from tests.stock_write_scenarios import StockWriteScenarios
from src.db import connect_sqlite

app = fixtures.app

class SQLiteStockWritesTest(StockWriteScenarios, unittest.TestCase):
    setUp = fixtures.MercadoPagoFlowTest.setUp
    tearDown = fixtures.MercadoPagoFlowTest.tearDown
    create_sports_schema = fixtures.MercadoPagoFlowTest.create_sports_schema

    def open_db(self):
        return connect_sqlite(app.config['DATABASE'])

    def test_edit_form_stale_revision_is_rejected(self):
        self.prepare_writers()
        with self.client.session_transaction() as session:
            session['user_id'] = self.user_id
        page = self.client.get('/products/1/edit').get_data(as_text=True)
        revision = re.search('name="stock_revision" value="(\\d+)"', page).group(1)
        db = self.open_db()
        from src.services.stock_conferences import apply_conference
        apply_conference(db,1,1)
        db.close()
        data = dict(name='Água',category='Cerveja',units_per_case='0',price='3,00',cost='1,00',stock='22',min_stock='5',stock_reason='Ajuste manual',stock_revision=revision)
        with patch('src.routes.products.notify_low_stock') as alert:
            response = self.client.post('/products/1/edit', data=data)
            self.assertIn('mudou desde',response.get_data(as_text=True))
            alert.assert_not_called()
        self.assertEqual(self.read('SELECT stock FROM products WHERE id=1'), [(17,)])
        revision = self.read('SELECT stock_revision FROM products WHERE id=1')[0][0]
        data.update(stock='19',stock_revision=revision)
        with patch('src.routes.products.notify_low_stock'):
            self.assertEqual(self.client.post('/products/1/edit',data=data).status_code,302)
        self.assertEqual(self.read('SELECT stock FROM products WHERE id=1'), [(19,)])
