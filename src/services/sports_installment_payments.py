"""Independent Pix charge creation; reconciliation belongs to Phase 3."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
from urllib.error import HTTPError, URLError

from src.catalog import SPORTS_MATERIAL_CATEGORY
from src.services.mercadopago import MercadoPagoError, create_pix_order
from src.services.pix import generate_qrcode_base64


def create_installment_pix_order(access_token, installment, external_reference, idempotency_key, payer_email):
    """Use only the amount loaded from the persisted installment."""
    return create_pix_order(access_token, external_reference, int(installment["amount_cents"]),
                            idempotency_key, payer_email)


def _attempt(db, attempt_id):
    return dict(db.execute('SELECT * FROM sports_installment_payment_attempts WHERE id=?',
                           (attempt_id,)).fetchone())


def create_installment_payment_attempt(db, installment_id, access_token, idempotency_key=None, player_id=None):
    """Persist intent before the API call; replay never repeats that call.

    An active QR or an in-flight attempt is reused even with a different key,
    preventing concurrent live charges. An expired QR allows a new attempt,
    without changing installment due dates or payment states.
    The optional player_id enforces client ownership; staff omit it.
    This service owns its transactions. No sale, plan, installment or stock
    is updated here. 'creating' attempts require reconciliation after a crash.
    """
    if type(installment_id) is not int or installment_id <= 0:
        raise ValueError('Parcela inválida.')
    if not access_token:
        raise ValueError('Mercado Pago não configurado.')
    try:
        key = str(UUID(str(idempotency_key))) if idempotency_key is not None else str(uuid4())
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError('A chave de idempotência deve ser um UUID.') from exc
    now = datetime.now(timezone.utc)
    with db:
        if not db.is_postgres and not db.conn.in_transaction:
            db.execute('BEGIN IMMEDIATE')
        lock = ' FOR UPDATE OF i' if db.is_postgres else ''
        installment = db.execute(
            '''SELECT i.*,p.sale_id,p.status plan_status,s.player_id,s.payment_status,
                      pl.email
               FROM sports_installments i JOIN sports_installment_plans p ON p.id=i.plan_id
               JOIN sales s ON s.id=p.sale_id LEFT JOIN players pl ON pl.id=s.player_id
               WHERE i.id=?''' + lock, (installment_id,),
        ).fetchone()
        if not installment or (player_id is not None and installment['player_id'] != player_id):
            raise ValueError('Parcela inexistente ou sem permissão de acesso.')
        from src.services.pix_checkout_closures import assert_checkout_not_closing
        assert_checkout_not_closing(db, installment['sale_id'])
        if installment['status'] != 'pending' or installment['plan_status'] == 'paid':
            raise ValueError('Parcela já paga ou indisponível para cobrança.')
        if installment['payment_status'] in {'canceled', 'refunded', 'failed', 'expired'}:
            raise ValueError('Venda indisponível para cobrança.')
        items = db.execute(
            '''SELECT p.category,d.order_mode,d.fulfillment_status FROM sale_items si
               JOIN products p ON p.id=si.product_id
               LEFT JOIN sports_sale_item_details d ON d.sale_item_id=si.id
               WHERE si.sale_id=?''', (installment['sale_id'],),
        ).fetchall()
        if not items or any(row['category'] != SPORTS_MATERIAL_CATEGORY or
                            row['order_mode'] != 'ready' or row['fulfillment_status'] == 'cancelled'
                            for row in items):
            raise ValueError('Cobrança disponível somente para Material Esportivo de pronta entrega.')
        email = str(installment['email'] or '').strip()
        if '@' not in email:
            raise ValueError('Cadastre um e-mail válido para o peladeiro.')
        previous = db.execute('SELECT * FROM sports_installment_payment_attempts WHERE idempotency_key=?',
                              (key,)).fetchone()
        if previous:
            if previous['installment_id'] != installment_id:
                raise ValueError('Chave de idempotência já utilizada por outra parcela.')
            return dict(previous)
        active = db.execute(
            "SELECT * FROM sports_installment_payment_attempts WHERE installment_id=? AND status IN ('creating','pending') ORDER BY id DESC",
            (installment_id,),
        ).fetchall()
        for previous in active:
            expires = previous['expires_at']
            expiration = datetime.fromisoformat(str(expires)) if expires else None
            if expiration and expiration.tzinfo is None:
                expiration = expiration.replace(tzinfo=timezone.utc)
            if previous['status'] == 'creating' or expiration is None or expiration > now:
                return dict(previous)
            db.execute("UPDATE sports_installment_payment_attempts SET status='expired',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",
                       (previous['id'],))
        reference = f"sports3x_p{installment['plan_id']}_i{installment['installment_number']}_a{uuid4().hex}"
        expires_at = (now + timedelta(minutes=30)).isoformat()
        attempt_id = db.execute(
            '''INSERT INTO sports_installment_payment_attempts
               (installment_id,amount_cents,external_reference,idempotency_key,expires_at)
               VALUES(?,?,?,?,?)''',
            (installment_id, installment['amount_cents'], reference, key, expires_at),
        ).lastrowid

    # The durable attempt exists even if the external request fails.
    try:
        order = create_installment_pix_order(access_token, installment, reference, key, email)
        payments = (order.get('transactions') or {}).get('payments') or []
        payment = payments[0] if payments else {}
        method = payment.get('payment_method') or {}
        qr_code = method.get('qr_code')
        with db:
            db.execute(
                '''UPDATE sports_installment_payment_attempts
                   SET mercado_pago_order_id=?,mercado_pago_payment_id=?,qr_code=?,
                       qr_code_base64=?,ticket_url=?,status=?,updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status='creating' ''',
                (str(order['id']) if order.get('id') else None,
                 str(payment['id']) if payment.get('id') else None,
                 qr_code, method.get('qr_code_base64'), method.get('ticket_url'),
                 'pending' if order.get('id') and qr_code else 'failed', attempt_id),
            )
        if not order.get('id') or not qr_code:
            raise MercadoPagoError('O Mercado Pago não retornou uma cobrança Pix válida.')
        if not method.get('qr_code_base64'):
            encoded = generate_qrcode_base64(qr_code)
            with db:
                db.execute('UPDATE sports_installment_payment_attempts SET qr_code_base64=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                           (encoded, attempt_id))
    except MercadoPagoError as exc:
        # A timeout/connection error or provider 5xx may follow successful
        # creation. Keep it in-flight and block a second independent charge
        # until Phase 3 can reconcile it using the durable reference/key.
        cause = exc.__cause__
        uncertain = (isinstance(cause, (URLError, TimeoutError)) and
                     (not isinstance(cause, HTTPError) or cause.code >= 500))
        if uncertain:
            raise
        with db:
            db.execute("UPDATE sports_installment_payment_attempts SET status='failed',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='creating'",
                       (attempt_id,))
        raise
    return _attempt(db, attempt_id)
