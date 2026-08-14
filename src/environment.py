"""Centralized runtime environment safeguards."""

import os
from pathlib import Path


LOCAL_ENV_FILENAME = ".env.development.local"
LEGACY_ENV_FILENAME = ".env.local"

_LOCAL_BLOCKED_KEYS = {
    "DATABASE_URL",
    "SUPABASE_DB_URL",
    "NOW_REGION",
    "APPLY_POSTGRES_MIGRATIONS",
    "APPLY_RLS",
}
_LOCAL_BLOCKED_PREFIXES = (
    "MERCADOPAGO_",
    "GMAIL_",
    "VAPID_",
    "VERCEL",
    "CRON_",
)


def normalize_app_env(value=None):
    raw = value if value is not None else os.getenv("APP_ENV", "production")
    normalized = str(raw).strip().lower() or "production"
    if normalized in {"homologacao", "homologação"}:
        return "homologation"
    return normalized


def _is_vercel(environ):
    return bool(environ.get("VERCEL") or environ.get("NOW_REGION"))


def sanitize_local_environment(environ=None):
    """Remove remote-service configuration from a local process."""
    environ = os.environ if environ is None else environ
    for key in tuple(environ):
        if key in _LOCAL_BLOCKED_KEYS or key.startswith(_LOCAL_BLOCKED_PREFIXES):
            environ.pop(key, None)


def configure_runtime_environment(project_root, environ=None, loader=None):
    """Select and load the one dotenv file allowed for this runtime."""
    environ = os.environ if environ is None else environ
    running_on_vercel = _is_vercel(environ)
    default_env = "production" if running_on_vercel else "local"
    app_env = normalize_app_env(environ.get("APP_ENV", default_env))
    environ["APP_ENV"] = app_env

    selected_file = None
    if not running_on_vercel:
        filename = LOCAL_ENV_FILENAME if app_env == "local" else LEGACY_ENV_FILENAME
        candidate = Path(project_root) / filename
        if candidate.exists():
            selected_file = candidate
            if loader is not None:
                loader(candidate, override=False)

    if app_env == "local":
        sanitize_local_environment(environ)

    return selected_file


def environment_config(value=None):
    app_env = normalize_app_env(value)
    homologation = app_env == "homologation"
    production = app_env == "production"
    return {
        "APP_ENV": app_env,
        "IS_HOMOLOGATION": homologation,
        "EXTERNAL_PAYMENTS_ENABLED": production,
        "CRON_ENABLED": production,
    }
