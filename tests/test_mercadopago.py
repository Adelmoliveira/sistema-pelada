import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from PIL import Image
from openpyxl import load_workbook

from app import app
from src.db import get_db, init_postgres
from src.routes.auth import make_password_hash
from src.routes.sales import pix_access_token
from src.services.mercadopago import validate_webhook_signature
from src.services.mercadopago import MercadoPagoError
from src.services.mercadopago import create_pix_order
from src.services.email_reminders import dispatch_reminders, get_reminder_settings, outstanding_players, send_gmail
from src.services.cash_register import get_session, session_summary
from src.services.monthly_sales_report import monthly_sales_data
from src.services.stock_alerts import notify_low_stock
from src.services.push_notifications import send_player_push
from src.utils import alphabetical_key, brdate, local_today, month_bounds
from werkzeug.security import check_password_hash


class MercadoPagoFlowTest(unittest.TestCase):
    def test_postgres_schema_omits_sqlite_seed_syntax(self):
        class Recorder:
            def __init__(self):
                self.statements = []

            def execute(self, statement, params=()):
                self.statements.append(statement)
                return self

            def commit(self):
                return None

        recorder = Recorder()
        init_postgres(recorder)
        self.assertFalse(
            any("INSERT OR IGNORE" in statement.upper() for statement in recorder.statements)
        )
        schedule_seeds = [
            statement
            for statement in recorder.statements
            if statement.upper().startswith("INSERT INTO TRIBUTE_SCHEDULES")
        ]
        self.assertTrue(schedule_seeds)
        self.assertTrue(all("RETURNING WEEKDAY" in statement.upper() for statement in schedule_seeds))
        self.assertTrue(any("ADD COLUMN IF NOT EXISTS CLUB_QR_DATA" in statement.upper() for statement in recorder.statements))
        self.assertTrue(any("IDX_PLAYERS_CLUB_QR_TOKEN" in statement.upper() for statement in recorder.statements))
        self.assertTrue(any(
            "LOAD_ENTRIES_AREA_CODE_CHECK" in statement.upper() and "'INT'" in statement.upper()
            for statement in recorder.statements
        ))

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

    def test_player_first_access_creates_password_and_identifies_sale(self):
        with app.app_context():
            db = get_db()
            db.execute("UPDATE players SET war_name='Craque' WHERE id=?", (self.player_id,))
            db.commit()

        first_access = self.client.post("/login", data={"username": "Craque"})
        self.assertEqual(first_access.status_code, 200)
        self.assertIn("Primeiro acesso", first_access.get_data(as_text=True))
        self.assertIn('action="/cliente/senha"', first_access.get_data(as_text=True))
        configured = self.client.post(
            "/cliente/senha",
            data={"password": "senha-segura", "password_confirm": "senha-segura"},
        )
        self.assertEqual(configured.status_code, 303)
        self.assertEqual(configured.headers["Location"], "/minha-conta")
        with self.client.session_transaction() as session:
            client_user_id = session["user_id"]
        with app.app_context():
            db = get_db()
            client_user = db.execute("SELECT * FROM users WHERE id=?", (client_user_id,)).fetchone()
            self.assertEqual((client_user["role"], client_user["player_id"], client_user["username"]), ("client", self.player_id, "Craque"))

        page = self.client.get("/sale").get_data(as_text=True)
        self.assertIn("Craque", page)
        self.assertIn("<strong class=\"d-block fs-5\">Peladeiro</strong>", page)
        self.assertIn("<small>Peladeiro</small>", page)
        self.assertNotIn("Quem está comprando?", page)
        self.assertIn("Novo chamado", self.client.get("/sale").get_data(as_text=True))

        sale = self.client.post(
            "/sale",
            data={"product_id": self.product_id, "quantity": 1, "payment_method": "Dinheiro", "notes": ""},
        )
        self.assertEqual(sale.status_code, 303)
        with app.app_context():
            db = get_db()
            created = db.execute("SELECT player_id,payment_status FROM sales ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual((created["player_id"], created["payment_status"]), (self.player_id, "pending_cash"))

    def test_login_without_username_returns_form_instead_of_bad_request(self):
        response = self.client.post("/login", data={"password": "qualquer"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Informe seu nome de usuário", response.get_data(as_text=True))

    def test_manager_changes_player_password_from_player_record(self):
        with app.app_context():
            db = get_db()
            db.execute("UPDATE players SET war_name='Craque' WHERE id=?", (self.player_id,))
            db.commit()
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        response = self.client.post(
            f"/players/{self.player_id}/password",
            data={"new_password": "senha-nova-123"},
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            db = get_db()
            user = db.execute("SELECT * FROM users WHERE player_id=?", (self.player_id,)).fetchone()
            self.assertIsNotNone(user)
            self.assertTrue(check_password_hash(user["password_hash"], "senha-nova-123"))

    def test_player_photo_is_saved_and_sent_to_delivery_queue(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        image = BytesIO()
        Image.new("RGB", (24, 24), (20, 100, 180)).save(image, format="JPEG")
        image.seek(0)
        created = self.client.post(
            "/players",
            data={"name": "Peladeiro com foto", "war_name": "Foto", "membership_type": "regular",
                  "photo": (image, "foto.jpg")},
            content_type="multipart/form-data",
        )
        self.assertEqual(created.status_code, 302)
        with app.app_context():
            db = get_db()
            player = db.execute("SELECT * FROM players WHERE war_name='Foto'").fetchone()
            self.assertTrue(player["thumbnail_data"].startswith("data:image/jpeg;base64,"))
            sale = db.execute(
                "INSERT INTO sales(player_id,payment_method,total_cents,paid,payment_status,ready_for_delivery) VALUES(?,?,?,?,?,1)",
                (player["id"], "Dinheiro", 300, 1, "approved"),
            )
            db.execute("INSERT INTO sale_items(sale_id,product_id,quantity,unit_price_cents,unit_cost_cents) VALUES(?,?,?,?,?)",
                       (sale.lastrowid, self.product_id, 1, 300, 100))
            db.commit()
        players_page = self.client.get("/players").get_data(as_text=True)
        self.assertIn('alt="Foto de Foto"', players_page)
        feed = self.client.get("/orders/feed")
        self.assertEqual(feed.status_code, 200)
        self.assertTrue(feed.get_json()["pending"][-1]["player_photo"].startswith("data:image/jpeg;base64,"))

    def test_client_can_update_own_photo_from_my_account(self):
        with app.app_context():
            db = get_db()
            db.execute("UPDATE players SET war_name='Craque' WHERE id=?", (self.player_id,))
            db.commit()
        self.client.post("/login", data={"username": "Craque"})
        self.client.post("/cliente/senha", data={"password": "senha-segura", "password_confirm": "senha-segura"})
        image = BytesIO()
        Image.new("RGB", (24, 24), (180, 80, 20)).save(image, format="JPEG")
        image.seek(0)
        response = self.client.post("/minha-conta", data={
            "birth_date": "1990-07-17", "phone": "(12) 99999-1111", "emergency_phone": "(12) 98888-2222",
            "postal_code": "12245000", "photo": (image, "perfil.jpg")
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Foto atualizada com sucesso", response.get_data(as_text=True))
        with app.app_context():
            player = get_db().execute("SELECT thumbnail_data FROM players WHERE id=?", (self.player_id,)).fetchone()
            self.assertTrue(player["thumbnail_data"].startswith("data:image/jpeg;base64,"))

    def test_client_can_use_and_revoke_club_qr_card_without_login(self):
        with app.app_context():
            db = get_db()
            client_user_id = db.execute(
                "INSERT INTO users(username,name,password_hash,role,player_id) VALUES(?,?,?,'client',?)",
                ("qr-clube", "QR Clube", "hash", self.player_id),
            ).lastrowid
            db.execute("UPDATE players SET war_name='Craque QR' WHERE id=?", (self.player_id,))
            db.commit()

        with self.client.session_transaction() as session:
            session["user_id"] = client_user_id
        image = BytesIO()
        Image.new("RGB", (96, 96), "white").save(image, format="PNG")
        expected_data = base64.b64encode(image.getvalue()).decode("ascii")
        image.seek(0)
        uploaded = self.client.post(
            "/minha-conta/carteirinha",
            data={"action": "upload", "club_qr": (image, "entrada-gpcta.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(uploaded.status_code, 302)

        with app.app_context():
            player = get_db().execute(
                "SELECT club_qr_data,club_qr_token FROM players WHERE id=?",
                (self.player_id,),
            ).fetchone()
            first_token = player["club_qr_token"]
            self.assertGreaterEqual(len(first_token), 40)
            self.assertEqual(player["club_qr_data"], f"data:image/png;base64,{expected_data}")

        with self.client.session_transaction() as session:
            session.clear()
        public_card = self.client.get(f"/carteirinha/{first_token}")
        self.assertEqual(public_card.status_code, 200)
        self.assertIn("Craque QR", public_card.get_data(as_text=True))
        self.assertIn("QR Code de entrada no GPCTA", public_card.get_data(as_text=True))
        self.assertIn("QR Code fornecido pelo DCTA", public_card.get_data(as_text=True))
        self.assertNotIn("QR Code fornecido pelo clube", public_card.get_data(as_text=True))
        self.assertIn('class="card-logo-button"', public_card.get_data(as_text=True))
        self.assertIn('aria-label="Voltar à tela de login"', public_card.get_data(as_text=True))
        self.assertIn('action="/logout"', public_card.get_data(as_text=True))
        self.assertIn("no-store", public_card.headers["Cache-Control"])
        self.assertEqual(public_card.headers["X-Robots-Tag"], "noindex, nofollow")
        manifest = self.client.get(f"/carteirinha/{first_token}/manifest.webmanifest")
        self.assertEqual(manifest.status_code, 200)
        self.assertEqual(manifest.get_json()["start_url"], f"/carteirinha/{first_token}")
        self.assertEqual(self.client.get("/carteirinha/token-invalido").status_code, 404)
        with self.client.session_transaction() as session:
            session["user_id"] = client_user_id
        account_html = self.client.get("/minha-conta").get_data(as_text=True)
        self.assertIn('id="toggle-club-card-device"', account_html)
        self.assertIn(f'data-token="{first_token}"', account_html)
        self.assertIn("gpcta-club-card-token", account_html)
        self.assertIn("Ativar neste dispositivo", account_html)
        self.assertIn("Desativar neste dispositivo", account_html)
        self.assertIn("window.addEventListener('pageshow',render)", account_html)
        self.assertNotIn('id="activate-club-card-device"', account_html)
        self.assertNotIn('id="deactivate-club-card-device"', account_html)

        with self.client.session_transaction() as session:
            session["user_id"] = client_user_id
        regenerated = self.client.post(
            "/minha-conta/carteirinha",
            data={"action": "regenerate"},
        )
        self.assertEqual(regenerated.status_code, 302)
        with app.app_context():
            second_token = get_db().execute(
                "SELECT club_qr_token FROM players WHERE id=?", (self.player_id,)
            ).fetchone()["club_qr_token"]
        self.assertNotEqual(first_token, second_token)
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get(f"/carteirinha/{first_token}").status_code, 404)
        self.assertEqual(self.client.get(f"/carteirinha/{second_token}").status_code, 200)

        with self.client.session_transaction() as session:
            session["user_id"] = client_user_id
        removed = self.client.post("/minha-conta/carteirinha", data={"action": "remove"})
        self.assertEqual(removed.status_code, 302)
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get(f"/carteirinha/{second_token}").status_code, 404)

    def test_login_logo_opens_the_club_card_saved_on_the_device(self):
        page = self.client.get("/login").get_data(as_text=True)
        self.assertIn('id="club-card-logo"', page)
        self.assertIn('aria-label="Abrir carteirinha GPCTA"', page)
        self.assertIn("gpcta-club-card-token", page)
        self.assertIn("fetch(target,{method:'HEAD',cache:'no-store'})", page)
        self.assertNotIn("Toque para abrir sua carteirinha", page)

    def test_manager_can_update_branding_logo(self):
        """The branding settings table is keyed by text, not a numeric id."""
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        image = BytesIO()
        Image.new("RGB", (48, 48), (12, 86, 140)).save(image, format="PNG")
        image.seek(0)
        response = self.client.post(
            "/minha-conta/logo",
            data={"logo": (image, "logo.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            setting = get_db().execute(
                "SELECT value FROM app_settings WHERE key=?", ("branding_logo_data",)
            ).fetchone()
            self.assertTrue(setting["value"].startswith("data:image/jpeg;base64,"))
        logo = self.client.get("/branding/logo")
        self.assertEqual(logo.status_code, 200)
        self.assertTrue(logo.data.startswith(b"\xff\xd8"))

    def test_product_photo_and_food_category_are_available_in_quick_sale(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        photo = BytesIO()
        Image.new("RGB", (900, 600), (220, 150, 40)).save(photo, format="PNG")
        photo.seek(0)
        response = self.client.post(
            "/products",
            data={
                "name": "Sanduíche natural",
                "category": "Alimentos",
                "package_type": "",
                "units_per_case": "0",
                "price": "12,50",
                "cost": "7,00",
                "stock": "8",
                "initial_cases": "0",
                "min_stock": "2",
                "supplier_email": "",
                "photo": (photo, "sanduiche.png"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            product = get_db().execute("SELECT * FROM products WHERE name=?", ("Sanduíche natural",)).fetchone()
            self.assertEqual(product["category"], "Alimentos")
            self.assertTrue(product["photo_data"].startswith("data:image/jpeg;base64,"))
            self.assertTrue(product["thumbnail_data"].startswith("data:image/jpeg;base64,"))
            product_id = product["id"]

        sale_page = self.client.get("/sale").get_data(as_text=True)
        self.assertIn("Sanduíche natural", sale_page)
        self.assertIn("Foto de Sanduíche natural", sale_page)
        self.assertIn('value="Alimentos"', sale_page)

        removed = self.client.post(
            f"/products/{product_id}/edit",
            data={
                "name": "Sanduíche natural",
                "category": "Alimentos",
                "package_type": "",
                "units_per_case": "0",
                "price": "12,50",
                "cost": "7,00",
                "min_stock": "2",
                "stock": "8",
                "stock_reason": "",
                "supplier_email": "",
                "remove_photo": "1",
            },
        )
        self.assertEqual(removed.status_code, 302)
        with app.app_context():
            product = get_db().execute("SELECT photo_data,thumbnail_data FROM products WHERE id=?", (product_id,)).fetchone()
            self.assertEqual((product["photo_data"], product["thumbnail_data"]), ("", ""))

    def test_client_quick_sale_has_live_topbar_cart_indicator(self):
        with app.app_context():
            db = get_db()
            db.execute("UPDATE users SET role='client',player_id=? WHERE id=?", (self.player_id, self.user_id))
            db.commit()
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        page = self.client.get("/sale").get_data(as_text=True)
        self.assertIn('id="topbar-cart"', page)
        self.assertIn('id="topbar-cart-count"', page)
        self.assertIn('href="#sale-cart-panel"', page)
        self.assertIn("topbarCart.hidden=quantity===0", page)
        self.assertIn("panel.scrollIntoView({behavior:'smooth'", page)

    def test_client_can_update_profile_and_change_own_password(self):
        with app.app_context():
            db = get_db()
            db.execute("UPDATE players SET war_name='Perfil' WHERE id=?", (self.player_id,))
            db.commit()
        self.client.post("/login", data={"username": "Perfil"})
        self.client.post("/cliente/senha", data={"password": "senha-segura", "password_confirm": "senha-segura"})
        response = self.client.post("/minha-conta", data={
            "birth_date": "1990-07-17", "phone": "(12) 99999-1111", "emergency_phone": "Maria (12) 98888-2222",
            "postal_code": "12245000", "address_street": "Rua Teste", "address_number": "50",
            "address_complement": "Casa", "address_neighborhood": "Centro", "address_city": "São José dos Campos", "address_state": "sp",
        })
        self.assertEqual(response.status_code, 200)
        with app.app_context():
            player = get_db().execute("SELECT * FROM players WHERE id=?", (self.player_id,)).fetchone()
            self.assertEqual((player["birth_date"], player["postal_code"], player["address_state"]), ("1990-07-17", "12245000", "SP"))
            self.assertEqual(player["emergency_phone"], "Maria (12) 98888-2222")
        changed = self.client.post("/minha-conta/senha", data={"password": "senha-nova-123", "password_confirm": "senha-nova-123"})
        self.assertEqual(changed.status_code, 302)
        self.client.post("/logout")
        login = self.client.post("/login", data={"username": "Perfil", "password": "senha-nova-123"})
        self.assertEqual(login.status_code, 303)

    def test_birthday_notice_is_shown_to_authenticated_users(self):
        with app.app_context():
            db = get_db()
            today = local_today()
            db.execute("UPDATE players SET war_name='Aniversariante', birth_date=? WHERE id=?",
                       (f"1990-{today.month:02d}-{today.day:02d}", self.player_id))
            db.commit()
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        page = self.client.get("/finance").get_data(as_text=True)
        self.assertIn("Hoje é aniversário do peladeiro", page)
        self.assertIn("Aniversariante", page)

    def test_client_can_view_month_birthdays_from_sidebar(self):
        with app.app_context():
            db = get_db()
            today = local_today()
            db.execute("UPDATE players SET war_name='Aniversariante', birth_date=? WHERE id=?",
                       (f"1990-{today.month:02d}-{today.day:02d}", self.player_id))
            db.commit()
        self.client.post("/login", data={"username": "Aniversariante"})
        self.client.post("/cliente/senha", data={"password": "senha-segura", "password_confirm": "senha-segura"})
        page = self.client.get("/aniversariantes")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Aniversariantes do mês", page.get_data(as_text=True))
        self.assertIn("Aniversariante", page.get_data(as_text=True))
        sidebar = self.client.get("/sale").get_data(as_text=True)
        self.assertIn("Parabéns, Peladeiro Aniversariante!", sidebar)
        self.assertIn("muitas resenhas e gols na nossa pelada", sidebar)
        self.assertIn("Aniversariantes do mês", sidebar)
        self.assertIn('<span>Compra rápida</span>', sidebar)
        self.assertNotIn('<span>Bar</span>', sidebar)

    def test_female_birthday_message_uses_name_without_peladeiro(self):
        with app.app_context():
            db = get_db()
            today = local_today()
            db.execute("UPDATE players SET war_name='Maria Eduarda', gender='female', birth_date=? WHERE id=?",
                       (f"1990-{today.month:02d}-{today.day:02d}", self.player_id))
            db.commit()
        self.client.post("/login", data={"username": "Maria Eduarda"})
        self.client.post("/cliente/senha", data={"password": "senha-segura", "password_confirm": "senha-segura"})
        page = self.client.get("/sale").get_data(as_text=True)
        self.assertIn("Parabéns, Maria Eduarda!", page)
        self.assertNotIn("Parabéns, Peladeiro Maria Eduarda!", page)

    def test_female_birthday_notice_uses_feminine_generic_message(self):
        with app.app_context():
            db = get_db()
            today = local_today()
            db.execute("UPDATE players SET war_name='Maria Eduarda', gender='female', birth_date=? WHERE id=?",
                       (f"1990-{today.month:02d}-{today.day:02d}", self.player_id))
            db.commit()
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        page = self.client.get("/finance").get_data(as_text=True)
        self.assertIn("Hoje é aniversário da", page)
        self.assertIn("à aniversariante", page)

    def test_new_maintenance_request_prefills_logged_client_war_name(self):
        with app.app_context():
            db = get_db()
            db.execute("UPDATE players SET war_name='Nome de Guerra' WHERE id=?", (self.player_id,))
            db.commit()
        self.client.post("/login", data={"username": "Nome de Guerra"})
        self.client.post("/cliente/senha", data={"password": "senha-segura", "password_confirm": "senha-segura"})
        page = self.client.get("/infra/maintenance/new").get_data(as_text=True)
        self.assertIn('value="Nome de Guerra" readonly', page)
        self.assertIn("Preenchido automaticamente pelo usuário logado", page)

    def test_pix_reconciliation_uses_payment_confirmation_date(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        today = local_today().isoformat()
        with app.app_context():
            db = get_db()
            included = db.execute(
                """INSERT INTO sales(player_id,payment_method,total_cents,paid,created_at,paid_at)
                VALUES(?,'Pix',700,1,'2026-01-01 12:00:00',?)""",
                (self.player_id, f"{today} 15:00:00"),
            ).lastrowid
            excluded = db.execute(
                """INSERT INTO sales(player_id,payment_method,total_cents,paid,created_at,paid_at)
                VALUES(?,'Pix',900,1,?,'2026-01-02 12:00:00')""",
                (self.player_id, f"{today} 15:01:00"),
            ).lastrowid
            db.commit()
        page = self.client.get(f"/pix?day={today}").get_data(as_text=True)
        self.assertIn(f"#{included}", page)
        self.assertNotIn(f"#{excluded}", page)
        self.assertIn("Pix confirmados", page)
        invalid = self.client.get("/pix?day=data-invalida").get_data(as_text=True)
        self.assertIn("data informada era inválida", invalid)

    def test_finance_dashboard_shows_monthly_and_annual_average_ticket(self):
        today = local_today()
        current_month = today.replace(day=1).isoformat()
        previous_month = (today.replace(day=1) - timedelta(days=1)).replace(day=1).isoformat()
        with app.app_context():
            db = get_db()
            for total, paid_at, method in (
                (1000, f"{current_month} 10:00:00", "Pix"),
                (3000, f"{current_month} 11:00:00", "Dinheiro"),
                (6000, f"{previous_month} 12:00:00", "Débito"),
                (9000, f"{current_month} 13:00:00", "Cortesia"),
            ):
                db.execute(
                    """INSERT INTO sales(player_id,payment_method,total_cents,paid,payment_status,paid_at)
                       VALUES(?,?,?,?,?,?)""",
                    (self.player_id, method, total, 1, "approved", paid_at),
                )
            db.commit()
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id

        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Ticket médio do bar", page)
        self.assertIn("R$ 20,00", page)
        self.assertIn("R$ 33,33", page)
        self.assertIn("2 venda(s) paga(s)", page)
        self.assertIn("3 venda(s) paga(s)", page)
        self.assertIn('id="ticket-average-chart"', page)

    def test_football_dashboard_shows_monthly_annual_attendance_and_extremes(self):
        today = local_today()
        other_months = [month for month in range(1, 13) if month != today.month][:2]
        dates_and_counts = (
            (date(today.year, today.month, 1).isoformat(), 1),
            (date(today.year, other_months[0], 1).isoformat(), 3),
            (date(today.year, other_months[1], 1).isoformat(), 2),
        )
        with app.app_context():
            db = get_db()
            player_ids = [self.player_id]
            for index in range(2):
                player_ids.append(db.execute(
                    "INSERT INTO players(name,war_name) VALUES(?,?)",
                    (f"Participante {index}", f"P{index}"),
                ).lastrowid)
            for match_date, confirmed_count in dates_and_counts:
                sumula_id = db.execute(
                    """INSERT INTO football_sumulas(match_date,day_pelada,situacao,created_by)
                       VALUES(?,'SABADO','FINALIZADA',?)""",
                    (match_date, self.user_id),
                ).lastrowid
                for player_id in player_ids[:confirmed_count]:
                    db.execute(
                        "INSERT INTO football_participants(sumula_id,player_id,status) VALUES(?,?,'CONFIRMADO')",
                        (sumula_id, player_id),
                    )
                if confirmed_count < len(player_ids):
                    db.execute(
                        "INSERT INTO football_participants(sumula_id,player_id,status) VALUES(?,?,'AUSENTE')",
                        (sumula_id, player_ids[-1]),
                    )
            db.commit()
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id

        page = self.client.get("/futebol").get_data(as_text=True)
        highest_label = ("Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez")[other_months[0] - 1]
        current_label = ("Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez")[today.month - 1]
        self.assertIn("Participação nas peladas", page)
        self.assertIn("Média do mês atual", page)
        self.assertIn("Média anual", page)
        self.assertIn(f"Maior participação: {highest_label}", page)
        self.assertIn(f"Menor participação: {current_label}", page)
        self.assertIn('id="football-attendance-chart"', page)

    def test_football_dashboard_shows_monthly_and_annual_responsibility_leaders(self):
        today = local_today()
        current_dates = (today.replace(day=1), today.replace(day=2))
        earlier_month = 1 if today.month != 1 else 2
        earlier_date = date(today.year, earlier_month, 1)
        with app.app_context():
            db = get_db()
            helper_player_id = db.execute(
                "INSERT INTO players(name,war_name) VALUES(?,?)",
                ("Ajudante", "Apoio"),
            ).lastrowid
            sumula_ids = []
            for match_date in (*current_dates, earlier_date):
                sumula_ids.append(
                    db.execute(
                        "INSERT INTO football_sumulas(match_date,day_pelada,situacao,created_by) VALUES(?,'SABADO','FINALIZADA',?)",
                        (match_date.isoformat(), self.user_id),
                    ).lastrowid
                )
            responsibilities = (
                (sumula_ids[0], self.player_id, "SORTEIO"),
                (sumula_ids[0], helper_player_id, "SUMULA"),
                (sumula_ids[0], helper_player_id, "QUADRO"),
                (sumula_ids[0], self.player_id, "ARBITRO_VOLUNTARIO"),
                (sumula_ids[1], self.player_id, "SORTEIO"),
                (sumula_ids[1], self.player_id, "SUMULA"),
                (sumula_ids[1], helper_player_id, "QUADRO"),
                (sumula_ids[1], helper_player_id, "ARBITRO_VOLUNTARIO"),
                (sumula_ids[2], helper_player_id, "SORTEIO"),
                (sumula_ids[2], helper_player_id, "SUMULA"),
                (sumula_ids[2], self.player_id, "QUADRO"),
                (sumula_ids[2], helper_player_id, "ARBITRO_VOLUNTARIO"),
            )
            for sumula_id, player_id, responsibility_type in responsibilities:
                db.execute(
                    "INSERT INTO football_responsibles(sumula_id,player_id,responsibility_type) VALUES(?,?,?)",
                    (sumula_id, player_id, responsibility_type),
                )
            db.commit()

        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        page = self.client.get("/futebol").get_data(as_text=True)
        self.assertIn("Destaques de apoio à gestão", page)
        self.assertRegex(page, r'data-responsibility-period="month" data-responsibility-type="SORTEIO"><strong>Peladeiro</strong><br><span class="text-muted small">2 vezes</span>')
        self.assertRegex(page, r'data-responsibility-period="month" data-responsibility-type="SUMULA"><strong>Apoio, Peladeiro</strong><br><span class="text-muted small">1 vez</span>')
        self.assertRegex(page, r'data-responsibility-period="month" data-responsibility-type="QUADRO"><strong>Apoio</strong><br><span class="text-muted small">2 vezes</span>')
        self.assertRegex(page, r'data-responsibility-period="month" data-responsibility-type="ARBITRO_VOLUNTARIO"><strong>Apoio, Peladeiro</strong><br><span class="text-muted small">1 vez</span>')
        self.assertRegex(page, r'data-responsibility-period="year" data-responsibility-type="SORTEIO"><strong>Peladeiro</strong><br><span class="text-muted small">2 vezes</span>')
        self.assertRegex(page, r'data-responsibility-period="year" data-responsibility-type="SUMULA"><strong>Apoio</strong><br><span class="text-muted small">2 vezes</span>')
        self.assertRegex(page, r'data-responsibility-period="year" data-responsibility-type="QUADRO"><strong>Apoio</strong><br><span class="text-muted small">2 vezes</span>')
        self.assertRegex(page, r'data-responsibility-period="year" data-responsibility-type="ARBITRO_VOLUNTARIO"><strong>Apoio</strong><br><span class="text-muted small">2 vezes</span>')

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

    def test_manager_can_list_complete_player_records_and_download_pdf(self):
        with app.app_context():
            db = get_db()
            db.execute("UPDATE players SET war_name='Craque', birth_date='1990-07-17', phone='11999999999', emergency_phone='11888888888', postal_code='12245000', address_street='Rua Teste', address_number='50', address_city='São José dos Campos', address_state='SP' WHERE id=?", (self.player_id,))
            for index in range(11):
                db.execute("INSERT INTO players(name,war_name) VALUES(?,?)", (f"Peladeiro Extra {index}", f"Extra{index}"))
            db.commit()
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        page = self.client.get("/players/report?q=Craque")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("Cadastro completo dos peladeiros", html)
        self.assertIn("Craque", html)
        self.assertIn("Rua Teste", html)
        self.assertIn(f"/players/report/{self.player_id}", html)
        self.assertIn('target="_blank"', html)
        detail = self.client.get(f"/players/report/{self.player_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Contato de emergência", detail.get_data(as_text=True))
        report = self.client.get("/players/report.pdf?q=Craque")
        self.assertEqual(report.status_code, 200)
        self.assertTrue(report.data.startswith(b"%PDF-"))
        self.assertIn("cadastro-completo-peladeiros.pdf", report.headers["Content-Disposition"])
        paged = self.client.get("/players/report?page=2")
        self.assertEqual(paged.status_code, 200)
        self.assertIn("Próxima", paged.get_data(as_text=True))

    def test_manager_sidebar_groups_modules_and_links(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        page = self.client.get("/players").get_data(as_text=True)
        self.assertIn('id="app-sidebar"', page)
        modules = ["Bar", "Financeiro", "Infra-Estrutura", "Relatórios", "Urgente", "Administração"]
        positions = [page.index(f"<span>{label}</span>") for label in modules]
        self.assertEqual(positions, sorted(positions))
        for links in (
            ["Caixa", "Conferir Pix", "Estoque", "Produtos", "Pedidos", "Venda rápida"],
            ["Mensalidades", "Livro-caixa", "Lembretes"],
            ["Manutenção", "Materiais", "Relação de Carga"],
            ["Peladeiros", "Cadastro completo / PDF", "Usuários"],
        ):
            link_positions = [page.index(f">{label}</a>") for label in links]
            self.assertEqual(link_positions, sorted(link_positions))
        self.assertIn('data-bs-target="#sidebar-bar"', page)
        self.assertIn(">Bar e vendas</a>", page)
        self.assertIn(">Compras e estoque</a>", page)
        self.assertIn(">Consolidado</a>", page)
        self.assertIn('class="offcanvas-lg offcanvas-start app-sidebar"', page)
        self.assertIn('alt="Logo GPCTA"', page)
        self.assertNotIn('class="navbar ', page)
        self.assertNotIn('class="sidebar-user"', page)
        self.assertIn('class="topbar-account"', page)
        # The account block now wraps the manager name in a link so it is
        # reachable from the top bar as well as the sidebar.
        self.assertIn('<strong>Teste</strong>', page)
        self.assertIn('<small>Gerente</small>', page)
        self.assertEqual(page.count('<strong>Teste</strong>'), 1)
        self.assertEqual(page.count('>Relatórios</a>'), 0)
        finance_page = self.client.get("/finance").get_data(as_text=True)
        self.assertIn('<input type="month" name="start_month"', finance_page)
        self.assertIn(f'value="{local_today().strftime("%Y-%m")}"', finance_page)
        self.assertIn('action="/logout"', page)
        self.assertIn('class="logout-button"', page)
        self.assertIn('aria-label="Sair do sistema"', page)
        self.assertIn('id="pwa-install"', page)
        self.assertIn("Aniversariantes do mês", page)
        self.assertIn('href="/aniversariantes"', page)

    def test_display_profile_opens_live_orders_and_birthdays_panel(self):
        with app.app_context():
            db = get_db()
            cursor = db.execute(
                "INSERT INTO users(username,name,password_hash,role) VALUES(?,?,?,'display')",
                ("painel", "Painel da TV", "hash"),
            )
            display_id = cursor.lastrowid
            db.commit()
        with self.client.session_transaction() as session:
            session["user_id"] = display_id
        panel = self.client.get("/painel")
        self.assertEqual(panel.status_code, 200)
        html = panel.get_data(as_text=True)
        self.assertIn("Pedidos em andamento", html)
        self.assertIn("Aniversariantes do mês", html)
        self.assertEqual(self.client.get("/painel/feed").status_code, 200)

    def test_display_user_is_created_and_logs_in_without_password(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        created = self.client.post("/users", data={
            "name": "Monitor", "username": "monitor-tv", "role": "display", "password": "",
        })
        self.assertEqual(created.status_code, 302)
        self.client.post("/logout")
        login = self.client.post("/login", data={"username": "monitor-tv", "password": ""})
        self.assertEqual(login.status_code, 303)
        self.assertTrue(login.headers["Location"].endswith("/painel"))

    def test_urgent_is_visible_and_accessible_to_every_user_role(self):
        with app.app_context():
            db = get_db()
            role_ids = {"manager": self.user_id}
            for role in ("staff", "client", "infra", "maintenance"):
                cursor = db.execute(
                    "INSERT INTO users(username,name,password_hash,role) VALUES(?,?,?,?)",
                    (f"teste.{role}", f"Teste {role}", "hash", role),
                )
                role_ids[role] = cursor.lastrowid
            db.commit()

        for role, user_id in role_ids.items():
            with self.subTest(role=role):
                with self.client.session_transaction() as session:
                    session["user_id"] = user_id
                response = self.client.get("/urgent")
                self.assertEqual(response.status_code, 200)
                page = response.get_data(as_text=True)
                self.assertIn('class="sidebar-module sidebar-direct urgent active"', page)
                self.assertIn("<span>Urgente</span>", page)

    def test_passwordless_maintenance_user_only_opens_new_requests(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        created = self.client.post(
            "/users",
            data={"name": "Portaria", "username": "manutencao", "role": "maintenance", "password": ""},
        )
        self.assertEqual(created.status_code, 302)
        with app.app_context():
            user = get_db().execute("SELECT * FROM users WHERE username='manutencao'").fetchone()
            self.assertEqual((user["role"], user["password_required"]), ("maintenance", 0))
            maintenance_user_id = user["id"]

        self.client.post("/logout")
        login = self.client.post(
            "/login", data={"username": "manutencao", "password": "", "next": "/logout"}
        )
        self.assertEqual(login.status_code, 303)
        self.assertTrue(login.headers["Location"].endswith("/infra/maintenance/new"))

        form = self.client.get("/infra/maintenance/new")
        self.assertEqual(form.status_code, 200)
        page = form.get_data(as_text=True)
        self.assertIn("<span>Novo chamado</span>", page)
        self.assertIn("<span>Urgente</span>", page)
        self.assertNotIn("<span>Infra-Estrutura</span>", page)
        self.assertNotIn("Acompanhamento e resolução", page)
        self.assertIn('id="pwa-install"', page)
        self.assertNotIn("← Voltar", page)

        submitted = self.client.post(
            "/infra/maintenance/new",
            data={
                "title": "Lâmpada queimada",
                "area_code": "SAL",
                "location": "Entrada principal",
                "category": "electrical",
                "priority": "medium",
                "description": "A luminária da entrada não acende.",
                "occurred_on": "2026-07-14",
                "notes": "Verificar antes do evento.",
                "status": "completed",
                "responsible": "valor indevido",
                "cost": "999,99",
            },
        )
        self.assertEqual(submitted.status_code, 302)
        self.assertTrue(submitted.headers["Location"].endswith("/infra/maintenance/new"))
        with app.app_context():
            maintenance = get_db().execute(
                "SELECT * FROM maintenance_requests WHERE created_by=?", (maintenance_user_id,)
            ).fetchone()
            self.assertIsNotNone(maintenance)
            self.assertEqual(
                (maintenance["status"], maintenance["responsible"], maintenance["cost_cents"], maintenance["notes"]),
                ("open", "", 0, "Verificar antes do evento."),
            )

        for forbidden_path in ("/infra/maintenance", "/infra/materials", "/sale", "/users"):
            denied = self.client.get(forbidden_path)
            self.assertEqual(denied.status_code, 302)
            self.assertTrue(denied.headers["Location"].endswith("/infra/maintenance/new"))

        stale_post = self.client.post("/", headers={"Accept": "text/html"})
        self.assertEqual(stale_post.status_code, 303)
        self.assertTrue(stale_post.headers["Location"].endswith("/infra/maintenance/new"))

    def test_staff_sees_bar_and_can_only_open_new_maintenance_requests(self):
        with app.app_context():
            db = get_db()
            cursor = db.execute(
                "INSERT INTO users(username,name,password_hash,role) VALUES(?,?,?,'staff')",
                ("atendente", "Atendente", "hash"),
            )
            staff_id = cursor.lastrowid
            db.commit()
        with self.client.session_transaction() as session:
            session["user_id"] = staff_id

        form = self.client.get("/infra/maintenance/new")
        self.assertEqual(form.status_code, 200)
        page = form.get_data(as_text=True)
        self.assertIn("<span>Bar</span>", page)
        self.assertIn("<span>Infra-Estrutura</span>", page)
        self.assertIn(">Novo chamado</a>", page)
        self.assertIn(">Relação de Carga</a>", page)
        self.assertIn(">Validar conferência</a>", page)
        self.assertIn("<span>Urgente</span>", page)
        self.assertNotIn(">Materiais</a>", page)
        self.assertNotIn(">Manutenção</a>", page)
        self.assertNotIn("Acompanhamento e resolução", page)

        relation = self.client.get("/infra/load-relation")
        self.assertEqual(relation.status_code, 200)
        self.assertNotIn("Edição em lote", relation.get_data(as_text=True))
        check = self.client.get("/infra/load-relation/check")
        self.assertEqual(check.status_code, 200)

        submitted = self.client.post(
            "/infra/maintenance/new",
            data={
                "title": "Torneira pingando",
                "area_code": "BAR",
                "location": "Pia do balcão",
                "category": "plumbing",
                "priority": "high",
                "description": "A torneira não fecha completamente.",
                "occurred_on": "2026-07-15",
                "status": "completed",
                "responsible": "valor indevido",
                "cost": "500,00",
            },
        )
        self.assertEqual(submitted.status_code, 302)
        self.assertTrue(submitted.headers["Location"].endswith("/infra/maintenance/new"))
        with app.app_context():
            maintenance = get_db().execute(
                "SELECT * FROM maintenance_requests WHERE created_by=?", (staff_id,)
            ).fetchone()
            self.assertEqual(
                (maintenance["status"], maintenance["responsible"], maintenance["cost_cents"]),
                ("open", "", 0),
            )

        for forbidden_path in ("/infra/maintenance", "/infra/materials", "/infra/load-relation/qr-codes"):
            denied = self.client.get(forbidden_path)
            self.assertEqual(denied.status_code, 302)
            self.assertEqual(denied.headers["Location"], "/orders")

    def test_client_can_create_maintenance_request_without_internal_access_error(self):
        """A abertura pelo peladeiro não deve redirecionar para detalhes internos."""
        with app.app_context():
            db = get_db()
            db.execute("UPDATE players SET war_name='Peladeiro' WHERE id=?", (self.player_id,))
            db.execute(
                "UPDATE users SET role='client', player_id=? WHERE id=?",
                (self.player_id, self.user_id),
            )
            db.commit()
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id

        response = self.client.post(
            "/infra/maintenance/new",
            data={
                "title": "Lâmpada queimada",
                "area_code": "BAR",
                "location": "Balcão",
                "category": "electrical",
                "priority": "medium",
                "description": "A lâmpada não acende.",
                "occurred_on": "2026-08-05",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Chamado MAN-", page)
        self.assertNotIn("Seu usuário não possui acesso a essa funcionalidade.", page)
        with app.app_context():
            request_row = get_db().execute(
                "SELECT id,created_by,status FROM maintenance_requests WHERE created_by=? ORDER BY id DESC LIMIT 1",
                (self.user_id,),
            ).fetchone()
            self.assertIsNotNone(request_row)
            self.assertEqual(request_row["status"], "open")
            history_row = get_db().execute(
                "SELECT status,observation FROM maintenance_request_history WHERE request_id=(SELECT id FROM maintenance_requests WHERE created_by=? ORDER BY id DESC LIMIT 1)",
                (self.user_id,),
            ).fetchone()
            self.assertEqual((history_row["status"], history_row["observation"]), ("open", ""))

        mine = self.client.get("/infra/maintenance/mine")
        self.assertEqual(mine.status_code, 200)
        mine_page = mine.get_data(as_text=True)
        self.assertIn("Meus chamados", mine_page)
        self.assertIn("Lâmpada queimada", mine_page)
        self.assertIn("Aberto", mine_page)
        self.assertIn("Detalhes", mine_page)
        self.assertIn("Chamados por status", mine_page)
        self.assertIn('data-status-count="open">1</strong>', mine_page)
        self.assertIn('data-status-count="completed">0</strong>', mine_page)
        self.assertIn("maintenance-card-priority-medium", mine_page)
        self.assertIn("Tempo aberto:", mine_page)
        self.assertIn("maintenance-age-green", mine_page)
        # A listagem é resumida; os dados completos ficam na tela de detalhes.
        self.assertNotIn("A lâmpada não acende.", mine_page)
        self.assertNotIn("/edit", mine_page)

        request_id = request_row["id"]
        detail = self.client.get(f"/infra/maintenance/mine/{request_id}")
        self.assertEqual(detail.status_code, 200)
        detail_page = detail.get_data(as_text=True)
        self.assertIn("Lâmpada queimada", detail_page)
        self.assertIn("A lâmpada não acende.", detail_page)
        self.assertIn("← Meus chamados", detail_page)
        self.assertNotIn("Editar", detail_page)

        # A tela detalhada também deve respeitar o vínculo com o peladeiro.
        denied_detail = self.client.get("/infra/maintenance/999999")
        self.assertEqual(denied_detail.status_code, 302)

        # Chamados antigos podem ter sido gravados por outro registro de
        # usuário, mas ainda pertencem ao mesmo peladeiro. Eles devem aparecer
        # em "Meus chamados" pelo vínculo users.player_id.
        with app.app_context():
            db = get_db()
            legacy_user = db.execute(
                "INSERT INTO users(username,name,password_hash,role,player_id) VALUES(?,?,?,'client',?)",
                ("peladeiro-legado", "Peladeiro legado", "hash", self.player_id),
            )
            legacy_user_id = legacy_user.lastrowid
            db.execute(
                """INSERT INTO maintenance_requests
                   (code,title,area_code,location,category,priority,description,
                    occurred_on,created_by)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    "MAN-LEGACY-0001",
                    "Chamado antigo",
                    "BAR",
                    "Balcão",
                    "electrical",
                    "medium",
                    "Registro criado antes da conta atual.",
                    "2026-08-01",
                    legacy_user_id,
                ),
            )
            db.commit()

        mine_after_legacy = self.client.get("/infra/maintenance/mine")
        self.assertEqual(mine_after_legacy.status_code, 200)
        self.assertIn("Chamado antigo", mine_after_legacy.get_data(as_text=True))

        duplicate = self.client.post(
            "/infra/maintenance/new",
            data={
                "title": "  lâmpada QUEIMADA ",
                "area_code": "BAR",
                "location": "Outro local",
                "category": "electrical",
                "priority": "high",
                "description": "Outro relato para o mesmo problema.",
                "occurred_on": "2026-08-05",
            },
            follow_redirects=True,
        )
        self.assertEqual(duplicate.status_code, 200)
        duplicate_page = duplicate.get_data(as_text=True)
        self.assertIn("Já existe o chamado MAN-", duplicate_page)
        self.assertIn("aberto por Peladeiro", duplicate_page)
        with app.app_context():
            self.assertEqual(
                get_db().execute(
                    "SELECT COUNT(*) total FROM maintenance_requests WHERE created_by=?", (self.user_id,)
                ).fetchone()["total"],
                1,
            )

    def test_material_crud_with_optimized_photo(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id

        invalid = self.client.post("/infra/materials/new", data={"description": ""})
        self.assertEqual(invalid.status_code, 200)
        self.assertIn("descrição é obrigatória", invalid.get_data(as_text=True))
        invalid_photo = self.client.post(
            "/infra/materials/new",
            data={"description": "Teste", "photo": (BytesIO(b"nao-e-imagem"), "foto.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(invalid_photo.status_code, 200)
        self.assertIn("foto enviada é inválida", invalid_photo.get_data(as_text=True))

        photo = BytesIO()
        Image.new("RGB", (1400, 900), color=(20, 110, 180)).save(photo, format="PNG")
        photo.seek(0)
        created = self.client.post(
            "/infra/materials/new",
            data={
                "description": "Analisador de espectro",
                "load_sheet": "FCG-1877",
                "notes": "Material em bom estado.",
                "photo": (photo, "analisador.png"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(created.status_code, 302)
        with app.app_context():
            material = get_db().execute("SELECT * FROM materials").fetchone()
            material_id = material["id"]
            original_photo = material["photo_data"]
            self.assertTrue(original_photo.startswith("data:image/jpeg;base64,"))
            self.assertTrue(material["thumbnail_data"].startswith("data:image/jpeg;base64,"))

        listing = self.client.get("/infra/materials?q=espectro").get_data(as_text=True)
        self.assertIn("Analisador de espectro", listing)
        self.assertIn("FCG-1877", listing)
        detail = self.client.get(f"/infra/materials/{material_id}").get_data(as_text=True)
        self.assertIn("Material em bom estado.", detail)
        self.assertIn("FCG - Código de controle patrimonial", detail)

        edited = self.client.post(
            f"/infra/materials/{material_id}/edit",
            data={"description": "Analisador atualizado", "load_sheet": "FCG-2000", "notes": "Revisado."},
        )
        self.assertEqual(edited.status_code, 302)
        with app.app_context():
            material = get_db().execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
            self.assertEqual((material["description"], material["photo_data"]), ("Analisador atualizado", original_photo))

        removed = self.client.post(
            f"/infra/materials/{material_id}/edit",
            data={"description": "Analisador atualizado", "load_sheet": "", "notes": "", "remove_photo": "1"},
        )
        self.assertEqual(removed.status_code, 302)
        with app.app_context():
            material = get_db().execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
            self.assertEqual((material["photo_data"], material["thumbnail_data"]), ("", ""))

        deleted = self.client.post(f"/infra/materials/{material_id}/delete")
        self.assertEqual(deleted.status_code, 302)
        with app.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM materials").fetchone()[0], 0)

        self.assertEqual(self.client.get("/infra/load-relation").status_code, 200)

    def test_load_relation_crud_generates_bmp_photos_and_pdf(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        with app.app_context():
            db = get_db()
            cursor = db.execute(
                "INSERT INTO materials(description,load_sheet) VALUES(?,?)",
                ("Cadeira giratória", "FCG-1317918"),
            )
            material_id = cursor.lastrowid
            db.commit()

        missing_material = self.client.post("/infra/load-relation/new", data={"material_id": ""})
        self.assertEqual(missing_material.status_code, 200)
        self.assertIn("Selecione um material", missing_material.get_data(as_text=True))

        photos = []
        for index, color in enumerate(((25, 90, 150), (180, 110, 30)), start=1):
            photo = BytesIO()
            Image.new("RGB", (800, 600), color=color).save(photo, format="JPEG")
            photo.seek(0)
            photos.append((photo, f"foto-{index}.jpg"))
        created = self.client.post(
            "/infra/load-relation/new",
            data={
                "material_id": str(material_id),
                "area_code": "COZ",
                "serial_number": "SERIE-001",
                "location": "Sala G-7",
                "notes": "Carga em bom estado.",
                "photos": photos,
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(created.status_code, 302)
        with app.app_context():
            db = get_db()
            entry = db.execute("SELECT * FROM load_entries").fetchone()
            entry_id = entry["id"]
            self.assertEqual((entry["bmp"], entry["area_code"]), (f"BMP-{entry_id:06d} | COZ", "COZ"))
            stored_photos = db.execute(
                "SELECT * FROM load_entry_photos WHERE load_entry_id=? ORDER BY id", (entry_id,)
            ).fetchall()
            self.assertEqual(len(stored_photos), 2)
            self.assertTrue(stored_photos[0]["thumbnail_data"].startswith("data:image/jpeg;base64,"))
            first_photo_id = stored_photos[0]["id"]

        listing = self.client.get("/infra/load-relation?q=cadeira").get_data(as_text=True)
        self.assertIn("Cadeira giratória", listing)
        self.assertIn(f"BMP-{entry_id:06d}", listing)
        self.assertIn("| COZ", listing)
        filtered_listing = self.client.get("/infra/load-relation?area=BAR").get_data(as_text=True)
        self.assertNotIn("Cadeira giratória", filtered_listing)
        detail = self.client.get(f"/infra/load-relation/{entry_id}").get_data(as_text=True)
        self.assertIn("Carga em bom estado.", detail)
        self.assertIn("SERIE-001", detail)
        self.assertIn("Pendente", detail)
        check_page = self.client.get("/infra/load-relation/check")
        self.assertEqual(check_page.status_code, 200)
        self.assertIn(f"BMP-{entry_id:06d} | COZ", check_page.get_data(as_text=True))
        checked = self.client.post(f"/infra/load-relation/{entry_id}/check")
        self.assertEqual(checked.status_code, 302)
        with app.app_context():
            checked_entry = get_db().execute("SELECT * FROM load_entries WHERE id=?", (entry_id,)).fetchone()
            self.assertIsNotNone(checked_entry["last_checked_at"])
            self.assertEqual(checked_entry["last_checked_by"], self.user_id)
            self.assertIsNotNone(checked_entry["next_check_due_at"])
        checked_detail = self.client.get(f"/infra/load-relation/{entry_id}").get_data(as_text=True)
        self.assertIn("Válida até", checked_detail)
        with app.app_context():
            db = get_db()
            db.execute(
                """INSERT INTO load_entry_photos
                   (load_entry_id,photo_data,thumbnail_data,photo_kind,captured_by)
                   VALUES(?,?,?,?,?)""",
                (entry_id, "data:image/jpeg;base64,AAAA", "data:image/jpeg;base64,BBBB", "conference", self.user_id),
            )
            db.commit()
        timeline = self.client.get(f"/infra/load-relation/{entry_id}").get_data(as_text=True)
        self.assertIn("Linha do tempo das conferências", timeline)
        self.assertIn("Foto anterior", timeline)
        self.assertIn("Foto da conferência", timeline)

        qr_page = self.client.get(f"/infra/load-relation/{entry_id}/qr-code")
        self.assertEqual(qr_page.status_code, 200)
        self.assertIn("data:image/png;base64,", qr_page.get_data(as_text=True))
        self.assertIn(f"/infra/load-relation/{entry_id}", qr_page.get_data(as_text=True))

        qr_selection = self.client.get("/infra/load-relation/qr-codes?area=COZ")
        self.assertEqual(qr_selection.status_code, 200)
        self.assertIn(f"BMP-{entry_id:06d} | COZ", qr_selection.get_data(as_text=True))
        labels = self.client.post(
            "/infra/load-relation/qr-codes.pdf",
            data={"entry_ids": str(entry_id), "size": "standard", "area_code": "COZ"},
        )
        self.assertEqual(labels.status_code, 200)
        self.assertEqual(labels.mimetype, "application/pdf")
        self.assertTrue(labels.data.startswith(b"%PDF-"))

        blocked_material_delete = self.client.post(f"/infra/materials/{material_id}/delete")
        self.assertEqual(blocked_material_delete.status_code, 302)
        with app.app_context():
            self.assertIsNotNone(
                get_db().execute("SELECT id FROM materials WHERE id=?", (material_id,)).fetchone()
            )

        edited = self.client.post(
            f"/infra/load-relation/{entry_id}/edit",
            data={
                "material_id": str(material_id),
                "area_code": "SAL",
                "serial_number": "SERIE-002",
                "location": "Armário H-14",
                "notes": "Inventariado.",
                "remove_photo_ids": str(first_photo_id),
            },
        )
        self.assertEqual(edited.status_code, 302)
        with app.app_context():
            db = get_db()
            entry = db.execute("SELECT * FROM load_entries WHERE id=?", (entry_id,)).fetchone()
            photo_count = db.execute(
                "SELECT COUNT(*) FROM load_entry_photos WHERE load_entry_id=?", (entry_id,)
            ).fetchone()[0]
            self.assertEqual(
                (entry["bmp"], entry["area_code"], entry["serial_number"], entry["location"], photo_count),
                (f"BMP-{entry_id:06d} | SAL", "SAL", "SERIE-002", "Armário H-14", 2),
            )

        report = self.client.get("/infra/load-relation/report.pdf?q=cadeira")
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.mimetype, "application/pdf")
        self.assertTrue(report.data.startswith(b"%PDF-"))
        self.assertIn("attachment", report.headers["Content-Disposition"])

        discharged = self.client.post(f"/infra/load-relation/{entry_id}/discharge")
        self.assertEqual(discharged.status_code, 302)
        with app.app_context():
            entry = get_db().execute("SELECT * FROM load_entries WHERE id=?", (entry_id,)).fetchone()
            self.assertEqual((entry["status"], entry["discharged_by"]), ("discharged", self.user_id))
            self.assertIsNotNone(entry["discharged_at"])
        listing = self.client.get("/infra/load-relation").get_data(as_text=True)
        self.assertIn("Descarregado", listing)
        self.assertNotIn(f'action="/infra/load-relation/{entry_id}/discharge"', listing)

        deleted = self.client.post(f"/infra/load-relation/{entry_id}/delete")
        self.assertEqual(deleted.status_code, 302)
        with app.app_context():
            db = get_db()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM load_entries").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM load_entry_photos").fetchone()[0], 0)

    def test_load_entry_accepts_internal_headquarters_area(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        with app.app_context():
            db = get_db()
            material_id = db.execute(
                "INSERT INTO materials(description,load_sheet) VALUES(?,?)",
                ("Material interno", "FCG-INT"),
            ).lastrowid
            db.commit()

        form = self.client.get("/infra/load-relation/new")
        self.assertIn("INT - Interno sede", form.get_data(as_text=True))
        created = self.client.post(
            "/infra/load-relation/new",
            data={"material_id": str(material_id), "area_code": "INT", "status": "active"},
        )
        self.assertEqual(created.status_code, 302)
        with app.app_context():
            entry = get_db().execute(
                "SELECT bmp,area_code FROM load_entries WHERE material_id=?", (material_id,)
            ).fetchone()
            self.assertEqual(entry["area_code"], "INT")
            self.assertTrue(entry["bmp"].endswith(" | INT"))

    def test_qr_load_check_requires_and_atomically_stores_photo_evidence(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        with app.app_context():
            db = get_db()
            material_id = db.execute(
                "INSERT INTO materials(description,load_sheet) VALUES(?,?)",
                ("Carga para conferência", "FCG-QR"),
            ).lastrowid
            entry_id = db.execute(
                """INSERT INTO load_entries(material_id,bmp,area_code,status)
                   VALUES(?,?,'BAR','active')""",
                (material_id, "BMP-QR | BAR"),
            ).lastrowid
            db.commit()

        missing = self.client.post(f"/infra/load-relation/{entry_id}/check-auto")
        self.assertEqual(missing.status_code, 400)
        self.assertIn("foto", missing.get_json()["error"].lower())
        with app.app_context():
            db = get_db()
            entry = db.execute("SELECT * FROM load_entries WHERE id=?", (entry_id,)).fetchone()
            self.assertIsNone(entry["last_checked_at"])
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM load_entry_photos WHERE load_entry_id=?", (entry_id,)).fetchone()[0],
                0,
            )

        photo = BytesIO()
        Image.new("RGB", (900, 700), color=(55, 125, 75)).save(photo, format="JPEG")
        photo.seek(0)
        checked = self.client.post(
            f"/infra/load-relation/{entry_id}/check-auto",
            data={"photo": (photo, "conferencia.jpg")},
            content_type="multipart/form-data",
        )
        self.assertEqual(checked.status_code, 200, checked.get_json())
        self.assertTrue(checked.get_json()["ok"])
        with app.app_context():
            db = get_db()
            entry = db.execute("SELECT * FROM load_entries WHERE id=?", (entry_id,)).fetchone()
            evidence = db.execute(
                "SELECT * FROM load_entry_photos WHERE load_entry_id=?", (entry_id,)
            ).fetchone()
            self.assertIsNotNone(entry["last_checked_at"])
            self.assertEqual(entry["last_checked_by"], self.user_id)
            self.assertEqual(evidence["photo_kind"], "reference")
            self.assertEqual(evidence["captured_by"], self.user_id)
            self.assertIsNotNone(evidence["captured_at"])
            self.assertTrue(evidence["photo_data"].startswith("data:image/jpeg;base64,"))

        detail = self.client.get(f"/infra/load-relation/{entry_id}").get_data(as_text=True)
        self.assertIn("Referência inicial", detail)
        self.assertIn("utilizada como referência", detail)
        self.assertIn("Por Teste", detail)

    def test_load_batch_movement_status_and_report_filters(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        with app.app_context():
            db = get_db()
            db.execute("INSERT INTO materials(description,load_sheet) VALUES(?,?)", ("Banqueta", "FCG-9"))
            material_id = db.execute("SELECT id FROM materials WHERE description=?", ("Banqueta",)).fetchone()["id"]
            db.commit()
        response = self.client.post("/infra/load-relation/batch", data={
            "material_id": str(material_id), "quantity": "3", "area_code": "SAL",
            "serial_prefix": "BAN-", "location": "Salão", "responsible": "Equipe A",
            "status": "active", "notes": "Lote de teste",
        })
        self.assertEqual(response.status_code, 200)
        with app.app_context():
            db = get_db()
            entries = db.execute("SELECT * FROM load_entries ORDER BY id").fetchall()
            self.assertEqual(len(entries), 3)
            self.assertEqual([entry["bmp"] for entry in entries], [f"BMP-{entry['id']:06d} | SAL" for entry in entries])
            entry_id = entries[0]["id"]
        moved = self.client.post(f"/infra/load-relation/{entry_id}/move", data={
            "location": "Sala 2", "responsible": "Equipe B", "reason": "Remanejamento patrimonial"
        })
        self.assertEqual(moved.status_code, 302)
        changed = self.client.post(f"/infra/load-relation/{entry_id}/status", data={"status": "maintenance"})
        self.assertEqual(changed.status_code, 302)
        detail = self.client.get(f"/infra/load-relation/{entry_id}")
        self.assertEqual(detail.status_code, 200)
        body = detail.get_data(as_text=True)
        self.assertIn("Remanejamento patrimonial", body)
        self.assertIn("Em manutenção", body)
        report = self.client.get("/infra/load-relation/report?q=banqueta&status=maintenance")
        self.assertEqual(report.status_code, 200)
        self.assertIn("Equipe B", report.get_data(as_text=True))

    def test_maintenance_list_colors_priority_and_open_age(self):
        today = local_today()
        with app.app_context():
            db = get_db()
            for index, (priority, age) in enumerate((("low", 12), ("medium", 20), ("high", 30), ("urgent", 5)), start=1):
                db.execute(
                    """INSERT INTO maintenance_requests
                       (code,title,area_code,category,priority,description,status,occurred_on,created_at,created_by)
                       VALUES(?,?,?,'electrical',?,?,'open',?,?,?)""",
                    (
                        f"MAN-AGE-{index}", f"Chamado idade {age}", "BAR", priority,
                        "Teste de indicador de tempo.", today.isoformat(),
                        (today - timedelta(days=age)).isoformat(), self.user_id,
                    ),
                )
            db.commit()
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id

        page = self.client.get("/infra/maintenance").get_data(as_text=True)
        for priority in ("low", "medium", "high", "urgent"):
            self.assertIn(f"maintenance-row-priority-{priority}", page)
        self.assertIn('class="maintenance-age-green">12</strong>', page)
        self.assertIn('class="maintenance-age-orange">20</strong>', page)
        self.assertIn('class="maintenance-age-red">30</strong>', page)
        self.assertIn("Tempo aberto", page)
        self.assertIn("16 a 25 dias", page)
        self.assertIn('<option value="open" selected>Aberto</option>', page)

    def test_maintenance_crud_dashboard_photos_and_report(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        invalid = self.client.post("/infra/maintenance/new", data={"title": ""})
        self.assertEqual(invalid.status_code, 200)
        self.assertIn("título do problema é obrigatório", invalid.get_data(as_text=True))

        problem_photo = BytesIO()
        Image.new("RGB", (900, 700), color=(180, 60, 40)).save(problem_photo, format="JPEG")
        problem_photo.seek(0)
        created = self.client.post(
            "/infra/maintenance/new",
            data={
                "title": "Vazamento no banheiro",
                "area_code": "BAN",
                "location": "Banheiro masculino",
                "category": "plumbing",
                "priority": "urgent",
                "description": "Vazamento próximo ao lavatório.",
                "responsible": "Equipe hidráulica",
                "status": "open",
                "occurred_on": "2026-07-14",
                "due_on": "2026-07-15",
                "cost": "0,00",
                "problem_photos": (problem_photo, "problema.jpg"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(created.status_code, 302)
        with app.app_context():
            db = get_db()
            maintenance = db.execute("SELECT * FROM maintenance_requests").fetchone()
            request_id = maintenance["id"]
            self.assertEqual((maintenance["code"], maintenance["area_code"]), (f"MAN-{request_id:06d}", "BAN"))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM maintenance_photos").fetchone()[0], 1)

        listing = self.client.get("/infra/maintenance?area=BAN&priority=urgent")
        self.assertEqual(listing.status_code, 200)
        self.assertIn("Vazamento no banheiro", listing.get_data(as_text=True))
        dashboard = self.client.get("/infra/maintenance/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("Painel de manutenção", dashboard.get_data(as_text=True))
        detail = self.client.get(f"/infra/maintenance/{request_id}")
        self.assertIn("Vazamento próximo", detail.get_data(as_text=True))

        resolution_photo = BytesIO()
        Image.new("RGB", (900, 700), color=(40, 150, 80)).save(resolution_photo, format="JPEG")
        resolution_photo.seek(0)
        completed = self.client.post(
            f"/infra/maintenance/{request_id}/edit",
            data={
                "title": "Vazamento no banheiro",
                "area_code": "BAN",
                "location": "Banheiro masculino",
                "category": "plumbing",
                "priority": "urgent",
                "description": "Vazamento próximo ao lavatório.",
                "responsible": "Equipe hidráulica",
                "status": "completed",
                "occurred_on": "2026-07-14",
                "due_on": "2026-07-15",
                "completed_on": "2026-07-14",
                "resolution": "Sifão substituído e instalação testada.",
                "cost": "125,50",
                "notes": "Serviço conferido.",
                "resolution_photos": (resolution_photo, "resolucao.jpg"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(completed.status_code, 302)
        with app.app_context():
            db = get_db()
            maintenance = db.execute("SELECT * FROM maintenance_requests WHERE id=?", (request_id,)).fetchone()
            self.assertEqual((maintenance["status"], maintenance["cost_cents"]), ("completed", 12550))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM maintenance_photos").fetchone()[0], 2)

        report = self.client.get("/infra/maintenance/report.pdf?area=BAN")
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.mimetype, "application/pdf")
        self.assertTrue(report.data.startswith(b"%PDF-"))

        deleted = self.client.post(f"/infra/maintenance/{request_id}/delete")
        self.assertEqual(deleted.status_code, 302)
        with app.app_context():
            db = get_db()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM maintenance_requests").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM maintenance_photos").fetchone()[0], 0)

    def test_login_shows_centered_logo_without_navigation_bar_and_copyright(self):
        page = self.client.get("/login").get_data(as_text=True)
        self.assertIn('class="club-card-logo-button mb-3"', page)
        self.assertIn('class="login-logo"', page)
        self.assertNotIn('class="navbar ', page)
        self.assertIn("PELADEIROS GPCTA", page)
        self.assertNotIn("BAR PELADEIROS GPCTA", page)
        self.assertIn("Copyright © 2026 | Grupo de Peladas do CTA - GPCTA", page)
        self.assertNotIn(">Sair</button>", page)

    def test_pwa_assets_are_public_installable_and_do_not_cache_private_pages(self):
        login = self.client.get("/login").get_data(as_text=True)
        self.assertIn('rel="manifest" href="/static/manifest.webmanifest"', login)
        self.assertIn('rel="apple-touch-icon"', login)
        self.assertIn('id="pwa-install"', login)
        self.assertIn('src="/static/pwa.js"', login)

        manifest_response = self.client.get("/static/manifest.webmanifest")
        self.assertEqual(manifest_response.status_code, 200)
        manifest = manifest_response.get_json()
        manifest_response.close()
        self.assertEqual(manifest["name"], "Peladeiros GPCTA")
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual({icon["sizes"] for icon in manifest["icons"]}, {"192x192", "512x512"})

        worker_response = self.client.get("/service-worker.js")
        worker = worker_response.get_data(as_text=True)
        worker_response.close()
        self.assertEqual(worker_response.status_code, 200)
        self.assertEqual(worker_response.headers["Service-Worker-Allowed"], "/")
        self.assertIn('const OFFLINE_URL = "/offline"', worker)
        self.assertIn('data.web_push === 8030', worker)
        self.assertIn('notification.navigate || notification.url', worker)
        self.assertNotIn('request.mode === "navigate"', worker)
        self.assertNotIn('"/sale"', worker)
        self.assertNotIn('"/finance"', worker)
        self.assertEqual(self.client.get("/offline").status_code, 200)

    def test_transient_database_failure_preserves_authenticated_session(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id

        with patch("app.get_db", side_effect=RuntimeError("falha temporária simulada")):
            response = self.client.get("/players")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["Retry-After"], "3")
        self.assertIn("Sua sessão foi preservada", response.get_data(as_text=True))
        with self.client.session_transaction() as session:
            self.assertEqual(session.get("user_id"), self.user_id)

        with patch("app.get_db", side_effect=RuntimeError("falha temporária simulada")):
            static_response = self.client.get("/static/pwa.js")
        self.assertEqual(static_response.status_code, 200)
        static_response.close()

    def test_two_simultaneous_session_reads_are_read_only(self):
        """Concurrent page/unread-count requests must not race on the user row."""
        with app.app_context():
            db = get_db()
            db.execute("INSERT INTO players(name,war_name) VALUES(?,?)", ("Leitor", "leitor"))
            player_id = db.execute("SELECT id FROM players WHERE war_name=?", ("leitor",)).fetchone()["id"]
            db.execute(
                "INSERT INTO users(username,name,password_hash,role,player_id) VALUES(?,?,?,'client',?)",
                ("leitor", "Leitor", "hash", player_id),
            )
            db.commit()
            client_user_id = db.execute("SELECT id FROM users WHERE username=?", ("leitor",)).fetchone()["id"]

        clients = [app.test_client(), app.test_client()]
        for client in clients:
            with client.session_transaction() as session:
                session["user_id"] = client_user_id

        def fetch_unread(client):
            return client.get("/notifications/push/unread-count", headers={"Accept": "application/json"})

        # The schema is already prepared by setUp; prevent this test's two
        # independent connections from needlessly rerunning SQLite setup.
        with patch("src.db.init_sqlite"):
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(fetch_unread, clients))

        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assertEqual([response.get_json()["count"] for response in responses], [0, 0])

    def test_password_hash_is_compatible_with_local_python(self):
        password_hash = make_password_hash("senha-segura-123")
        self.assertTrue(password_hash.startswith("pbkdf2:sha256:"))
        self.assertTrue(check_password_hash(password_hash, "senha-segura-123"))

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

    def test_manager_can_assign_profile_after_user_creation(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        created = self.client.post(
            "/users",
            data={
                "name": "Equipe",
                "username": "equipe.teste",
                "role": "staff",
                "password": "senha-staff-123",
            },
        )
        self.assertEqual(created.status_code, 302)
        with app.app_context():
            target = get_db().execute("SELECT * FROM users WHERE username=?", ("equipe.teste",)).fetchone()
        changed = self.client.post(
            f"/users/{target['id']}/edit",
            data={"name": "Equipe", "username": "equipe.teste", "role": "infra"},
        )
        self.assertEqual(changed.status_code, 302)
        with app.app_context():
            target = get_db().execute("SELECT role,password_required FROM users WHERE id=?", (target["id"],)).fetchone()
            self.assertEqual((target["role"], target["password_required"]), ("infra", 1))

    def test_infra_user_sees_and_accesses_only_infra(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        created = self.client.post(
            "/users",
            data={
                "name": "Equipe Infra",
                "username": "infra.teste",
                "role": "infra",
                "password": "senha-infra-123",
            },
        )
        self.assertEqual(created.status_code, 302)
        with app.app_context():
            infra_user = get_db().execute(
                "SELECT * FROM users WHERE username=?", ("infra.teste",)
            ).fetchone()
            self.assertEqual(infra_user["role"], "infra")

        self.client.post("/logout")
        login = self.client.post(
            "/login", data={"username": "infra.teste", "password": "senha-infra-123"}
        )
        self.assertEqual(login.status_code, 303)
        self.assertTrue(login.headers["Location"].endswith("/infra/maintenance"))

        page = self.client.get("/infra/load-relation")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("<span>Infra-Estrutura</span>", html)
        self.assertIn(">Manutenção</a>", html)
        self.assertIn('class="sidebar-module sidebar-direct urgent ', html)
        self.assertIn("<span>Urgente</span>", html)
        self.assertIn('id="pwa-install"', html)
        for hidden_module in ("Bar", "Financeiro", "Administração"):
            self.assertNotIn(f"<span>{hidden_module}</span>", html)
        for hidden_link in ("Caixa", "Conferir Pix", "Estoque", "Produtos", "Pedidos", "Peladeiros", "Relatórios", "Usuários", "Venda rápida", "Compra rápida"):
            self.assertNotIn(f">{hidden_link}</a>", html)
        self.assertEqual(self.client.get("/infra/materials").status_code, 200)
        self.assertEqual(self.client.get("/infra/maintenance").status_code, 200)
        self.assertEqual(self.client.get("/urgent").status_code, 200)

        for forbidden_path in ("/", "/sale", "/stock", "/cash", "/players", "/users"):
            denied = self.client.get(forbidden_path)
            self.assertEqual(denied.status_code, 302)
            self.assertTrue(denied.headers["Location"].endswith("/infra/maintenance"))

        self.client.post("/logout")
        protected = self.client.get("/infra/materials")
        self.assertEqual(protected.status_code, 302)
        self.assertIn("next=/infra/materials", protected.headers["Location"])
        resumed = self.client.post(
            "/login",
            data={
                "username": "infra.teste", "password": "senha-infra-123",
                "next": "/infra/materials",
            },
        )
        self.assertTrue(resumed.headers["Location"].endswith("/infra/materials"))

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

        with patch("src.services.email_reminders.smtplib.SMTP_SSL") as smtp_ssl:
            send_gmail("diretoriagpcta@gmail.com", "test", "teste@example.com", "Pendência", "Olá **Peladeiro**")
        message = smtp_ssl.return_value.__enter__.return_value.send_message.call_args.args[0]
        html_part = message.get_payload()[1].get_payload(decode=True).decode("utf-8")
        self.assertIn("PELADEIROS GPCTA", html_part)
        self.assertIn("Lembrete de pendência financeira", html_part)

    def test_manager_edits_reminder_and_cron_requires_secret(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        page = self.client.get("/finance/reminders")
        self.assertEqual(page.status_code, 200)
        with patch("src.routes.finance.send_gmail") as send_test:
            test_email = self.client.post(
                "/finance/reminders/send-test", data={"test_email": "teste@example.com"}
            )
        self.assertEqual(test_email.status_code, 302)
        send_test.assert_called_once()
        self.assertEqual(send_test.call_args.args[2], "teste@example.com")
        self.assertIn("Peladeiro de teste", send_test.call_args.args[4])
        with patch("src.routes.finance.send_gmail") as invalid_send:
            invalid_email = self.client.post(
                "/finance/reminders/send-test", data={"test_email": "endereco-invalido"}
            )
        self.assertEqual(invalid_email.status_code, 302)
        invalid_send.assert_not_called()
        response = self.client.post(
            "/finance/reminders/settings",
            data={
                "enabled": "1",
                # O agendamento mensal aceita dias de 1 a 28 para funcionar
                # inclusive em fevereiro; o teste deve continuar válido nos
                # dias 29, 30 e 31 do mês corrente.
                "schedule_day": str(min(local_today().day, 28)),
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
            cron_day = local_today().replace(day=min(local_today().day, 28))
            with patch("src.routes.finance.local_today", return_value=cron_day):
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

    def test_weekly_tribute_cron_requires_secret_and_runs_on_scheduled_days(self):
        unauthorized = self.client.get("/cron/weekly-tribute")
        self.assertEqual(unauthorized.status_code, 401)

        with patch("src.routes.finance.datetime") as clock, patch(
            "src.routes.finance.send_weekly_tribute_notifications", return_value=3
        ) as send_mock:
            clock.now.return_value = datetime(2026, 8, 5, 17, 0)
            response = self.client.get(
                "/cron/weekly-tribute",
                headers={"Authorization": "Bearer cron-secret-test"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["sent"], 3)
        send_mock.assert_called_once()

        with patch("src.routes.finance.datetime") as clock, patch(
            "src.routes.finance.send_weekly_tribute_notifications", return_value=4
        ) as send_mock:
            clock.now.return_value = datetime(2026, 8, 8, 15, 7)
            response = self.client.get(
                "/cron/weekly-tribute",
                headers={"Authorization": "Bearer cron-secret-test"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["sent"], 4)
        send_mock.assert_called_once()

        with patch("src.routes.finance.datetime") as clock, patch(
            "src.routes.finance.send_weekly_tribute_notifications"
        ) as send_mock:
            clock.now.return_value = datetime(2026, 8, 8, 14, 7)
            response = self.client.get(
                "/cron/weekly-tribute",
                headers={"Authorization": "Bearer cron-secret-test"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["sent"], 0)
        send_mock.assert_not_called()

        with patch("src.routes.finance.datetime") as clock, patch(
            "src.routes.finance.send_weekly_tribute_notifications"
        ) as send_mock:
            clock.now.return_value = datetime(2026, 8, 3, 17, 0)
            response = self.client.get(
                "/cron/weekly-tribute",
                headers={"Authorization": "Bearer cron-secret-test"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["sent"], 0)
        send_mock.assert_not_called()

    def test_weekly_tribute_workflow_uses_sao_paulo_schedule(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/weekly-tribute.yml").read_text()
        self.assertIn('cron: "7 17 * * 3"', workflow)
        self.assertIn('cron: "7 15 * * 6"', workflow)
        self.assertEqual(workflow.count('timezone: "America/Sao_Paulo"'), 2)
        self.assertNotIn('cron: "0 * * * *"', workflow)

    def test_weekly_tribute_accepts_dedicated_hashed_secret(self):
        dedicated_secret = "segredo-exclusivo-da-homenagem"
        with app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO app_settings(key,value) VALUES(?,?)",
                ("weekly_tribute_cron_secret_hash", hashlib.sha256(dedicated_secret.encode()).hexdigest()),
            )
            db.commit()
        with patch("src.routes.finance.datetime") as clock, patch(
            "src.routes.finance.send_weekly_tribute_notifications", return_value=2
        ) as send_mock:
            clock.now.return_value = datetime(2026, 8, 5, 17, 7)
            response = self.client.get(
                "/cron/weekly-tribute",
                headers={"Authorization": f"Bearer {dedicated_secret}"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["sent"], 2)
        send_mock.assert_called_once()

    def test_manager_sends_tribute_test_to_only_one_active_player(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        with patch("src.routes.football.send_player_push", return_value={"sent": 1, "skipped": 0}) as send_mock:
            response = self.client.post(
                "/futebol/notificacoes/testar-homenagem",
                data={"player_id": str(self.player_id)},
            )
        self.assertEqual(response.status_code, 302)
        send_mock.assert_called_once_with(
            unittest.mock.ANY,
            self.player_id,
            "PELADEIROS GPCTA",
            "🗣️ VEEENHAAAMMM...",
            "/notificacoes",
            "/futebol/notificacoes/homenagem/imagem",
            True,
            True,
            "🗣️ VEEENHAAAMMM...",
        )
        with app.app_context():
            saved = get_db().execute(
                "SELECT audience,sent_count FROM push_announcements ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(saved["audience"], f"player:{self.player_id}")
            self.assertEqual(saved["sent_count"], 1)

    def test_tribute_test_rejects_invalid_player(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        with patch("src.routes.football.send_player_push") as send_mock:
            response = self.client.post(
                "/futebol/notificacoes/testar-homenagem",
                data={"player_id": "999999"},
            )
        self.assertEqual(response.status_code, 302)
        send_mock.assert_not_called()

    def test_manager_configures_tribute_schedule_and_sanitizes_rich_text(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        response = self.client.post(
            "/futebol/notificacoes/homenagem/configuracao",
            data={
                "tribute_enabled": "1", "tribute_title": "Convocação",
                "tribute_body_html": "<strong>Venham</strong><script>alert(1)</script><big>agora</big>",
                "day_1": "1", "hour_1": "19",
            },
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            db = get_db(); settings = db.execute("SELECT * FROM tribute_settings WHERE id=1").fetchone()
            schedule = db.execute("SELECT * FROM tribute_schedules WHERE weekday=1").fetchone()
            self.assertEqual(settings["title"], "Convocação")
            self.assertEqual(settings["body"], "Venhamagora")
            self.assertNotIn("script", settings["body_html"])
            self.assertEqual((schedule["enabled"], schedule["hour"]), (1, 19))

    def test_tribute_image_is_in_push_payload_and_inbox(self):
        with app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO push_subscriptions(player_id,endpoint,p256dh,auth) VALUES(?,?,?,?)",
                (self.player_id, "https://push.example/test", "key", "auth"),
            )
            db.commit()
            webpush_mock = unittest.mock.Mock()
            pywebpush_module = ModuleType("pywebpush")
            pywebpush_module.webpush = webpush_mock
            with patch.dict(os.environ, {"VAPID_PRIVATE_KEY": "test-key"}), patch.dict(
                sys.modules, {"pywebpush": pywebpush_module}
            ):
                result = send_player_push(
                    db,
                    self.player_id,
                    "Teste da homenagem",
                    "🗣️ VEEENHAAAMMM...",
                    "/notificacoes",
                    "/static/images/veeenhaaammm.png",
                    True,
                    True,
                )
            self.assertEqual(result["sent"], 1)
            payload = json.loads(webpush_mock.call_args.kwargs["data"])
            self.assertEqual(payload["web_push"], 8030)
            self.assertEqual(payload["notification"]["title"], "Teste da homenagem")
            self.assertEqual(payload["notification"]["navigate"], "/notificacoes")
            self.assertIs(payload["notification"]["silent"], False)
            self.assertEqual(
                payload["notification"]["image"],
                "/static/images/veeenhaaammm.png",
            )
            inbox = db.execute(
                "SELECT image_url FROM push_inbox WHERE player_id=? ORDER BY id DESC LIMIT 1",
                (self.player_id,),
            ).fetchone()
            self.assertEqual(inbox["image_url"], "/static/images/veeenhaaammm.png")
            subscription = db.execute(
                "SELECT last_push_status,last_push_at FROM push_subscriptions WHERE player_id=?",
                (self.player_id,),
            ).fetchone()
            self.assertEqual(subscription["last_push_status"], "accepted")
            self.assertIsNotNone(subscription["last_push_at"])

    def test_stock_page_groups_intelligent_alerts(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        with app.app_context():
            db = get_db()
            db.execute(
                "UPDATE products SET stock=1,min_stock=2,expiry_date=? WHERE id=?",
                ((local_today() + timedelta(days=5)).isoformat(), self.product_id),
            )
            db.commit()
        page = self.client.get("/stock").get_data(as_text=True)
        self.assertIn("Alertas inteligentes", page)
        self.assertIn("Estoque baixo", page)
        self.assertIn("Próximos do vencimento", page)
        self.assertIn("5 dia(s)", page)

    def test_manager_downloads_monthly_sales_accountability_pdf(self):
        month = local_today().strftime("%Y-%m")
        with app.app_context():
            db = get_db()
            for method, total, paid_time, quantity in (
                ("Dinheiro", 600, f"{month}-10 15:00:00", 2),
                ("Pix", 300, f"{month}-11 15:00:00", 1),
                ("Pix", 300, f"{month}-12 15:00:00", 1),
                ("Cortesia", 300, f"{month}-13 15:00:00", 1),
            ):
                sale = db.execute(
                    """INSERT INTO sales(player_id,payment_method,total_cents,paid,paid_at)
                    VALUES(?,?,?,?,?)""",
                    (self.player_id, method, total, 1, paid_time),
                )
                db.execute(
                    """INSERT INTO sale_items
                    (sale_id,product_id,quantity,unit_price_cents,unit_cost_cents)
                    VALUES(?,?,?,?,?)""",
                    (sale.lastrowid, self.product_id, quantity, 300, 100),
                )
            db.commit()
            data = monthly_sales_data(db, month)
            self.assertEqual(
                (data["summary"]["revenue"], data["summary"]["sales_count"], data["summary"]["items_sold"], data["summary"]["profit"]),
                (1200, 3, 4, 800),
            )
            self.assertEqual((data["most_used_payment"], data["summary"]["courtesy_items"]), ("Pix", 1))
            self.assertEqual(
                (data["consumers"][0]["name"], data["consumers"][0]["purchases"], data["consumers"][0]["items"], data["consumers"][0]["total"]),
                ("Peladeiro", 3, 4, 1200),
            )

        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        page = self.client.get(f"/reports?month={month}").get_data(as_text=True)
        self.assertIn("PDF de vendas mensais", page)
        report = self.client.get(f"/reports/monthly-sales.pdf?month={month}")
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.mimetype, "application/pdf")
        self.assertTrue(report.data.startswith(b"%PDF-"))
        self.assertIn(f"vendas-mensais-{month}.pdf", report.headers["Content-Disposition"])

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

    def test_cash_order_waits_for_staff_payment_delivery_or_cancel(self):
        with app.app_context():
            db = get_db()
            cursor = db.execute(
                "INSERT INTO users(username,name,password_hash,role,password_required) VALUES(?,?,?,'client',0)",
                ("peladeiro.caixa", "Peladeiro Caixa", "hash"),
            )
            client_id = cursor.lastrowid
            db.commit()
        with self.client.session_transaction() as session:
            session["user_id"] = client_id

        created = self.client.post(
            "/sale",
            data={
                "player_id": str(self.player_id),
                "product_id": [str(self.product_id)],
                "quantity": ["2"],
                "payment_method": "Dinheiro",
                "notes": "Precisa de troco.",
            },
        )
        self.assertEqual(created.status_code, 303)
        with app.app_context():
            db = get_db()
            cash_sale = db.execute(
                "SELECT * FROM sales WHERE payment_method='Dinheiro' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            sale_id = cash_sale["id"]
            self.assertEqual(
                (cash_sale["paid"], cash_sale["payment_status"], cash_sale["ready_for_delivery"]),
                (0, "pending_cash", 1),
            )
            self.assertEqual(
                db.execute("SELECT stock FROM products WHERE id=?", (self.product_id,)).fetchone()["stock"],
                3,
            )

        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        orders_page = self.client.get("/orders").get_data(as_text=True)
        self.assertIn("Confirmar pagamento e entregar", orders_page)
        self.assertIn("Cancelar", orders_page)
        feed = self.client.get("/orders/feed", headers={"Accept": "application/json"}).get_json()
        self.assertEqual(len(feed["pending"]), 1)
        self.assertEqual(
            (feed["pending"][0]["id"], feed["pending"][0]["waiting_cash"], feed["pending"][0]["notes"]),
            (sale_id, True, "Precisa de troco."),
        )

        delivered = self.client.post(f"/orders/{sale_id}/deliver", headers={"Accept": "application/json"})
        self.assertEqual(delivered.status_code, 200)
        with app.app_context():
            sale = get_db().execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
            self.assertEqual((sale["paid"], sale["payment_status"]), (1, "approved"))
            self.assertIsNotNone(sale["paid_at"])
            self.assertIsNotNone(sale["delivered_at"])

        with self.client.session_transaction() as session:
            session["user_id"] = client_id
        self.client.post(
            "/sale",
            data={
                "player_id": str(self.player_id),
                "product_id": [str(self.product_id)],
                "quantity": ["1"],
                "payment_method": "Dinheiro",
                "notes": "Pedido a cancelar.",
            },
        )
        with app.app_context():
            db = get_db()
            canceled_id = db.execute("SELECT MAX(id) FROM sales").fetchone()[0]
            self.assertEqual(db.execute("SELECT stock FROM products WHERE id=?", (self.product_id,)).fetchone()["stock"], 2)
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        canceled = self.client.post(f"/orders/{canceled_id}/cancel", headers={"Accept": "application/json"})
        self.assertEqual(canceled.status_code, 200)
        repeated = self.client.post(f"/orders/{canceled_id}/cancel", headers={"Accept": "application/json"})
        self.assertEqual(repeated.status_code, 409)
        with app.app_context():
            db = get_db()
            sale = db.execute("SELECT * FROM sales WHERE id=?", (canceled_id,)).fetchone()
            self.assertEqual((sale["paid"], sale["payment_status"], sale["ready_for_delivery"]), (0, "canceled", 0))
            self.assertEqual(db.execute("SELECT stock FROM products WHERE id=?", (self.product_id,)).fetchone()["stock"], 3)

    def test_cash_register_reconciles_sales_movements_reversal_and_closing(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id

        opened = self.client.post(
            "/cash/open", data={"opening_cash": "100,00", "opening_bank": "500,00"}
        )
        self.assertEqual(opened.status_code, 303)
        today = local_today().isoformat()
        with app.app_context():
            db = get_db()
            db.execute(
                """INSERT INTO sales(player_id,payment_method,total_cents,paid,paid_at)
                VALUES(?, 'Dinheiro',300,1,?)""",
                (self.player_id, f"{today} 15:00:00"),
            )
            db.execute(
                """INSERT INTO sales(player_id,payment_method,total_cents,paid,paid_at)
                VALUES(?, 'Pix',500,1,?)""",
                (self.player_id, f"{today} 15:01:00"),
            )
            db.execute(
                """INSERT INTO sales(player_id,payment_method,total_cents,paid,paid_at)
                VALUES(?, 'Cortesia',900,1,?)""",
                (self.player_id, f"{today} 15:02:00"),
            )
            db.commit()

        movement_response = self.client.post(
            "/cash/movements",
            data={
                "account": "cash",
                "direction": "out",
                "category": "expense",
                "amount": "2,00",
                "description": "Compra de gelo",
            },
        )
        self.assertEqual(movement_response.status_code, 303)
        with app.app_context():
            db = get_db()
            cash_session = get_session(db)
            summary = session_summary(db, cash_session)
            self.assertEqual(
                (summary["cash_sales"], summary["bank_sales"], summary["expected_cash"], summary["expected_bank"]),
                (300, 500, 10100, 50500),
            )
            movement_id = db.execute(
                "SELECT id FROM cash_movements WHERE description='Compra de gelo'"
            ).fetchone()["id"]

        reversed_response = self.client.post(f"/cash/movements/{movement_id}/reverse")
        self.assertEqual(reversed_response.status_code, 303)
        with app.app_context():
            db = get_db()
            cash_session = get_session(db)
            self.assertEqual(session_summary(db, cash_session)["expected_cash"], 10300)

        page = self.client.get("/cash").get_data(as_text=True)
        self.assertIn("Dinheiro físico esperado", page)
        self.assertIn("R$ 103,00", page)
        self.assertIn("Estornado", page)

        closed = self.client.post("/cash/close", data={"counted_cash": "103,00", "counted_bank": "504,00", "closing_notes": "Conferido."})
        self.assertEqual(closed.status_code, 303)
        with app.app_context():
            db = get_db()
            cash_session = get_session(db)
            self.assertEqual(cash_session["status"], "closed")
            self.assertEqual((cash_session["expected_cash_cents"], cash_session["expected_bank_cents"], cash_session["cash_difference_cents"], cash_session["bank_difference_cents"]), (10300, 50500, 0, -100))
            movement_count = db.execute("SELECT COUNT(*) total FROM cash_movements").fetchone()["total"]
        rejected = self.client.post("/cash/movements", data={"account": "cash", "direction": "in", "category": "other", "amount": "1,00", "description": "Tardio"})
        self.assertEqual(rejected.status_code, 303)
        with app.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) total FROM cash_movements").fetchone()["total"], movement_count)

    def test_cash_sales_are_paginated_by_ten(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        self.client.post("/cash/open", data={"opening_cash": "0,00", "opening_bank": "0,00"})
        with app.app_context():
            db = get_db()
            today = local_today().isoformat()
            for amount in range(1, 13):
                db.execute(
                    """INSERT INTO sales(player_id,payment_method,total_cents,paid,paid_at)
                       VALUES(?, 'Dinheiro', ?, 1, ?)""",
                    (self.player_id, amount * 100, f"{today} 10:{amount:02d}:00"),
                )
            db.commit()
        first = self.client.get("/cash").get_data(as_text=True)
        second = self.client.get("/cash?sales_page=2").get_data(as_text=True)
        self.assertIn("12 venda(s)", first)
        self.assertIn('aria-label="Paginação das vendas"', first)
        self.assertIn("#12", first)
        self.assertIn("#2", second)

    def test_staff_operates_cash_register_without_receiving_financial_balances(self):
        yesterday = (local_today() - timedelta(days=1)).isoformat()
        with app.app_context():
            db = get_db()
            staff = db.execute(
                """INSERT INTO users(username,name,password_hash,role)
                VALUES('atendente-caixa','Atendente Caixa','hash','staff')"""
            )
            staff_id = staff.lastrowid
            db.execute(
                """INSERT INTO cash_sessions
                (business_date,opening_cash_cents,opening_bank_cents,status,opened_by,
                 counted_cash_cents,counted_bank_cents,expected_cash_cents,expected_bank_cents,
                 cash_difference_cents,bank_difference_cents,closed_by,closed_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                (yesterday, 10000, 60000, "closed", self.user_id, 12345, 67890,
                 12345, 67890, 0, 0, self.user_id),
            )
            db.commit()

        with self.client.session_transaction() as session:
            session["user_id"] = staff_id

        opening_page = self.client.get("/cash").get_data(as_text=True)
        self.assertIn("Abrir caixa de hoje", opening_page)
        for private_text in (
            "Dinheiro físico inicial", "Saldo inicial em conta", "Histórico avançado",
            "PDF deste dia", "R$ 123,45", "R$ 678,90",
        ):
            self.assertNotIn(private_text, opening_page)

        opened = self.client.post(
            "/cash/open", data={"opening_cash": "9999,99", "opening_bank": "9999,99"}
        )
        self.assertEqual(opened.status_code, 303)
        with app.app_context():
            cash_session = get_session(get_db())
            self.assertEqual(
                (cash_session["opening_cash_cents"], cash_session["opening_bank_cents"]),
                (12345, 67890),
            )

        open_page = self.client.get("/cash").get_data(as_text=True)
        self.assertIn("Encerrar caixa", open_page)
        for private_text in (
            "Dinheiro físico esperado", "Conta bancária / Pix esperada",
            "Nova entrada ou saída", "Transferir entre contas", "Vendas contabilizadas",
            "R$ 123,45", "R$ 678,90",
        ):
            self.assertNotIn(private_text, open_page)

        self.assertEqual(self.client.get("/cash/history").status_code, 302)
        self.assertEqual(self.client.get("/cash/history.pdf").status_code, 302)
        self.assertEqual(
            self.client.post(
                "/cash/movements",
                data={"account": "cash", "direction": "in", "category": "other",
                      "amount": "1,00", "description": "Tentativa"},
            ).status_code,
            302,
        )
        self.assertEqual(
            self.client.post(
                "/cash/transfers",
                data={"from_account": "cash", "to_account": "bank", "amount": "1,00"},
            ).status_code,
            302,
        )

        closed = self.client.post(
            "/cash/close", data={"counted_cash": "9999,99", "counted_bank": "9999,99"}
        )
        self.assertEqual(closed.status_code, 303)
        with app.app_context():
            cash_session = get_session(get_db())
            session_id = cash_session["id"]
            self.assertEqual(cash_session["status"], "closed")
            self.assertIsNone(cash_session["counted_cash_cents"])
            self.assertIsNone(cash_session["counted_bank_cents"])
            self.assertEqual(
                (cash_session["expected_cash_cents"], cash_session["expected_bank_cents"]),
                (12345, 67890),
            )

        closed_page = self.client.get("/cash").get_data(as_text=True)
        self.assertIn("Aguardando conferência", closed_page)
        self.assertNotIn("R$ 123,45", closed_page)
        self.assertNotIn("R$ 678,90", closed_page)

        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        manager_page = self.client.get("/cash").get_data(as_text=True)
        self.assertIn("Concluir conferência financeira", manager_page)
        self.assertIn("R$ 123,45", manager_page)
        self.assertIn("R$ 678,90", manager_page)
        self.assertIn("Pendente", self.client.get("/cash/history").get_data(as_text=True))
        pending_pdf = self.client.get("/cash/history.pdf")
        self.assertEqual(pending_pdf.status_code, 200)
        self.assertTrue(pending_pdf.data.startswith(b"%PDF-"))
        reconciled = self.client.post(
            f"/cash/{session_id}/reconcile",
            data={"counted_cash": "120,00", "counted_bank": "680,00", "closing_notes": "Conferido pelo gerente."},
        )
        self.assertEqual(reconciled.status_code, 303)
        with app.app_context():
            cash_session = get_session(get_db())
            self.assertEqual(
                (cash_session["counted_cash_cents"], cash_session["counted_bank_cents"],
                 cash_session["cash_difference_cents"], cash_session["bank_difference_cents"]),
                (12000, 68000, -345, 110),
            )

    def test_finance_and_bar_keep_separate_balances_with_audited_transfers(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id

        setup_page = self.client.get("/finance/ledger").get_data(as_text=True)
        self.assertIn("Informe o patrimônio atual do Financeiro", setup_page)
        initialized = self.client.post(
            "/finance/ledger/initialize",
            data={"opening_cash": "200,00", "opening_bank": "1.000,00"},
        )
        self.assertEqual(initialized.status_code, 303)
        self.client.post("/cash/open", data={"opening_cash": "100,00", "opening_bank": "500,00"})

        fundraising = self.client.post(
            "/finance/ledger/movements",
            data={"account": "bank", "direction": "in", "category": "fundraising",
                  "amount": "300,00", "description": "Arrecadação do evento"},
        )
        expense = self.client.post(
            "/finance/ledger/movements",
            data={"account": "cash", "direction": "out", "category": "expense",
                  "amount": "50,00", "description": "Material administrativo"},
        )
        self.assertEqual((fundraising.status_code, expense.status_code), (303, 303))

        membership = self.client.post(
            "/finance",
            data={"player_id": self.player_id, "start_month": local_today().strftime("%Y-%m"),
                  "months_count": "1", "payment_method": "Pix", "notes": "Recebido"},
        )
        self.assertEqual(membership.status_code, 302)
        with app.app_context():
            db = get_db()
            payment_id = db.execute("SELECT id FROM membership_payments").fetchone()["id"]
            membership_entry = db.execute(
                "SELECT * FROM finance_movements WHERE source='membership' AND source_id=?",
                (payment_id,),
            ).fetchone()
            self.assertEqual(
                (membership_entry["account"], membership_entry["direction"], membership_entry["amount_cents"]),
                ("bank", "in", 1500),
            )

        to_bar = self.client.post(
            "/finance/ledger/transfers",
            data={"direction": "finance_to_bar", "amount": "250,00", "description": "Aporte para estoque"},
        )
        to_finance = self.client.post(
            "/finance/ledger/transfers",
            data={"direction": "bar_to_finance", "amount": "100,00", "description": "Devolução de aporte"},
        )
        self.assertEqual((to_bar.status_code, to_finance.status_code), (303, 303))

        with app.app_context():
            db = get_db()
            from src.services.finance_accounts import finance_summary, latest_bar_balances
            finance_balances = finance_summary(db)
            bar_balances = latest_bar_balances(db)
            self.assertEqual(
                (finance_balances["cash"], finance_balances["bank"], bar_balances["bank"]),
                (15000, 116500, 65000),
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM interaccount_transfers").fetchone()[0], 2)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM cash_movements WHERE source='finance_transfer'").fetchone()[0],
                2,
            )

        accounts_page = self.client.get("/finance/ledger").get_data(as_text=True)
        self.assertIn("Banco do Financeiro", accounts_page)
        self.assertIn("R$ 1.165,00", accounts_page)
        self.assertIn("R$ 1.815,00", accounts_page)
        self.assertIn("Aporte para estoque", accounts_page)
        self.assertIn("Histórico do Livro-caixa", accounts_page)
        filtered = self.client.get("/finance/ledger?account=bank&category=fundraising&q=evento")
        self.assertEqual(filtered.status_code, 200)
        self.assertIn("Arrecadação do evento", filtered.get_data(as_text=True))
        ledger_pdf = self.client.get("/finance/ledger.pdf?account=bank")
        self.assertEqual(ledger_pdf.status_code, 200)
        self.assertTrue(ledger_pdf.data.startswith(b"%PDF-"))
        self.assertIn("livro-caixa-financeiro-", ledger_pdf.headers["Content-Disposition"])

        deleted = self.client.post(f"/finance/{payment_id}/delete")
        self.assertEqual(deleted.status_code, 302)
        with app.app_context():
            db = get_db()
            from src.services.finance_accounts import finance_summary
            self.assertEqual(finance_summary(db)["bank"], 115000)
            reversal = db.execute(
                "SELECT * FROM finance_movements WHERE source='membership_reversal' AND source_id=?",
                (payment_id,),
            ).fetchone()
            self.assertEqual((reversal["direction"], reversal["amount_cents"]), ("out", 1500))

        transfer_reversal = self.client.post(
            "/finance/ledger/transfers/1/reverse",
            data={"reason": "Lançamento duplicado no aporte"},
        )
        self.assertEqual(transfer_reversal.status_code, 303)
        with app.app_context():
            db = get_db()
            from src.services.finance_accounts import finance_summary, latest_bar_balances
            self.assertEqual(finance_summary(db)["bank"], 140000)
            self.assertEqual(latest_bar_balances(db)["bank"], 40000)
            transfer = db.execute("SELECT * FROM interaccount_transfers WHERE id=1").fetchone()
            self.assertIsNotNone(transfer["reversed_at"])
            finance_reversal = db.execute(
                "SELECT * FROM finance_movements WHERE source='interaccount_transfer_reversal' AND source_id=1"
            ).fetchone()
            self.assertEqual((finance_reversal["direction"], finance_reversal["amount_cents"]), ("in", 25000))
            cash_reversal = db.execute(
                "SELECT * FROM cash_movements WHERE source='finance_transfer_reversal' AND source_id=1"
            ).fetchone()
            self.assertEqual((cash_reversal["direction"], cash_reversal["amount_cents"]), ("out", 25000))
        audit_page = self.client.get("/finance/ledger").get_data(as_text=True)
        self.assertIn("Lançamento duplicado no aporte", audit_page)
        self.assertIn("Estornado", audit_page)

        with app.app_context():
            db = get_db()
            staff = db.execute(
                "INSERT INTO users(username,name,password_hash,role) VALUES('staff-finance','Staff','hash','staff')"
            )
            db.commit()
            staff_id = staff.lastrowid
        with self.client.session_transaction() as session:
            session["user_id"] = staff_id
        self.assertEqual(self.client.get("/finance/ledger").status_code, 302)
        self.assertEqual(
            self.client.post(
                "/finance/ledger/movements",
                data={"account": "bank", "direction": "in", "category": "other",
                      "amount": "999,00", "description": "Tentativa"},
            ).status_code,
            302,
        )

    def test_manager_corrects_restock_with_audit_trail(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        created = self.client.post(
            "/stock",
            data={
                "product_id": self.product_id,
                "quantity": 10,
                "cases": 0,
                "unit_cost": "2,00",
                "notes": "Entrada digitada errada",
            },
        )
        self.assertEqual(created.status_code, 302)
        with app.app_context():
            db = get_db()
            restock_id = db.execute("SELECT MAX(id) id FROM restocks").fetchone()["id"]
            self.assertEqual(db.execute("SELECT stock FROM products WHERE id=?", (self.product_id,)).fetchone()["stock"], 15)

        corrected = self.client.post(
            f"/stock/restocks/{restock_id}/correct",
            data={"quantity": 6, "unit_cost": "1,50", "reason": "Quantidade e custo digitados errados"},
        )
        self.assertEqual(corrected.status_code, 303)
        with app.app_context():
            db = get_db()
            product = db.execute("SELECT * FROM products WHERE id=?", (self.product_id,)).fetchone()
            original = db.execute("SELECT * FROM restocks WHERE id=?", (restock_id,)).fetchone()
            correction = db.execute("SELECT * FROM restock_corrections WHERE restock_id=?", (restock_id,)).fetchone()
            self.assertEqual((product["stock"], product["cost_cents"]), (11, 150))
            self.assertEqual((original["quantity"], original["unit_cost_cents"]), (10, 200))
            self.assertEqual(
                (correction["previous_quantity"], correction["corrected_quantity"], correction["previous_unit_cost_cents"], correction["corrected_unit_cost_cents"]),
                (10, 6, 200, 150),
            )

        corrected_again = self.client.post(
            f"/stock/restocks/{restock_id}/correct",
            data={"quantity": 7, "unit_cost": "1,75", "reason": "Recontagem feita pelo gerente"},
        )
        self.assertEqual(corrected_again.status_code, 303)
        with app.app_context():
            db = get_db()
            product = db.execute("SELECT * FROM products WHERE id=?", (self.product_id,)).fetchone()
            latest = db.execute("SELECT * FROM restock_corrections ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual((product["stock"], product["cost_cents"]), (12, 175))
            self.assertEqual((latest["previous_quantity"], latest["corrected_quantity"]), (6, 7))
            self.assertEqual(db.execute("SELECT COUNT(*) total FROM restock_corrections").fetchone()["total"], 2)

        page = self.client.get("/stock").get_data(as_text=True)
        self.assertIn("Corrigida", page)
        self.assertIn("Original: 10 un.", page)
        self.assertIn("Recontagem feita pelo gerente", page)

        with app.app_context():
            db = get_db()
            staff = db.execute(
                "INSERT INTO users(username,name,password_hash,role) VALUES(?,?,?,'staff')",
                ("staff.estoque", "Staff Estoque", "hash"),
            ).lastrowid
            db.commit()
        with self.client.session_transaction() as session:
            session["user_id"] = staff
        denied = self.client.get(f"/stock/restocks/{restock_id}/correct")
        self.assertEqual(denied.status_code, 302)

    def test_paid_stock_purchase_creates_atomic_cash_outflow(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id

        rejected = self.client.post(
            "/stock",
            data={
                "product_id": self.product_id,
                "quantity": 3,
                "cases": 0,
                "unit_cost": "2,00",
                "payment_account": "bank",
                "notes": "Sem caixa",
            },
        )
        self.assertEqual(rejected.status_code, 302)
        with app.app_context():
            db = get_db()
            self.assertEqual(db.execute("SELECT stock FROM products WHERE id=?", (self.product_id,)).fetchone()["stock"], 5)
            self.assertEqual(db.execute("SELECT COUNT(*) total FROM restocks").fetchone()["total"], 0)

        self.client.post("/cash/open", data={"opening_cash": "0,00", "opening_bank": "100,00"})
        accepted = self.client.post(
            "/stock",
            data={
                "product_id": self.product_id,
                "quantity": 3,
                "cases": 0,
                "unit_cost": "2,00",
                "payment_account": "bank",
                "notes": "Compra paga por Pix",
            },
        )
        self.assertEqual(accepted.status_code, 302)
        with app.app_context():
            db = get_db()
            self.assertEqual(db.execute("SELECT stock FROM products WHERE id=?", (self.product_id,)).fetchone()["stock"], 8)
            movement = db.execute("SELECT * FROM cash_movements WHERE source='restock'").fetchone()
            self.assertEqual(
                (movement["account"], movement["direction"], movement["category"], movement["amount_cents"]),
                ("bank", "out", "purchase", 600),
            )
            summary = session_summary(db, get_session(db))
            self.assertEqual(summary["expected_bank"], 9400)

    def test_stock_report_pdf_lists_current_stock_entries_and_exits(self):
        from src.services.stock_report_pdf import stock_report_data

        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        with app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO restocks(product_id,quantity,unit_cost_cents,notes,created_at) VALUES(?,?,?,?,?)",
                (self.product_id, 4, 100, "Entrada de teste", "2026-07-10 12:00:00"),
            )
            sale = db.execute(
                "INSERT INTO sales(player_id,payment_method,total_cents,paid,payment_status,paid_at,created_at) VALUES(?,?,?,?,?,?,?)",
                (self.player_id, "Dinheiro", 600, 1, "approved", "2026-07-11 12:00:00", "2026-07-11 12:00:00"),
            )
            db.execute("INSERT INTO sale_items(sale_id,product_id,quantity,unit_price_cents,unit_cost_cents) VALUES(?,?,?,?,?)",
                       (sale.lastrowid, self.product_id, 2, 300, 100))
            db.commit()
            rows = stock_report_data(db, "2026-07-01", "2026-07-31")
            agua = next(row for row in rows if row["name"] == "Água")
            self.assertEqual((agua["stock"], agua["entries"], agua["exits"], agua["net"]), (5, 4, 2, 2))

        response = self.client.get("/stock/report.pdf?start=2026-07-01&end=2026-07-31")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertIn("relatorio-estoque-", response.headers["Content-Disposition"])
        self.assertTrue(response.data.startswith(b"%PDF-"))

    def test_monthly_stock_conference_requires_reason_and_preserves_expected_stock(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id

        invalid = self.client.post(
            "/stock/conference",
            data={
                "conference_month": "2026-08",
                f"physical_{self.product_id}": "3",
                f"reason_{self.product_id}": "",
            },
        )
        self.assertEqual(invalid.status_code, 200)
        self.assertIn("Informe o motivo da diferença", invalid.get_data(as_text=True))
        with app.app_context():
            db = get_db()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM stock_conferences").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT stock FROM products WHERE id=?", (self.product_id,)).fetchone()["stock"], 5)

        created = self.client.post(
            "/stock/conference",
            data={
                "conference_month": "2026-08",
                "notes": "Fechamento do bar",
                f"physical_{self.product_id}": "3",
                f"reason_{self.product_id}": "Perda registrada",
            },
        )
        self.assertEqual(created.status_code, 302)
        with app.app_context():
            db = get_db()
            conference = db.execute("SELECT * FROM stock_conferences WHERE conference_month='2026-08'").fetchone()
            item = db.execute("SELECT * FROM stock_conference_items WHERE conference_id=?", (conference["id"],)).fetchone()
            audit = db.execute("SELECT action FROM stock_conference_audit WHERE conference_month='2026-08'").fetchone()
            self.assertEqual((item["expected_stock"], item["physical_stock"], item["difference"], item["reason"]), (5, 3, -2, "Perda registrada"))
            self.assertEqual(audit["action"], "REGISTRADA")
            self.assertEqual(db.execute("SELECT stock FROM products WHERE id=?", (self.product_id,)).fetchone()["stock"], 5)

        duplicate = self.client.post(
            "/stock/conference",
            data={"conference_month": "2026-08", f"physical_{self.product_id}": "5"},
            follow_redirects=True,
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertIn("Já existe uma conferência", duplicate.get_data(as_text=True))

    def test_manager_can_remove_conference_with_audit_and_staff_cannot(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        created = self.client.post(
            "/stock/conference",
            data={"conference_month": "2026-09", f"physical_{self.product_id}": "5"},
        )
        self.assertEqual(created.status_code, 302)
        with app.app_context():
            conference_id = get_db().execute("SELECT id FROM stock_conferences WHERE conference_month='2026-09'").fetchone()["id"]
            db = get_db()
            db.execute("UPDATE users SET role='staff' WHERE id=?", (self.user_id,))
            db.commit()
        denied = self.client.post(f"/stock/conference/{conference_id}/delete")
        self.assertIn(denied.status_code, (302, 403))
        with app.app_context():
            db = get_db()
            self.assertIsNotNone(db.execute("SELECT id FROM stock_conferences WHERE id=?", (conference_id,)).fetchone())
            db.execute("UPDATE users SET role='manager' WHERE id=?", (self.user_id,))
            db.commit()
        deleted = self.client.post(f"/stock/conference/{conference_id}/delete")
        self.assertEqual(deleted.status_code, 302)
        with app.app_context():
            db = get_db()
            self.assertIsNone(db.execute("SELECT id FROM stock_conferences WHERE id=?", (conference_id,)).fetchone())
            self.assertEqual(db.execute("SELECT action FROM stock_conference_audit WHERE conference_month='2026-09' ORDER BY id DESC").fetchone()["action"], "EXCLUIDA")

    def test_low_stock_alert_consolidates_products_for_supplier_once(self):
        with app.app_context(), patch.dict("os.environ", {
            "GMAIL_SMTP_USER": "bar@example.com",
            "GMAIL_APP_PASSWORD": "app-password",
            "STOCK_ALERT_ATTENDANT_EMAIL": "atendente@example.com",
            "STOCK_ALERT_MANAGER_EMAIL": "gerente@example.com",
        }):
            db = get_db()
            db.execute("UPDATE products SET stock=2,min_stock=2,supplier_email='fornecedor@example.com' WHERE id=?", (self.product_id,))
            sent = []
            result = notify_low_stock(db, [self.product_id], send_func=lambda *args: sent.append(args[2]))
            self.assertEqual(result["sent"], 1)
            self.assertEqual(sent, ["fornecedor@example.com"])
            again = notify_low_stock(db, [self.product_id], send_func=lambda *args: sent.append(args[2]))
            self.assertEqual(again["skipped"], 1)
            self.assertEqual(len(sent), 1)

    def test_low_stock_alert_resets_after_replenishment(self):
        with app.app_context(), patch.dict("os.environ", {
            "GMAIL_SMTP_USER": "bar@example.com",
            "GMAIL_APP_PASSWORD": "app-password",
            "STOCK_ALERT_ATTENDANT_EMAIL": "atendente@example.com",
        }):
            db = get_db()
            db.execute("UPDATE products SET stock=1,min_stock=2,supplier_email='fornecedor@example.com' WHERE id=?", (self.product_id,))
            sent = []
            notify_low_stock(db, [self.product_id], send_func=lambda *args: sent.append(args[2]))
            db.execute("UPDATE products SET stock=5 WHERE id=?", (self.product_id,))
            notify_low_stock(db, [self.product_id], send_func=lambda *args: sent.append(args[2]))
            db.execute("UPDATE products SET stock=2 WHERE id=?", (self.product_id,))
            result = notify_low_stock(db, [self.product_id], send_func=lambda *args: sent.append(args[2]))
            self.assertEqual(result["sent"], 1)
            self.assertEqual(len(sent), 2)

    def test_staff_and_manager_can_generate_low_stock_pdf(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        with app.app_context():
            db = get_db()
            db.execute("UPDATE products SET stock=1,min_stock=5,supplier_email='fornecedor@example.com' WHERE id=?", (self.product_id,))
            db.commit()
        response = self.client.get("/stock/low-report.pdf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertIn("estoque-baixo-", response.headers["Content-Disposition"])
        self.assertTrue(response.data.startswith(b"%PDF-"))
    def test_cash_transfer_history_filters_and_pdf(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        self.client.post("/cash/open", data={"opening_cash": "100,00", "opening_bank": "50,00"})

        transferred = self.client.post(
            "/cash/transfers",
            data={
                "from_account": "cash",
                "to_account": "bank",
                "amount": "30,00",
                "description": "Depósito do dinheiro das vendas",
            },
        )
        self.assertEqual(transferred.status_code, 303)
        with app.app_context():
            db = get_db()
            cash_session = get_session(db)
            summary = session_summary(db, cash_session)
            self.assertEqual((summary["expected_cash"], summary["expected_bank"]), (7000, 8000))
            transfer = db.execute("SELECT * FROM cash_transfers").fetchone()
            transfer_id = transfer["id"]
            legs = db.execute(
                "SELECT account,direction,amount_cents FROM cash_movements ORDER BY id"
            ).fetchall()
            self.assertEqual(
                [(row["account"], row["direction"], row["amount_cents"]) for row in legs],
                [("cash", "out", 3000), ("bank", "in", 3000)],
            )

        history = self.client.get(
            "/cash/history?account=cash&category=transfer&q=dep%C3%B3sito"
        )
        self.assertEqual(history.status_code, 200)
        history_html = history.get_data(as_text=True)
        self.assertIn("Histórico avançado do Caixa", history_html)
        self.assertIn("Depósito do dinheiro das vendas", history_html)
        self.assertIn("Movimentações e transferências (1)", history_html)

        pdf = self.client.get("/cash/history.pdf?category=transfer")
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.data.startswith(b"%PDF-"))
        self.assertIn("caixa-", pdf.headers["Content-Disposition"])

        reversed_transfer = self.client.post(f"/cash/transfers/{transfer_id}/reverse")
        self.assertEqual(reversed_transfer.status_code, 303)
        with app.app_context():
            db = get_db()
            summary = session_summary(db, get_session(db))
            self.assertEqual((summary["expected_cash"], summary["expected_bank"]), (10000, 5000))
            transfer = db.execute("SELECT * FROM cash_transfers WHERE id=?", (transfer_id,)).fetchone()
            self.assertIsNotNone(transfer["reversed_at"])
            self.assertEqual(db.execute("SELECT COUNT(*) total FROM cash_movements").fetchone()["total"], 4)

    def test_goal_types_control_assists_and_own_goal_statistics(self):
        with app.app_context():
            db = get_db()
            assist_player_id = db.execute(
                "INSERT INTO players(name,war_name) VALUES(?,?)",
                ("Assistente", "Garçom"),
            ).lastrowid
            sumula_id = db.execute(
                "INSERT INTO football_sumulas(match_date,day_pelada,situacao,created_by) VALUES(?,'SABADO','EM_ANDAMENTO',?)",
                ("2026-08-08", self.user_id),
            ).lastrowid
            match_id = db.execute(
                "INSERT INTO football_matches(sumula_id,number,blue_score,white_score,status) VALUES(?,1,3,0,'ENCERRADA')",
                (sumula_id,),
            ).lastrowid
            for player_id in (self.player_id, assist_player_id):
                db.execute(
                    "INSERT INTO football_participants(sumula_id,player_id,status) VALUES(?,?,'CONFIRMADO')",
                    (sumula_id, player_id),
                )
            client_user_id = db.execute(
                "INSERT INTO users(username,name,password_hash,role,player_id) VALUES(?,?,?,'client',?)",
                ("artilheiro", "Artilheiro", "hash", self.player_id),
            ).lastrowid
            db.commit()

        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id

        detail_html = self.client.get(f"/futebol/sumulas/{sumula_id}").get_data(as_text=True)
        self.assertNotIn("Gol normal", detail_html)
        self.assertNotIn('name="minute"', detail_html)
        self.assertNotIn('placeholder="Min."', detail_html)
        self.assertIn("['NORMAL','Gol']", detail_html)
        self.assertIn("['REBOTE','Rebote']", detail_html)
        self.assertIn("['FALTA','Falta']", detail_html)
        self.assertIn("['PENALTY','Penalty']", detail_html)
        self.assertIn("['ROUBADA','Roubada']", detail_html)
        self.assertIn("['CONTRA','Gol Contra']", detail_html)

        common_goal = {
            "action": "goal",
            "match_id": str(match_id),
            "author_player_id": str(self.player_id),
            "assist_player_id": str(assist_player_id),
            "benefited_team": "AZUL",
            "minute": "1",
        }
        for goal_type in ("NORMAL", "REBOTE", "CONTRA"):
            response = self.client.post(
                f"/futebol/sumulas/{sumula_id}",
                data={**common_goal, "goal_type": goal_type},
            )
            self.assertEqual(response.status_code, 302)

        with app.app_context():
            db = get_db()
            goals = db.execute(
                "SELECT goal_type,own_goal,assist_player_id FROM football_goals ORDER BY id"
            ).fetchall()
            self.assertEqual(
                [(row["goal_type"], row["own_goal"], row["assist_player_id"]) for row in goals],
                [("NORMAL", 0, assist_player_id), ("REBOTE", 0, None), ("CONTRA", 1, None)],
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM football_goals WHERE minute IS NOT NULL").fetchone()[0],
                0,
            )
            db.execute(
                "UPDATE football_sumulas SET situacao='FINALIZADA',finalized_at=CURRENT_TIMESTAMP WHERE id=?",
                (sumula_id,),
            )
            db.commit()

        statistics_html = self.client.get("/futebol/estatisticas").get_data(as_text=True)
        self.assertIn("Gols contra", statistics_html)
        self.assertRegex(statistics_html, r"Assistências registradas</small><h2>1</h2>")
        self.assertIn('<h2 class="text-danger">−1</h2>', statistics_html)
        self.assertIn('<td class="text-danger fw-semibold">−1</td>', statistics_html)
        self.assertRegex(
            statistics_html,
            r"Peladeiro</td><td>100\.0%</td><td>0</td><td>0</td><td>0</td><td>0</td><td>1</td><td class=\"text-danger fw-semibold\">−1</td><td>0</td>",
        )
        self.assertRegex(
            statistics_html,
            r"Garçom</td><td>100\.0%</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td><td class=\"text-muted\">0</td><td>1</td>",
        )

        with self.client.session_transaction() as session:
            session["user_id"] = client_user_id
        client_html = self.client.get("/futebol/minha-pelada").get_data(as_text=True)
        self.assertIn(
            '<strong class="fs-3">1</strong><small class="d-block text-muted">Gols</small>',
            client_html,
        )
        self.assertIn(
            '<strong class="fs-3">0</strong><small class="d-block text-muted">Assistências</small>',
            client_html,
        )

    def test_football_statistics_paginates_ranking_and_recent_results_independently(self):
        with app.app_context():
            db = get_db()
            for number in range(1, 17):
                player_id = db.execute(
                    "INSERT INTO players(name,war_name) VALUES(?,?)",
                    (f"Jogador paginado {number:02d}", f"Ranking {number:02d}"),
                ).lastrowid
                db.execute(
                    "INSERT INTO football_historical_stats(player_id,stat_date,goals,created_by) VALUES(?,'2026-08-08',1,?)",
                    (player_id, self.user_id),
                )
            for day in range(1, 13):
                sumula_id = db.execute(
                    "INSERT INTO football_sumulas(match_date,day_pelada,situacao,created_by) VALUES(?,'SABADO','FINALIZADA',?)",
                    (f"2026-08-{day:02d}", self.user_id),
                ).lastrowid
                db.execute(
                    "INSERT INTO football_matches(sumula_id,number,blue_score,white_score,status) VALUES(?,1,2,1,'ENCERRADA')",
                    (sumula_id,),
                )
            db.commit()

        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id

        first_page = self.client.get("/futebol/estatisticas?year=2026&month=8").get_data(as_text=True)
        self.assertEqual(first_page.count('data-ranking-row="'), 15)
        self.assertEqual(first_page.count('data-result-date="'), 10)
        self.assertIn('data-ranking-page="1"', first_page)
        self.assertIn('data-results-page="1"', first_page)
        self.assertIn('data-result-date="2026-08-12"', first_page)
        self.assertNotIn('data-result-date="2026-08-02"', first_page)

        second_page = self.client.get(
            "/futebol/estatisticas?year=2026&month=8&ranking_page=2&results_page=2"
        ).get_data(as_text=True)
        self.assertEqual(second_page.count('data-ranking-row="'), 1)
        self.assertEqual(second_page.count('data-result-date="'), 2)
        self.assertIn('data-ranking-page="2"', second_page)
        self.assertIn('data-results-page="2"', second_page)
        self.assertIn('data-result-date="2026-08-02"', second_page)
        self.assertIn('data-result-date="2026-08-01"', second_page)
        self.assertNotIn('data-result-date="2026-08-03"', second_page)

    def test_football_statistics_ranks_players_by_first_and_second_match(self):
        with app.app_context():
            db = get_db()
            rival_id = db.execute(
                "INSERT INTO players(name,war_name) VALUES(?,?)", ("Rival da partida", "Rival")
            ).lastrowid
            sumula_id = db.execute(
                "INSERT INTO football_sumulas(match_date,day_pelada,situacao,created_by) VALUES('2026-08-08','SABADO','FINALIZADA',?)",
                (self.user_id,),
            ).lastrowid
            match_1 = db.execute(
                "INSERT INTO football_matches(sumula_id,number,blue_score,white_score,status) VALUES(?,1,1,2,'ENCERRADA')",
                (sumula_id,),
            ).lastrowid
            match_2 = db.execute(
                "INSERT INTO football_matches(sumula_id,number,blue_score,white_score,status) VALUES(?,2,3,1,'ENCERRADA')",
                (sumula_id,),
            ).lastrowid
            for match_id in (match_1, match_2):
                db.execute(
                    "INSERT INTO football_lineups(match_id,player_id,team,position) VALUES(?,?,'AZUL','ATACANTE')",
                    (match_id, self.player_id),
                )
                db.execute(
                    "INSERT INTO football_lineups(match_id,player_id,team,position) VALUES(?,?,'BRANCO','DEFENSOR')",
                    (match_id, rival_id),
                )
            db.execute(
                "INSERT INTO football_goals(match_id,author_player_id,benefited_team,assist_player_id,goal_type,own_goal) VALUES(?,?,'AZUL',?,'NORMAL',0)",
                (match_1, self.player_id, rival_id),
            )
            db.execute(
                "INSERT INTO football_goals(match_id,author_player_id,benefited_team,goal_type,own_goal) VALUES(?,?,'BRANCO','CONTRA',1)",
                (match_1, self.player_id),
            )
            db.execute(
                "INSERT INTO football_goals(match_id,author_player_id,benefited_team,goal_type,own_goal) VALUES(?,?,'AZUL','NORMAL',0)",
                (match_2, self.player_id),
            )
            db.execute(
                "INSERT INTO football_goals(match_id,author_player_id,benefited_team,goal_type,own_goal) VALUES(?,?,'AZUL','CONTRA',1)",
                (match_2, rival_id),
            )
            db.commit()

        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        html = self.client.get("/futebol/estatisticas?year=2026&month=8").get_data(as_text=True)
        self.assertIn("Ranking por partida", html)
        self.assertIn('data-match-ranking="1"', html)
        self.assertIn('data-match-ranking="2"', html)
        self.assertIn(
            f'<tr data-match-ranking-row="1-{self.player_id}"><td>Peladeiro</td><td>1</td><td>1</td><td class="">0</td><td class="text-danger fw-semibold">−1</td><td>0</td></tr>',
            html,
        )
        self.assertIn(
            f'<tr data-match-ranking-row="1-{rival_id}"><td>Rival</td><td>1</td><td>0</td><td class="">0</td><td class="text-muted">0</td><td>1</td></tr>',
            html,
        )
        self.assertIn(
            f'<tr data-match-ranking-row="2-{self.player_id}"><td>Peladeiro</td><td>1</td><td>0</td><td class="">1</td><td class="text-muted">0</td><td>0</td></tr>',
            html,
        )
        self.assertIn(
            f'<tr data-match-ranking-row="2-{rival_id}"><td>Rival</td><td>1</td><td>1</td><td class="text-danger fw-semibold">-1</td><td class="text-danger fw-semibold">−1</td><td>0</td></tr>',
            html,
        )

    def test_football_statistics_paginates_each_match_ranking_independently(self):
        with app.app_context():
            db = get_db()
            player_ids = []
            for number in range(1, 13):
                player_ids.append(
                    db.execute(
                        "INSERT INTO players(name,war_name) VALUES(?,?)",
                        (f"Jogador por partida {number:02d}", f"Partida {number:02d}"),
                    ).lastrowid
                )
            sumula_id = db.execute(
                "INSERT INTO football_sumulas(match_date,day_pelada,situacao,created_by) VALUES('2026-08-08','SABADO','FINALIZADA',?)",
                (self.user_id,),
            ).lastrowid
            for match_number in (1, 2):
                match_id = db.execute(
                    "INSERT INTO football_matches(sumula_id,number,blue_score,white_score,status) VALUES(?,?,2,1,'ENCERRADA')",
                    (sumula_id, match_number),
                ).lastrowid
                for player_id in player_ids:
                    db.execute(
                        "INSERT INTO football_lineups(match_id,player_id,team,position) VALUES(?,?,'AZUL','ATACANTE')",
                        (match_id, player_id),
                    )
            db.commit()

        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id

        first_page = self.client.get(
            "/futebol/estatisticas?year=2026&month=8"
        ).get_data(as_text=True)
        self.assertEqual(first_page.count('data-match-ranking-row="1-'), 10)
        self.assertEqual(first_page.count('data-match-ranking-row="2-'), 10)
        self.assertIn('data-match-ranking-page="1-1"', first_page)
        self.assertIn('data-match-ranking-page="2-1"', first_page)

        independent_page = self.client.get(
            "/futebol/estatisticas?year=2026&month=8&match1_page=2"
        ).get_data(as_text=True)
        self.assertEqual(independent_page.count('data-match-ranking-row="1-'), 2)
        self.assertEqual(independent_page.count('data-match-ranking-row="2-'), 10)
        self.assertIn('data-match-ranking-page="1-2"', independent_page)
        self.assertIn('data-match-ranking-page="2-1"', independent_page)

        both_second_pages = self.client.get(
            "/futebol/estatisticas?year=2026&month=8&match1_page=2&match2_page=2"
        ).get_data(as_text=True)
        self.assertEqual(both_second_pages.count('data-match-ranking-row="1-'), 2)
        self.assertEqual(both_second_pages.count('data-match-ranking-row="2-'), 2)
        self.assertIn('data-match-ranking-page="1-2"', both_second_pages)
        self.assertIn('data-match-ranking-page="2-2"', both_second_pages)

    def test_transfer_window_lists_only_players_eligible_for_a_new_position(self):
        with app.app_context():
            db = get_db()
            eligible_id = db.execute(
                "INSERT INTO players(name,war_name,football_position,football_join_date) VALUES(?,?,?,?)",
                ("Apto transferência", "Apto", "DEFESA", "2025-01-01"),
            ).lastrowid
            db.execute(
                "INSERT INTO players(name,war_name,football_position,football_join_date) VALUES(?,?,?,?)",
                ("Sem tempo mínimo", "Recente", "DEFESA", local_today().isoformat()),
            )
            db.execute(
                "INSERT INTO players(name,war_name,football_position,football_join_date) VALUES(?,?,?,?)",
                ("Sem frequência", "Ausente", "DEFESA", "2025-01-01"),
            )
            db.execute(
                "INSERT INTO players(name,football_position) VALUES(?,?)",
                ("Defensor de apoio", "DEFESA"),
            )
            for index in range(3):
                db.execute(
                    "INSERT INTO players(name,football_position) VALUES(?,?)",
                    (f"Meio de apoio {index}", "MEIO"),
                )
                db.execute(
                    "INSERT INTO players(name,football_position) VALUES(?,?)",
                    (f"Atacante de apoio {index}", "ATAQUE"),
                )
            sumula_id = db.execute(
                "INSERT INTO football_sumulas(match_date,day_pelada,situacao,created_by) VALUES(?,'SABADO','FINALIZADA',?)",
                (local_today().isoformat(), self.user_id),
            ).lastrowid
            db.execute(
                "INSERT INTO football_participants(sumula_id,player_id,status) VALUES(?,?,'CONFIRMADO')",
                (sumula_id, eligible_id),
            )
            db.commit()

        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        html = self.client.get("/futebol/transferencia").get_data(as_text=True)
        self.assertIn("Peladeiros aptos à transferência", html)
        self.assertIn(f'data-eligible-transfer-player="{eligible_id}"', html)
        self.assertIn("Apto", html)
        self.assertIn("Disponível para solicitar", html)
        self.assertIn("Meio", html)
        self.assertIn("Ataque", html)
        self.assertNotIn("Recente", html)
        self.assertNotIn("Ausente", html)

    def test_client_mathematician_menu_shows_mathematical_and_zebra_percentages(self):
        with app.app_context():
            db = get_db()
            client_user_id = db.execute(
                "INSERT INTO users(username,name,password_hash,role,player_id) VALUES(?,?,?,'client',?)",
                ("matematico", "Cliente matemático", "hash", self.player_id),
            ).lastrowid
            sumula_id = db.execute(
                "INSERT INTO football_sumulas(match_date,day_pelada,situacao,created_by) VALUES('2026-08-08','SABADO','FINALIZADA',?)",
                (self.user_id,),
            ).lastrowid
            for number, blue_score, white_score in ((1, 3, 2), (2, 4, 2), (3, 5, 2)):
                db.execute(
                    "INSERT INTO football_matches(sumula_id,number,blue_score,white_score,status) VALUES(?,?,?,?,'ENCERRADA')",
                    (sumula_id, number, blue_score, white_score),
                )
            ignored_sumula = db.execute(
                "INSERT INTO football_sumulas(match_date,day_pelada,situacao,created_by) VALUES('2026-08-09','SABADO','RASCUNHO',?)",
                (self.user_id,),
            ).lastrowid
            db.execute(
                "INSERT INTO football_matches(sumula_id,number,blue_score,white_score,status) VALUES(?,1,9,0,'ENCERRADA')",
                (ignored_sumula,),
            )
            db.commit()

        with self.client.session_transaction() as session:
            session["user_id"] = client_user_id
        response = self.client.get("/futebol/e-matematico")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("É matemático!!!!", html)
        self.assertIn('href="/futebol/e-matematico"', html)
        self.assertIn("66.7%", html)
        self.assertIn("33.3%", html)
        self.assertIn("2 de 3", html)
        self.assertIn("1 de 3", html)
        self.assertEqual(html.count('data-mathematician-result="'), 3)
        self.assertIn("Matemática", html)
        self.assertIn("Zebra", html)

    def test_third_match_accepts_participant_orders_45_to_66(self):
        with app.app_context():
            db = get_db()
            occupied_player_ids = [self.player_id]
            for number in range(2, 45):
                occupied_player_ids.append(
                    db.execute(
                        "INSERT INTO players(name,war_name) VALUES(?,?)",
                        (f"Jogador {number}", f"J{number}"),
                    ).lastrowid
                )
            third_match_player_id = db.execute(
                "INSERT INTO players(name,war_name) VALUES(?,?)",
                ("Jogador da terceira", "Terceira"),
            ).lastrowid
            sumula_id = db.execute(
                "INSERT INTO football_sumulas(match_date,day_pelada,situacao,created_by) VALUES(?,'SABADO','EM_ANDAMENTO',?)",
                ("2026-08-15", self.user_id),
            ).lastrowid
            for match_number in (1, 2, 3):
                db.execute(
                    "INSERT INTO football_matches(sumula_id,number) VALUES(?,?)",
                    (sumula_id, match_number),
                )
            for draw_order, player_id in enumerate(occupied_player_ids, start=1):
                db.execute(
                    "INSERT INTO football_participants(sumula_id,player_id,status,draw_order) VALUES(?,?,'CONFIRMADO',?)",
                    (sumula_id, player_id, draw_order),
                )
            db.commit()

        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id

        page = self.client.get(f"/futebol/sumulas/{sumula_id}").get_data(as_text=True)
        self.assertIn('max="66"', page)
        self.assertIn('value="45" placeholder="Ordem do sorteio"', page)
        self.assertIn("Partida 3", page)
        self.assertIn("ordens 45 a 66", page)

        added = self.client.post(
            f"/futebol/sumulas/{sumula_id}",
            data={
                "action": "participant",
                "player_id": str(third_match_player_id),
                "status": "CONFIRMADO",
                "preferred_position": "ATACANTE",
            },
        )
        self.assertEqual(added.status_code, 302)

        with app.app_context():
            db = get_db()
            participant = db.execute(
                "SELECT id,draw_order FROM football_participants WHERE sumula_id=? AND player_id=?",
                (sumula_id, third_match_player_id),
            ).fetchone()
            self.assertEqual(participant["draw_order"], 45)
            participant_id = participant["id"]

        moved = self.client.post(
            f"/futebol/sumulas/{sumula_id}",
            data={
                "action": "update_participant_order",
                "participant_id": str(participant_id),
                "draw_order": "66",
            },
        )
        self.assertEqual(moved.status_code, 302)
        rejected = self.client.post(
            f"/futebol/sumulas/{sumula_id}",
            data={
                "action": "update_participant_order",
                "participant_id": str(participant_id),
                "draw_order": "67",
            },
        )
        self.assertEqual(rejected.status_code, 302)

        with app.app_context():
            draw_order = get_db().execute(
                "SELECT draw_order FROM football_participants WHERE id=?",
                (participant_id,),
            ).fetchone()[0]
            self.assertEqual(draw_order, 66)

        detail_html = self.client.get(f"/futebol/sumulas/{sumula_id}").get_data(as_text=True)
        self.assertIn("Terceira", detail_html)
        self.assertIn('value="66" title="Ordem do sorteio"', detail_html)
        print_html = self.client.get(f"/futebol/sumulas/{sumula_id}/imprimir").get_data(as_text=True)
        self.assertIn("3ª PARTIDA (ordens 45 a 66)", print_html)
        self.assertIn("Terceira", print_html)

    def test_participant_order_can_start_at_three_and_then_stays_sequential(self):
        with app.app_context():
            db = get_db()
            second_player_id = db.execute(
                "INSERT INTO players(name,war_name) VALUES(?,?)",
                ("Segundo da sequência", "Sequência 2"),
            ).lastrowid
            sumula_id = db.execute(
                "INSERT INTO football_sumulas(match_date,day_pelada,situacao,created_by) VALUES(?,'SABADO','EM_ANDAMENTO',?)",
                ("2026-08-22", self.user_id),
            ).lastrowid
            db.execute("INSERT INTO football_matches(sumula_id,number) VALUES(?,1)", (sumula_id,))
            db.commit()

        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id

        first = self.client.post(
            f"/futebol/sumulas/{sumula_id}",
            data={
                "action": "participant",
                "player_id": str(self.player_id),
                "status": "CONFIRMADO",
                "preferred_position": "DEFENSOR",
                "draw_order": "3",
            },
        )
        self.assertEqual(first.status_code, 302)
        page = self.client.get(f"/futebol/sumulas/{sumula_id}").get_data(as_text=True)
        self.assertIn('value="4" placeholder="Ordem do sorteio"', page)
        self.assertNotIn("participantOrder.value=", page)

        second = self.client.post(
            f"/futebol/sumulas/{sumula_id}",
            data={
                "action": "participant",
                "player_id": str(second_player_id),
                "status": "CONFIRMADO",
                "preferred_position": "MEIO_CAMPO",
                "draw_order": "",
            },
        )
        self.assertEqual(second.status_code, 302)

        with app.app_context():
            rows = get_db().execute(
                "SELECT player_id,draw_order FROM football_participants WHERE sumula_id=? ORDER BY draw_order",
                (sumula_id,),
            ).fetchall()
            self.assertEqual(
                [(row["player_id"], row["draw_order"]) for row in rows],
                [(self.player_id, 3), (second_player_id, 4)],
            )

    def test_simple_participant_text_import_creates_matches_and_draw_orders(self):
        with app.app_context():
            db = get_db()
            player_ids = {}
            for name in ("EDVAL", "DIEGO", "NEWTON", "WALTER", "REGIO", "BARBOZA", "LUCCA"):
                player_ids[name] = db.execute(
                    "INSERT INTO players(name,war_name) VALUES(?,?)", (name.title(), name)
                ).lastrowid
            sumula_id = db.execute(
                "INSERT INTO football_sumulas(match_date,day_pelada,situacao,created_by) VALUES(?,'SABADO','RASCUNHO',?)",
                ("2026-08-29", self.user_id),
            ).lastrowid
            db.execute("INSERT INTO football_matches(sumula_id,number) VALUES(?,1)", (sumula_id,))
            db.commit()

        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id

        detail = self.client.get(f"/futebol/sumulas/{sumula_id}")
        detail_html = detail.get_data(as_text=True)
        self.assertIn("Importar participantes", detail_html)
        self.assertIn("Relação dos participantes", detail_html)
        self.assertIn("Baixar Excel simples", detail_html)
        template = self.client.get("/futebol/sumulas/modelo-importacao.xlsx")
        self.assertEqual(template.status_code, 200)
        workbook = load_workbook(BytesIO(template.data))
        self.assertEqual(workbook.sheetnames, ["Instruções", "Participantes", "Peladeiros"])
        self.assertEqual(
            tuple(cell.value for cell in workbook["Participantes"][1]),
            ("Partida", "Posição", "ID do peladeiro", "Nome do peladeiro", "Status", "Observação"),
        )
        participant_text = """1ª PARTIDA
G1: EDVAL
D1: DIEGO
M1: NEWTON
A1: WALTER
2ª PARTIDA
D1: REGIO
3ª PARTIDA
M1: BARBOZA
A5: LUCCA"""
        imported = self.client.post(
            f"/futebol/sumulas/{sumula_id}/importar-participantes-texto",
            data={"participant_text": participant_text},
            follow_redirects=True,
        )
        html = imported.get_data(as_text=True)
        self.assertEqual(imported.status_code, 200)
        self.assertIn("7 participantes importados em 3 partida(s)", html)
        with app.app_context():
            db = get_db()
            rows = db.execute(
                """SELECT p.war_name,fp.draw_order,fp.preferred_position
                   FROM football_participants fp JOIN players p ON p.id=fp.player_id
                   WHERE fp.sumula_id=? ORDER BY fp.draw_order""",
                (sumula_id,),
            ).fetchall()
            self.assertEqual(
                [(row["war_name"], row["draw_order"], row["preferred_position"]) for row in rows],
                [
                    ("EDVAL", 1, "GOLEIRO"), ("DIEGO", 3, "DEFENSOR"),
                    ("NEWTON", 11, "MEIO_CAMPO"), ("WALTER", 17, "ATACANTE"),
                    ("REGIO", 25, "DEFENSOR"), ("BARBOZA", 55, "MEIO_CAMPO"),
                    ("LUCCA", 65, "ATACANTE"),
                ],
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM football_matches WHERE sumula_id=?", (sumula_id,)).fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM football_lineups").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM football_goals").fetchone()[0], 0)

    def test_invalid_participant_text_import_writes_nothing(self):
        with app.app_context():
            db = get_db()
            second_id = db.execute("INSERT INTO players(name,war_name) VALUES(?,?)", ("Segundo", "Segundo")).lastrowid
            sumula_id = db.execute(
                "INSERT INTO football_sumulas(match_date,day_pelada,situacao,created_by) VALUES(?,'SABADO','RASCUNHO',?)",
                ("2026-09-05", self.user_id),
            ).lastrowid
            db.execute("INSERT INTO football_matches(sumula_id,number) VALUES(?,1)", (sumula_id,))
            db.commit()
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        rejected = self.client.post(
            f"/futebol/sumulas/{sumula_id}/importar-participantes-texto",
            data={"participant_text": "1ª PARTIDA\nD1: Peladeiro\nD1: Segundo"},
            follow_redirects=True,
        )
        self.assertIn("posição D1 repetida na partida 1", rejected.get_data(as_text=True))
        with app.app_context():
            db = get_db()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM football_participants WHERE sumula_id=?", (sumula_id,)).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM football_goals").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
