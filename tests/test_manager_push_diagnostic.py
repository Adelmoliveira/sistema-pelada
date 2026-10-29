"""Unit tests for manager push diagnostic. No real push requests."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.services.manager_push_diagnostic import (
    send_diagnostic,
    reserve_diagnostic,
)


class DiagnosticTransportTests(unittest.TestCase):

    def setUp(self):
        self.subscription = {
            "endpoint": "https://fcm.googleapis.com/fcm/send/test",
            "p256dh": "test",
            "auth": "test",
        }

    @patch(
        "src.services.manager_push_diagnostic.vapid_configuration_error",
        return_value="vapid_key_mismatch",
    )
    def test_invalid_vapid_blocks_send(self, _mock):
        result = send_diagnostic(self.subscription)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["code"], "vapid_key_mismatch")

    @patch(
        "src.services.manager_push_diagnostic.vapid_configuration_error",
        return_value=None,
    )
    @patch(
        "src.services.manager_push_diagnostic.validate_subscription",
        side_effect=ValueError("invalid"),
    )
    def test_invalid_subscription_blocks_send(self, _validate, _vapid):
        result = send_diagnostic(self.subscription)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["code"], "invalid_subscription")

    @patch(
        "src.services.manager_push_diagnostic.vapid_configuration_error",
        return_value=None,
    )
    @patch(
        "src.services.manager_push_diagnostic.validate_subscription",
        return_value=("https://fcm.googleapis.com/test", "key", "auth"),
    )
    @patch("pywebpush.webpush")
    def test_provider_acceptance(self, mock_push, _validate, _vapid):
        mock_push.return_value = SimpleNamespace(status_code=201)

        with patch.dict(
            "os.environ",
            {"VAPID_PRIVATE_KEY": "fake-test-key"},
        ):
            result = send_diagnostic(self.subscription)

        self.assertEqual(result["code"], "provider_accepted")
        self.assertTrue(result["accepted"])
        mock_push.assert_called_once()

    @patch(
        "src.services.manager_push_diagnostic.vapid_configuration_error",
        return_value=None,
    )
    @patch(
        "src.services.manager_push_diagnostic.validate_subscription",
        return_value=("https://fcm.googleapis.com/test", "key", "auth"),
    )
    @patch("pywebpush.webpush")
    def test_http_400_is_not_accepted(self, mock_push, _validate, _vapid):
        mock_push.return_value = SimpleNamespace(status_code=400)

        with patch.dict(
            "os.environ",
            {"VAPID_PRIVATE_KEY": "fake-test-key"},
        ):
            result = send_diagnostic(self.subscription)

        self.assertFalse(result["accepted"])
        self.assertEqual(result["code"], "webpush_http_400")
        mock_push.assert_called_once()


class DiagnosticReservationTests(unittest.TestCase):

    def test_sqlite_is_rejected(self):
        db = SimpleNamespace(is_postgres=False)

        with self.assertRaises(RuntimeError):
            reserve_diagnostic(db, 1)


class DiagnosticRouteSecurityTests(unittest.TestCase):
    """Exercise the real Flask route with a disposable test database."""

    def setUp(self):
        from tests import test_mercadopago as fixtures
        self.fixture = fixtures.MercadoPagoFlowTest(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)

        from app import app
        self.app = app
        self.client = self.fixture.client
        self.app.config["WTF_CSRF_ENABLED"] = True

    def test_anonymous_request_does_not_send(self):
        with patch(
            "src.services.manager_push_diagnostic.send_diagnostic"
        ) as sender:
            response = self.client.post(
                "/stock/purchase-monitoring/test-push"
            )

        self.assertIn(response.status_code, (302, 303, 401, 403))
        sender.assert_not_called()

    def test_manager_without_csrf_does_not_send(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.fixture.user_id

        with patch(
            "src.services.manager_push_diagnostic.send_diagnostic"
        ) as sender:
            response = self.client.post(
                "/stock/purchase-monitoring/test-push"
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers.get("Location"),
            "/login",
        )
        sender.assert_not_called()


    def test_authorized_manager_sends_once(self):
        from flask_wtf.csrf import generate_csrf

        with self.client.session_transaction() as session:
            session["user_id"] = self.fixture.user_id
            session["manager_push_binding"] = "binding-test"

        with self.app.test_request_context():
            pass

        with self.client.session_transaction() as session:
            session["csrf_token"] = "diagnostic-test-token"

        from itsdangerous import URLSafeTimedSerializer

        serializer = URLSafeTimedSerializer(
            self.app.secret_key,
            salt="wtf-csrf-token",
        )
        token = serializer.dumps("diagnostic-test-token")

        subscription = {
            "id": 1,
            "endpoint": "https://push.example.test/subscription",
            "p256dh": "test-key",
            "auth": "test-auth",
        }

        with (
            patch(
                "src.services.manager_push_diagnostic.find_current_subscription",
                return_value=subscription,
            ) as finder,
            patch(
                "src.services.manager_push_diagnostic.reserve_diagnostic",
                return_value=True,
            ) as reserve,
            patch(
                "src.services.manager_push.vapid_configuration_error",
                return_value=None,
            ),
            patch(
                "src.services.manager_push_diagnostic.send_diagnostic",
                return_value={"accepted": True, "code": "provider_accepted"},
            ) as sender,
        ):
            response = self.client.post(
                "/stock/purchase-monitoring/test-push",
                data={"csrf_token": token},
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(finder.call_count, 2)
        reserve.assert_called_once()
        sender.assert_called_once_with(subscription)

class DiagnosticOwnershipTests(unittest.TestCase):
    """Verify manager, session and device ownership without sending push."""

    def setUp(self):
        import hashlib
        import sqlite3

        from src.db import DbWrapper
        from src.services.manager_push_diagnostic import (
            find_current_subscription,
        )

        self.find = find_current_subscription
        self.binding = "binding-test"
        self.device = "a" * 64
        self.user_id = 1

        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row

        self.connection.executescript("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                role TEXT NOT NULL,
                active INTEGER NOT NULL
            );

            CREATE TABLE manager_push_subscriptions (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                endpoint TEXT NOT NULL,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                binding_hash TEXT NOT NULL,
                device_hash TEXT NOT NULL,
                active INTEGER NOT NULL,
                revoked_at TEXT
            );

            CREATE TABLE manager_push_revocations (
                binding_hash TEXT PRIMARY KEY
            );

            CREATE TABLE push_subscriptions (
                endpoint TEXT PRIMARY KEY
            );

            INSERT INTO users VALUES (1, 'manager', 1);
        """)

        binding_hash = hashlib.sha256(
            self.binding.encode()
        ).hexdigest()

        device_hash = hashlib.sha256(
            self.device.encode()
        ).hexdigest()

        self.connection.execute(
            """
            INSERT INTO manager_push_subscriptions
            (id,user_id,endpoint,p256dh,auth,binding_hash,
             device_hash,active,revoked_at)
            VALUES (1,1,'https://fcm.googleapis.com/test',
                    'key','auth',?,?,1,NULL)
            """,
            (binding_hash, device_hash),
        )

        self.db = DbWrapper(self.connection, is_postgres=False)
        self.addCleanup(self.connection.close)

    def test_current_device_is_accepted(self):
        result = self.find(
            self.db, self.user_id, self.binding, self.device
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], 1)

    def test_different_device_is_rejected(self):
        result = self.find(
            self.db, self.user_id, self.binding, "other-device"
        )
        self.assertIsNone(result)

    def test_revoked_binding_is_rejected(self):
        import hashlib

        digest = hashlib.sha256(
            self.binding.encode()
        ).hexdigest()

        self.connection.execute(
            "INSERT INTO manager_push_revocations VALUES (?)",
            (digest,),
        )

        result = self.find(
            self.db, self.user_id, self.binding, self.device
        )
        self.assertIsNone(result)

    def test_user_without_manager_role_is_rejected(self):
        self.connection.execute(
            "UPDATE users SET role='staff' WHERE id=1"
        )

        result = self.find(
            self.db, self.user_id, self.binding, self.device
        )
        self.assertIsNone(result)

if __name__ == "__main__":
    unittest.main()
