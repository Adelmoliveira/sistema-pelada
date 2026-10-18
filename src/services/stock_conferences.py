"""Transactional conference operations; legacy audit identity is exact, never month-only."""
from contextlib import contextmanager

from src.catalog import SPORTS_MATERIAL_CATEGORY


@contextmanager
def conference_transaction(db):
    # SQLite readers cannot safely upgrade a deferred transaction after a writer.
    # Take its writer lock before reading. PostgreSQL uses row locks below.
    if not db.is_postgres:
        db.execute("BEGIN IMMEDIATE")
    with db:
        yield


def application(db, conference_id):
    row = db.execute("""SELECT a.created_at,u.name applied_by_name
        FROM stock_conference_applications a LEFT JOIN users u ON u.id=a.user_id
        WHERE a.conference_id=?""", (conference_id,)).fetchone()
    if row:
        return dict(row)
    # The existing writer includes the immutable ID in this exact event text.
    row = db.execute("""SELECT a.created_at,u.name applied_by_name
        FROM stock_conference_audit a LEFT JOIN users u ON u.id=a.user_id
        WHERE a.action='APLICADA' AND a.details=? ORDER BY a.id LIMIT 1""",
        (f"Conferência #{conference_id} aplicada ao estoque.",)).fetchone()
    return dict(row) if row else None


def locked_conference(db, conference_id):
    suffix = " FOR UPDATE" if db.is_postgres else ""
    row = db.execute("SELECT * FROM stock_conferences WHERE id=?" + suffix, (conference_id,)).fetchone()
    if not row:
        raise ValueError("Conferência não encontrada.")
    return row


def apply_conference(db, conference_id, user_id):
    changed = []
    with conference_transaction(db):
        conference = locked_conference(db, conference_id)
        if application(db, conference_id):
            raise ValueError("Esta conferência já foi aplicada ao estoque.")
        items = db.execute("SELECT * FROM stock_conference_items WHERE conference_id=? ORDER BY product_id,id", (conference_id,)).fetchall()
        if not items or len({i['product_id'] for i in items}) != len(items):
            raise ValueError("Conferência sem itens válidos. Revise a conferência.")
        # UNIQUE conference_id is the durable database-level idempotency guard.
        db.execute("INSERT INTO stock_conference_applications(conference_id,user_id) VALUES(?,?)", (conference_id,user_id))
        for item in items:
            suffix = " FOR UPDATE" if db.is_postgres else ""
            product = db.execute("SELECT * FROM products WHERE id=?" + suffix, (item['product_id'],)).fetchone()
            if not product or not product['active'] or product['category'] == SPORTS_MATERIAL_CATEGORY:
                raise ValueError("Produto excluído, desativado ou fora do Bar. Revise a conferência.")
            if item['expected_revision'] is None:
                raise ValueError("Conferência histórica sem versão do estoque. Revise e registre uma nova contagem; nenhum ajuste foi aplicado.")
            if product['stock'] != item['expected_stock'] or product['stock_revision'] != item['expected_revision']:
                raise ValueError(f"O estoque de {product['name']} mudou desde a conferência. Revise a conferência e registre uma nova contagem.")
            difference = item['physical_stock'] - item['expected_stock']
            if item['physical_stock'] < 0 or difference != item['difference'] or (difference and not item['reason'].strip()):
                raise ValueError("Item de conferência inválido. Revise a conferência.")
            if difference:
                updated = db.execute("""UPDATE products SET stock=? WHERE id=? AND stock=? AND stock_revision=? AND active=1""",
                    (item['physical_stock'],product['id'],item['expected_stock'],item['expected_revision']))
                if updated.rowcount != 1:
                    raise ValueError("O estoque mudou durante a aplicação. Revise a conferência.")
                db.execute("""INSERT INTO stock_adjustments(product_id,user_id,conference_id,previous_stock,new_stock,difference,reason)
                    VALUES(?,?,?,?,?,?,?)""", (product['id'],user_id,conference_id,product['stock'],item['physical_stock'],difference,
                    f"Conferência mensal {conference['conference_month']}: {item['reason']}"))
                changed.append(product['id'])
        db.execute("INSERT INTO stock_conference_audit(conference_month,action,details,user_id) VALUES(?,?,?,?)",
            (conference['conference_month'],'APLICADA',f'Conferência #{conference_id} aplicada ao estoque.',user_id))
    return changed
