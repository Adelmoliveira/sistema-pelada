#!/usr/bin/env python3
"""Apply the PostgreSQL schema and migrations outside the HTTP request path.

Usage:
    DATABASE_URL='postgresql://...' python scripts/migrate_postgres_schema.py

Run this once per deployment (or from a controlled release job). It is
intentionally not called by ``app.py`` or by Vercel request handlers.
"""

import os
import sys

from src.db import run_postgres_migrations


def main():
    database_url = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    if not database_url:
        raise SystemExit("Defina DATABASE_URL ou SUPABASE_DB_URL antes de executar a migração.")
    try:
        run_postgres_migrations(database_url)
    except Exception as exc:
        print(f"Falha na migração PostgreSQL ({type(exc).__name__}): {exc}", file=sys.stderr)
        raise SystemExit(1)
    print("Migração PostgreSQL concluída com sucesso.")


if __name__ == "__main__":
    main()
