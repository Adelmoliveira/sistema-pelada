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
