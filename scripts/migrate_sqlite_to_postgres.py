import os
import sqlite3
import psycopg2
from psycopg2 import sql

SQLITE_DB = 'bar.db'
POSTGRES_URL = os.environ.get('DATABASE_URL')

if not POSTGRES_URL:
    raise SystemExit('Defina DATABASE_URL com a URL do PostgreSQL do Supabase antes de rodar este script.')

sqlite_conn = sqlite3.connect(SQLITE_DB)
sqlite_conn.row_factory = sqlite3.Row

pg_conn = psycopg2.connect(POSTGRES_URL, sslmode='require')
pg_conn.autocommit = False
cur = pg_conn.cursor()

# Cria as tabelas básicas do esquema em PostgreSQL
schema_sql = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    password_required INTEGER NOT NULL DEFAULT 1,
    role TEXT NOT NULL CHECK(role IN ('manager','staff','client','infra','maintenance')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS players (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    war_name TEXT DEFAULT '',
    cpf TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    emergency_phone TEXT DEFAULT '',
    email TEXT DEFAULT '',
    membership_type TEXT NOT NULL DEFAULT 'regular',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS products (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    package_type TEXT NOT NULL DEFAULT '',
    units_per_case INTEGER NOT NULL DEFAULT 0,
    price_cents INTEGER NOT NULL,
    cost_cents INTEGER NOT NULL DEFAULT 0,
    stock INTEGER NOT NULL DEFAULT 0,
    min_stock INTEGER NOT NULL DEFAULT 5,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sales (
    id SERIAL PRIMARY KEY,
    player_id INTEGER NOT NULL REFERENCES players(id),
    payment_method TEXT NOT NULL CHECK(payment_method IN ('Pix','Dinheiro','Débito','Cortesia')),
    total_cents INTEGER NOT NULL,
    paid INTEGER NOT NULL DEFAULT 1,
    notes TEXT DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sale_items (
    id SERIAL PRIMARY KEY,
    sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL,
    unit_price_cents INTEGER NOT NULL,
    unit_cost_cents INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS restocks (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL,
    unit_cost_cents INTEGER NOT NULL DEFAULT 0,
    notes TEXT DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS membership_payments (
    id SERIAL PRIMARY KEY,
    player_id INTEGER NOT NULL REFERENCES players(id),
    amount_cents INTEGER NOT NULL,
    months_count INTEGER NOT NULL,
    start_month TEXT NOT NULL,
    payment_method TEXT NOT NULL CHECK(payment_method IN ('Pix','Dinheiro','Débito')),
    notes TEXT DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS membership_months (
    id SERIAL PRIMARY KEY,
    payment_id INTEGER NOT NULL REFERENCES membership_payments(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(id),
    month TEXT NOT NULL,
    UNIQUE(player_id, month)
);
"""
cur.execute(schema_sql)

for table in ['users','players','products','sales','sale_items','restocks','membership_payments','membership_months']:
    rows = sqlite_conn.execute(f'SELECT * FROM {table}').fetchall()
    if not rows:
        continue
    columns = [col[1] for col in sqlite_conn.execute(f'PRAGMA table_info({table})')]
    placeholders = ', '.join(['%s'] * len(columns))
    cols_sql = ', '.join([sql.Identifier(col).as_string(cur) for col in columns])
    insert_sql = sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(
        sql.Identifier(table),
        sql.SQL(', ').join(map(sql.Identifier, columns)),
        sql.SQL(', ').join(sql.Placeholder() * len(columns))
    )
    for row in rows:
        values = [row[col] for col in columns]
        cur.execute(insert_sql, values)

pg_conn.commit()
print('Migração concluída com sucesso.')

sqlite_conn.close()
pg_conn.close()
