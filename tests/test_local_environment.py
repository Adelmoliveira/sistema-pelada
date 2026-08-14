import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.db import connect_db
from src.environment import configure_runtime_environment, environment_config


class LocalEnvironmentIsolationTest(unittest.TestCase):
    def test_local_loads_only_development_file_and_removes_remote_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env.local").write_text(
                "DATABASE_URL=postgresql://remote.invalid/db\n"
                "MERCADOPAGO_ACCESS_TOKEN=remote-token\n",
                encoding="utf-8",
            )
            local_database = root / "isolated.db"
            (root / ".env.development.local").write_text(
                f"APP_ENV=local\nDATABASE_PATH={local_database}\nSECRET_KEY=disposable\n",
                encoding="utf-8",
            )
            environ = {
                "APP_ENV": "local",
                "DATABASE_URL": "postgresql://inherited.invalid/db",
                "SUPABASE_DB_URL": "postgresql://inherited.invalid/other",
                "MERCADOPAGO_ACCESS_TOKEN": "inherited-token",
                "GMAIL_APP_PASSWORD": "inherited-password",
                "VAPID_PRIVATE_KEY": "inherited-key",
                "CRON_SECRET": "inherited-secret",
                "VERCEL_OIDC_TOKEN": "inherited-token",
            }
            loaded = []

            def loader(path, override=False):
                loaded.append((Path(path).name, override))
                for line in Path(path).read_text(encoding="utf-8").splitlines():
                    key, value = line.split("=", 1)
                    environ.setdefault(key, value)

            selected = configure_runtime_environment(root, environ=environ, loader=loader)

            self.assertEqual(selected, root / ".env.development.local")
            self.assertEqual(loaded, [(".env.development.local", False)])
            self.assertEqual(environ["APP_ENV"], "local")
            self.assertEqual(environ["DATABASE_PATH"], str(local_database))
            for key in (
                "DATABASE_URL",
                "SUPABASE_DB_URL",
                "MERCADOPAGO_ACCESS_TOKEN",
                "GMAIL_APP_PASSWORD",
                "VAPID_PRIVATE_KEY",
                "CRON_SECRET",
                "VERCEL_OIDC_TOKEN",
            ):
                self.assertNotIn(key, environ)

    def test_local_database_connection_ignores_postgres_urls(self):
        isolated_database = "/tmp/sistema-pelada-isolated-test.db"
        configured_app = SimpleNamespace(config={
            "APP_ENV": "local",
            "DATABASE": isolated_database,
            "DATABASE_URL": "postgresql://configured.invalid/db",
        })
        sentinel = object()
        inherited = {
            "DATABASE_URL": "postgresql://inherited.invalid/db",
            "SUPABASE_DB_URL": "postgresql://inherited.invalid/other",
            "VERCEL": "",
            "NOW_REGION": "",
        }
        with patch.dict(os.environ, inherited, clear=False), \
             patch("src.db.connect_sqlite", return_value=sentinel) as sqlite_connection, \
             patch("psycopg2.connect") as postgres_connection:
            result = connect_db(configured_app)

        self.assertIs(result, sentinel)
        sqlite_connection.assert_called_once_with(isolated_database)
        postgres_connection.assert_not_called()

    def test_local_disables_external_payments_and_cron(self):
        config = environment_config("local")
        self.assertEqual(config["APP_ENV"], "local")
        self.assertFalse(config["EXTERNAL_PAYMENTS_ENABLED"])
        self.assertFalse(config["CRON_ENABLED"])

    def test_homologacao_alias_remains_isolated(self):
        config = environment_config("homologacao")
        self.assertEqual(config["APP_ENV"], "homologation")
        self.assertTrue(config["IS_HOMOLOGATION"])
        self.assertFalse(config["EXTERNAL_PAYMENTS_ENABLED"])
        self.assertFalse(config["CRON_ENABLED"])

    def test_production_keeps_external_integrations_enabled(self):
        config = environment_config("production")
        self.assertTrue(config["EXTERNAL_PAYMENTS_ENABLED"])
        self.assertTrue(config["CRON_ENABLED"])

    def test_vercel_uses_injected_environment_without_loading_dotenv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env.local").write_text("DATABASE_URL=wrong\n", encoding="utf-8")
            environ = {
                "VERCEL": "1",
                "APP_ENV": "production",
                "DATABASE_URL": "postgresql://injected.invalid/db",
            }
            loaded = []

            selected = configure_runtime_environment(
                root,
                environ=environ,
                loader=lambda *args, **kwargs: loaded.append((args, kwargs)),
            )

            self.assertIsNone(selected)
            self.assertEqual(loaded, [])
            self.assertEqual(environ["DATABASE_URL"], "postgresql://injected.invalid/db")

    def test_application_uses_the_configured_isolated_database_path(self):
        from app import app

        expected = Path(__file__).resolve().parents[1] / "bar_local.db"
        self.assertEqual(app.config["APP_ENV"], "local")
        self.assertEqual(Path(app.config["DATABASE"]), expected)
        self.assertIsNone(app.config["DATABASE_URL"])


if __name__ == "__main__":
    unittest.main()
