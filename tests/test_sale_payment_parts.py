import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.db import connect_sqlite, initialize_sqlite_database
from src.services.sale_payment_parts import (
    approve_payment_part,
    associate_payment_part,
    cancel_active_payment_parts,
    create_payment_part,
    get_payment_parts,
    validate_approved_parts_total,
)


class SalePaymentPartsTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "payments.db")
        initialize_sqlite_database(self.path)
        self.db = connect_sqlite(self.path)

    def tearDown(self):
        self.db.close()
        self.tempdir.cleanup()

    def create_sale(self, total=1000, paid=0, status="pending"):
        cursor = self.db.execute(
            """INSERT INTO sales(payment_method,total_cents,paid,payment_status)
               VALUES('Pix',?,?,?)""",
            (total, paid, status),
        )
        self.db.commit()
        return cursor.lastrowid

    def test_schema_rejects_invalid_amount_method_status_and_duplicate_method(self):
        sale_id = self.create_sale()
        create_payment_part(self.db, sale_id, "Pix", 1000, "pending")
        create_payment_part(self.db, sale_id, "Pix", 1000, "pending")
        self.assertEqual(len(get_payment_parts(self.db, sale_id)), 1)
        with self.assertRaises(ValueError):
            create_payment_part(self.db, sale_id, "Pix", 900, "pending")
        for sql, params in (
            ("INSERT INTO sale_payment_parts(sale_id,method,amount_cents,status) VALUES(?,?,?,?)", (sale_id, "Pix", 0, "pending")),
            ("INSERT INTO sale_payment_parts(sale_id,method,amount_cents,status) VALUES(?,?,?,?)", (sale_id, "Cartão", 1, "pending")),
            ("INSERT INTO sale_payment_parts(sale_id,method,amount_cents,status) VALUES(?,?,?,?)", (sale_id, "Dinheiro", 1, "unknown")),
        ):
            with self.subTest(params=params), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql, params)

    def test_association_and_approval_are_idempotent(self):
        sale_id = self.create_sale()
        create_payment_part(self.db, sale_id, "Pix", 1000, "pending", "sale-ref")
        self.assertEqual(associate_payment_part(self.db, sale_id, "Pix", payment_id="pay-1"), 1)
        self.assertEqual(approve_payment_part(self.db, sale_id, "Pix", payment_id="pay-1"), 1)
        self.assertEqual(approve_payment_part(self.db, sale_id, "Pix", payment_id="pay-1"), 0)
        part = get_payment_parts(self.db, sale_id)[0]
        self.assertEqual((part["status"], part["external_reference"], part["payment_id"]), ("approved", "sale-ref", "pay-1"))
        self.assertIsNotNone(part["confirmed_at"])

    def test_cancel_only_changes_active_parts_and_is_idempotent(self):
        sale_id = self.create_sale()
        create_payment_part(self.db, sale_id, "Créditos", 400, "reserved")
        create_payment_part(self.db, sale_id, "Dinheiro", 600, "pending")
        self.assertEqual(cancel_active_payment_parts(self.db, sale_id), 2)
        self.assertEqual(cancel_active_payment_parts(self.db, sale_id), 0)
        self.assertEqual({part["status"] for part in get_payment_parts(self.db, sale_id)}, {"canceled"})

    def test_paid_sale_requires_approved_parts_to_equal_total(self):
        for method in ("Pix", "Dinheiro"):
            with self.subTest(method=method):
                sale_id = self.create_sale(total=800, paid=1, status="approved")
                create_payment_part(self.db, sale_id, "Créditos", 700, "approved")
                create_payment_part(self.db, sale_id, method, 100, "approved")
                self.assertTrue(validate_approved_parts_total(self.db, sale_id))
        self.db.execute(
            "UPDATE sale_payment_parts SET amount_cents=50 WHERE sale_id=? AND method='Dinheiro'",
            (sale_id,),
        )
        with self.assertRaises(RuntimeError):
            validate_approved_parts_total(self.db, sale_id)

    def test_historical_paid_sale_without_parts_remains_valid(self):
        sale_id = self.create_sale(total=1000, paid=1, status="approved")
        self.assertTrue(validate_approved_parts_total(self.db, sale_id))


if __name__ == "__main__":
    unittest.main()
