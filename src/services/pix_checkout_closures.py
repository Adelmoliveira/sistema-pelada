"""Explicit abandonment workflow. No scheduler, UI or automatic invocation.

Owns transactions. Network calls occur outside locks. The final transaction
locks plans/ installments before the sale, matching reconciliation/charge
creation, and checks both payment evidence and the complete charge inventory.
Unknown creation outcomes without an order id require recovery, never release.
"""
from datetime import datetime, timedelta, timezone
from src.services.mercadopago import get_order, cancel_order


DOMAINS = ('bar', 'sports')
TERMINAL = {'canceled', 'expired', 'failed', 'refunded'}


def _time(value):
    value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def is_abandonment_due(sale, now):
    """sales.created_at is the initial checkout for all three flows, not QR time."""
    return _time(now) >= _time(sale['created_at']) + timedelta(minutes=5)


def _exists(db, table):
    if db.is_postgres:
        return bool(db.execute('SELECT to_regclass(?) AS relation', ('public.' + table,)).fetchone()['relation'])
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _domain(db, sale_id):
    for domain in DOMAINS:
        if _exists(db, domain + '_installment_plans') and db.execute(
                f'SELECT id FROM {domain}_installment_plans WHERE sale_id=?', (sale_id,)).fetchone():
            return domain
    return None


def has_confirmed_payment(db, sale):
    if sale['paid'] or sale['payment_status'] == 'approved' or sale['paid_at']:
        return True
    if db.execute("SELECT 1 FROM sale_payment_parts WHERE sale_id=? AND status IN ('approved','refunded')",
                  (sale['id'],)).fetchone():
        return True
    if db.execute("SELECT 1 FROM bar_credit_reservations WHERE sale_id=? AND status='consumed'",
                  (sale['id'],)).fetchone():
        return True
    for domain in DOMAINS:
        if not _exists(db, domain + '_installment_plans'):
            continue
        if db.execute(f'''SELECT 1 FROM {domain}_installments i JOIN {domain}_installment_plans p
                          ON p.id=i.plan_id WHERE p.sale_id=? AND (i.status='paid' OR i.paid_at IS NOT NULL)''',
                      (sale['id'],)).fetchone():
            return True
        if _exists(db, domain + '_installment_payment_attempts') and db.execute(f'''
                SELECT 1 FROM {domain}_installment_payment_attempts a
                JOIN {domain}_installments i ON i.id=a.installment_id
                JOIN {domain}_installment_plans p ON p.id=i.plan_id
                WHERE p.sale_id=? AND a.status='approved' ''', (sale['id'],)).fetchone():
            return True
    return False


def can_abandon_checkout(db, sale):
    """Payment anywhere overrides installment order or full-sale paid flag."""
    return (sale['payment_method'] == 'Pix' and sale['payment_status'] not in TERMINAL
            and not sale['delivered_at'] and not has_confirmed_payment(db, sale)
            and not (_exists(db, 'sports_sale_item_details') and db.execute(
                "SELECT 1 FROM sports_sale_item_details d JOIN sale_items i ON i.id=d.sale_item_id WHERE i.sale_id=? AND d.order_mode!='ready'",
                (sale['id'],)).fetchone()))


def _lock_sale(db, sale_id):
    if not db.is_postgres and not db.conn.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    suffix = ' FOR UPDATE' if db.is_postgres else ''
    domain = _domain(db, sale_id)
    if domain:
        plan = db.execute(f'SELECT id FROM {domain}_installment_plans WHERE sale_id=?' + suffix,
                          (sale_id,)).fetchone()
        db.execute(f'SELECT id FROM {domain}_installments WHERE plan_id=? ORDER BY id' + suffix,
                   (plan['id'],)).fetchall()
    row = db.execute('SELECT * FROM sales WHERE id=?' + suffix, (sale_id,)).fetchone()
    if not row:
        raise ValueError('Venda não encontrada.')
    return dict(row)


def _record(db, sale_id):
    return dict(db.execute('SELECT * FROM pix_checkout_closures WHERE sale_id=?', (sale_id,)).fetchone())


def _update(db, sale_id, status, error=None):
    db.execute('UPDATE pix_checkout_closures SET status=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE sale_id=?',
               (status, error, sale_id))


def abort_closure_on_payment(db, sale_id):
    """Join the caller's payment transaction; completed closures are immutable."""
    if _exists(db, 'pix_checkout_closures'):
        db.execute("""UPDATE pix_checkout_closures
                      SET status='aborted_payment',last_error=NULL,updated_at=CURRENT_TIMESTAMP
                      WHERE sale_id=? AND status IN ('requested','awaiting_provider','retryable')""",
                   (sale_id,))


