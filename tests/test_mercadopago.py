import hashlib
import hmac
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from app import app
from src.db import get_db
from src.routes.sales import pix_access_token
from src.services.mercadopago import validate_webhook_signature
from src.services.mercadopago import MercadoPagoError
from src.services.mercadopago import create_pix_order
from src.services.email_reminders import dispatch_reminders, get_reminder_settings, outstanding_players
from src.utils import alphabetical_key, brdate, local_today, month_bounds


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
            GMAIL_SMTP_USER="diretoriagpcta@gmail.com",
            GMAIL_APP_PASSWORD="app-password-test",
            CRON_SECRET="cron-secret-test",
        )
        with app.app_context():
            db = get_db()
            db.execute("INSERT INTO users(username,name,password_hash,role) VALUES(?,?,?,'manager')", ("teste", "Teste", "hash"))
            db.execute("INSERT INTO players(name,email) VALUES(?,?)", ("Peladeiro", "peladeiro@example.com"))
            db.execute(
                "INSERT INTO products(name,category,price_cents,cost_cents,stock) VALUES(?,?,?,?,?)",
                ("Água", "Bebida", 300, 100, 5),
            )
            db.commit()
            user = db.execute("SELECT * FROM users WHERE username='teste'").fetchone()
            self.user_id = user["id"]
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
            "status": "action_required",
            "transactions": {"payments": [{
                "id": f"PAY-{order_id}",
                "payment_method": {
                    "id": "pix",
                    "type": "bank_transfer",
                    "qr_code": "000201010212TESTE6304ABCD",
                },
            }]},
        }
        with patch("src.routes.sales.create_pix_order", return_value=response_data):
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

    @patch("src.services.mercadopago._request")
    def test_pix_order_uses_interoperable_bank_transfer(self, request_mock):
        request_mock.return_value = {"id": "ORD-PIX"}
        create_pix_order("token", "pelada_ref", 300, "key", "peladeiro@example.com")
        method, path, token, payload, idempotency_key = request_mock.call_args.args
        payment = payload["transactions"]["payments"][0]
        self.assertEqual((method, path, token, idempotency_key), ("POST", "/v1/orders", "token", "key"))
        self.assertEqual(payload["type"], "online")
        self.assertEqual(payload["processing_mode"], "automatic")
        self.assertEqual(payment["payment_method"], {"id": "pix", "type": "bank_transfer"})
        self.assertEqual(payload["payer"]["email"], "peladeiro@example.com")

    def test_pix_requires_player_email_before_reserving_stock(self):
        with app.app_context():
            db = get_db()
            db.execute("UPDATE players SET email='' WHERE id=?", (self.player_id,))
            db.commit()
        response = self.client.post(
            "/pix/mercadopago/orders",
            headers=self.headers(),
            json={
                "player_id": self.player_id,
                "items": [{"product_id": self.product_id, "quantity": 1}],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("e-mail", response.get_json()["error"])
        with app.app_context():
            db = get_db()
            self.assertEqual(db.execute("SELECT stock FROM products WHERE id=?", (self.product_id,)).fetchone()["stock"], 5)
            self.assertEqual(db.execute("SELECT COUNT(*) AS total FROM sales").fetchone()["total"], 0)

    def test_dates_use_sao_paulo_business_timezone(self):
        self.assertEqual(brdate(datetime(2026, 7, 14, 0, 30)), "13/07/2026 21:30")
        month, start, end = month_bounds("2026-07")
        self.assertEqual((month, start, end), ("2026-07", "2026-07-01 03:00:00", "2026-08-01 03:00:00"))
        with app.app_context():
            local_day = get_db().execute("SELECT date(?)", ("2026-07-14 00:30:00",)).fetchone()[0]
        self.assertEqual(local_day, "2026-07-13")

    def test_player_names_sort_ignoring_case_and_accents(self):
        names = ["Zeca", "áureo", "Ana", "Álvaro", "bruno"]
        self.assertEqual(
            sorted(names, key=alphabetical_key),
            ["Álvaro", "Ana", "áureo", "bruno", "Zeca"],
        )

    def test_players_page_sorts_by_displayed_name_after_import(self):
        with app.app_context():
            db = get_db()
            db.execute("INSERT INTO players(name,war_name,email) VALUES(?,?,?)", ("Zeca", "", "zeca@example.com"))
            db.execute("INSERT INTO players(name,war_name,email) VALUES(?,?,?)", ("Ana", "Bia", "bia@example.com"))
            db.execute("INSERT INTO players(name,war_name,email) VALUES(?,?,?)", ("Álvaro", "", "alvaro@example.com"))
            db.commit()
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        page = self.client.get("/players").get_data(as_text=True)
        self.assertLess(page.index("<strong>Álvaro</strong>"), page.index("<strong>Bia</strong>"))
        self.assertLess(page.index("<strong>Bia</strong>"), page.index("<strong>Peladeiro</strong>"))
        self.assertLess(page.index("<strong>Peladeiro</strong>"), page.index("<strong>Zeca</strong>"))
        urgent_page = self.client.get("/urgent").get_data(as_text=True)
        self.assertLess(urgent_page.index("<td>Álvaro</td>"), urgent_page.index("<td>Ana</td>"))
        self.assertLess(urgent_page.index("<td>Ana</td>"), urgent_page.index("<td>Peladeiro</td>"))
        self.assertLess(urgent_page.index("<td>Peladeiro</td>"), urgent_page.index("<td>Zeca</td>"))

    def test_manager_can_edit_user_display_name_and_username(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        response = self.client.post(
            f"/users/{self.user_id}/edit",
            data={"name": "Ana", "username": "ana.staff"},
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            user = get_db().execute("SELECT * FROM users WHERE id=?", (self.user_id,)).fetchone()
            self.assertEqual((user["name"], user["username"], user["role"]), ("Ana", "ana.staff", "manager"))

    def test_reminders_calculate_debt_render_and_prevent_duplicate_email(self):
        sent_messages = []

        def fake_send(sender, password, recipient, subject, body):
            sent_messages.append((sender, recipient, subject, body))

        with app.app_context():
            db = get_db()
            settings = get_reminder_settings(db)
            debtors = outstanding_players(db, date(2026, 7, 5))
            self.assertEqual(debtors[0]["amount_cents"], 10500)
            first = dispatch_reminders(
                db, settings, "diretoriagpcta@gmail.com", "test", date(2026, 7, 5), fake_send
            )
            second = dispatch_reminders(
                db, settings, "diretoriagpcta@gmail.com", "test", date(2026, 7, 5), fake_send
            )
            self.assertEqual(first, {"sent": 1, "failed": 0, "skipped": 0, "without_email": 0})
            self.assertEqual(second, {"sent": 0, "failed": 0, "skipped": 1, "without_email": 0})
            self.assertEqual(len(sent_messages), 1)
            self.assertIn("Peladeiro", sent_messages[0][3])
            self.assertIn("R$ 105,00", sent_messages[0][3])

    def test_manager_edits_reminder_and_cron_requires_secret(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        page = self.client.get("/finance/reminders")
        self.assertEqual(page.status_code, 200)
        response = self.client.post(
            "/finance/reminders/settings",
            data={
                "enabled": "1",
                "schedule_day": str(local_today().day),
                "subject": "Cobrança para {{ nome }}",
                "body": "Total: {{ total }}",
            },
        )
        self.assertEqual(response.status_code, 302)
        unauthorized = self.client.get("/cron/payment-reminders")
        self.assertEqual(unauthorized.status_code, 401)
        with patch("src.routes.finance.dispatch_reminders", return_value={
            "sent": 1, "failed": 0, "skipped": 0, "without_email": 0,
        }) as dispatch_mock:
            authorized = self.client.get(
                "/cron/payment-reminders", headers={"Authorization": "Bearer cron-secret-test"}
            )
        self.assertEqual(authorized.status_code, 200)
        dispatch_mock.assert_called_once()

    def test_manager_downloads_debtors_pdf(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        response = self.client.get("/finance/reminders/debtors.pdf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertTrue(response.data.startswith(b"%PDF-"))
        self.assertIn("attachment", response.headers["Content-Disposition"])

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

        with patch("src.routes.sales.create_pix_order", side_effect=MercadoPagoError("falha simulada")):
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

    def test_paid_pix_enters_delivery_queue_and_staff_confirms_it(self):
        sale_id = self.create_order("ORD-DELIVERY", 2)
        data_id = "ORD-DELIVERY"
        request_id = "request-delivery"
        timestamp = "1742505638683"
        template = f"id:{data_id.lower()};request-id:{request_id};ts:{timestamp};"
        signature = hmac.new(b"webhook-secret", template.encode(), hashlib.sha256).hexdigest()
        approved = {
            "id": data_id,
            "type": "online",
            "status": "processed",
            "status_detail": "accredited",
            "total_paid_amount": "6.00",
            "transactions": {"payments": [{"id": "PAY-DELIVERY"}]},
        }
        response = self.client.post(
            f"/webhooks/mercadopago?data.id={data_id}&type=order",
            headers={"X-Request-Id": request_id, "X-Signature": f"ts={timestamp},v1={signature}"},
            json={"type": "order", "data": approved},
        )
        self.assertEqual(response.status_code, 200)

        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        page = self.client.get("/orders")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Pedidos para entregar", page.get_data(as_text=True))
        feed = self.client.get("/orders/feed", headers={"Accept": "application/json"})
        self.assertEqual(feed.status_code, 200)
        pending = feed.get_json()["pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual((pending[0]["id"], pending[0]["items"][0]["quantity"]), (sale_id, 2))

        delivered = self.client.post(f"/orders/{sale_id}/deliver", headers={"Accept": "application/json"})
        self.assertEqual(delivered.status_code, 200)
        feed = self.client.get("/orders/feed", headers={"Accept": "application/json"}).get_json()
        self.assertEqual(feed["pending"], [])
        self.assertEqual(feed["delivered"][0]["delivered_by_name"], "Teste")


if __name__ == "__main__":
    unittest.main()
