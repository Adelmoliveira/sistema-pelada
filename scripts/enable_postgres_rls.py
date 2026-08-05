"""Enable Row Level Security on every application table in public schema.

The application connects with the PostgreSQL owner role, so this does not
change its behavior. Supabase API roles (anon/authenticated) have no policies
and therefore receive no rows unless an explicit policy is added later.

Run explicitly with:
    APPLY_RLS=1 PYTHONPATH=. .venv/bin/python scripts/enable_postgres_rls.py
"""

from __future__ import annotations

import os
import sys

import psycopg2
from psycopg2 import sql


def main() -> int:
    if os.environ.get("APPLY_RLS") != "1":
        raise SystemExit("Defina APPLY_RLS=1 para confirmar a ativação do RLS.")

    dsn = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        raise SystemExit("Defina DATABASE_URL ou SUPABASE_DB_URL antes de executar.")

    conn = psycopg2.connect(dsn)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT tablename
                    FROM pg_catalog.pg_tables
                    WHERE schemaname = 'public'
                    ORDER BY tablename
                    """
                )
                tables = [row[0] for row in cur.fetchall()]
                for table in tables:
                    cur.execute(
                        sql.SQL("ALTER TABLE public.{} ENABLE ROW LEVEL SECURITY").format(
                            sql.Identifier(table)
                        )
                    )
                print(f"RLS ativado em {len(tables)} tabela(s) públicas.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
