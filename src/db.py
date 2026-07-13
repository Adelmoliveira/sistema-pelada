import os
import sqlite3
from flask import g, current_app

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    password_required INTEGER NOT NULL DEFAULT 1,
    role TEXT NOT NULL CHECK(role IN ('manager','staff','client')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    war_name TEXT DEFAULT '',
    cpf TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    emergency_phone TEXT DEFAULT '',
    email TEXT DEFAULT '',
    membership_type TEXT NOT NULL DEFAULT 'regular',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    package_type TEXT NOT NULL DEFAULT '',
    units_per_case INTEGER NOT NULL DEFAULT 0 CHECK(units_per_case >= 0),
    price_cents INTEGER NOT NULL CHECK(price_cents >= 0),
    cost_cents INTEGER NOT NULL DEFAULT 0 CHECK(cost_cents >= 0),
    stock INTEGER NOT NULL DEFAULT 0 CHECK(stock >= 0),
    min_stock INTEGER NOT NULL DEFAULT 5 CHECK(min_stock >= 0),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL REFERENCES players(id),
    payment_method TEXT NOT NULL CHECK(payment_method IN ('Pix','Dinheiro','Débito','Cortesia')),
    total_cents INTEGER NOT NULL,
    paid INTEGER NOT NULL DEFAULT 1,
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sale_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    unit_price_cents INTEGER NOT NULL,
    unit_cost_cents INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS restocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    unit_cost_cents INTEGER NOT NULL DEFAULT 0,
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS stock_adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    user_id INTEGER REFERENCES users(id),
    previous_stock INTEGER NOT NULL CHECK(previous_stock >= 0),
    new_stock INTEGER NOT NULL CHECK(new_stock >= 0),
    difference INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS membership_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL REFERENCES players(id),
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    months_count INTEGER NOT NULL CHECK(months_count BETWEEN 1 AND 12),
    start_month TEXT NOT NULL,
    payment_method TEXT NOT NULL CHECK(payment_method IN ('Pix','Dinheiro','Débito')),
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS membership_months (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id INTEGER NOT NULL REFERENCES membership_payments(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(id),
    month TEXT NOT NULL,
    UNIQUE(player_id, month)
);
CREATE INDEX IF NOT EXISTS idx_sales_created ON sales(created_at);
CREATE INDEX IF NOT EXISTS idx_items_sale ON sale_items(sale_id);
"""

class CursorWrapper:
    def __init__(self, cursor):
        self.cursor = cursor
        self._lastrowid = None

    @property
    def lastrowid(self):
        return self._lastrowid

    @lastrowid.setter
    def lastrowid(self, val):
        self._lastrowid = val

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def close(self):
        self.cursor.close()

    def __getattr__(self, name):
        return getattr(self.cursor, name)

    def __iter__(self):
        return iter(self.cursor)

class DbWrapper:
    def __init__(self, conn, is_postgres=False):
        self.conn = conn
        self.is_postgres = is_postgres

    def execute(self, sql, params=None):
        if self.is_postgres:
            sql_clean = sql.replace('?', '%s')
            
            is_insert = sql_clean.strip().upper().startswith('INSERT')
            if is_insert and 'RETURNING' not in sql_clean.upper():
                sql_clean += ' RETURNING id'

            cursor = self.conn.cursor()
            cursor.execute(sql_clean, params)
            
            wrapped = CursorWrapper(cursor)
            if is_insert:
                try:
                    row = cursor.fetchone()
                    if row:
                        wrapped.lastrowid = row[0]
                except Exception:
                    pass
            return wrapped
        else:
            cursor = self.conn.execute(sql, params or ())
            return CursorWrapper(cursor)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()

def get_db():
    if "db" not in g:
        g.db = connect_db(current_app)
    return g.db

def connect_db(app):
    db_url = os.environ.get("DATABASE_URL") or app.config.get("DATABASE_URL")
    if not db_url:
        # Desenvolvimento local: usa o SQLite já configurado pela aplicação.
        # Na Vercel o filesystem é temporário, portanto o Supabase continua
        # obrigatório para evitar perda silenciosa de dados em produção.
        if os.environ.get("VERCEL") or os.environ.get("NOW_REGION"):
            raise RuntimeError("DATABASE_URL não configurada. Defina a URL do Supabase no ambiente da aplicação.")
        database_path = app.config.get("DATABASE")
        if not database_path:
            raise RuntimeError("Banco local não configurado. Defina DATABASE ou DATABASE_URL.")
        conn = sqlite3.connect(database_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        wrapper = DbWrapper(conn, is_postgres=False)
        init_sqlite(wrapper)
        return wrapper

    if not (db_url.startswith("postgresql://") or db_url.startswith("postgres://")):
        raise RuntimeError("DATABASE_URL inválida. Use uma URL PostgreSQL do Supabase.")

    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(
        db_url,
        sslmode="require",
        connect_timeout=10,
        cursor_factory=psycopg2.extras.DictCursor
    )
    wrapper = DbWrapper(conn, is_postgres=True)
    init_postgres(wrapper)
    return wrapper

def migrate_payment_method(connection):
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sales'"
    ).fetchone()
    if not row or "Fiado" not in (row[0] or ""):
        return
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.executescript("""
        BEGIN;
        ALTER TABLE sale_items RENAME TO sale_items_old;
        ALTER TABLE sales RENAME TO sales_old;
        CREATE TABLE sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL REFERENCES players(id),
            payment_method TEXT NOT NULL CHECK(payment_method IN ('Pix','Dinheiro','Débito','Cortesia')),
            total_cents INTEGER NOT NULL,
            paid INTEGER NOT NULL DEFAULT 1,
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE sale_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
            product_id INTEGER NOT NULL REFERENCES products(id),
            quantity INTEGER NOT NULL CHECK(quantity > 0),
            unit_price_cents INTEGER NOT NULL,
            unit_cost_cents INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO sales(id,player_id,payment_method,total_cents,paid,notes,created_at)
        SELECT id,player_id,CASE WHEN payment_method='Fiado' THEN 'Débito' ELSE payment_method END,
               total_cents,1,notes,created_at FROM sales_old;
        INSERT INTO sale_items SELECT * FROM sale_items_old;
        DROP TABLE sale_items_old;
        DROP TABLE sales_old;
        CREATE INDEX IF NOT EXISTS idx_sales_created ON sales(created_at);
        CREATE INDEX IF NOT EXISTS idx_items_sale ON sale_items(sale_id);
        COMMIT;
    """)
    connection.execute("PRAGMA foreign_keys = ON")

def migrate_product_categories(connection):
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='products'"
    ).fetchone()
    if not row or "CHECK(category IN" not in (row[0] or ""):
        return
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.executescript("""
        BEGIN;
        ALTER TABLE sale_items RENAME TO sale_items_category_old;
        ALTER TABLE restocks RENAME TO restocks_category_old;
        ALTER TABLE products RENAME TO products_category_old;
        CREATE TABLE products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL,
            package_type TEXT NOT NULL DEFAULT '',
            units_per_case INTEGER NOT NULL DEFAULT 0 CHECK(units_per_case >= 0),
            price_cents INTEGER NOT NULL CHECK(price_cents >= 0),
            cost_cents INTEGER NOT NULL DEFAULT 0 CHECK(cost_cents >= 0),
            stock INTEGER NOT NULL DEFAULT 0 CHECK(stock >= 0),
            min_stock INTEGER NOT NULL DEFAULT 5 CHECK(min_stock >= 0),
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE sale_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
            product_id INTEGER NOT NULL REFERENCES products(id),
            quantity INTEGER NOT NULL CHECK(quantity > 0),
            unit_price_cents INTEGER NOT NULL,
            unit_cost_cents INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE restocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id),
            quantity INTEGER NOT NULL CHECK(quantity > 0),
            unit_cost_cents INTEGER NOT NULL DEFAULT 0,
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO products(id,name,category,package_type,units_per_case,price_cents,cost_cents,stock,min_stock,active,created_at)
        SELECT id,name,category,package_type,units_per_case,price_cents,cost_cents,stock,min_stock,active,created_at
        FROM products_category_old;
        INSERT INTO sale_items SELECT * FROM sale_items_category_old;
        INSERT INTO restocks SELECT * FROM restocks_category_old;
        DROP TABLE sale_items_category_old;
        DROP TABLE restocks_category_old;
        DROP TABLE products_category_old;
        CREATE INDEX IF NOT EXISTS idx_items_sale ON sale_items(sale_id);
        COMMIT;
    """)
    connection.execute("PRAGMA foreign_keys = ON")

def init_sqlite(wrapper):
    conn = wrapper.conn
    migrate_payment_method(conn)
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(players)")}
    if "email" not in columns:
        conn.execute("ALTER TABLE players ADD COLUMN email TEXT DEFAULT ''")
        conn.commit()
    if "membership_type" not in columns:
        conn.execute("ALTER TABLE players ADD COLUMN membership_type TEXT NOT NULL DEFAULT 'regular'")
        conn.commit()
    if "war_name" not in columns:
        conn.execute("ALTER TABLE players ADD COLUMN war_name TEXT DEFAULT ''")
    if "emergency_phone" not in columns:
        conn.execute("ALTER TABLE players ADD COLUMN emergency_phone TEXT DEFAULT ''")
    if "cpf" not in columns:
        conn.execute("ALTER TABLE players ADD COLUMN cpf TEXT DEFAULT ''")
    conn.commit()
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_players_cpf ON players(cpf) WHERE cpf<>''")
    conn.commit()
    product_columns = {row[1] for row in conn.execute("PRAGMA table_info(products)")}
    if "package_type" not in product_columns:
        conn.execute("ALTER TABLE products ADD COLUMN package_type TEXT NOT NULL DEFAULT ''")
    if "units_per_case" not in product_columns:
        conn.execute("ALTER TABLE products ADD COLUMN units_per_case INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    migrate_product_categories(conn)
    
    user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if "password_required" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN password_required INTEGER NOT NULL DEFAULT 1")
        conn.commit()

def init_postgres(wrapper):
    wrapper.execute("""
    CREATE OR REPLACE FUNCTION date(t timestamp with time zone) RETURNS date AS $$
        SELECT t::date;
    $$ LANGUAGE SQL IMMUTABLE;
    """)
    wrapper.execute("""
    CREATE OR REPLACE FUNCTION date(t timestamp without time zone) RETURNS date AS $$
        SELECT t::date;
    $$ LANGUAGE SQL IMMUTABLE;
    """)
    wrapper.execute("""
    CREATE OR REPLACE FUNCTION date(t text) RETURNS date AS $$
        SELECT t::date;
    $$ LANGUAGE SQL IMMUTABLE;
    """)
    
    pg_schema = SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    pg_schema = pg_schema.replace("COLLATE NOCASE", "")
    pg_schema = pg_schema.replace("created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP", "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP")
    
    for stmt in pg_schema.split(';'):
        stmt_clean = stmt.strip()
        if stmt_clean:
            wrapper.execute(stmt_clean)
    
    # Run migration to add password_required if not exists in postgres
    wrapper.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_required INTEGER NOT NULL DEFAULT 1")
    wrapper.commit()
