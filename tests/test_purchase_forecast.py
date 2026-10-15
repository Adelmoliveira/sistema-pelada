import sqlite3
import unittest

from src.db import DbWrapper
from src.services.purchase_forecast import purchase_forecast


SCHEMA = """
CREATE TABLE products (
 id INTEGER PRIMARY KEY, name TEXT, category TEXT, units_per_case INTEGER DEFAULT 0,
 stock INTEGER DEFAULT 0, active INTEGER DEFAULT 1, created_at TEXT
);
CREATE TABLE sales (
 id INTEGER PRIMARY KEY, delivered_at TEXT, payment_status TEXT, payment_method TEXT
);
CREATE TABLE sale_items (
 id INTEGER PRIMARY KEY, sale_id INTEGER, product_id INTEGER, quantity INTEGER
);
CREATE TABLE sale_item_deliveries (
 id INTEGER PRIMARY KEY, sale_item_id INTEGER, quantity INTEGER, delivered_at TEXT
);
CREATE TABLE sale_item_delivery_restorations (
 id INTEGER PRIMARY KEY, delivery_id INTEGER UNIQUE, quantity INTEGER
);
CREATE TABLE sale_cancellations (id INTEGER PRIMARY KEY, sale_id INTEGER UNIQUE);
"""


class PurchaseForecastTest(unittest.TestCase):
    def setUp(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        self.db = DbWrapper(connection)

    def tearDown(self):
        self.db.close()

    def add_product(self, name="Água", stock=0, units_per_case=0, created_at="2025-01-01 00:00:00", active=1):
        cursor = self.db.execute(
            "INSERT INTO products(name,category,units_per_case,stock,active,created_at) VALUES(?,?,?,?,?,?)",
            (name, "Bebidas", units_per_case, stock, active, created_at),
        )
        self.db.commit()
        return cursor.lastrowid

    def add_delivery(self, product_id, quantity, delivered_at, payment_status="pending", payment_method="Pix"):
        sale = self.db.execute(
            "INSERT INTO sales(payment_status,payment_method) VALUES(?,?)",
            (payment_status, payment_method),
        ).lastrowid
        item = self.db.execute(
            "INSERT INTO sale_items(sale_id,product_id,quantity) VALUES(?,?,?)",
            (sale, product_id, quantity),
        ).lastrowid
        delivery = self.db.execute(
            "INSERT INTO sale_item_deliveries(sale_item_id,quantity,delivered_at) VALUES(?,?,?)",
            (item, quantity, delivered_at),
        ).lastrowid
        self.db.commit()
        return delivery

    def add_legacy(self, product_id, quantity, delivered_at, payment_status="paid", canceled=False):
        sale = self.db.execute(
            "INSERT INTO sales(delivered_at,payment_status,payment_method) VALUES(?,?,?)",
            (delivered_at, payment_status, "Dinheiro"),
        ).lastrowid
        self.db.execute(
            "INSERT INTO sale_items(sale_id,product_id,quantity) VALUES(?,?,?)",
            (sale, product_id, quantity),
        )
        if canceled:
            self.db.execute("INSERT INTO sale_cancellations(sale_id) VALUES(?)", (sale,))
        self.db.commit()
        return sale

    def product_result(self, **kwargs):
        report = purchase_forecast(self.db, planned_month="2026-04", **kwargs)
        return report, report["products"][0]

    def test_three_month_average_includes_month_without_sales(self):
        product = self.add_product()
        self.add_delivery(product, 6, "2026-01-15 15:00:00")
        self.add_delivery(product, 12, "2026-03-15 15:00:00")
        _report, result = self.product_result(history_months=3, safety_margin_percent=0)
        self.assertEqual([6, 0, 12], [row["quantity"] for row in result["monthly_consumption"]])
        self.assertEqual(result["average_monthly_consumption"], 6)
        self.assertEqual(result["forecast_units"], 6)

    def test_six_month_average(self):
        product = self.add_product()
        for month, quantity in enumerate((3, 6, 9, 12, 15, 18), 10):
            year = 2025 if month <= 12 else 2026
            actual_month = month if month <= 12 else month - 12
            self.add_delivery(product, quantity, f"{year}-{actual_month:02d}-15 15:00:00")
        report = purchase_forecast(self.db, "2026-04", 6, 0)
        self.assertEqual(report["products"][0]["average_monthly_consumption"], 10.5)

    def test_product_without_history_is_flagged(self):
        self.add_product()
        _report, result = self.product_result()
        self.assertTrue(result["insufficient_history"])
        self.assertIn("no_consumption_history", result["limitations"])
        self.assertEqual(result["suggested_purchase_quantity"], 0)
        self.assertEqual(result["trend_direction"], "insufficient_data")

    def test_partial_history_is_flagged_without_dropping_zero_months(self):
        product = self.add_product(created_at="2026-01-15 12:00:00")
        self.add_delivery(product, 9, "2026-03-15 15:00:00")
        _report, result = self.product_result(safety_margin_percent=0)
        self.assertTrue(result["insufficient_history"])
        self.assertEqual(result["average_monthly_consumption"], 3)

    def test_sufficient_and_insufficient_stock(self):
        enough = self.add_product("Com estoque", stock=20)
        low = self.add_product("Sem estoque", stock=2)
        for product in (enough, low):
            for month in (1, 2, 3):
                self.add_delivery(product, 10, f"2026-{month:02d}-15 15:00:00")
        results = {row["name"]: row for row in purchase_forecast(self.db, "2026-04", 3, 0)["products"]}
        self.assertEqual(results["Com estoque"]["net_required_units"], 0)
        self.assertEqual(results["Sem estoque"]["net_required_units"], 8)

    def test_safety_margin_and_case_rounding(self):
        product = self.add_product(stock=1, units_per_case=6)
        for month in (1, 2, 3):
            self.add_delivery(product, 10, f"2026-{month:02d}-15 15:00:00")
        _report, result = self.product_result(safety_margin_percent=10)
        self.assertEqual(result["gross_requirement_units"], 11)
        self.assertEqual(result["net_required_units"], 10)
        self.assertEqual(result["suggested_purchase_quantity"], 2)
        self.assertEqual(result["purchase_unit"], "cases")
        self.assertEqual(result["suggested_purchase_units"], 12)

    def test_product_without_packaging_is_suggested_in_units(self):
        product = self.add_product(stock=1)
        for month in (1, 2, 3):
            self.add_delivery(product, 5, f"2026-{month:02d}-15 15:00:00")
        _report, result = self.product_result(safety_margin_percent=0)
        self.assertEqual(result["purchase_unit"], "units")
        self.assertEqual(result["suggested_purchase_quantity"], 4)

    def test_restoration_reduces_original_delivery_month_and_never_below_zero(self):
        product = self.add_product()
        delivery = self.add_delivery(product, 10, "2026-02-15 15:00:00")
        self.db.execute(
            "INSERT INTO sale_item_delivery_restorations(delivery_id,quantity) VALUES(?,?)",
            (delivery, 14),
        )
        self.db.commit()
        report, result = self.product_result(safety_margin_percent=0)
        self.assertEqual([0, 0, 0], [row["quantity"] for row in result["monthly_consumption"]])
        self.assertEqual(report["anomalies"][0]["code"], "restoration_exceeds_delivery")

    def test_courtesy_and_unpaid_installment_delivery_count_but_abandoned_pix_does_not(self):
        product = self.add_product()
        self.add_delivery(product, 3, "2026-01-15 15:00:00", "pending", "Pix")
        self.add_delivery(product, 4, "2026-01-16 15:00:00", "paid", "Cortesia")
        # Merely created/abandoned Pix: no delivered_at and no detailed delivery.
        sale = self.db.execute("INSERT INTO sales(payment_status,payment_method) VALUES('pending','Pix')").lastrowid
        self.db.execute("INSERT INTO sale_items(sale_id,product_id,quantity) VALUES(?,?,99)", (sale, product))
        self.db.commit()
        _report, result = self.product_result(safety_margin_percent=0)
        self.assertEqual(result["monthly_consumption"][0]["quantity"], 7)

    def test_legacy_fallback_avoids_duplicate_and_flags_canceled_sale(self):
        product = self.add_product()
        self.add_legacy(product, 8, "2026-01-15 15:00:00")
        canceled = self.add_legacy(product, 50, "2026-01-16 15:00:00", canceled=True)
        # delivered_at plus a detail must use only the detail, never sale_items quantity.
        sale = self.db.execute(
            "INSERT INTO sales(delivered_at,payment_status,payment_method) VALUES('2026-01-17 15:00:00','paid','Dinheiro')"
        ).lastrowid
        item = self.db.execute(
            "INSERT INTO sale_items(sale_id,product_id,quantity) VALUES(?,?,20)", (sale, product)
        ).lastrowid
        self.db.execute(
            "INSERT INTO sale_item_deliveries(sale_item_id,quantity,delivered_at) VALUES(?,?,?)",
            (item, 5, "2026-01-17 15:00:00"),
        )
        self.db.commit()
        report, result = self.product_result(safety_margin_percent=0)
        self.assertEqual(result["monthly_consumption"][0]["quantity"], 13)
        self.assertEqual(report["anomalies"][0]["sale_id"], canceled)

    def test_inactive_product_has_no_purchase_suggestion(self):
        product = self.add_product(active=0)
        for month in (1, 2, 3):
            self.add_delivery(product, 9, f"2026-{month:02d}-15 15:00:00")
        _report, result = self.product_result(safety_margin_percent=0)
        self.assertEqual(result["net_required_units"], 9)
        self.assertEqual(result["suggested_purchase_quantity"], 0)
        self.assertIn("inactive_product", result["limitations"])

    def test_validation_and_methodology(self):
        self.add_product()
        for window in (2, 4):
            with self.assertRaises(ValueError):
                purchase_forecast(self.db, "2026-04", window, 10)
        for margin in (-1, 31):
            with self.assertRaises(ValueError):
                purchase_forecast(self.db, "2026-04", 3, margin)
        report = purchase_forecast(self.db, "2026-04", 3, 10)
        self.assertFalse(report["methodology"]["trend_applied"])
        self.assertFalse(report["methodology"]["pending_restock_deducted"])

    def test_queries_are_read_only_and_portable_through_db_wrapper(self):
        self.add_product()
        statements = []
        self.db.conn.set_trace_callback(statements.append)
        purchase_forecast(self.db, "2026-04")
        normalized = [statement.lstrip().upper() for statement in statements]
        self.assertTrue(normalized)
        self.assertTrue(all(statement.startswith("SELECT") for statement in normalized))
        self.assertFalse(any(token in " ".join(normalized) for token in ("STRFTIME(", "JULIANDAY(", "DATE(")))


if __name__ == "__main__":
    unittest.main()
