import hashlib
import hmac
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app
from src.db import get_db
from src.routes.sales import pix_access_token
from src.services.mercadopago import validate_webhook_signature
from src.services.mercadopago import MercadoPagoError


class MercadoPagoFlowTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True,
            DATABASE=str(Path(self.tempdir.name) / "test.db"),
            DATABASE_URL=None,
            WTF_CSRF_ENABLED=False,
            SECRET_KEY="test-secret",
            MERCADOPAGO_ACCESS_TOKEN="APP_USR-test",
            MERCADOPAGO_POS_ID="CAIXA_TESTE",
            MERCADOPAGO_WEBHOOK_SECRET="webhook-secret",
        )
        with app.app_context():
            db = get_db()
            db.execute("INSERT INTO users(username,name,password_hash,role) VALUES(?,?,?,'manager')", ("teste", "Teste", "hash"))
            db.execute("INSERT INTO players(name) VALUES(?)", ("Peladeiro",))
            db.execute(
                "INSERT INTO products(name,category,price_cents,cost_cents,stock) VALUES(?,?,?,?,?)",
                ("Água", "Bebida", 300, 100, 5),
            )
            db.commit()
            user = db.execute("SELECT * FROM users WHERE username='teste'").fetchone()
            self.token = pix_access_token(user)
            self.player_id = db.execute("SELECT id FROM players WHERE name='Peladeiro'").fetchone()["id"]
            self.product_id = db.execute("SELECT id FROM products WHERE name='Água'").fetchone()["id"]
        self.client = app.test_client()

    def tearDown(self):
        self.tempdir.cleanup()

    def headers(self):
        return {"Accept": "application/json", "X-Pix-Token": self.token}

    def create_order(self, order_id, quantity):
        response_data = {
            "id": order_id,
            "status": "created",
            "type_response": {"qr_data": "000201010212TESTE6304ABCD"},
            "transactions": {"payments": [{"id": f"PAY-{order_id}"}]},
        }
        with patch("src.routes.sales.create_qr_order", return_value=response_data):
            response = self.client.post(
                "/pix/mercadopago/orders",
                headers=self.headers(),
                json={
                    "player_id": self.player_id,
                    "items": [{"product_id": self.product_id, "quantity": quantity}],
                },
            )
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["sale_id"]

    def test_payment_approval_and_expiration_are_idempotent(self):
        sale_id = self.create_order("ORD-APPROVED", 2)
        with app.app_context():
            db = get_db()
            self.assertEqual(db.execute("SELECT stock FROM products WHERE id=?", (self.product_id,)).fetchone()["stock"], 3)
            sale = db.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
            self.assertEqual((sale["paid"], sale["payment_status"]), (0, "pending"))

        approved = {
            "id": "ORD-APPROVED",
            "status": "processed",
            "status_detail": "accredited",
            "total_paid_amount": "6.00",
            "transactions": {"payments": [{"id": "PAY-APPROVED"}]},
        }
        with patch("src.routes.sales.get_order", return_value=approved):
            first = self.client.get(f"/pix/mercadopago/orders/{sale_id}/status", headers=self.headers())
            second = self.client.get(f"/pix/mercadopago/orders/{sale_id}/status", headers=self.headers())
        self.assertTrue(first.get_json()["paid"])
        self.assertTrue(second.get_json()["paid"])
        with app.app_context():
            db = get_db()
            self.assertEqual(db.execute("SELECT stock FROM products WHERE id=?", (self.product_id,)).fetchone()["stock"], 3)

        expired_sale_id = self.create_order("ORD-EXPIRED", 1)
        expired = {"id": "ORD-EXPIRED", "status": "expired", "status_detail": "expired", "total_amount": "3.00"}
        with patch("src.routes.sales.get_order", return_value=expired):
            self.client.get(f"/pix/mercadopago/orders/{expired_sale_id}/status", headers=self.headers())
            self.client.get(f"/pix/mercadopago/orders/{expired_sale_id}/status", headers=self.headers())
        with app.app_context():
            db = get_db()
            self.assertEqual(db.execute("SELECT stock FROM products WHERE id=?", (self.product_id,)).fetchone()["stock"], 3)
            sale = db.execute("SELECT * FROM sales WHERE id=?", (expired_sale_id,)).fetchone()
            self.assertEqual(sale["payment_status"], "expired")

    def test_webhook_signature(self):
        data_id = "ORDABC123"
        request_id = "request-123"
        timestamp = "1742505638683"
        template = f"id:{data_id.lower()};request-id:{request_id};ts:{timestamp};"
        signature = hmac.new(b"webhook-secret", template.encode(), hashlib.sha256).hexdigest()
        header = f"ts={timestamp},v1={signature}"
        self.assertTrue(validate_webhook_signature(header, request_id, data_id, "webhook-secret"))
        self.assertFalse(validate_webhook_signature(header, request_id, data_id, "wrong-secret"))

    def test_legacy_pix_remains_available_until_credentials_are_configured(self):
        app.config.update(
            MERCADOPAGO_ACCESS_TOKEN=None,
            MERCADOPAGO_POS_ID=None,
            MERCADOPAGO_WEBHOOK_SECRET=None,
        )
        response = self.client.get(
            "/pix/qrcode?amount_cents=300",
            headers={"Accept": "application/json", "X-Pix-Token": self.token},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["image"].startswith("data:image/png;base64,"))

    def test_webhook_approves_order_and_api_failure_restores_stock(self):
        sale_id = self.create_order("ORD-WEBHOOK", 1)
        data_id = "ORD-WEBHOOK"
        request_id = "request-webhook"
        timestamp = "1742505638683"
        template = f"id:{data_id.lower()};request-id:{request_id};ts:{timestamp};"
        signature = hmac.new(b"webhook-secret", template.encode(), hashlib.sha256).hexdigest()
        approved = {
            "id": data_id,
            "external_reference": None,
            "status": "processed",
            "status_detail": "accredited",
            "total_paid_amount": "3.00",
            "transactions": {"payments": [{"id": "PAY-WEBHOOK"}]},
        }
        with patch("src.routes.sales.get_order") as get_order_mock:
            response = self.client.post(
                f"/webhooks/mercadopago?data.id={data_id}&type=order",
                headers={"X-Request-Id": request_id, "X-Signature": f"ts={timestamp},v1={signature}"},
                json={"type": "order", "data": approved},
            )
        self.assertEqual(response.status_code, 200)
        get_order_mock.assert_not_called()
        with app.app_context():
            db = get_db()
            sale = db.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
            self.assertEqual((sale["paid"], sale["payment_status"]), (1, "approved"))

        with patch("src.routes.sales.create_qr_order", side_effect=MercadoPagoError("falha simulada")):
            failed = self.client.post(
                "/pix/mercadopago/orders",
                headers=self.headers(),
                json={
                    "player_id": self.player_id,
                    "items": [{"product_id": self.product_id, "quantity": 1}],
                },
            )
        self.assertEqual(failed.status_code, 502)
        with app.app_context():
            db = get_db()
            self.assertEqual(db.execute("SELECT stock FROM products WHERE id=?", (self.product_id,)).fetchone()["stock"], 4)

    def test_webhook_simulator_acknowledges_unknown_order(self):
        data_id = "123456"
        request_id = "request-simulator"
        timestamp = "1742505638683"
        template = f"id:{data_id};request-id:{request_id};ts:{timestamp};"
        signature = hmac.new(b"webhook-secret", template.encode(), hashlib.sha256).hexdigest()
        payload = {
            "action": "order.processed",
            "type": "order",
            "data": {
                "id": data_id,
                "external_reference": "ext_ref_1234",
                "status": "processed",
                "status_detail": "accredited",
                "total_paid_amount": 100000,
                "type": "point",
            },
        }
        with (
            patch("src.routes.sales.get_order") as get_order_mock,
            patch("src.routes.sales.get_db") as get_db_mock,
        ):
            response = self.client.post(
                f"/webhooks/mercadopago?data.id={data_id}&type=order",
                headers={"X-Request-Id": request_id, "X-Signature": f"ts={timestamp},v1={signature}"},
                json=payload,
            )
        self.assertEqual(response.status_code, 200)
        get_order_mock.assert_not_called()
        get_db_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
