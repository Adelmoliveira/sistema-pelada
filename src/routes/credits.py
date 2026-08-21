import uuid

from flask import Blueprint, current_app, g, jsonify, render_template, request, url_for, redirect, flash

from src.db import get_db
from src.routes.auth import roles_allowed
from src.services.bar_credits import PRESET_AMOUNTS_CENTS, adjust_balance, approve_topup, balance, low_balance_threshold, max_topup_amount, refund_topup
from src.services.mercadopago import MercadoPagoError, create_pix_order, get_order
from src.services.pix import generate_qrcode_base64
from src.utils import money

bp = Blueprint("credits", __name__, url_prefix="/creditos")


def _order_payment_id(order):
    payments = (order.get("transactions") or {}).get("payments") or []
    return str(payments[0].get("id")) if payments and payments[0].get("id") else None


def _player_id():
    return int(g.user["player_id"] or 0)


@bp.get("/")
@roles_allowed("client")
def index():
    db = get_db()
    player_id = _player_id()
    account = balance(db, player_id)
    transactions = db.execute(
        "SELECT * FROM bar_credit_transactions WHERE player_id=? ORDER BY created_at DESC,id DESC LIMIT 50",
        (player_id,),
    ).fetchall()
    pending = db.execute(
        "SELECT * FROM bar_credit_topups WHERE player_id=? AND paid=0 AND payment_status IN ('creating','pending') ORDER BY id DESC LIMIT 5",
        (player_id,),
    ).fetchall()
    return render_template(
        "credits.html", account=account, transactions=transactions, pending=pending,
        preset_amounts=PRESET_AMOUNTS_CENTS, low_balance_threshold_cents=low_balance_threshold(),
    )


@bp.get("/saldo")
@roles_allowed("client")
def current_balance():
    """Return the connected player's current balance for live UI updates."""
    account = get_db().execute(
        "SELECT balance_cents FROM bar_credit_accounts WHERE player_id=?",
        (_player_id(),),
    ).fetchone()
    return jsonify(balance_cents=int(account["balance_cents"] or 0) if account else 0)


@bp.get("/pendentes")
@roles_allowed("client")
def pending_topups():
    """Return the number of Pix credit top-ups awaiting confirmation.

    This endpoint is intentionally read-only. The Mercado Pago webhook (or the
    normal top-up status polling on the credits page) is responsible for marking
    a top-up as approved; the menu indicator simply reflects that persisted
    state and never changes payment data itself.
    """
    row = get_db().execute(
        """SELECT COUNT(*) AS total
           FROM bar_credit_topups
           WHERE player_id=? AND paid=0
             AND payment_status IN ('creating','pending')""",
        (_player_id(),),
    ).fetchone()
    return jsonify(count=int(row["total"] or 0))


@bp.get("/recibo/<int:topup_id>")
@roles_allowed("client")
def receipt(topup_id):
    db = get_db()
    topup = db.execute(
        """SELECT t.*,p.name,p.war_name,p.cpf FROM bar_credit_topups t
           JOIN players p ON p.id=t.player_id WHERE t.id=? AND t.player_id=?""",
        (topup_id, _player_id()),
    ).fetchone()
    if not topup or not topup["paid"]:
        return "Recarga não encontrada ou ainda não confirmada.", 404
    return render_template("credit_receipt.html", topup=topup)


@bp.get("/gerenciar")
@roles_allowed("manager")
def manage():
    db = get_db()
    players = db.execute(
        """SELECT p.id,p.name,p.war_name,COALESCE(a.balance_cents,0) balance_cents
           FROM players p LEFT JOIN bar_credit_accounts a ON a.player_id=p.id
           WHERE p.active=1 ORDER BY p.name"""
    ).fetchall()
    audits = db.execute(
        """SELECT a.*,p.name,p.war_name,u.name actor_name
           FROM bar_credit_audit a JOIN players p ON p.id=a.player_id
           LEFT JOIN users u ON u.id=a.actor_user_id
           ORDER BY a.id DESC LIMIT 100"""
    ).fetchall()
    topups = db.execute(
        """SELECT t.*,p.name,p.war_name FROM bar_credit_topups t JOIN players p ON p.id=t.player_id
           WHERE t.paid=1 ORDER BY t.id DESC LIMIT 50"""
    ).fetchall()
    return render_template("credit_manage.html", players=players, audits=audits, topups=topups)


@bp.post("/gerenciar/ajuste")
@roles_allowed("manager")
def manage_adjust():
    db = get_db()
    try:
        player_id = int(request.form["player_id"])
        amount_cents = int(request.form["amount_cents"])
        with db:
            adjust_balance(db, player_id, amount_cents, g.user["id"], request.form.get("reason"))
        flash("Ajuste de créditos registrado com auditoria.", "success")
    except (KeyError, ValueError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("credits.manage"))


