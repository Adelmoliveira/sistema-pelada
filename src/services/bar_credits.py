"""Credit wallet for bar purchases."""
from datetime import datetime

from flask import current_app

from src.services.email_reminders import send_gmail_html
from src.services.finance_accounts import create_finance_movement
from src.services.push_notifications import send_player_push_once
from src.utils import money

# Valores pré-definidos exibidos ao peladeiro. Mantemos apenas recargas a
# partir de R$ 50 para deixar a escolha mais simples e evitar opções pequenas.
PRESET_AMOUNTS_CENTS = (5000, 15000, 20000, 25000, 30000, 40000, 50000)


def low_balance_threshold():
    try:
        return max(0, int(current_app.config.get("BAR_CREDIT_LOW_THRESHOLD_CENTS", 1000)))
    except (TypeError, ValueError):
        return 1000


def max_topup_amount():
    """Upper safety limit for a single Pix credit recharge."""
    try:
        return max(5000, int(current_app.config.get("BAR_CREDIT_MAX_TOPUP_CENTS", 50000)))
    except (TypeError, ValueError):
        return 50000


def ensure_account(db, player_id):
    db.execute(
        "INSERT INTO bar_credit_accounts(player_id) VALUES(?) ON CONFLICT(player_id) DO NOTHING",
        (player_id,),
    )
    return db.execute("SELECT * FROM bar_credit_accounts WHERE player_id=?", (player_id,)).fetchone()


def balance(db, player_id):
    return ensure_account(db, player_id)


def available_balance(db, player_id):
    """Return ledger balance less active, non-expired reservations."""
    account = ensure_account(db, player_id)
    reserved = db.execute(
        """SELECT COALESCE(SUM(amount_cents), 0) AS total
           FROM bar_credit_reservations
           WHERE player_id=? AND status='reserved'
             AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)""",
        (player_id,),
    ).fetchone()
    return max(0, int(account["balance_cents"] or 0) - int(reserved["total"] or 0))


def _lock_credit_account(db, player_id):
    """Serialize reservation decisions for one wallet inside the caller transaction."""
    ensure_account(db, player_id)
    db.execute(
        """UPDATE bar_credit_accounts SET updated_at=updated_at
           WHERE player_id=?""",
        (player_id,),
    )


def _compatible_reservation(reservation, player_id, amount_cents, expires_at):
    same_expiration = (
        (reservation["expires_at"] is None and expires_at is None)
        or str(reservation["expires_at"]) == str(expires_at)
    )
    return (
        int(reservation["player_id"]) == int(player_id)
        and int(reservation["amount_cents"]) == amount_cents
        and same_expiration
    )


def reserve_credit(db, player_id, sale_id, amount_cents, expires_at=None):
    """Reserve available credit without changing the ledger balance."""
    try:
        amount_cents = int(amount_cents)
    except (TypeError, ValueError) as exc:
        raise ValueError("O valor da reserva deve ser maior que zero.") from exc
    if amount_cents <= 0:
        raise ValueError("O valor da reserva deve ser maior que zero.")

    existing = db.execute(
        "SELECT * FROM bar_credit_reservations WHERE sale_id=?", (sale_id,)
    ).fetchone()
    if existing:
        if _compatible_reservation(existing, player_id, amount_cents, expires_at):
            return existing
        raise ValueError("Esta venda já possui uma reserva de créditos diferente.")

    _lock_credit_account(db, player_id)
    existing = db.execute(
        "SELECT * FROM bar_credit_reservations WHERE sale_id=?", (sale_id,)
    ).fetchone()
    if existing:
        if _compatible_reservation(existing, player_id, amount_cents, expires_at):
            return existing
        raise ValueError("Esta venda já possui uma reserva de créditos diferente.")
    if available_balance(db, player_id) < amount_cents:
        raise ValueError("Saldo de créditos disponível insuficiente para esta reserva.")
    db.execute(
        """INSERT INTO bar_credit_reservations
           (sale_id,player_id,amount_cents,expires_at)
           VALUES(?,?,?,?)""",
        (sale_id, player_id, amount_cents, expires_at),
    )
    return db.execute(
        "SELECT * FROM bar_credit_reservations WHERE sale_id=?", (sale_id,)
    ).fetchone()


