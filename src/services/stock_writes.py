"""Stock writers that share the conference's locking and audit transaction."""
from src.services.stock_conferences import conference_transaction


def _product(db, product_id):
    suffix = ' FOR UPDATE' if db.is_postgres else ''
    product = db.execute('SELECT * FROM products WHERE id=?' + suffix, (product_id,)).fetchone()
    if not product:
        raise ValueError('Produto não encontrado. Recarregue a página.')
    return product


def update_product_stock(db, product_id, expected_revision, fields, user_id, reason):
    allowed = {'name', 'category', 'package_type', 'units_per_case', 'price_cents',
               'cost_cents', 'min_stock', 'stock', 'supplier_email', 'photo_data',
               'thumbnail_data', 'expiry_date', 'case_sale_enabled'}
    if not fields or set(fields) - allowed or 'stock' not in fields:
        raise ValueError('Campos de produto inválidos.')
    with conference_transaction(db):
        previous = _product(db, product_id)
        if previous['stock_revision'] != expected_revision:
            raise ValueError('O estoque mudou desde a leitura do formulário. Recarregue e revise o ajuste.')
        difference = fields['stock'] - previous['stock']
        if fields['stock'] < 0 or (difference and not reason):
            raise ValueError('Informe uma quantidade válida e o motivo do ajuste de estoque.')
        assignments = ','.join(f'{key}=?' for key in fields)
        updated = db.execute(f'UPDATE products SET {assignments} WHERE id=? AND stock_revision=?',
                             tuple(fields.values()) + (product_id, expected_revision))
        if updated.rowcount != 1:
            raise ValueError('O estoque mudou durante o ajuste. Recarregue a página.')
        if difference:
            db.execute('''INSERT INTO stock_adjustments
                (product_id,user_id,previous_stock,new_stock,difference,reason) VALUES(?,?,?,?,?,?)''',
                (product_id, user_id, previous['stock'], fields['stock'], difference, reason))


def correct_restock_stock(db, restock_id, quantity, cost, reason, user_id):
    if quantity < 0 or cost < 0 or len(reason.strip()) < 5:
        raise ValueError('Informe quantidade, custo e motivo válidos para a correção.')
    with conference_transaction(db):
        reference = db.execute('SELECT product_id FROM restocks WHERE id=?', (restock_id,)).fetchone()
        if not reference:
            raise ValueError('Reposição não encontrada.')
        # All writers take the product lock first. Re-read the correction history
        # only after this lock: never reuse the balance/history read by the page.
        product = _product(db, reference['product_id'])
        suffix = ' FOR UPDATE' if db.is_postgres else ''
        restock = db.execute('SELECT * FROM restocks WHERE id=?' + suffix, (restock_id,)).fetchone()
        if not restock or restock['product_id'] != product['id']:
            raise ValueError('Reposição alterada. Recarregue a página.')
        correction = db.execute('SELECT * FROM restock_corrections WHERE restock_id=? ORDER BY id DESC LIMIT 1',
                                (restock_id,)).fetchone()
        previous_quantity = correction['corrected_quantity'] if correction else restock['quantity']
        previous_cost = correction['corrected_unit_cost_cents'] if correction else restock['unit_cost_cents']
        difference = quantity - previous_quantity
        new_stock = product['stock'] + difference
        if new_stock < 0:
            raise ValueError('Não é possível reduzir essa quantidade porque parte do estoque já foi utilizada.')
        updated = db.execute('''UPDATE products SET stock=stock+?,
            cost_cents=CASE WHEN ?=(SELECT MAX(id) FROM restocks WHERE product_id=?)
                           THEN ? ELSE cost_cents END
            WHERE id=? AND stock_revision=? AND stock+?>=0''',
            (difference, restock_id, product['id'], cost, product['id'], product['stock_revision'], difference))
        if updated.rowcount != 1:
            raise ValueError('O estoque mudou durante a correção. Recarregue a página.')
        db.execute('''INSERT INTO restock_corrections
            (restock_id,previous_quantity,corrected_quantity,previous_unit_cost_cents,
             corrected_unit_cost_cents,reason,created_by) VALUES(?,?,?,?,?,?,?)''',
            (restock_id, previous_quantity, quantity, previous_cost, cost, reason, user_id))
        db.execute('''INSERT INTO stock_adjustments
            (product_id,user_id,previous_stock,new_stock,difference,reason) VALUES(?,?,?,?,?,?)''',
            (product['id'], user_id, product['stock'], new_stock, difference,
             f'Correção da reposição #{restock_id}: {reason}'))
