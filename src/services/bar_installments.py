"""Model case-only Bar installments without changing sale, stock or payment state."""
import json
from datetime import date, datetime, timedelta


def _checkout_items(items, use_bar_credit):
    if use_bar_credit is not False:
        raise ValueError('Créditos não são permitidos no Pix em 2x.')
    if not isinstance(items, list) or not items:
        raise ValueError('Informe um checkout composto somente por caixas.')
    quantities = {}
    for item in items:
        if not isinstance(item, dict) or item.get('sale_mode') != 'case':
            raise ValueError('Unidades e carrinhos mistos não aceitam Pix em 2x.')
        pid, quantity = item.get('product_id'), item.get('quantity')
        if type(pid) is not int or pid <= 0 or type(quantity) is not int or quantity <= 0:
            raise ValueError('Produto ou quantidade de caixas inválida.')
        quantities[pid] = quantities.get(pid, 0) + quantity
    return quantities


def validate_checkout(db, items, use_bar_credit=False):
    """Validate explicit checkout modes and price using the existing case rules.

    Call at checkout, before creating a plan. No quantity-based inference and
    no values supplied by the browser are used as product configuration.
    """
    from src.routes.sales import _price_bar_requests
    quantities = _checkout_items(items, use_bar_credit)
    placeholders = ','.join('?' for _ in quantities)
    products = db.execute(
        f'''SELECT id,name,category,active,case_sale_enabled,units_per_case,price_cents
            FROM products WHERE active=1 AND id IN ({placeholders})''', tuple(quantities),
    ).fetchall()
    products = {row['id']: row for row in products}
    if len(products) != len(quantities):
        raise ValueError('Produto inexistente ou inativo.')
    units = dict(quantities)
    _, total = _price_bar_requests(units, quantities, products)
    if not 2 <= total <= 2_147_483_647:
        raise ValueError('O total deve permitir duas parcelas positivas em centavos.')
    snapshot = [dict(product_id=pid, sale_mode='case', case_quantity=quantities[pid],
                     units_per_case=int(products[pid]['units_per_case']), quantity=units[pid],
                     unit_price_cents=int(products[pid]['price_cents'])) for pid in sorted(quantities)]
    return {'total_cents': total, 'items': snapshot}


def create_installment_plan(db, sale_id, total_cents, purchase_date, items, use_bar_credit=False):
    """Own transaction. Persist validated checkout evidence; replay uses history.

    A replay must match the original cart, sale items, total and schedule.
    Later changes to product prices or case configuration never reprice it.
    """
    quantities = _checkout_items(items, use_bar_credit)
    if type(sale_id) is not int or sale_id <= 0:
        raise ValueError('Venda inválida.')
    if type(total_cents) is not int or not 2 <= total_cents <= 2_147_483_647:
        raise ValueError('Total inválido para duas parcelas.')
    if isinstance(purchase_date, str):
        try:
            purchase_date = date.fromisoformat(purchase_date)
        except ValueError as exc:
            raise ValueError('Data da compra inválida.') from exc
    if not isinstance(purchase_date, date) or isinstance(purchase_date, datetime):
        raise ValueError('Informe a data civil da compra.')
    try:
        schedule = [(1, total_cents // 2, purchase_date.isoformat()),
                    (2, total_cents - total_cents // 2, (purchase_date + timedelta(days=30)).isoformat())]
    except OverflowError as exc:
        raise ValueError('Vencimento fora do intervalo permitido.') from exc
    with db:
        if not db.is_postgres and not db.conn.in_transaction:
            db.execute('BEGIN IMMEDIATE')
        lock = ' FOR UPDATE' if db.is_postgres else ''
        sale = db.execute('SELECT * FROM sales WHERE id=?' + lock, (sale_id,)).fetchone()
        if not sale or sale['total_cents'] != total_cents:
            raise ValueError('Total incompatível com a venda.')
        existing = db.execute('SELECT * FROM bar_installment_plans WHERE sale_id=?', (sale_id,)).fetchone()
        if not existing and (sale['payment_method'] != 'Pix' or sale['event_id'] or not sale['player_id'] or
                             sale['paid'] or sale['delivered_at'] or sale['ready_for_delivery'] or
                             sale['payment_status'] not in {'creating', 'pending'}):
            raise ValueError('Venda incompatível com um novo plano Pix em 2x.')
        if existing:
            snapshot = json.loads(existing['checkout_snapshot'])
            if existing['checkout_origin'] != 'case_only' or existing['total_cents'] != total_cents or quantities != {
                    row['product_id']: row['case_quantity'] for row in snapshot}:
                raise ValueError('Checkout incompatível com o plano existente.')
        else:
            validated = validate_checkout(db, items, use_bar_credit)
            if validated['total_cents'] != total_cents:
                raise ValueError('Total incompatível com o preço atual das caixas.')
            snapshot = validated['items']
        actual = db.execute('SELECT product_id,quantity,unit_price_cents FROM sale_items WHERE sale_id=? ORDER BY product_id',
                            (sale_id,)).fetchall()
        expected = [(r['product_id'], r['quantity'], r['unit_price_cents']) for r in snapshot]
        if [(r['product_id'], r['quantity'], r['unit_price_cents']) for r in actual] != expected:
            raise ValueError('Itens da venda incompatíveis com o checkout por caixa.')
        if db.execute("SELECT 1 FROM sale_payment_parts WHERE sale_id=? AND method='Créditos'", (sale_id,)).fetchone() or db.execute(
                'SELECT 1 FROM bar_credit_reservations WHERE sale_id=?', (sale_id,)).fetchone():
            raise ValueError('Venda com créditos não aceita Pix em 2x.')
        if not existing:
            plan_id = db.execute('''INSERT INTO bar_installment_plans
                                   (sale_id,total_cents,checkout_origin,checkout_snapshot)
                                   VALUES(?,?,'case_only',?)''',
                                 (sale_id, total_cents, json.dumps(snapshot, sort_keys=True))).lastrowid
            for number, amount, due in schedule:
                db.execute('INSERT INTO bar_installments(plan_id,installment_number,amount_cents,due_date) VALUES(?,?,?,?)',
                           (plan_id, number, amount, due))
        plan = db.execute('SELECT * FROM bar_installment_plans WHERE sale_id=?', (sale_id,)).fetchone()
        installments = db.execute('SELECT * FROM bar_installments WHERE plan_id=? ORDER BY installment_number',
                                  (plan['id'],)).fetchall()
        if [(r['installment_number'], r['amount_cents'], str(r['due_date'])) for r in installments] != schedule:
            raise ValueError('Parcelas ou vencimentos incompatíveis com o plano.')
        return {'plan': dict(plan), 'installments': [dict(r) for r in installments]}
