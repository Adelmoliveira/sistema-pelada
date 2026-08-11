#!/usr/bin/env python3
"""Apply the PostgreSQL schema and migrations outside the HTTP request path.

Usage:
    DATABASE_URL='postgresql://...' python scripts/migrate_postgres_schema.py

Run this once per deployment (or from a controlled release job). It is
intentionally not called by ``app.py`` or by Vercel request handlers.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    # Load local variables without requiring users to `source` the file.
    # This safely handles tokens/passwords containing shell metacharacters.
    load_dotenv(".env.local")
except ImportError:
    pass

from src.db import run_postgres_migrations


def main():
    if os.environ.get("APPLY_POSTGRES_MIGRATIONS") != "1":
        raise SystemExit("Defina APPLY_POSTGRES_MIGRATIONS=1 para confirmar a execução controlada.")
    database_url = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    if not database_url:
        raise SystemExit("Defina DATABASE_URL ou SUPABASE_DB_URL antes de executar a migração.")
    try:
        applied = run_postgres_migrations(database_url)
    except RuntimeError as exc:
        print(f"Falha na migração PostgreSQL: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print(f"Falha na migração PostgreSQL ({type(exc).__name__}).", file=sys.stderr)
        raise SystemExit(1)
    if applied:
        print("Migrations aplicadas:")
        for version in applied:
            print(f"- {version}")
    else:
        print("Banco já está atualizado; nenhuma migration pendente.")


if __name__ == "__main__":
    main()
