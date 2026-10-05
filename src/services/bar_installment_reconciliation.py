"""Reconcile independent Pix attempts without treating a deposit as full payment."""
from decimal import Decimal, InvalidOperation

import json
def order_payment_id(order):
    payments = (order.get('transactions') or {}).get('payments') or []
    return str(payments[0]['id']) if payments and payments[0].get('id') else None


def table_available(db, name):
    if name not in {'bar_installment_plans', 'bar_installment_payment_attempts'}:
        raise ValueError('Tabela inválida.')
    if db.is_postgres:
        return bool(db.execute('SELECT to_regclass(?) AS relation', ('public.' + name,)).fetchone()['relation'])
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def has_bar_plan(db, sale_id):
    return table_available(db, 'bar_installment_plans') and bool(db.execute(
        'SELECT 1 FROM bar_installment_plans WHERE sale_id=?', (sale_id,)).fetchone())


def find_installment_attempt(db, order):
    if not table_available(db, 'bar_installment_payment_attempts'):
        if str(order.get('external_reference') or '').startswith('bar2x_'):
            raise ValueError('Tentativa Bar não encontrada.')
        return None
    identifiers = {
        'external_reference': order.get('external_reference'),
        'mercado_pago_order_id': order.get('id'),
        'mercado_pago_payment_id': order_payment_id(order),
    }
    matches = {}
    for column, value in identifiers.items():
        if value:
            row = db.execute(f'SELECT * FROM bar_installment_payment_attempts WHERE {column}=?',
                             (str(value),)).fetchone()
            if row:
                matches[row['id']] = dict(row)
    if len(matches) > 1:
        raise ValueError('Identificadores apontam para tentativas diferentes.')
    if not matches:
        if str(identifiers['external_reference'] or '').startswith('bar2x_'):
            raise ValueError('Tentativa parcelada não encontrada.')
        return None
    attempt = next(iter(matches.values()))
    for column, value in identifiers.items():
        if value and attempt[column] and str(value) != str(attempt[column]):
            raise ValueError('Identificador divergente da tentativa.')
    return attempt