@bp.post("/gerenciar/estorno/<int:topup_id>")
@roles_allowed("manager")
def manage_refund(topup_id):
    db = get_db()
    try:
        with db:
            refund_topup(db, topup_id, g.user["id"], request.form.get("reason"))
        flash("Recarga estornada com auditoria.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("credits.manage"))


@bp.post("/comprar")
@roles_allowed("client")
def purchase():
    if not current_app.config.get("EXTERNAL_PAYMENTS_ENABLED", True):
        return jsonify(error="Recarga Pix indisponível na homologação."), 403
    raw_amount = request.form.get("amount_cents") or (request.get_json(silent=True) or {}).get("amount_cents")
    try:
        amount_cents = int(raw_amount)
    except (TypeError, ValueError):
        return jsonify(error="Escolha um valor de recarga válido."), 400
    if amount_cents not in PRESET_AMOUNTS_CENTS:
        return jsonify(error="Escolha um dos valores disponíveis para recarga."), 400
    if amount_cents > max_topup_amount():
        return jsonify(error="O valor máximo de uma recarga é limitado por segurança."), 400
    access_token = current_app.config.get("MERCADOPAGO_ACCESS_TOKEN") if current_app.config.get("EXTERNAL_PAYMENTS_ENABLED", True) else None
    if not access_token:
        return jsonify(error="A integração com Mercado Pago ainda não foi configurada."), 503
    db = get_db()
    player = db.execute("SELECT id,email FROM players WHERE id=? AND active=1", (_player_id(),)).fetchone()
    email = str(player["email"] or "").strip().lower() if player else ""
    if not player or "@" not in email:
        return jsonify(error="Cadastre um e-mail válido antes de comprar créditos."), 400
    external_reference = f"credito_{uuid.uuid4().hex}"
    idempotency_key = (request.headers.get("Idempotency-Key") or str(uuid.uuid4())).strip()[:120]
    existing = db.execute(
        "SELECT id,mercadopago_order_id,payment_status FROM bar_credit_topups WHERE player_id=? AND idempotency_key=?",
        (player["id"], idempotency_key),
    ).fetchone()
    if existing:
        return jsonify(error="Esta recarga já foi registrada. Aguarde a confirmação do pagamento."), 409
    cur = db.execute(
        """INSERT INTO bar_credit_topups(player_id,amount_cents,payment_status,external_reference,idempotency_key)
           VALUES(?,?, 'creating',?,?)""",
        (player["id"], amount_cents, external_reference, idempotency_key),
    )
    topup_id = cur.lastrowid
    db.commit()
    try:
        order = create_pix_order(access_token, external_reference, amount_cents, idempotency_key, email)
        order_id = order.get("id")
        payments = (order.get("transactions") or {}).get("payments") or []
        payment = payments[0].get("payment_method") if payments else {}
        payload = payment.get("qr_code") if payment else None
        if not order_id or not payload:
            raise MercadoPagoError("O Mercado Pago não retornou o QR Code Pix.")
        db.execute("UPDATE bar_credit_topups SET mercadopago_order_id=?,payment_status='pending' WHERE id=?", (order_id, topup_id))
        db.commit()
        image = generate_qrcode_base64(payload)
        return jsonify(
            topup_id=topup_id, order_id=order_id, payload=payload,
            image=f"data:image/png;base64,{image}", amount=money(amount_cents),
            status_url=url_for("credits.status", topup_id=topup_id), status="pending",
        ), 201
    except Exception as exc:
        current_app.logger.error("Erro ao criar recarga de créditos: %s", exc)
        db.execute("UPDATE bar_credit_topups SET payment_status='failed' WHERE id=?", (topup_id,))
        db.commit()
        return jsonify(error=str(exc) if isinstance(exc, MercadoPagoError) else "Não foi possível criar a recarga Pix."), 502


@bp.get("/status/<int:topup_id>")
@roles_allowed("client")
def status(topup_id):
    db = get_db()
    topup = db.execute("SELECT * FROM bar_credit_topups WHERE id=? AND player_id=?", (topup_id, _player_id())).fetchone()
    if not topup:
        return jsonify(error="Recarga não encontrada."), 404
    access_token = current_app.config.get("MERCADOPAGO_ACCESS_TOKEN") if current_app.config.get("EXTERNAL_PAYMENTS_ENABLED", True) else None
    if topup["payment_status"] == "pending" and topup["mercadopago_order_id"] and access_token:
        try:
            order = get_order(access_token, topup["mercadopago_order_id"])
            if order.get("status") == "processed" and order.get("status_detail") == "accredited":
                with db:
                        approve_topup(db, topup, _order_payment_id(order), created_by=g.user["id"])
                topup = db.execute("SELECT * FROM bar_credit_topups WHERE id=?", (topup_id,)).fetchone()
            elif order.get("status") in ("expired", "canceled"):
                db.execute("UPDATE bar_credit_topups SET payment_status=? WHERE id=? AND paid=0", (order["status"], topup_id))
                db.commit()
        except MercadoPagoError as exc:
            current_app.logger.warning("Falha ao consultar recarga %s: %s", topup_id, exc)
    account = balance(db, _player_id())
    return jsonify(status=topup["payment_status"], paid=bool(topup["paid"]), balance_cents=int(account["balance_cents"] or 0))
