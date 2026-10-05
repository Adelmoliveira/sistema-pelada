"""Thin entry points to the existing closure engine; no second cancellation path."""
from datetime import datetime, timedelta, timezone

from src.services.pix_checkout_closures import (
    _exists, _time, can_abandon_checkout, has_confirmed_payment,
    request_closure, process_closure,
)


def closure_for_sale(db, sale_id):
    if not _exists(db, 'pix_checkout_closures'):
        return None
    row = db.execute('SELECT * FROM pix_checkout_closures WHERE sale_id=?', (sale_id,)).fetchone()
    return dict(row) if row else None


def cancel_client_checkout(db, sale_id, player_id, user_id, access_token):
    sale = db.execute('SELECT * FROM sales WHERE id=? AND player_id=?', (sale_id, player_id)).fetchone()
    if not sale:
        raise PermissionError('Compra não encontrada ou de outro peladeiro.')
    sale = dict(sale)
    closure = closure_for_sale(db, sale_id)
    if has_confirmed_payment(db, sale):
        raise ValueError('Compra com pagamento confirmado não pode ser cancelada.')
    if closure and closure['status'] == 'completed':
        return closure
    if not can_abandon_checkout(db, sale) or (closure and closure['status'] == 'aborted_payment'):
        raise ValueError('Compra indisponível para cancelamento.')
    closure = request_closure(db, sale_id, 'client_cancel', requested_by=user_id, player_id=player_id)
    if closure['status'] == 'aborted_payment':
        raise ValueError('Pagamento confirmado durante a solicitação; compra preservada.')
    return process_closure(db, sale_id, access_token)


def expire_recent_checkouts(db, access_token, not_before, now=None, limit=3):
    """Explicit deployment cutoff is required; retries never reset checkout age.

    Five minutes starts closure. Provider uncertainty keeps the reservation.
    Only this disposable-testable entry point is used by the authenticated cron.
    """
    if not not_before:
        raise ValueError('Configure PIX_ABANDONMENT_NOT_BEFORE com o marco UTC do deploy.')
    cutoff = _time(not_before)
    now = _time(now or datetime.now(timezone.utc))
    if cutoff > now:
        return dict(processed=0, completed=0, pending=0, aborted=0)
    # sales.created_at is the original UTC timestamp for every checkout.
    rows = db.execute('''SELECT s.* FROM sales s
        LEFT JOIN pix_checkout_closures c ON c.sale_id=s.id
        WHERE s.payment_method='Pix' AND s.paid=0
          AND s.payment_status IN ('creating','pending')
          AND s.created_at>=? AND s.created_at<=?
          AND (c.id IS NULL OR c.status IN ('requested','awaiting_provider','retryable'))
        ORDER BY COALESCE(c.updated_at,s.created_at),s.id LIMIT ?''',
        (cutoff.replace(tzinfo=None), (now-timedelta(minutes=5)).replace(tzinfo=None), limit)).fetchall()
    result = dict(processed=0, completed=0, pending=0, aborted=0)
    for row in rows:
        sale = dict(row)
        if not can_abandon_checkout(db, sale):
            continue
        request_closure(db, sale['id'], 'timeout', now=now)
        closure = process_closure(db, sale['id'], access_token)
        result['processed'] += 1
        result['completed' if closure['status']=='completed' else 'aborted' if closure['status']=='aborted_payment' else 'pending'] += 1
    return result


def decorate_purchase_closure(db, sale):
    """Terminal closure overrides installment badges without deleting history."""
    closure = closure_for_sale(db, sale['id'])
    status = closure['status'] if closure else None
    sale['closure'] = closure
    sale['closure_pending'] = status in {'requested','awaiting_provider','retryable'}
    terminal = status == 'completed' or sale['payment_status'] in {'canceled','expired','failed','refunded'}
    sale['closure_terminal'] = terminal
    sale['can_cancel_purchase'] = (not terminal and status not in {'completed','aborted_payment'}
                                   and can_abandon_checkout(db, sale))
    if terminal:
        expired = sale['payment_status'] == 'expired' or (closure and closure['reason'] == 'timeout')
        sale.update(display_status='ENCERRADO', display_status_label='Compra expirada' if expired else 'Compra cancelada' if sale['payment_status'] == 'canceled' else 'Compra estornada' if sale['payment_status']=='refunded' else 'Pagamento não concluído', display_status_class='secondary')
    elif sale['closure_pending']:
        sale.update(display_status='ENCERRAMENTO_PENDENTE', display_status_label='Cancelamento em processamento', display_status_class='warning')
