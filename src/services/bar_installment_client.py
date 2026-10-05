"""Client case checkout and historical Bar installment summaries."""
import json
from uuid import UUID
from datetime import timedelta
from src.utils import local_today
from src.services.bar_installments import validate_checkout, create_installment_plan
from src.services.bar_installment_reconciliation import table_available, withdrawal_allowed


def normalized_items(items):
    if not isinstance(items, list) or not items:
        raise ValueError('Escolha caixas para esta compra.')
    normalized=[]
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('Item inválido.')
        try:
            pid, qty = item['product_id'], item['quantity']
            if isinstance(pid, bool) or isinstance(qty, bool) or not str(pid).isdigit() or not str(qty).isdigit():
                raise ValueError('Quantidade inválida.')
            normalized.append(dict(product_id=int(pid),quantity=int(qty),sale_mode=item.get('sale_mode')))
        except (KeyError,TypeError,ValueError) as exc:
            raise ValueError('Item inválido.') from exc
    return normalized


def preview_checkout(db, items, use_bar_credit=False):
    result=validate_checkout(db, normalized_items(items),use_bar_credit)
    today=local_today()
    total=result['total_cents']
    return dict(total_cents=total, installments=[dict(installment_number=n,amount_cents=a,due_date=(today+timedelta(days=d)).isoformat())
                for n,a,d in ((1,total//2,0),(2,total-total//2,30))])


def create_checkout(db, player_id, items, key, use_bar_credit=False, notes=''):
    try: key=str(UUID(str(key)))
    except (ValueError,TypeError,AttributeError) as exc: raise ValueError('Chave de compra inválida.') from exc
    items=normalized_items(items)
    # This validation rejects mixed carts and credit requests even on replay.
    from src.services.bar_installments import _checkout_items
    quantities=_checkout_items(items,use_bar_credit)
    reference='bar2x_checkout_'+key
    with db:
        if not db.is_postgres and not db.conn.in_transaction: db.execute('BEGIN IMMEDIATE')
        player=db.execute('SELECT id,email FROM players WHERE id=? AND active=1',(player_id,)).fetchone()
        if not player or '@' not in str(player['email'] or ''): raise ValueError('Cadastre um e-mail válido.')
        if db.is_postgres: db.execute('SELECT id FROM players WHERE id=? FOR UPDATE',(player_id,)).fetchone()
        existing=db.execute('SELECT id,player_id FROM sales WHERE external_reference=?',(reference,)).fetchone()
        if existing:
            if existing['player_id'] != player_id: raise ValueError('Chave de compra indisponível.')
            plan=db.execute('SELECT checkout_snapshot FROM bar_installment_plans WHERE sale_id=?',(existing['id'],)).fetchone()
            if not plan or quantities != {i['product_id']:i['case_quantity'] for i in json.loads(plan['checkout_snapshot'])}:
                raise ValueError('Esta chave identifica outra compra.')
            return existing['id']
        result=validate_checkout(db,items,use_bar_credit)
        sale_id=db.execute("""INSERT INTO sales(player_id,payment_method,total_cents,paid,payment_status,external_reference,notes)
                              VALUES(?,'Pix',?,0,'pending',?,?)""",(player_id,result['total_cents'],reference,str(notes)[:500])).lastrowid
        for item in result['items']:
            product=db.execute('SELECT * FROM products WHERE id=?',(item['product_id'],)).fetchone()
            changed=db.execute('UPDATE products SET stock=stock-? WHERE id=? AND active=1 AND stock>=?',
                               (item['quantity'],item['product_id'],item['quantity']))
            if changed.rowcount != 1: raise ValueError('Estoque insuficiente. Atualize a compra.')
            from src.routes.sales import _insert_bar_items
            _insert_bar_items(db,sale_id,item['product_id'],product,[(item['quantity'],item['unit_price_cents'])])
        create_installment_plan(db,sale_id,result['total_cents'],local_today(),items,use_bar_credit)
        return sale_id


def plan_summary(db,sale_id,player_id):
    if not table_available(db,'bar_installment_plans'): return None
    plan=db.execute('SELECT p.* FROM bar_installment_plans p JOIN sales s ON s.id=p.sale_id WHERE p.sale_id=? AND s.player_id=?',
                    (sale_id,player_id)).fetchone()
    if not plan: return None
    rows=db.execute('SELECT * FROM bar_installments WHERE plan_id=? ORDER BY installment_number',(plan['id'],)).fetchall()
    paid=sum(r['amount_cents'] for r in rows if r['status']=='paid')
    return dict(total_cents=plan['total_cents'],status=plan['status'],paid_cents=paid,balance_cents=plan['total_cents']-paid,
                withdrawal_allowed=withdrawal_allowed(db,sale_id),installments=[dict(r,display_status='Pago' if r['status']=='paid' else 'Vencido' if str(r['due_date'])<local_today().isoformat() else 'Pendente') for r in rows])
