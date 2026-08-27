import sqlite3
import unittest

from flask import Flask

from src.db import DbWrapper, SCHEMA
from src.services.bar_credits import (
    available_balance,
    consume,
    consume_reservation,
    credit_cash_change,
    release_reservation,
    reserve_credit,
)


class BarCreditReservationServiceTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["BAR_CREDIT_LOW_THRESHOLD_CENTS"] = 100
        self.context = self.app.app_context()
        self.context.push()
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(SCHEMA)
        self.db = DbWrapper(connection)
        self.player_id = self.db.execute(
            "INSERT INTO players(name) VALUES(?)", ("Carteira Teste",)
        ).lastrowid
        self.db.execute(
            "INSERT INTO bar_credit_accounts(player_id,balance_cents) VALUES(?,1000)",
            (self.player_id,),
        )

    def tearDown(self):
        self.db.close()
        self.context.pop()

    def create_sale(self, payment_method="Pix"):
        return self.db.execute(
            """INSERT INTO sales(player_id,payment_method,total_cents,paid,payment_status)
               VALUES(?,?,1000,0,'pending')""",
            (self.player_id, payment_method),
        ).lastrowid

    def ledger_balance(self):
        return self.db.execute(
            "SELECT balance_cents FROM bar_credit_accounts WHERE player_id=?",
            (self.player_id,),
        ).fetchone()["balance_cents"]

    def test_reservation_reduces_only_available_balance(self):
        sale_id = self.create_sale()
        reserve_credit(self.db, self.player_id, sale_id, 400)
        self.assertEqual(self.ledger_balance(), 1000)
        self.assertEqual(available_balance(self.db, self.player_id), 600)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) AS total FROM bar_credit_transactions").fetchone()["total"],
            0,
        )

    def test_reservations_cannot_promise_the_same_balance_twice(self):
        reserve_credit(self.db, self.player_id, self.create_sale(), 400)
        with self.assertRaisesRegex(ValueError, "disponível insuficiente"):
            reserve_credit(self.db, self.player_id, self.create_sale(), 700)
        self.assertEqual(available_balance(self.db, self.player_id), 600)

    def test_compatible_repeated_reservation_is_idempotent(self):
        sale_id = self.create_sale()
        first = reserve_credit(self.db, self.player_id, sale_id, 400)
        repeated = reserve_credit(self.db, self.player_id, sale_id, 400)
        self.assertEqual(first["id"], repeated["id"])
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) AS total FROM bar_credit_reservations").fetchone()["total"],
            1,
        )

    def test_consumption_is_atomic_and_idempotent(self):
        sale_id = self.create_sale()
        reserve_credit(self.db, self.player_id, sale_id, 400)
        self.assertEqual(consume_reservation(self.db, sale_id), (600, True))
        self.assertEqual(self.ledger_balance(), 600)
        self.assertEqual(available_balance(self.db, self.player_id), 600)
        reservation = self.db.execute(
            "SELECT status,consumed_at FROM bar_credit_reservations WHERE sale_id=?", (sale_id,)
        ).fetchone()
        self.assertEqual(reservation["status"], "consumed")
        self.assertIsNotNone(reservation["consumed_at"])

        self.assertEqual(consume_reservation(self.db, sale_id), (600, False))
        self.assertEqual(self.ledger_balance(), 600)
        self.assertEqual(
            self.db.execute(
                """SELECT COUNT(*) AS total FROM bar_credit_transactions
                   WHERE sale_id=? AND type='CONSUMPTION'""", (sale_id,)
            ).fetchone()["total"],
            1,
        )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) AS total FROM bar_credit_audit WHERE reason=?",
                (f"Venda #{sale_id}",),
            ).fetchone()["total"],
            1,
        )

    def test_release_is_idempotent_and_does_not_change_ledger(self):
        sale_id = self.create_sale()
        reserve_credit(self.db, self.player_id, sale_id, 400)
        self.assertTrue(release_reservation(self.db, sale_id))
        self.assertFalse(release_reservation(self.db, sale_id))
        self.assertEqual(self.ledger_balance(), 1000)
        self.assertEqual(available_balance(self.db, self.player_id), 1000)
        reservation = self.db.execute(
            "SELECT status,released_at FROM bar_credit_reservations WHERE sale_id=?", (sale_id,)
        ).fetchone()
        self.assertEqual(reservation["status"], "released")
        self.assertIsNotNone(reservation["released_at"])

    def test_expired_reservation_does_not_reduce_available_balance(self):
        reserve_credit(
            self.db, self.player_id, self.create_sale(), 400,
            expires_at="2000-01-01 00:00:00",
        )
        self.assertEqual(available_balance(self.db, self.player_id), 1000)

    def test_existing_consumption_and_cash_change_remain_unchanged(self):
        consumed_sale = self.create_sale(payment_method="Créditos")
        self.assertEqual(consume(self.db, self.player_id, 200, consumed_sale)[0], 800)
        change_sale = self.create_sale(payment_method="Dinheiro")
        self.assertEqual(credit_cash_change(self.db, self.player_id, 100, change_sale)[0], 900)
        self.assertEqual(credit_cash_change(self.db, self.player_id, 100, change_sale), (900, False))


if __name__ == "__main__":
    unittest.main()
