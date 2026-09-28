import tempfile
import unittest
from pathlib import Path

from src.db import connect_sqlite, initialize_sqlite_database
from src.services.cash_register import history_rows, payment_breakdown, session_summary


class CashPaymentPartsTest(unittest.TestCase):
    business_date = "2026-09-28"

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "cash-parts.db")
        initialize_sqlite_database(self.path)
        self.db = connect_sqlite(self.path)
        self.session_id = self.db.execute(
            """INSERT INTO cash_sessions
               (business_date,opening_cash_cents,opening_bank_cents,status)
               VALUES(?,0,0,'open')""",
            (self.business_date,),
        ).lastrowid
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tempdir.cleanup()

    def create_sale(self, method, total=800, parts=()):
        sale_id = self.db.execute(
            """INSERT INTO sales(payment_method,total_cents,paid,payment_status,paid_at)
               VALUES(?,?,1,'approved',?)""",
            (method, total, f"{self.business_date} 12:00:00"),
        ).lastrowid
        for part_method, amount, status in parts:
            self.db.execute(
                """INSERT INTO sale_payment_parts(sale_id,method,amount_cents,status)
                   VALUES(?,?,?,?)""",
                (sale_id, part_method, amount, status),
            )
        self.db.commit()
        return sale_id

    def cash_session(self):
        return self.db.execute("SELECT * FROM cash_sessions WHERE id=?", (self.session_id,)).fetchone()

    def test_historical_sales_fall_back_per_sale(self):
        cash_id = self.create_sale("Dinheiro")
        pix_id = self.create_sale("Pix")
        self.assertEqual(payment_breakdown(self.db, {"id": cash_id, "payment_method": "Dinheiro", "total_cents": 800}),
                         {"Dinheiro": 800, "Pix": 0, "Créditos": 0})
        self.assertEqual(payment_breakdown(self.db, {"id": pix_id, "payment_method": "Pix", "total_cents": 800}),
                         {"Dinheiro": 0, "Pix": 800, "Créditos": 0})
        summary = session_summary(self.db, self.cash_session())
        self.assertEqual((summary["cash_sales"], summary["bank_sales"]), (800, 800))

    def test_mixed_integral_and_inactive_parts_have_no_double_count(self):
        self.create_sale("Dinheiro", parts=(("Créditos", 600, "approved"), ("Dinheiro", 200, "approved")))
        self.create_sale("Pix", parts=(("Créditos", 600, "approved"), ("Pix", 200, "approved")))
        self.create_sale("Dinheiro", parts=(("Dinheiro", 800, "approved"),))
        self.create_sale("Pix", parts=(("Pix", 800, "approved"),))
        self.create_sale("Créditos", parts=(("Créditos", 800, "approved"),))
        self.create_sale("Dinheiro", parts=(("Créditos", 600, "reserved"), ("Dinheiro", 200, "pending")))
        self.create_sale("Dinheiro", parts=(("Créditos", 600, "canceled"), ("Dinheiro", 200, "canceled")))

        summary = session_summary(self.db, self.cash_session())
        self.assertEqual((summary["cash_sales"], summary["bank_sales"], summary["credit_sales"]),
                         (1000, 1000, 2000))
        self.assertEqual((summary["expected_cash"], summary["expected_bank"]), (1000, 1000))
        data = history_rows(self.db, self.business_date, self.business_date)
        self.assertEqual(data["totals"]["sales"], 5600)
        self.assertEqual((data["totals"]["cash_sales"], data["totals"]["bank_sales"], data["totals"]["credit_sales"]),
                         (1000, 1000, 2000))

    def test_cash_and_bank_filters_use_only_the_approved_component(self):
        cash_sale = self.create_sale(
            "Dinheiro", parts=(("Créditos", 600, "approved"), ("Dinheiro", 200, "approved"))
        )
        pix_sale = self.create_sale(
            "Pix", parts=(("Créditos", 600, "approved"), ("Pix", 200, "approved"))
        )
        cash_data = history_rows(self.db, self.business_date, self.business_date, account="cash")
        bank_data = history_rows(self.db, self.business_date, self.business_date, account="bank")
        self.assertEqual(([row["id"] for row in cash_data["sales"]], cash_data["totals"]["sales"]), ([cash_sale], 200))
        self.assertEqual(([row["id"] for row in bank_data["sales"]], bank_data["totals"]["sales"]), ([pix_sale], 200))
        self.assertEqual(cash_data["sales"][0]["display_amount_cents"], 200)
        self.assertEqual(bank_data["sales"][0]["display_amount_cents"], 200)
        self.assertEqual(cash_data["sales"][0]["payment_label"], "Créditos + Dinheiro")
        self.assertEqual(bank_data["sales"][0]["payment_label"], "Créditos + Pix")


if __name__ == "__main__":
    unittest.main()
