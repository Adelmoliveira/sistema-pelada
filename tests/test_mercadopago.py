import hashlib
import hmac
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app import app
from src.db import get_db
from src.routes.auth import make_password_hash
from src.routes.sales import pix_access_token
from src.services.mercadopago import validate_webhook_signature
from src.services.mercadopago import MercadoPagoError
from src.services.mercadopago import create_pix_order
from src.services.email_reminders import dispatch_reminders, get_reminder_settings, outstanding_players
from src.services.cash_register import get_session, session_summary
from src.services.monthly_sales_report import monthly_sales_data
from src.utils import alphabetical_key, brdate, local_today, month_bounds
from werkzeug.security import check_password_hash


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
        with self.client.session_transaction() as session:
            client_user_id = session["user_id"]
        with app.app_context():
            db = get_db()
            client_user = db.execute("SELECT * FROM users WHERE id=?", (client_user_id,)).fetchone()
            self.assertEqual((client_user["role"], client_user["player_id"], client_user["username"]), ("client", self.player_id, "Craque"))

        page = self.client.get("/sale").get_data(as_text=True)
        self.assertIn("Craque", page)
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

    def test_manager_sidebar_groups_modules_and_links(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        page = self.client.get("/players").get_data(as_text=True)
        self.assertIn('id="app-sidebar"', page)
        modules = ["Bar", "Financeiro", "Infra-Estrutura", "Urgente", "Administração"]
        positions = [page.index(f"<span>{label}</span>") for label in modules]
        self.assertEqual(positions, sorted(positions))
        for links in (
            ["Caixa", "Conferir Pix", "Estoque", "Produtos", "Pedidos", "Venda rápida"],
            ["Visão financeira", "Livro-caixa", "Lembretes", "Relatórios"],
            ["Manutenção", "Materiais", "Relação de Carga"],
            ["Peladeiros", "Usuários"],
        ):
            link_positions = [page.index(f">{label}</a>") for label in links]
            self.assertEqual(link_positions, sorted(link_positions))
        self.assertIn('data-bs-target="#sidebar-bar"', page)
        self.assertIn('class="offcanvas-lg offcanvas-start app-sidebar"', page)
        self.assertIn('alt="Logo GPCTA"', page)
        self.assertNotIn('class="navbar ', page)
        self.assertNotIn('class="sidebar-user"', page)
        self.assertIn('class="topbar-account"', page)
        self.assertIn('<strong>Teste</strong><small>Gerente</small>', page)
        self.assertEqual(page.count('<strong>Teste</strong>'), 1)
        self.assertEqual(page.count('>Relatórios</a>'), 1)
        self.assertIn('action="/logout"', page)
        self.assertIn('id="pwa-install"', page)

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
        self.assertIn("<span>Urgente</span>", page)
        self.assertNotIn(">Materiais</a>", page)
        self.assertNotIn(">Relação de Carga</a>", page)
        self.assertNotIn(">Manutenção</a>", page)
        self.assertNotIn("Acompanhamento e resolução", page)

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

        for forbidden_path in ("/infra/maintenance", "/infra/materials", "/infra/load-relation"):
            denied = self.client.get(forbidden_path)
            self.assertEqual(denied.status_code, 302)
            self.assertEqual(denied.headers["Location"], "/")

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
                (f"BMP-{entry_id:06d} | SAL", "SAL", "SERIE-002", "Armário H-14", 1),
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
        self.assertIn('class="login-logo mb-3"', page)
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
        self.assertTrue(login.headers["Location"].endswith("/infra/load-relation"))

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
        for hidden_link in ("Caixa", "Conferir Pix", "Estoque", "Produtos", "Pedidos", "Peladeiros", "Relatórios", "Usuários", "Venda rápida"):
            self.assertNotIn(f">{hidden_link}</a>", html)
        self.assertEqual(self.client.get("/infra/materials").status_code, 200)
        self.assertEqual(self.client.get("/infra/maintenance").status_code, 200)
        self.assertEqual(self.client.get("/urgent").status_code, 200)

        for forbidden_path in ("/", "/sale", "/stock", "/cash", "/players", "/users"):
            denied = self.client.get(forbidden_path)
            self.assertEqual(denied.status_code, 302)
            self.assertTrue(denied.headers["Location"].endswith("/infra/load-relation"))

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

        closed = self.client.post(
            "/cash/close",
            data={"counted_cash": "103,00", "counted_bank": "504,00", "closing_notes": "Conferido."},
        )
        self.assertEqual(closed.status_code, 303)
        with app.app_context():
            db = get_db()
            cash_session = get_session(db)
            self.assertEqual(cash_session["status"], "closed")
            self.assertEqual(
                (cash_session["expected_cash_cents"], cash_session["expected_bank_cents"], cash_session["cash_difference_cents"], cash_session["bank_difference_cents"]),
                (10300, 50500, 0, -100),
            )
            movement_count = db.execute("SELECT COUNT(*) total FROM cash_movements").fetchone()["total"]

        rejected = self.client.post(
            "/cash/movements",
            data={"account": "cash", "direction": "in", "category": "other", "amount": "1,00", "description": "Tardio"},
        )
        self.assertEqual(rejected.status_code, 303)
        with app.app_context():
            self.assertEqual(
                get_db().execute("SELECT COUNT(*) total FROM cash_movements").fetchone()["total"],
                movement_count,
            )

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


if __name__ == "__main__":
    unittest.main()
