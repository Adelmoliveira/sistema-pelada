"""Create sports ready-stock installment plans without initiating payments."""

from datetime import date, datetime, timedelta

from src.catalog import SPORTS_MATERIAL_CATEGORY


def create_installment_plan(db, sale_id, total_cents, purchase_date):
    """Atomically create or return an identical three-part plan.

    Amounts and due dates are historical snapshots. Existing plans with
    different input or incomplete schedules are rejected, never rewritten.
    This function owns its transaction, matching other database services.
    It never changes sales, fulfillment, reservations or stock.
    """
    if type(sale_id) is not int or sale_id <= 0:
        raise ValueError("Venda inválida.")
    if type(total_cents) is not int or not 3 <= total_cents <= 2_147_483_647:
        raise ValueError("O total deve permitir três parcelas positivas em centavos.")
    if isinstance(purchase_date, str):
        try:
            purchase_date = date.fromisoformat(purchase_date)
        except ValueError as exc:
            raise ValueError("Data da compra inválida.") from exc
    if not isinstance(purchase_date, date) or isinstance(purchase_date, datetime):
        raise ValueError("Informe a data civil da compra, sem horário.")
    base = total_cents // 3
    amounts = (base, base, total_cents - 2 * base)
    try:
        schedule = [(number, amount, (purchase_date + timedelta(days=offset)).isoformat())
                    for number, amount, offset in zip((1, 2, 3), amounts, (0, 30, 60))]
    except OverflowError as exc:
        raise ValueError("Data da compra fora do intervalo permitido.") from exc

    with db:
        if not db.is_postgres and not db.conn.in_transaction:
            db.execute("BEGIN IMMEDIATE")
        lock = " FOR UPDATE" if db.is_postgres else ""
        sale = db.execute("SELECT id,total_cents,paid,payment_status,delivered_at FROM sales WHERE id=?" + lock, (sale_id,)).fetchone()
        if not sale or int(sale["total_cents"]) != total_cents:
            raise ValueError("O total informado não corresponde à venda.")
        existing = db.execute("SELECT id FROM sports_installment_plans WHERE sale_id=?", (sale_id,)).fetchone()
        if not existing and (sale["paid"] or sale["delivered_at"] or
                             sale["payment_status"] in {"approved", "refunded", "failed", "expired", "canceled"}):
            raise ValueError("A venda não está elegível para um novo parcelamento.")
        items = db.execute(
            """SELECT p.category,d.order_mode,d.fulfillment_status
               FROM sale_items si JOIN products p ON p.id=si.product_id
               LEFT JOIN sports_sale_item_details d ON d.sale_item_id=si.id
               WHERE si.sale_id=?""", (sale_id,),
        ).fetchall()
        if not items or any(row["category"] != SPORTS_MATERIAL_CATEGORY or
                            row["order_mode"] != "ready" or
                            row["fulfillment_status"] == "cancelled" or
                            (not existing and row["fulfillment_status"] not in {"reserved", "available"}) for row in items):
            raise ValueError("Parcelamento disponível somente para Material Esportivo de pronta entrega.")
        inserted = db.execute(
            """INSERT INTO sports_installment_plans(sale_id,total_cents)
               VALUES(?,?) ON CONFLICT(sale_id) DO NOTHING""", (sale_id, total_cents),
        ).rowcount == 1
        plan = db.execute("SELECT * FROM sports_installment_plans WHERE sale_id=?", (sale_id,)).fetchone()
        if int(plan["total_cents"]) != total_cents:
            raise ValueError("A venda já possui um plano com total diferente.")
        if inserted:
            for number, amount, due_date in schedule:
                db.execute(
                    """INSERT INTO sports_installments(plan_id,installment_number,amount_cents,due_date)
                       VALUES(?,?,?,?)""", (plan["id"], number, amount, due_date),
                )
        installments = db.execute(
            "SELECT * FROM sports_installments WHERE plan_id=? ORDER BY installment_number",
            (plan["id"],),
        ).fetchall()
        actual = [(int(row["installment_number"]), int(row["amount_cents"]), str(row["due_date"]))
                  for row in installments]
        if actual != schedule or sum(row["amount_cents"] for row in installments) != total_cents:
            raise ValueError("O plano existente possui parcelas ou vencimentos incompatíveis.")
        return {"plan": dict(plan), "installments": [dict(row) for row in installments]}