def consume_reservation(db, sale_id, actor_user_id=None):
    """Consume one reservation and its wallet credit exactly once."""
    lock_suffix = " FOR UPDATE" if getattr(db, "is_postgres", False) else ""
    reservation = db.execute(
        "SELECT * FROM bar_credit_reservations WHERE sale_id=?" + lock_suffix,
        (sale_id,),
    ).fetchone()
    if not reservation:
        raise ValueError("Reserva de créditos não encontrada.")
    if reservation["status"] == "consumed":
        account = ensure_account(db, reservation["player_id"])
        return int(account["balance_cents"] or 0), False
    if reservation["status"] != "reserved":
        raise ValueError("Esta reserva de créditos não está disponível para consumo.")
    if reservation["expires_at"] is not None:
        active = db.execute(
            """SELECT 1 FROM bar_credit_reservations
               WHERE sale_id=? AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)""",
            (sale_id,),
        ).fetchone()
        if not active:
            raise ValueError("Esta reserva de créditos expirou.")

    _lock_credit_account(db, reservation["player_id"])
    updated = db.execute(
        """UPDATE bar_credit_reservations
           SET status='consumed',consumed_at=CURRENT_TIMESTAMP
           WHERE sale_id=? AND status='reserved'
             AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)""",
        (sale_id,),
    )
    if updated.rowcount != 1:
        latest = db.execute(
            "SELECT status FROM bar_credit_reservations WHERE sale_id=?", (sale_id,)
        ).fetchone()
        if latest and latest["status"] == "consumed":
            account = ensure_account(db, reservation["player_id"])
            return int(account["balance_cents"] or 0), False
        raise ValueError("O estado da reserva de créditos mudou.")
    new_balance, _ = consume(
        db, reservation["player_id"], reservation["amount_cents"],
        sale_id, actor_user_id,
    )
    return new_balance, True


def release_reservation(db, sale_id):
    """Release an active reservation without changing the ledger balance."""
    reservation = db.execute(
        "SELECT status FROM bar_credit_reservations WHERE sale_id=?", (sale_id,)
    ).fetchone()
    if not reservation:
        raise ValueError("Reserva de créditos não encontrada.")
    if reservation["status"] == "released":
        return False
    if reservation["status"] != "reserved":
        raise ValueError("Uma reserva consumida não pode ser liberada.")
    updated = db.execute(
        """UPDATE bar_credit_reservations
           SET status='released',released_at=CURRENT_TIMESTAMP
           WHERE sale_id=? AND status='reserved'""",
        (sale_id,),
    )
    return updated.rowcount == 1


def _audit(db, player_id, action, amount_cents=0, *, topup_id=None,
           transaction_id=None, actor_user_id=None, reason=""):
    db.execute(
        """INSERT INTO bar_credit_audit
           (player_id,action,amount_cents,topup_id,transaction_id,actor_user_id,reason)
           VALUES(?,?,?,?,?,?,?)""",
        (player_id, action, int(amount_cents or 0), topup_id, transaction_id,
         actor_user_id, (reason or "").strip()),
    )


def consume(db, player_id, amount_cents, sale_id, created_by=None):
    account = ensure_account(db, player_id)
    current = int(account["balance_cents"] or 0)
    amount_cents = int(amount_cents)
    if amount_cents <= 0 or current < amount_cents:
        raise ValueError("Saldo de créditos insuficiente para este pedido.")
    new_balance = current - amount_cents
    db.execute(
        "UPDATE bar_credit_accounts SET balance_cents=?,low_balance_notified=CASE WHEN ? > ? THEN 0 ELSE low_balance_notified END,updated_at=CURRENT_TIMESTAMP WHERE player_id=?",
        (new_balance, new_balance, low_balance_threshold(), player_id),
    )
    cur = db.execute(
        """INSERT INTO bar_credit_transactions(player_id,type,amount_cents,balance_after_cents,description,sale_id,created_by)
           VALUES(?,?,?,?,?,?,?)""",
        (player_id, "CONSUMPTION", -amount_cents, new_balance, "Consumo no bar", sale_id, created_by),
    )
    _audit(db, player_id, "CONSUMO", -amount_cents, transaction_id=cur.lastrowid,
           actor_user_id=created_by, reason=f"Venda #{sale_id}")
    return new_balance, new_balance <= low_balance_threshold() and not bool(account["low_balance_notified"])


