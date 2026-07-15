from src.utils import local_today


ACCOUNT_LABELS = {"cash": "Dinheiro físico", "bank": "Conta / Pix"}
CATEGORY_LABELS = {
    "adjustment": "Ajuste",
    "deposit": "Depósito / aporte",
    "expense": "Despesa",
    "purchase": "Compra de estoque",
    "withdrawal": "Retirada",
    "other": "Outro",
}


def get_session(db, business_date=None):
    business_date = business_date or local_today().isoformat()
    return db.execute(
        """SELECT s.*,op.name opened_by_name,cl.name closed_by_name
        FROM cash_sessions s
        LEFT JOIN users op ON op.id=s.opened_by
        LEFT JOIN users cl ON cl.id=s.closed_by
        WHERE s.business_date=?""",
        (business_date,),
    ).fetchone()


def create_movement(
    db,
    session_id,
    account,
    direction,
    category,
    amount_cents,
    description,
    created_by,
    source="manual",
    source_id=None,
    reversed_movement_id=None,
):
    if account not in ACCOUNT_LABELS:
        raise ValueError("Conta do caixa inválida.")
    if direction not in {"in", "out"}:
        raise ValueError("Tipo de movimentação inválido.")
    if category not in CATEGORY_LABELS:
        raise ValueError("Categoria inválida.")
    if int(amount_cents or 0) <= 0:
        raise ValueError("O valor deve ser maior que zero.")
    if not (description or "").strip():
        raise ValueError("Informe a descrição da movimentação.")

    return db.execute(
        """INSERT INTO cash_movements
        (session_id,account,direction,category,amount_cents,description,source,source_id,
         created_by,reversed_movement_id)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            session_id,
            account,
            direction,
            category,
            int(amount_cents),
            description.strip(),
            source,
            source_id,
            created_by,
            reversed_movement_id,
        ),
    )


def session_summary(db, session):
    sales = db.execute(
        """SELECT
        COALESCE(SUM(CASE WHEN payment_method='Dinheiro' THEN total_cents ELSE 0 END),0) cash_sales,
        COALESCE(SUM(CASE WHEN payment_method IN ('Pix','Débito') THEN total_cents ELSE 0 END),0) bank_sales
        FROM sales
        WHERE paid=1 AND payment_method<>'Cortesia'
          AND date(COALESCE(paid_at,created_at))=?""",
        (session["business_date"],),
    ).fetchone()
    totals = db.execute(
        """SELECT
        COALESCE(SUM(CASE WHEN account='cash' AND direction='in' THEN amount_cents ELSE 0 END),0) cash_in,
        COALESCE(SUM(CASE WHEN account='cash' AND direction='out' THEN amount_cents ELSE 0 END),0) cash_out,
        COALESCE(SUM(CASE WHEN account='bank' AND direction='in' THEN amount_cents ELSE 0 END),0) bank_in,
        COALESCE(SUM(CASE WHEN account='bank' AND direction='out' THEN amount_cents ELSE 0 END),0) bank_out
        FROM cash_movements WHERE session_id=?""",
        (session["id"],),
    ).fetchone()

    calculated_cash = (
        session["opening_cash_cents"] + sales["cash_sales"] + totals["cash_in"] - totals["cash_out"]
    )
    calculated_bank = (
        session["opening_bank_cents"] + sales["bank_sales"] + totals["bank_in"] - totals["bank_out"]
    )
    closed = session["status"] == "closed"
    expected_cash = session["expected_cash_cents"] if closed else calculated_cash
    expected_bank = session["expected_bank_cents"] if closed else calculated_bank

    movements = db.execute(
        """SELECT m.*,u.name user_name,
        EXISTS(SELECT 1 FROM cash_movements r WHERE r.reversed_movement_id=m.id) reversed
        FROM cash_movements m LEFT JOIN users u ON u.id=m.created_by
        WHERE m.session_id=? ORDER BY m.id DESC""",
        (session["id"],),
    ).fetchall()
    sale_rows = db.execute(
        """SELECT s.id,s.payment_method,s.total_cents,COALESCE(s.paid_at,s.created_at) payment_date,
        p.name player_name
        FROM sales s JOIN players p ON p.id=s.player_id
        WHERE s.paid=1 AND s.payment_method<>'Cortesia'
          AND date(COALESCE(s.paid_at,s.created_at))=?
        ORDER BY COALESCE(s.paid_at,s.created_at) DESC,s.id DESC""",
        (session["business_date"],),
    ).fetchall()
    return {
        "cash_sales": sales["cash_sales"],
        "bank_sales": sales["bank_sales"],
        "cash_in": totals["cash_in"],
        "cash_out": totals["cash_out"],
        "bank_in": totals["bank_in"],
        "bank_out": totals["bank_out"],
        "expected_cash": expected_cash,
        "expected_bank": expected_bank,
        "calculated_cash": calculated_cash,
        "calculated_bank": calculated_bank,
        "movements": movements,
        "sales": sale_rows,
        "changed_after_close": closed
        and (calculated_cash != expected_cash or calculated_bank != expected_bank),
    }
