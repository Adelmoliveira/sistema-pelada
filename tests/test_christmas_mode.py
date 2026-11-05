import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["DATABASE_URL"] = ""
os.environ["SUPABASE_DB_URL"] = ""

from app import app
from src.db import get_db, initialize_sqlite_database
from src.routes.auth import CHRISTMAS_MODE_SETTING_KEY, christmas_mode_enabled, make_password_hash


class ChristmasModeSettingsTests(unittest.TestCase):
    def setUp(self):
        self.original_config = dict(app.config)
        self.tempdir = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True,
            DATABASE=str(Path(self.tempdir.name) / "test.db"),
            DATABASE_URL=None,
            SECRET_KEY="christmas-test-secret",
            WTF_CSRF_ENABLED=False,
        )
        initialize_sqlite_database(app.config["DATABASE"])
        with app.app_context():
            db = get_db()
            manager = db.execute(
                "INSERT INTO users(username,name,password_hash,role) VALUES(?,?,?,'manager')",
                ("manager-natal", "Gestor Natal", make_password_hash("senha-natal")),
            )
            staff = db.execute(
                "INSERT INTO users(username,name,password_hash,role) VALUES(?,?,?,'staff')",
                ("staff-natal", "Staff Natal", make_password_hash("senha-staff")),
            )
            db.execute(
                "INSERT INTO app_settings(key,value) VALUES(?,?)",
                ("unrelated_setting", "preserve-me"),
            )
            db.commit()
            self.manager_id = manager.lastrowid
            self.staff_id = staff.lastrowid
        self.client = app.test_client()

    def tearDown(self):
        app.config.clear()
        app.config.update(self.original_config)
        self.tempdir.cleanup()

    def login_as(self, user_id):
        with self.client.session_transaction() as session:
            session["user_id"] = user_id

    def setting_value(self, key=CHRISTMAS_MODE_SETTING_KEY):
        with app.app_context():
            row = get_db().execute(
                "SELECT value FROM app_settings WHERE key=?", (key,)
            ).fetchone()
            return row["value"] if row else None

    def set_christmas_mode(self, value):
        with app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO app_settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (CHRISTMAS_MODE_SETTING_KEY, value),
            )
            db.commit()

    def test_default_and_invalid_stored_values_are_disabled(self):
        with app.app_context():
            self.assertFalse(christmas_mode_enabled())
            db = get_db()
            db.execute(
                "INSERT INTO app_settings(key,value) VALUES(?,?)",
                (CHRISTMAS_MODE_SETTING_KEY, "yes"),
            )
            db.commit()
            self.assertFalse(christmas_mode_enabled())

    def test_manager_can_enable_then_disable_and_setting_persists(self):
        self.login_as(self.manager_id)
        enabled = self.client.post(
            "/administracao/modo-natal", data={"enabled": "1"}
        )
        self.assertEqual(enabled.status_code, 303)
        self.assertEqual(self.setting_value(), "1")
        page = self.client.get("/administracao/modo-natal")
        self.assertIn("Ativado", page.get_data(as_text=True))

        disabled = self.client.post(
            "/administracao/modo-natal", data={"enabled": "0"}
        )
        self.assertEqual(disabled.status_code, 303)
        self.assertEqual(self.setting_value(), "0")

    def test_invalid_value_is_rejected_without_changing_settings(self):
        self.login_as(self.manager_id)
        response = self.client.post(
            "/administracao/modo-natal", data={"enabled": "true"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.setting_value())
        self.assertEqual(self.setting_value("unrelated_setting"), "preserve-me")

    def test_only_manager_can_access_settings(self):
        self.login_as(self.staff_id)
        response = self.client.get("/administracao/modo-natal")
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("Modo Natal</a>", response.get_data(as_text=True))

    def test_csrf_is_required_for_updates(self):
        app.config["WTF_CSRF_ENABLED"] = True
        self.login_as(self.manager_id)
        response = self.client.post(
            "/administracao/modo-natal", data={"enabled": "1"}
        )
        self.assertEqual(response.status_code, 303)
        self.assertIsNone(self.setting_value())

    def test_read_failure_does_not_prevent_login_page(self):
        with patch("src.routes.auth.get_db", side_effect=RuntimeError("read failed")):
            response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn("PELADEIROS GPCTA", response.get_data(as_text=True))

    def test_login_works_with_mode_inactive_and_active(self):
        inactive = self.client.post(
            "/login", data={"username": "manager-natal", "password": "senha-natal"}
        )
        self.assertEqual(inactive.status_code, 303)

        with app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO app_settings(key,value) VALUES(?,?)",
                (CHRISTMAS_MODE_SETTING_KEY, "1"),
            )
            db.commit()
        with self.client.session_transaction() as session:
            session.clear()
        active = self.client.post(
            "/login", data={"username": "manager-natal", "password": "senha-natal"}
        )
        self.assertEqual(active.status_code, 303)

    def test_update_preserves_unrelated_settings(self):
        self.login_as(self.manager_id)
        self.client.post("/administracao/modo-natal", data={"enabled": "1"})
        self.assertEqual(self.setting_value("unrelated_setting"), "preserve-me")

    def test_login_renders_hat_and_versioned_css_only_when_enabled(self):
        disabled = self.client.get("/login").get_data(as_text=True)
        self.assertNotIn("christmas-santa-hat", disabled)
        self.assertNotIn("christmas-mode.css", disabled)

        self.set_christmas_mode("1")
        enabled = self.client.get("/login").get_data(as_text=True)
        self.assertIn('class="christmas-santa-hat"', enabled)
        self.assertIn('aria-hidden="true"', enabled)
        self.assertIn('focusable="false"', enabled)
        self.assertIn("christmas/santa-hat-realistic.png?v=20261105-3", enabled)
        self.assertIn("christmas-mode.css?v=20261105-5", enabled)

    def test_christmas_decoration_preserves_logo_and_club_card_button(self):
        self.set_christmas_mode("1")
        page = self.client.get("/login").get_data(as_text=True)
        self.assertIn("/branding/logo", page)
        self.assertIn('id="club-card-logo"', page)
        self.assertIn('aria-label="Abrir carteirinha GPCTA"', page)
        self.assertIn("gpcta-club-card-token", page)
        self.assertIn("fetch(target,{method:'HEAD',cache:'no-store'})", page)

    def test_hat_asset_is_local_noninteractive_and_responsive(self):
        css = self.client.get("/static/christmas-mode.css").get_data(as_text=True)
        self.assertIn("pointer-events: none", css)
        self.assertIn("position: absolute", css)
        self.assertIn("@media (max-width: 575.98px)", css)
        png = self.client.get("/static/christmas/santa-hat-realistic.png")
        self.assertEqual(png.status_code, 200)
        self.assertTrue(png.data.startswith(b"\x89PNG\r\n\x1a\n"))


if __name__ == "__main__":
    unittest.main()