def credit_cash_change(db, player_id, amount_cents, sale_id, created_by=None):
    """Convert cash change into wallet credit exactly once per sale."""
    amount_cents = int(amount_cents)
    if amount_cents <= 0:
        raise ValueError("O troco convertido deve ser maior que zero.")
    existing = db.execute(
        """SELECT balance_after_cents FROM bar_credit_transactions
           WHERE sale_id=? AND type='ADJUSTMENT'
             AND description='Troco convertido em crédito'""",
        (sale_id,),
    ).fetchone()
    if existing:
        return int(existing["balance_after_cents"]), False
    account = ensure_account(db, player_id)
    new_balance = int(account["balance_cents"] or 0) + amount_cents
    db.execute(
        """UPDATE bar_credit_accounts
           SET balance_cents=?,low_balance_notified=0,updated_at=CURRENT_TIMESTAMP
           WHERE player_id=?""",
        (new_balance, player_id),
    )
    cur = db.execute(
        """INSERT INTO bar_credit_transactions
           (player_id,type,amount_cents,balance_after_cents,description,sale_id,created_by)
           VALUES(?,'ADJUSTMENT',?,?,?,?,?)""",
        (player_id, amount_cents, new_balance, "Troco convertido em crédito", sale_id, created_by),
    )
    _audit(
        db, player_id, "TROCO_CONVERTIDO", amount_cents,
        transaction_id=cur.lastrowid, actor_user_id=created_by,
        reason=f"Pedido #{sale_id}",
    )
    return new_balance, True


def approve_topup(db, topup, payment_id=None, created_by=None):
    """Credit a Pix top-up exactly once."""
    topup_id = topup["id"] if hasattr(topup, "keys") else topup
    row = db.execute("SELECT * FROM bar_credit_topups WHERE id=?", (topup_id,)).fetchone()
    if not row:
        return False
    if int(row["paid"] or 0):
        return True
    account = ensure_account(db, row["player_id"])
    new_balance = int(account["balance_cents"] or 0) + int(row["amount_cents"])
    db.execute(
        "UPDATE bar_credit_accounts SET balance_cents=?,low_balance_notified=0,updated_at=CURRENT_TIMESTAMP WHERE player_id=?",
        (new_balance, row["player_id"]),
    )
    player = db.execute("SELECT name FROM players WHERE id=?", (row["player_id"],)).fetchone()
    player_name = player["name"] if player else f"Peladeiro #{row['player_id']}"
    create_finance_movement(
        db, "bank", "in", "bar_credit_topup", int(row["amount_cents"]),
        f"Recarga de créditos — {player_name} — Pedido da recarga #{row['id']}",
        created_by, source="bar_credit_topup", source_id=row["id"],
    )
    cur = db.execute(
        """INSERT INTO bar_credit_transactions(player_id,type,amount_cents,balance_after_cents,description,topup_id)
           VALUES(?,?,?,?,?,?)""",
        (row["player_id"], "PURCHASE", int(row["amount_cents"]), new_balance, "Compra de créditos via Pix", row["id"]),
    )
    db.execute(
        "UPDATE bar_credit_topups SET paid=1,payment_status='approved',mercadopago_payment_id=?,paid_at=CURRENT_TIMESTAMP WHERE id=? AND paid=0",
        (payment_id, row["id"]),
    )
    _audit(db, row["player_id"], "RECARGA_APROVADA", row["amount_cents"],
           topup_id=row["id"], transaction_id=cur.lastrowid,
           reason="Pagamento Pix confirmado")
    return True


def adjust_balance(db, player_id, amount_cents, actor_user_id, reason):
    """Apply a manager-only correction, preserving a complete audit trail."""
    amount_cents = int(amount_cents)
    reason = (reason or "").strip()
    if not amount_cents or len(reason) < 5:
        raise ValueError("Informe um valor diferente de zero e uma justificativa (mínimo de 5 caracteres).")
    account = ensure_account(db, player_id)
    current = int(account["balance_cents"] or 0)
    new_balance = current + amount_cents
    if new_balance < 0:
        raise ValueError("O estorno não pode deixar o saldo negativo.")
    db.execute("UPDATE bar_credit_accounts SET balance_cents=?,low_balance_notified=0,updated_at=CURRENT_TIMESTAMP WHERE player_id=?", (new_balance, player_id))
    cur = db.execute(
        """INSERT INTO bar_credit_transactions(player_id,type,amount_cents,balance_after_cents,description,created_by)
           VALUES(?,?,?,?,?,?)""",
        (player_id, "ADJUSTMENT", amount_cents, new_balance, "Ajuste manual de créditos", actor_user_id),
    )
    _audit(db, player_id, "AJUSTE_MANUAL", amount_cents, transaction_id=cur.lastrowid,
           actor_user_id=actor_user_id, reason=reason)
    return new_balance


