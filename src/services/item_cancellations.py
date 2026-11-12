"""Cancel undelivered bar units without changing original receipts or line items."""
from src.catalog import SPORTS_MATERIAL_CATEGORY
from src.services.bar_installment_reconciliation import has_bar_plan
from src.services.bar_credits import _audit, _lock_credit_account, ensure_account


def cancellation_state(db, sale):
    """Conservative eligibility: ambiguous historical payments/pricing stay manual."""
    if not sale or not sale['player_id']:
        raise ValueError('Pedido sem peladeiro para receber o crédito.')
    if not sale['paid'] or sale['payment_status'] != 'approved' or not sale['ready_for_delivery']:
        raise ValueError('O pagamento deve estar aprovado e o pedido liberado.')
    if has_bar_plan(db, sale['id']) or db.execute(
        'SELECT 1 FROM sports_installment_plans WHERE sale_id=?', (sale['id'],)
    ).fetchone():
        raise ValueError('Venda parcelada exige tratamento administrativo próprio.')
    if sale['delivered_at']:
        raise ValueError('Pedido finalizado ou com entrega histórica; cancelamento automático bloqueado.')
    if db.execute('SELECT 1 FROM sale_cancellations WHERE sale_id=?', (sale['id'],)).fetchone():
        raise ValueError('Pedido já cancelado.')
    parts = db.execute('SELECT * FROM sale_payment_parts WHERE sale_id=?', (sale['id'],)).fetchall()
    if (sale['payment_method'] not in ('Dinheiro', 'Créditos') or not parts
            or any(p['status'] != 'approved' or p['method'] not in ('Dinheiro', 'Créditos') for p in parts)
            or sum(p['amount_cents'] for p in parts) != sale['total_cents']):
        raise ValueError('Composição financeira não suportada com segurança; revisão administrativa necessária.')
    invalid_delivery = db.execute('''SELECT 1 FROM sale_item_deliveries d
        JOIN sale_items i ON i.id=d.sale_item_id
        LEFT JOIN sale_item_delivery_restorations r ON r.delivery_id=d.id
        WHERE i.sale_id=? AND (d.quantity<=0 OR COALESCE(r.quantity,0)<0 OR COALESCE(r.quantity,0)>d.quantity)''',
        (sale['id'],)).fetchone()
    if invalid_delivery:
        raise ValueError('Histórico de entrega/restauração inconsistente; revisão administrativa necessária.')
    items = db.execute('''SELECT i.*,p.name,p.category,
        COALESCE((SELECT SUM(d.quantity-COALESCE(r.quantity,0))
          FROM sale_item_deliveries d LEFT JOIN sale_item_delivery_restorations r ON r.delivery_id=d.id
          WHERE d.sale_item_id=i.id),0) delivered_quantity
        FROM sale_items_net i JOIN products p ON p.id=i.product_id WHERE i.sale_id=? ORDER BY i.id''',
        (sale['id'],)).fetchall()
    if (not items or any(i['category'] == SPORTS_MATERIAL_CATEGORY or i['unit_price_cents'] <= 0
                        or i['quantity'] < 0 or not 0 <= i['delivered_quantity'] <= i['quantity'] for i in items)):
        raise ValueError('Histórico de itens ou entregas inconsistente; revisão administrativa necessária.')
    if sum(i['original_quantity'] * i['unit_price_cents'] for i in items) != sale['total_cents']:
        raise ValueError('Desconto ou rateio sem valor individual comprovado; cancelamento automático bloqueado.')
    return items


def cancel_item(db, sale_id, item_id, quantity, reason, key, actor_id):
    if (type(quantity) is not int or quantity <= 0 or type(item_id) is not int
            or not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 500
            or not isinstance(key, str) or not 16 <= len(key) <= 100):
        raise ValueError('Informe item, quantidade inteira positiva, justificativa (5 a 500 caracteres) e chave da operação.')
    reason = reason.strip()
    with db:
        if not db.is_postgres and not db.conn.in_transaction:
            db.execute('BEGIN IMMEDIATE')
        suffix = ' FOR UPDATE' if db.is_postgres else ''
        sale = db.execute('SELECT * FROM sales WHERE id=?' + suffix, (sale_id,)).fetchone()
        previous = db.execute('SELECT * FROM sale_item_cancellations WHERE idempotency_key=?', (key,)).fetchone()
        if previous:
            if (previous['sale_id'], previous['sale_item_id'], previous['quantity'], previous['reason'], previous['canceled_by']) != (
                    sale_id, item_id, quantity, reason, actor_id):
                raise ValueError('Chave de operação já utilizada com outros dados.')
            return dict(previous)
        items = cancellation_state(db, sale)
        item = next((i for i in items if i['id'] == item_id), None)
        if not item or quantity > item['quantity'] - item['delivered_quantity']:
            raise ValueError('Quantidade superior à pendente; unidades entregues ou canceladas não podem ser canceladas.')
        amount = quantity * item['unit_price_cents']
        # Serialize with every stock writer and the existing wallet lock.
        product = db.execute('SELECT stock FROM products WHERE id=?' + suffix, (item['product_id'],)).fetchone()
        if not product:
            raise ValueError('Produto não encontrado; estoque não pode ser restaurado.')
        _lock_credit_account(db, sale['player_id'])
        account = ensure_account(db, sale['player_id'])
        new_balance = account['balance_cents'] + amount
        db.execute('UPDATE bar_credit_accounts SET balance_cents=?,low_balance_notified=0,updated_at=CURRENT_TIMESTAMP WHERE player_id=?',
                   (new_balance, sale['player_id']))
        tx = db.execute('''INSERT INTO bar_credit_transactions
            (player_id,type,amount_cents,balance_after_cents,description,sale_id,created_by)
            VALUES(?,'REFUND',?,?,?,?,?)''',
            (sale['player_id'], amount, new_balance, f'Cancelamento parcial: item #{item_id}, {quantity} unidade(s)', sale_id, actor_id))
        updated = db.execute('UPDATE products SET stock=stock+? WHERE id=?', (quantity, item['product_id']))
        if updated.rowcount != 1:
            raise ValueError('Falha ao restaurar estoque.')
        _audit(db, sale['player_id'], 'CANCELAMENTO_ITEM', amount, transaction_id=tx.lastrowid,
               actor_user_id=actor_id, reason=f'Venda #{sale_id}; item #{item_id}; quantidade {quantity}: {reason}')
        operation = db.execute('''INSERT INTO sale_item_cancellations
            (sale_id,sale_item_id,quantity,amount_cents,stock_restored_quantity,credit_transaction_id,canceled_by,reason,idempotency_key)
            VALUES(?,?,?,?,?,?,?,?,?)''', (sale_id,item_id,quantity,amount,quantity,tx.lastrowid,actor_id,reason,key))
        # Do not mark canceled units as delivered (legacy consumption fallback).
        return dict(db.execute('SELECT * FROM sale_item_cancellations WHERE id=?', (operation.lastrowid,)).fetchone())


def cancellation_history(db, sale_id):
    return db.execute('''SELECT c.*,p.name product_name,u.name operator_name
        FROM sale_item_cancellations c JOIN sale_items i ON i.id=c.sale_item_id
        JOIN products p ON p.id=i.product_id JOIN users u ON u.id=c.canceled_by
        WHERE c.sale_id=? ORDER BY c.id''', (sale_id,)).fetchall()
