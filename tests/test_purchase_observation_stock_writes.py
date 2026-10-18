"""Reuse the real stock-write concurrency scenarios with capture enabled."""
import os
from pathlib import Path
import unittest

from tests import test_mercadopago as fixtures
from tests import test_stock_conferences_postgres as postgres_fixtures
from tests.stock_write_scenarios import StockWriteScenarios
from tests.test_purchase_observation import BASE
from src.db import connect_sqlite
from src.services.purchase_observation import activate_observation, process_observation


class ObservedWrites:
    def activate(self):
        db = self.open_db()
        try:
            activate_observation(db)
            while process_observation(db)['processed']:
                pass
        finally:
            db.close()

    def verify_state(self):
        db = self.open_db()
        try:
            for _ in range(100):
                if not process_observation(db)['processed']:
                    break
            observed = db.execute('SELECT stock_units FROM purchase_replenishment_state WHERE product_id=1').fetchone()[0]
            actual = db.execute('SELECT stock FROM products WHERE id=1').fetchone()[0]
            self.assertEqual(observed, actual)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM purchase_observation_events WHERE processed_at IS NULL').fetchone()[0], 0)
        finally:
            db.close()


class SQLiteObservedStockWritesTest(ObservedWrites, StockWriteScenarios, unittest.TestCase):
    create_sports_schema = fixtures.MercadoPagoFlowTest.create_sports_schema

    def setUp(self):
        fixtures.MercadoPagoFlowTest.setUp(self)
        self.activate()

    def open_db(self):
        return connect_sqlite(fixtures.app.config['DATABASE'])

    def tearDown(self):
        try:
            self.verify_state()
        finally:
            fixtures.MercadoPagoFlowTest.tearDown(self)


@unittest.skipUnless(os.environ.get('GPCTA_TEST_POSTGRES_SOCKET'), 'requires disposable local PostgreSQL socket')
class PostgresObservedStockWritesTest(ObservedWrites, StockWriteScenarios, unittest.TestCase):
    connect = postgres_fixtures.PostgresStockConferenceTest.connect
    open_db = postgres_fixtures.PostgresStockConferenceTest.connect
    cleanup = postgres_fixtures.PostgresStockConferenceTest.cleanup

    def setUp(self):
        postgres_fixtures.PostgresStockConferenceTest.setUp(self)
        db = self.open_db()
        try:
            with db.conn.cursor() as cursor:
                cursor.execute("ALTER TABLE products ADD COLUMN units_per_case INTEGER DEFAULT 0;"
                               "ALTER TABLE products ADD COLUMN created_at TEXT DEFAULT '2025-01-01';")
                cursor.execute(BASE[BASE.index('CREATE TABLE sales'):])
                cursor.execute(Path('supabase/migrations/20261018010000_purchase_observation.sql').read_text())
            db.commit()
        finally:
            db.close()
        self.activate()

    def tearDown(self):
        self.verify_state()


if __name__ == '__main__':
    unittest.main()
