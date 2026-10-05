import unittest
from unittest.mock import patch

from app import app


class PixExpirationCronAuthTest(unittest.TestCase):
    def setUp(self):
        original = dict(app.config)
        def restore():
            app.config.clear()
            app.config.update(original)
        self.addCleanup(restore)
        app.config.update(TESTING=True, CRON_SECRET='controlled-cron-secret',
                          CRON_ENABLED=True, EXTERNAL_PAYMENTS_ENABLED=True,
                          MERCADOPAGO_ACCESS_TOKEN='mock-token',
                          PIX_ABANDONMENT_NOT_BEFORE='2026-10-05T00:00:00Z')
        self.client = app.test_client()
        # The real app middleware must bypass session/database login probes.
        self.login_db = patch('app.get_db', side_effect=AssertionError('Login intercepted cron'))
        self.login_db.start()
        self.addCleanup(self.login_db.stop)

    def test_missing_bearer_unauthorized(self):
        with patch('src.services.pix_checkout_abandonment.expire_recent_checkouts') as service:
            response = self.client.get('/cron/expire-pix-checkouts')
        self.assertEqual(response.status_code, 401)
        service.assert_not_called()

    def test_invalid_bearer_unauthorized(self):
        with patch('src.services.pix_checkout_abandonment.expire_recent_checkouts') as service:
            response = self.client.get('/cron/expire-pix-checkouts', headers={'Authorization':'Bearer wrong'})
        self.assertEqual(response.status_code, 401)
        service.assert_not_called()

    def test_valid_bearer_reaches_endpoint_without_login(self):
        database = object()
        with patch('src.routes.finance.get_db', return_value=database), patch(
                'src.services.pix_checkout_abandonment.expire_recent_checkouts',
                return_value=dict(processed=0,completed=0,pending=0,aborted=0)) as service:
            response = self.client.get('/cron/expire-pix-checkouts',
                                       headers={'Authorization':'Bearer controlled-cron-secret'})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('Location', response.headers)
        service.assert_called_once_with(database, 'mock-token', '2026-10-05T00:00:00Z')
