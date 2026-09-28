PAYMENT_PART_METHODS = {"Créditos", "Pix", "Dinheiro"}
PAYMENT_PART_STATUSES = {"reserved", "pending", "approved", "canceled", "refunded"}


def create_payment_part(
    db, sale_id, method, amount_cents, status, external_reference=None, payment_id=None
):
    amount_cents = int(amount_cents)
    if method not in PAYMENT_PART_METHODS:
        raise ValueError("Método da parte de pagamento inválido.")
    if status not in PAYMENT_PART_STATUSES:
        raise ValueError("Status da parte de pagamento inválido.")
    if amount_cents <= 0:
        raise ValueError("O valor da parte de pagamento deve ser positivo.")
    db.execute(
        """INSERT INTO sale_payment_parts
           (sale_id,method,amount_cents,status,external_reference,payment_id,confirmed_at)
           VALUES(?,?,?,?,?,?,CASE WHEN ?='approved' THEN CURRENT_TIMESTAMP ELSE NULL END)
           ON CONFLICT(sale_id,method) DO NOTHING""",
        (sale_id, method, amount_cents, status, external_reference, payment_id, status),
    )
    part = db.execute(
        "SELECT * FROM sale_payment_parts WHERE sale_id=? AND method=?",
        (sale_id, method),
    ).fetchone()
    if not part or int(part["amount_cents"]) != amount_cents:
        raise ValueError("A venda já possui uma parte de pagamento incompatível.")
    return part


def get_payment_parts(db, sale_id):
    return db.execute(
        "SELECT * FROM sale_payment_parts WHERE sale_id=? ORDER BY id", (sale_id,)
    ).fetchall()


def associate_payment_part(db, sale_id, method, external_reference=None, payment_id=None):
    return db.execute(
        """UPDATE sale_payment_parts
           SET external_reference=COALESCE(?,external_reference),payment_id=COALESCE(?,payment_id)
           WHERE sale_id=? AND method=?""",
        (external_reference, payment_id, sale_id, method),
    ).rowcount


def approve_payment_part(db, sale_id, method, external_reference=None, payment_id=None):
    return db.execute(
        """UPDATE sale_payment_parts
           SET status='approved',confirmed_at=COALESCE(confirmed_at,CURRENT_TIMESTAMP),
               external_reference=COALESCE(?,external_reference),payment_id=COALESCE(?,payment_id)
           WHERE sale_id=? AND method=? AND status IN ('reserved','pending')""",
        (external_reference, payment_id, sale_id, method),
    ).rowcount


def cancel_active_payment_parts(db, sale_id):
    return db.execute(
        """UPDATE sale_payment_parts
           SET status='canceled',canceled_at=COALESCE(canceled_at,CURRENT_TIMESTAMP)
           WHERE sale_id=? AND status IN ('reserved','pending')""",
        (sale_id,),
    ).rowcount


def validate_approved_parts_total(db, sale_id):
    totals = db.execute(
        """SELECT s.total_cents,s.paid,s.payment_status,COUNT(pp.id) part_count,
                  COALESCE(SUM(CASE WHEN pp.status='approved' THEN pp.amount_cents ELSE 0 END),0) approved_cents
           FROM sales s LEFT JOIN sale_payment_parts pp ON pp.sale_id=s.id
           WHERE s.id=? GROUP BY s.id,s.total_cents,s.paid,s.payment_status""",
        (sale_id,),
    ).fetchone()
    if not totals or not totals["part_count"]:
        return True
    if int(totals["paid"] or 0) and totals["payment_status"] == "approved":
        if int(totals["approved_cents"] or 0) != int(totals["total_cents"] or 0):
            raise RuntimeError("As partes aprovadas não correspondem ao total da venda.")
    return True