def refund_topup(db, topup_id, actor_user_id, reason):
    """Refund an approved top-up once, with balance and audit protection."""
    row = db.execute("SELECT * FROM bar_credit_topups WHERE id=?", (topup_id,)).fetchone()
    if not row or not int(row["paid"] or 0):
        raise ValueError("Recarga aprovada não encontrada.")
    if row["refunded_at"]:
        raise ValueError("Esta recarga já foi estornada.")
    new_balance = adjust_balance(db, row["player_id"], -int(row["amount_cents"]), actor_user_id, reason)
    db.execute("UPDATE bar_credit_topups SET refunded_at=CURRENT_TIMESTAMP,payment_status='refunded' WHERE id=?", (topup_id,))
    original_movement = db.execute(
        "SELECT id FROM finance_movements WHERE source='bar_credit_topup' AND source_id=?",
        (topup_id,),
    ).fetchone()
    if original_movement:
        player = db.execute("SELECT name FROM players WHERE id=?", (row["player_id"],)).fetchone()
        player_name = player["name"] if player else f"Peladeiro #{row['player_id']}"
        create_finance_movement(
            db, "bank", "out", "bar_credit_topup", int(row["amount_cents"]),
            f"Estorno de recarga de créditos — {player_name} — Pedido #{topup_id}",
            actor_user_id, source="bar_credit_refund", source_id=topup_id,
            reversed_movement_id=original_movement["id"],
        )
    _audit(db, row["player_id"], "RECARGA_ESTORNADA", -int(row["amount_cents"]), topup_id=topup_id,
           actor_user_id=actor_user_id, reason=reason)
    return new_balance


def notify_low_balance(db, player_id, balance_cents):
    threshold = low_balance_threshold()
    if balance_cents > threshold:
        return {"sent": 0, "skipped": 1}
    player = db.execute("SELECT id,name,war_name,email FROM players WHERE id=?", (player_id,)).fetchone()
    if not player:
        return {"sent": 0, "skipped": 1}
    account = db.execute("SELECT low_balance_notified FROM bar_credit_accounts WHERE player_id=?", (player_id,)).fetchone()
    if account and account["low_balance_notified"]:
        return {"sent": 0, "skipped": 1, "reason": "já avisado"}
    name = player["war_name"] or player["name"]
    title = "Saldo de créditos baixo"
    body = f"Olá, {name}! Seu saldo de créditos no bar é {money(balance_cents)}. Faça uma nova recarga quando quiser."
    result = send_player_push_once(db, player_id, "bar_credit_low", datetime.now().strftime("%Y-%m"), title, body, "/creditos")
    sender = current_app.config.get("GMAIL_SMTP_USER")
    password = current_app.config.get("GMAIL_APP_PASSWORD")
    email = str(player["email"] or "").strip()
    if sender and password and "@" in email:
        html = f"<h2 style='color:#07558c'>PELADEIROS GPCTA</h2><p>Olá, <strong>{name}</strong>!</p><p>Seu saldo de créditos no bar está baixo:</p><p style='font-size:24px;color:#c62828'><strong>{money(balance_cents)}</strong></p><p>Faça uma nova recarga quando quiser.</p>"
        try:
            send_gmail_html(sender, password, email, "Saldo de créditos baixo - PELADEIROS GPCTA", body, html)
        except Exception as exc:
            current_app.logger.warning("Falha ao enviar alerta de crédito baixo: %s", exc)
    db.execute("UPDATE bar_credit_accounts SET low_balance_notified=1,updated_at=CURRENT_TIMESTAMP WHERE player_id=?", (player_id,))
    db.commit()
    return result
