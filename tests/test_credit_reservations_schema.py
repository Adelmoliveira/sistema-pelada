import sqlite3
import unittest

from src.db import SCHEMA


class CreditReservationsSchemaTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        self.player_id = self.db.execute(
            "INSERT INTO players(name) VALUES(?)", ("Teste Reserva",)
        ).lastrowid
        self.sale_id = self.db.execute(
            """INSERT INTO sales(player_id,payment_method,total_cents,paid,payment_status)
               VALUES(?, 'Pix', 1000, 0, 'pending')""",
            (self.player_id,),
        ).lastrowid

    def tearDown(self):
        self.db.close()

    def test_table_has_equivalent_sqlite_structure(self):
        columns = {
            row[1]: (row[2], row[3], row[4], row[5])
            for row in self.db.execute("PRAGMA table_info(bar_credit_reservations)")
        }
        self.assertEqual(
            set(columns),
            {
                "id", "sale_id", "player_id", "amount_cents", "status",
                "created_at", "expires_at", "consumed_at", "released_at",
            },
        )
        self.assertEqual(columns["status"][2], "'reserved'")

    def test_sale_accepts_at_most_one_reservation(self):
        self.db.execute(
            "INSERT INTO bar_credit_reservations(sale_id,player_id,amount_cents) VALUES(?,?,?)",
            (self.sale_id, self.player_id, 500),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO bar_credit_reservations(sale_id,player_id,amount_cents) VALUES(?,?,?)",
                (self.sale_id, self.player_id, 100),
            )

    def test_non_positive_amount_is_rejected(self):
        for amount in (0, -1):
            with self.subTest(amount=amount), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(
                    "INSERT INTO bar_credit_reservations(sale_id,player_id,amount_cents) VALUES(?,?,?)",
                    (self.sale_id, self.player_id, amount),
                )

    def test_invalid_status_is_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                """INSERT INTO bar_credit_reservations
                   (sale_id,player_id,amount_cents,status) VALUES(?,?,?,'invalid')""",
                (self.sale_id, self.player_id, 500),
            )

    def test_sale_and_player_foreign_keys_are_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO bar_credit_reservations(sale_id,player_id,amount_cents) VALUES(?,?,?)",
                (999999, self.player_id, 500),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO bar_credit_reservations(sale_id,player_id,amount_cents) VALUES(?,?,?)",
                (self.sale_id, 999999, 500),
            )


if __name__ == "__main__":
    unittest.main()
