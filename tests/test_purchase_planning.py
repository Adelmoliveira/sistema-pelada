import os
import tempfile
import unittest
from pathlib import Path

os.environ["DATABASE_URL"] = ""
os.environ["SUPABASE_DB_URL"] = ""

from app import app
from src.db import get_db, initialize_sqlite_database


class PurchasePlanningPageTest(unittest.TestCase):
    def setUp(self):
        original = dict(app.config)
        self.addCleanup(lambda: (app.config.clear(), app.config.update(original)))
        self.tempdir = tempfile.TemporaryDirectory(dir=".")
        self.addCleanup(self.tempdir.cleanup)
        database = str(Path(self.tempdir.name) / "planning.db")
        app.config.update(
            TESTING=True,
            DATABASE=database,
            DATABASE_URL=None,
            WTF_CSRF_ENABLED=False,
            SECRET_KEY="purchase-planning-test",
        )
        initialize_sqlite_database(database)
        self.client = app.test_client()
        with app.app_context():
            db = get_db()
            self.manager = db.execute(
                "INSERT INTO users(username,name,password_hash,role) VALUES('planning.manager','Gerente','hash','manager')"
            ).lastrowid
            self.staff = db.execute(
                "INSERT INTO users(username,name,password_hash,role) VALUES('planning.staff','Staff','hash','staff')"
            ).lastrowid
            self.client_user = db.execute(
                "INSERT INTO users(username,name,password_hash,role) VALUES('planning.client','Cliente','hash','client')"
            ).lastrowid
            self.product = db.execute(
                """INSERT INTO products(name,category,price_cents,cost_cents,stock,units_per_case,created_at)
                   VALUES('Água Planejada','Bebidas',500,250,1,6,'2025-01-01 12:00:00')"""
            ).lastrowid
            sale = db.execute(
                "INSERT INTO sales(payment_method,total_cents,payment_status) VALUES('Cortesia',0,'approved')"
            ).lastrowid
            item = db.execute(
                "INSERT INTO sale_items(sale_id,product_id,quantity,unit_price_cents) VALUES(?,?,12,0)",
                (sale, self.product),
            ).lastrowid
            operation = db.execute(
                "INSERT INTO sale_delivery_operations(sale_id,delivered_at) VALUES(?,'2026-03-15 15:00:00')",
                (sale,),
            ).lastrowid
            db.execute(
                """INSERT INTO sale_item_deliveries(delivery_operation_id,sale_item_id,quantity,delivered_at)
                   VALUES(?,?,12,'2026-03-15 15:00:00')""",
                (operation, item),
            )
            self.new_product = db.execute(
                """INSERT INTO products(name,category,price_cents,cost_cents,stock,created_at)
                   VALUES('Produto Novo','Alimentos',300,0,0,'2026-03-20 12:00:00')"""
            ).lastrowid
            db.commit()

    def login(self, user_id):
        with self.client.session_transaction() as session:
            session["user_id"] = user_id

    def test_manager_can_access_and_menu_contains_planning_link(self):
        self.login(self.manager)
        response = self.client.get("/stock/purchase-planning?month=2026-04")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Planejamento de Compras", page)
        self.assertIn("/stock/purchase-planning", page)
        self.assertIn('data-bs-target="#sidebar-stock"', page)
        self.assertIn('aria-expanded="true" aria-controls="sidebar-stock"', page)
        self.assertIn('class="collapse show" id="sidebar-stock"', page)
        self.assertIn('class="active" href="/stock/purchase-planning"', page)

    def test_staff_and_client_cannot_access(self):
        for user_id in (self.staff, self.client_user):
            self.login(user_id)
            response = self.client.get("/stock/purchase-planning?month=2026-04")
            self.assertEqual(response.status_code, 302)

    def test_filters_and_product_rendering(self):
        self.login(self.manager)
        page = self.client.get(
            "/stock/purchase-planning?month=2026-04&history=6&margin=20"
        ).get_data(as_text=True)
        self.assertIn('value="2026-04"', page)
        self.assertIn('value="6" selected', page)
        self.assertIn('value="20"', page)
        self.assertIn("Água Planejada", page)
        self.assertIn("2026-03: <strong>12</strong>", page)
        self.assertIn("caixa(s)", page)

    def test_product_without_history_is_visually_identified(self):
        self.login(self.manager)
        page = self.client.get("/stock/purchase-planning?month=2026-04").get_data(as_text=True)
        self.assertIn("Produto Novo", page)
        self.assertIn("Histórico insuficiente", page)
        self.assertIn("table-warning", page)

    def test_manual_adjustment_is_browser_only_and_validated(self):
        self.login(self.manager)
        page = self.client.get("/stock/purchase-planning?month=2026-04").get_data(as_text=True)
        self.assertIn("adjusted-quantity", page)
        self.assertIn('min="0" step="1"', page)
        self.assertIn("Number.isInteger(quantity)", page)
        self.assertIn("Diferença", page)
        self.assertIn("Ajustes não são gravados", page)

    def test_invalid_filters_are_rejected(self):
        self.login(self.manager)
        for query in (
            "month=abril-2026",
            "month=2026-04&history=4",
            "month=2026-04&margin=-1",
            "month=2026-04&margin=31",
            "month=2026-04&margin=10.5",
        ):
            self.assertEqual(self.client.get(f"/stock/purchase-planning?{query}").status_code, 400)

    def test_get_does_not_write_to_database(self):
        self.login(self.manager)
        with app.app_context():
            db = get_db()
            before = {
                "stock": db.execute("SELECT stock FROM products WHERE id=?", (self.product,)).fetchone()["stock"],
                "restocks": db.execute("SELECT COUNT(*) total FROM restocks").fetchone()["total"],
                "requests": db.execute("SELECT COUNT(*) total FROM bar_restock_requests").fetchone()["total"],
            }
        self.assertEqual(self.client.get("/stock/purchase-planning?month=2026-04").status_code, 200)
        with app.app_context():
            db = get_db()
            after = {
                "stock": db.execute("SELECT stock FROM products WHERE id=?", (self.product,)).fetchone()["stock"],
                "restocks": db.execute("SELECT COUNT(*) total FROM restocks").fetchone()["total"],
                "requests": db.execute("SELECT COUNT(*) total FROM bar_restock_requests").fetchone()["total"],
            }
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
