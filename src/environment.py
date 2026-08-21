"""Centralized runtime environment safeguards."""

import os


def normalize_app_env(value=None):
    raw = value if value is not None else os.getenv("APP_ENV", "production")
    return str(raw).strip().lower() or "production"


def environment_config(value=None):
    app_env = normalize_app_env(value)
    homologation = app_env == "homologation"
    return {
        "APP_ENV": app_env,
        "IS_HOMOLOGATION": homologation,
        "EXTERNAL_PAYMENTS_ENABLED": not homologation,
        "CRON_ENABLED": not homologation,
    }