def request_closure(db, sale_id, reason, requested_by=None, player_id=None, now=None):
    if reason not in {'client_cancel', 'timeout'}:
        raise ValueError('Motivo inválido.')
    if reason == 'client_cancel' and (requested_by is None or player_id is None):
        raise ValueError('Autor e proprietário obrigatórios.')
    with db:
        sale = _lock_sale(db, sale_id)
        if player_id is not None and sale['player_id'] != player_id:
            raise ValueError('Venda de outro peladeiro.')
        if requested_by is not None:
            user = db.execute('SELECT player_id FROM users WHERE id=?', (requested_by,)).fetchone()
            if not user or (reason == 'client_cancel' and user['player_id'] != sale['player_id']):
                raise ValueError('Autor não corresponde ao proprietário.')
        previous = db.execute('SELECT * FROM pix_checkout_closures WHERE sale_id=?', (sale_id,)).fetchone()
        if previous:
            return dict(previous)
        if reason == 'timeout' and not is_abandonment_due(sale, now or datetime.now(timezone.utc)):
            raise ValueError('Prazo de abandono ainda não atingido.')
        if sale['payment_method'] != 'Pix':
            raise ValueError('Venda não Pix.')
        if not has_confirmed_payment(db, sale) and not can_abandon_checkout(db, sale):
            raise ValueError('Venda indisponível para abandono.')
        db.execute('INSERT INTO pix_checkout_closures(sale_id,reason,requested_by,status) VALUES(?,?,?,?)',
                   (sale_id, reason, requested_by,
                    'aborted_payment' if has_confirmed_payment(db, sale) else 'requested'))
        return _record(db, sale_id)


def _charges(db, sale):
    domain = _domain(db, sale['id'])
    if not domain:
        # An in-flight whole-sale checkout may have no provider id yet.
        return [(None, sale['mercadopago_order_id'], sale['external_reference'])]
    rows = db.execute(f'''SELECT a.id,a.mercado_pago_order_id,a.external_reference
        FROM {domain}_installment_payment_attempts a JOIN {domain}_installments i ON i.id=a.installment_id
        JOIN {domain}_installment_plans p ON p.id=i.plan_id WHERE p.sale_id=? ORDER BY a.id''',
        (sale['id'],)).fetchall()
    return [(r['id'], r['mercado_pago_order_id'], r['external_reference']) for r in rows]


def _reconcile(db, sale_id, order):
    domain = _domain(db, sale_id)
    if domain == 'bar':
        from src.services.bar_installment_reconciliation import reconcile_installment_order
        reconcile_installment_order(db, order)
    elif domain == 'sports':
        from src.services.sports_installment_reconciliation import reconcile_installment_order
        reconcile_installment_order(db, order)
    else:
        from src.routes.sales import apply_mercadopago_status
        # Do not apply provider terminal state here: that function restores stock.
        sale = dict(db.execute('SELECT * FROM sales WHERE id=?', (sale_id,)).fetchone())
        apply_mercadopago_status(db, sale, order)


def _stock_domain(db, sale_id):
    """Products identify stock domain even for non-installment Pix checkouts."""
    rows = db.execute('SELECT p.category FROM sale_items i JOIN products p ON p.id=i.product_id WHERE i.sale_id=?',
                      (sale_id,)).fetchall()
    domains = {'sports' if row['category'] == 'Material Esportivo' else 'bar' for row in rows}
    if len(domains) != 1:
        raise ValueError('Venda vazia ou mista exige revisão antes da restauração.')
    return domains.pop()


def _release_bar_stock(db, sale_id):
    # Idempotence is provided by the closure marker in the same locked transaction.
    for row in db.execute('SELECT product_id,quantity FROM sale_items WHERE sale_id=?', (sale_id,)).fetchall():
        db.execute('UPDATE products SET stock=stock+? WHERE id=?', (row['quantity'], row['product_id']))


