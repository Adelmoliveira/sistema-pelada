"""Exercise existing real HTTP flows with observation capture activated."""
import unittest

from tests import test_mercadopago as fixtures
from tests import test_stock_conferences as conference_fixtures
from src.db import connect_sqlite
from src.services.purchase_observation import activate_observation, process_observation

app = fixtures.app


class ObservationRoutesTest(unittest.TestCase):
    tearDown = fixtures.MercadoPagoFlowTest.tearDown
    create_sports_schema = fixtures.MercadoPagoFlowTest.create_sports_schema
    create_order = fixtures.MercadoPagoFlowTest.create_order
    headers = fixtures.MercadoPagoFlowTest.headers
    login_manager = fixtures.MercadoPagoFlowTest.login_manager

    def setUp(self):
        fixtures.MercadoPagoFlowTest.setUp(self)
        db = connect_sqlite(app.config['DATABASE'])
        try:
            activate_observation(db)
            process_observation(db)
        finally:
            db.close()

    def assert_capture(self):
        db = connect_sqlite(app.config['DATABASE'])
        try:
            self.assertGreater(db.execute('SELECT COUNT(*) FROM purchase_observation_events WHERE processed_at IS NULL').fetchone()[0], 0)
            for _ in range(20):
                if not process_observation(db)['processed']:
                    break
            self.assertEqual(db.execute('SELECT COUNT(*) FROM purchase_replenishment_episodes WHERE notifiable<>0').fetchone()[0], 0)
        finally:
            db.close()

    def test_cash_sale_capture_preserves_existing_flow(self):
        fixtures.MercadoPagoFlowTest.test_bar_sale_still_uses_products_stock(self)
        self.assert_capture()

    def test_pix_and_delivery_capture_preserve_existing_flow(self):
        fixtures.MercadoPagoFlowTest.test_paid_pix_enters_delivery_queue_and_staff_confirms_it(self)
        self.assert_capture()

    def test_restoration_capture_preserves_existing_flow_and_idempotency(self):
        fixtures.MercadoPagoFlowTest.test_manager_restores_accidental_full_delivery_without_changing_payment_or_stock(self)
        self.assert_capture()

    def test_recovery_endpoint_auth_no_ui_session_and_csrf_enabled(self):
        app.config['WTF_CSRF_ENABLED'] = True
        self.assertEqual(self.client.get('/cron/process-purchase-observation').status_code, 401)
        self.assertEqual(self.client.get('/cron/process-purchase-observation', headers={'Authorization':'Bearer wrong'}).status_code, 401)
        response = self.client.get('/cron/process-purchase-observation', headers={'Authorization':'Bearer cron-secret-test'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['processed'], 0)


class ObservationConferenceRoutesTest(unittest.TestCase):
    setUp = ObservationRoutesTest.setUp
    tearDown = fixtures.MercadoPagoFlowTest.tearDown
    create_sports_schema = fixtures.MercadoPagoFlowTest.create_sports_schema
    conference_snapshot = fixtures.MercadoPagoFlowTest.conference_snapshot
    login = conference_fixtures.StockConferenceTest.login
    execute = conference_fixtures.StockConferenceTest.execute
    register = conference_fixtures.StockConferenceTest.register
    prepare = conference_fixtures.StockConferenceTest.prepare
    cid = conference_fixtures.StockConferenceTest.cid
    apply = conference_fixtures.StockConferenceTest.apply
    assert_capture = ObservationRoutesTest.assert_capture

    def test_conference_capture_preserves_versioning_and_application_idempotency(self):
        conference_fixtures.StockConferenceTest.test_negative_positive_zero_and_repeated_application_persist(self)
        self.assert_capture()


if __name__ == '__main__':
    unittest.main()
