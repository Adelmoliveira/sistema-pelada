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


    def assert_scoped_equivalent(self, ids, **options):
        full = purchase_forecast(self.db, "2026-04", **options)
        scoped = purchase_forecast(self.db, "2026-04", product_ids=ids, **options)
        expected = dict(full)
        expected["products"] = [p for p in full["products"] if p["product_id"] in ids]
        expected["anomalies"] = []
        for anomaly in full["anomalies"]:
            if "product_id" in anomaly:
                if anomaly["product_id"] in ids:
                    expected["anomalies"].append(anomaly)
            else:
                selected = [pid for pid in anomaly["product_ids"] if pid in ids]
                if selected:
                    expected["anomalies"].append(dict(anomaly, product_ids=selected))
        self.assertEqual(scoped, expected)
        return scoped

    def test_scoped_equivalence_across_windows_margins_and_edge_products(self):
        case = self.add_product("Caixa", stock=1, units_per_case=6)
        enough = self.add_product("Saldo alto", stock=1000)
        inactive = self.add_product("Inativo", active=0)
        new = self.add_product("Novo", created_at="2026-03-01 00:00:00")
        empty = self.add_product("Sem histórico", stock=-1, units_per_case=-1)
        sports = self.add_product("Material esportivo")
        self.db.execute("UPDATE products SET category='Material Esportivo' WHERE id=?", (sports,))
        for pid in (case, enough, inactive, new, sports):
            self.add_delivery(pid, 7, "2025-10-31 15:00:00")
            self.add_delivery(pid, 10, "2026-01-15 15:00:00")
            self.add_delivery(pid, 2, "2026-03-31 23:00:00")
        restored = self.add_delivery(case, 3, "2026-02-15 15:00:00")
        self.db.execute("INSERT INTO sale_item_delivery_restorations(delivery_id,quantity) VALUES(?,5)", (restored,))
        self.add_legacy(inactive, 9, "2026-01-10 15:00:00", canceled=True)
        self.add_legacy(case, 0, "2026-02-10 15:00:00")
        self.add_legacy(case, 8, "2026-03-10 15:00:00")
        for window in (3, 6):
            for margin in (0, 10, 30, "2.5"):
                for ids in ([case], [inactive], [empty], [new, enough], [case, inactive, empty, new, enough, sports]):
                    with self.subTest(window=window, margin=margin, ids=ids):
                        self.assert_scoped_equivalent(ids, history_months=window, safety_margin_percent=margin)

    def test_scoped_selection_order_duplicates_generator_unknown_and_sports(self):
        first = self.add_product("Zebra")
        second = self.add_product("Água")
        sports = self.add_product("Esportivo")
        self.db.execute("UPDATE products SET category='Material Esportivo' WHERE id=?", (sports,))
        ids = [second, first, first, sports, 9999]
        expected = self.assert_scoped_equivalent(ids)
        self.assertEqual(purchase_forecast(self.db, "2026-04", product_ids=iter(ids)), expected)
        self.assertEqual(purchase_forecast(self.db, "2026-04", product_ids=None), purchase_forecast(self.db, "2026-04"))
        self.assert_scoped_equivalent([sports, 9999])

    def test_empty_selection_has_metadata_without_database_access(self):
        class NoDatabase:
            def execute(self, *args):
                raise AssertionError("An empty selection must not query the database")
        report = purchase_forecast(NoDatabase(), "2026-04", product_ids=[])
        self.assertEqual(report["products"], [])
        self.assertEqual(report["anomalies"], [])
        self.assertEqual(report["history_period"], {"start":"2026-01", "end":"2026-03"})
        with self.assertRaises(ValueError):
            purchase_forecast(NoDatabase(), "2026-04", history_months=4, product_ids=[])

    def test_invalid_product_ids_fail_before_querying(self):
        from unittest.mock import Mock
        for ids in (1, "1", [True], [False], [0], [-1], [1.0], [None], ["1"], ["1) OR 1=1 --"]):
            with self.subTest(ids=ids):
                db = Mock()
                with self.assertRaises(ValueError):
                    purchase_forecast(db, "2026-04", product_ids=ids)
                db.execute.assert_not_called()

    def test_scoped_legacy_exclusion_remains_sale_wide_including_other_products(self):
        chosen = self.add_product("Selecionado")
        other = self.add_product("Outro")
        sale = self.add_legacy(chosen, 50, "2026-01-15 15:00:00")
        item = self.db.execute("INSERT INTO sale_items(sale_id,product_id,quantity) VALUES(?,?,9)", (sale,other)).lastrowid
        self.db.execute("INSERT INTO sale_item_deliveries(sale_item_id,quantity,delivered_at) VALUES(?,9,'2026-01-15 15:00:00')", (item,))
        scoped = self.assert_scoped_equivalent([chosen])
        self.assertEqual(scoped["products"][0]["average_monthly_consumption"],0)

    def test_shared_legacy_anomaly_contains_only_selected_products(self):
        first = self.add_product("Primeiro")
        second = self.add_product("Segundo")
        sale = self.add_legacy(first, 10, "2026-02-15 15:00:00", payment_status="expired")
        self.db.execute("INSERT INTO sale_items(sale_id,product_id,quantity) VALUES(?,?,20)", (sale,second))
        scoped = self.assert_scoped_equivalent([second])
        self.assertEqual(scoped["anomalies"][0]["product_ids"], [second])
        self.assertEqual(scoped["anomalies"][0]["reason"], "canceled_or_terminal")

    def test_timezone_boundaries_and_leap_year_match_full_report(self):
        product = self.add_product()
        for quantity, timestamp in ((99,"2026-01-01 02:59:59"), (3,"2026-01-01 03:00:00"),
                                    (6,"2026-04-01 02:59:59"), (99,"2026-04-01 03:00:00")):
            self.add_delivery(product, quantity, timestamp)
        report = self.assert_scoped_equivalent([product], safety_margin_percent=0)
        self.assertEqual([p["quantity"] for p in report["products"][0]["monthly_consumption"]],[3,0,6])
        self.add_delivery(product,12,"2024-02-29 15:00:00")
        complete = purchase_forecast(self.db,"2024-03")
        self.assertEqual(purchase_forecast(self.db,"2024-03",product_ids=[product]),complete)

    def test_scoped_queries_are_batched_and_return_only_selected_history(self):
        from unittest.mock import Mock
        ids = [self.add_product(f"Produto {index:03d}") for index in range(80)]
        for pid in ids:
            self.add_delivery(pid,6,"2026-01-15 15:00:00")
            self.add_legacy(pid,3,"2026-02-15 15:00:00")
        for selected in ([ids[0]],ids[:40]):
            with self.subTest(selected_count=len(selected)):
                db = Mock(wraps=self.db)
                rows_read = []
                def execute(sql, params):
                    rows = self.db.execute(sql,params).fetchall()
                    rows_read.append(rows)
                    cursor = Mock()
                    cursor.fetchall.return_value = rows
                    return cursor
                db.execute.side_effect = execute
                report = purchase_forecast(db,"2026-04",product_ids=selected)
                self.assertEqual(db.execute.call_count,3)
                self.assertEqual([len(rows) for rows in rows_read],[len(selected)]*3)
                self.assertEqual(len(report["products"]),len(selected))
                self.assertEqual({r["id"] for r in rows_read[0]},set(selected))
                for rows in rows_read[1:]:
                    self.assertEqual({r["product_id"] for r in rows},set(selected))
        db = Mock(wraps=self.db)
        purchase_forecast(db,"2026-04",product_ids=[99999])
        self.assertEqual(db.execute.call_count,1)

    def test_scoped_postgres_binding_matches_sqlite_without_a_server(self):
        from psycopg2.extensions import adapt
        product = self.add_product()
        self.add_legacy(product,8,"2026-01-15 15:00:00")
        self.add_delivery(product,6,"2026-02-15 15:00:00")
        connection = self.db.conn
        class BindingCursor:
            def execute(self, sql, params):
                values = tuple(adapt(value).getquoted().decode('utf-8') for value in params)
                self.cursor = connection.execute(sql % values)
            def fetchall(self):
                return self.cursor.fetchall()
        class PostgresConnection:
            def cursor(self):
                return BindingCursor()
        pg = DbWrapper(PostgresConnection(),is_postgres=True)
        self.assertEqual(purchase_forecast(pg,"2026-04",product_ids=[product]), purchase_forecast(self.db,"2026-04",product_ids=[product]))



if __name__ == "__main__":
    unittest.main()