def _release_sports_stock(db, sale_id):
    rows = db.execute("""SELECT si.id,si.quantity,d.variant_id,d.order_mode
        FROM sale_items si LEFT JOIN sports_sale_item_details d ON d.sale_item_id=si.id WHERE si.sale_id=?""",
        (sale_id,)).fetchall()
    for row in rows:
        if not row['variant_id'] or row['order_mode'] != 'ready':
            raise ValueError('Material sem variante pronta entrega não pode ser restaurado.')
        released = db.execute("UPDATE sports_stock_reservations SET status='released',updated_at=CURRENT_TIMESTAMP WHERE sale_item_id=? AND status='reserved'",
                              (row['id'],))
        if released.rowcount:
            db.execute('UPDATE sports_product_variants SET stock=stock+?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                       (row['quantity'], row['variant_id']))


def _release_stock(db, sale_id):
    """Dispatch physical restoration; no Sports table is needed for Bar."""
    if _stock_domain(db, sale_id) == 'sports':
        _release_sports_stock(db, sale_id)
    else:
        _release_bar_stock(db, sale_id)


def process_closure(db, sale_id, access_token):
    """Retry with the same per-order cancellation key. No locks held over HTTP."""
    with db:
        sale = _lock_sale(db, sale_id)
        record = _record(db, sale_id)
        if record['status'] in {'completed', 'aborted_payment'}:
            return record
        if has_confirmed_payment(db, sale):
            _update(db, sale_id, 'aborted_payment')
            return _record(db, sale_id)
        if not can_abandon_checkout(db, sale):
            _update(db, sale_id, 'retryable', 'Venda já terminal; requer revisão.')
            return _record(db, sale_id)
        charges = _charges(db, sale)
        _update(db, sale_id, 'awaiting_provider')
        db.execute('UPDATE pix_checkout_closures SET provider_cancel_requested_at=COALESCE(provider_cancel_requested_at,CURRENT_TIMESTAMP) WHERE sale_id=?', (sale_id,))
    try:
        for attempt_id, order_id, reference in charges:
            if not order_id:
                raise ValueError('Resultado de criação desconhecido; recuperar order antes de encerrar.')
            order = get_order(access_token, order_id)
            if str(order.get('id')) != str(order_id) or not reference or order.get('external_reference') != reference:
                raise ValueError('Identidade externa divergente.')
            if order.get('status') == 'processed' and order.get('status_detail') == 'accredited':
                _reconcile(db, sale_id, order)
                with db:
                    _lock_sale(db, sale_id)
                    _update(db, sale_id, 'aborted_payment')
                    return _record(db, sale_id)
            if order.get('status') not in {'canceled', 'expired'}:
                if order.get('status') not in {'created', 'action_required'}:
                    raise ValueError('Estado externo não confirma encerramento seguro.')
                cancel_order(access_token, order_id, f'checkout-close-{record["id"]}-{order_id}')
                # A successful HTTP response alone is insufficient.
                order = get_order(access_token, order_id)
            if (str(order.get('id')) != str(order_id) or order.get('external_reference') != reference
                    or order.get('status') not in {'canceled', 'expired'}):
                if order.get('status') == 'processed' and order.get('status_detail') == 'accredited':
                    _reconcile(db, sale_id, order)
                raise ValueError('Cancelamento externo não confirmado.')
        with db:
            sale = _lock_sale(db, sale_id)
            record = _record(db, sale_id)
            if record['status'] == 'completed':
                return record
            if has_confirmed_payment(db, sale):
                _update(db, sale_id, 'aborted_payment')
            elif not can_abandon_checkout(db, sale) or charges != _charges(db, sale):
                _update(db, sale_id, 'retryable', 'Estado/cobranças alterados durante processamento.')
            else:
                from src.services.bar_credits import release_reservation
                from src.services.sale_payment_parts import cancel_active_payment_parts
                _release_stock(db, sale_id)
                if db.execute('SELECT 1 FROM bar_credit_reservations WHERE sale_id=?', (sale_id,)).fetchone():
                    release_reservation(db, sale_id)
                cancel_active_payment_parts(db, sale_id)
                domain = _domain(db, sale_id)
                if domain:
                    db.execute(f"UPDATE {domain}_installment_payment_attempts SET status='canceled',updated_at=CURRENT_TIMESTAMP WHERE installment_id IN (SELECT i.id FROM {domain}_installments i JOIN {domain}_installment_plans p ON p.id=i.plan_id WHERE p.sale_id=?) AND status IN ('creating','pending')", (sale_id,))
                db.execute("UPDATE sales SET payment_status=?,ready_for_delivery=0 WHERE id=?",
                           ('expired' if record['reason'] == 'timeout' else 'canceled', sale_id))
                # Final audit only: sale_cancellations keeps its original meaning.
                db.execute('INSERT INTO sale_cancellations(sale_id,reason,canceled_by) VALUES(?,?,?)',
                           (sale_id, 'Pix abandonado: ' + record['reason'], record['requested_by']))
                db.execute("UPDATE pix_checkout_closures SET status='completed',provider_cancel_confirmed_at=CURRENT_TIMESTAMP,completed_at=CURRENT_TIMESTAMP,last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE sale_id=?", (sale_id,))
            return _record(db, sale_id)
    except Exception as exc:
        with db:
            sale = _lock_sale(db, sale_id)
            if _record(db, sale_id)['status'] != 'completed':
                _update(db, sale_id, 'aborted_payment' if has_confirmed_payment(db, sale) else 'retryable', str(exc))
            return _record(db, sale_id)


def assert_checkout_not_closing(db, sale_id):
    """Called under installment lock, before reusing or creating any QR."""
    if _exists(db, 'pix_checkout_closures') and db.execute(
            "SELECT 1 FROM pix_checkout_closures WHERE sale_id=? AND status!='aborted_payment'",
            (sale_id,)).fetchone():
        raise ValueError('Checkout em encerramento ou encerrado.')