def valid_plan(db, sale_id):
    plan = db.execute('SELECT * FROM bar_installment_plans WHERE sale_id=?', (sale_id,)).fetchone()
    if not plan:
        raise ValueError('Plano Bar não encontrado.')
    sale = db.execute('SELECT * FROM sales WHERE id=?', (sale_id,)).fetchone()
    installments = db.execute('SELECT * FROM bar_installments WHERE plan_id=? ORDER BY installment_number',
                              (plan['id'],)).fetchall()
    snapshot = json.loads(plan['checkout_snapshot'])
    items = db.execute('SELECT product_id,quantity,unit_price_cents FROM sale_items WHERE sale_id=? ORDER BY product_id',
                       (sale_id,)).fetchall()
    expected = [(r['product_id'], r['quantity'], r['unit_price_cents']) for r in snapshot]
    if (not sale or sale['payment_method'] != 'Pix' or sale['total_cents'] != plan['total_cents'] or
            plan['checkout_origin'] != 'case_only' or not snapshot or
            any(r['sale_mode'] != 'case' or r['case_quantity'] <= 0 or r['units_per_case'] <= 0 or
                r['quantity'] != r['case_quantity'] * r['units_per_case'] for r in snapshot) or
            [(r['product_id'], r['quantity'], r['unit_price_cents']) for r in items] != expected or
            sum(r['quantity'] * r['unit_price_cents'] for r in snapshot) != plan['total_cents'] or
            [i['installment_number'] for i in installments] != [1, 2] or
            [i['amount_cents'] for i in installments] != [plan['total_cents'] // 2, plan['total_cents'] - plan['total_cents'] // 2]):
        raise ValueError('Plano incompatível com checkout exclusivamente por caixa.')
    return plan, sale, installments


def reconcile_installment_order(db, order):
    """Own transaction; lock the plan before attempts to serialize both payments.

    Approved attempts remain in the audit even when another attempt already paid
    the installment. Such confirmations never increase the credited plan amount.
    Refunds require manual handling and do not reverse a definitive purchase.
    """
    attempt = find_installment_attempt(db, order)
    if not attempt:
        raise ValueError('Tentativa não encontrada.')
    with db:
        if not db.is_postgres and not db.conn.in_transaction:
            db.execute('BEGIN IMMEDIATE')
        plan_id = db.execute('SELECT plan_id FROM bar_installments WHERE id=?',
                             (attempt['installment_id'],)).fetchone()['plan_id']
        lock = ' FOR UPDATE' if db.is_postgres else ''
        plan = db.execute('SELECT * FROM bar_installment_plans WHERE id=?' + lock, (plan_id,)).fetchone()
        db.execute('SELECT id FROM sales WHERE id=?' + lock, (plan['sale_id'],)).fetchone()
        attempt = find_installment_attempt(db, order)
        plan, sale, installments = valid_plan(db, plan['sale_id'])
        installment = next(i for i in installments if i['id'] == attempt['installment_id'])
        if sale['payment_status'] in {'canceled', 'refunded', 'failed', 'expired'}:
            raise ValueError('Venda indisponível; confirmação exige tratamento manual.')
        status = order.get('status')
        approved = status == 'processed' and order.get('status_detail') == 'accredited'
        if approved:
            try:
                amount = Decimal(str(order.get('total_paid_amount'))) * 100
                if not amount.is_finite() or amount != amount.to_integral_value():
                    raise ValueError('Valor inválido.')
                cents = int(amount)
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError('Valor recebido inválido.') from exc
            if cents != attempt['amount_cents'] or cents != installment['amount_cents']:
                raise ValueError('Valor recebido diverge da parcela.')
        elif status == 'refunded':
            raise ValueError('Estorno parcelado exige tratamento administrativo manual.')
        elif status not in {'pending', 'created', 'processing', 'action_required', 'expired', 'failed', 'canceled'}:
            return attempt['status']
        new_status = 'approved' if approved else status if status in {'expired', 'failed', 'canceled'} else 'pending'
        # Do not let delayed pending/terminal events undo an approval or revive an expired QR.
        if attempt['status'] != 'approved' and (approved or attempt['status'] in {'creating', 'pending'}):
            db.execute('''UPDATE bar_installment_payment_attempts SET status=?,
                          mercado_pago_order_id=COALESCE(mercado_pago_order_id,?),
                          mercado_pago_payment_id=COALESCE(mercado_pago_payment_id,?),updated_at=CURRENT_TIMESTAMP
                          WHERE id=?''',
                       (new_status, str(order['id']) if order.get('id') else None,
                        str(order_payment_id(order)) if order_payment_id(order) else None, attempt['id']))
        if approved:
            from src.services.pix_checkout_closures import abort_closure_on_payment
            abort_closure_on_payment(db, sale['id'])
        if not approved or installment['status'] == 'paid':
            return 'approved' if attempt['status'] == 'approved' or approved else new_status
        db.execute("UPDATE bar_installments SET status='paid',paid_at=COALESCE(paid_at,CURRENT_TIMESTAMP),updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",
                   (installment['id'],))
        installments = db.execute('SELECT * FROM bar_installments WHERE plan_id=? ORDER BY installment_number',
                                   (plan['id'],)).fetchall()
        first_paid = installments[0]['status'] == 'paid'
        fully_paid = all(i['status'] == 'paid' for i in installments)
        db.execute('''UPDATE bar_installment_plans SET status=?,
                      first_installment_paid_at=CASE WHEN ? THEN COALESCE(first_installment_paid_at,CURRENT_TIMESTAMP) ELSE first_installment_paid_at END,
                      fully_paid_at=CASE WHEN ? THEN COALESCE(fully_paid_at,CURRENT_TIMESTAMP) ELSE fully_paid_at END,
                      updated_at=CURRENT_TIMESTAMP WHERE id=?''',
                   ('paid' if fully_paid else 'active' if first_paid else 'pending', first_paid, fully_paid, plan['id']))
        if first_paid:
            db.execute('UPDATE sales SET ready_for_delivery=1 WHERE id=?', (sale['id'],))
        if fully_paid:
            db.execute("UPDATE sales SET paid=1,payment_status='approved',paid_at=COALESCE(paid_at,CURRENT_TIMESTAMP) WHERE id=?",
                       (sale['id'],))
        return 'approved'


def withdrawal_allowed(db, sale_id):
    if not has_bar_plan(db, sale_id):
        return False
    plan, sale, installments = valid_plan(db, sale_id)
    return bool(plan['status'] in {'active', 'paid'} and plan['first_installment_paid_at'] and
                installments[0]['status'] == 'paid' and sale['payment_status'] not in {'failed','expired','canceled','refunded'})


def withdrawal_sql(db, alias='s'):
    if alias != 's':
        raise ValueError('Alias inválido.')
    if not table_available(db, 'bar_installment_plans'):
        return '0=1'
    return """EXISTS(SELECT 1 FROM bar_installment_plans bp JOIN bar_installments bi ON bi.plan_id=bp.id
                    WHERE bp.sale_id=s.id AND bp.status IN ('active','paid') AND bp.first_installment_paid_at IS NOT NULL
                    AND bi.installment_number=1 AND bi.status='paid'
                    AND s.payment_status NOT IN ('failed','expired','canceled','refunded'))"""
