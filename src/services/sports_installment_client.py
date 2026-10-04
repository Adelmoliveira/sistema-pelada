"""Client checkout and presentation for ready-stock sports installments."""
from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from src.catalog import SPORTS_MATERIAL_CATEGORY
from src.services.sports_installments import create_installment_plan
from src.services.sports_installment_reconciliation import installment_withdrawal_allowed


def today():
    return datetime.now(ZoneInfo('America/Sao_Paulo')).date()


def preview_checkout(db, items):
    if not isinstance(items, list) or not items:
        raise ValueError('Escolha Material Esportivo de pronta entrega.')
    normalized=[]
    demand={}
    total=0
    for item in items:
        if not isinstance(item, dict) or item.get('order_mode') != 'ready':
            raise ValueError('Pix 3x disponível somente para pronta entrega.')
        try:
            pid=int(item['product_id']); vid=int(item['variant_id']); quantity=int(item['quantity'])
            if str(quantity) != str(item['quantity']) or quantity <= 0:
                raise ValueError
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError('Produto, variante ou quantidade inválida.') from exc
        row=db.execute('''SELECT v.id,v.product_id,v.size,v.stock,v.active variant_active,
                          p.category,p.active,p.price_cents,p.cost_cents,p.name,
                          c.ready_sale_enabled,c.allow_custom_name,c.allow_custom_number
                          FROM sports_product_variants v JOIN products p ON p.id=v.product_id
                          JOIN sports_product_config c ON c.product_id=p.id WHERE v.id=?''',(vid,)).fetchone()
        if not row or row['product_id'] != pid or row['category'] != SPORTS_MATERIAL_CATEGORY or not row['active'] or not row['variant_active'] or not row['ready_sale_enabled']:
            raise ValueError('Produto não elegível para pronta entrega.')
        name=' '.join(str(item.get('custom_name') or '').split())
        number=' '.join(str(item.get('custom_number') or '').split())
        if len(name)>40 or len(number)>10 or (name and not row['allow_custom_name']) or (number and not row['allow_custom_number']):
            raise ValueError('Personalização inválida.')
        demand[vid]=demand.get(vid,0)+quantity
        if demand[vid]>row['stock']:
            raise ValueError('Estoque insuficiente.')
        total+=quantity*row['price_cents']
        normalized.append(dict(product_id=pid,variant_id=vid,quantity=quantity,custom_name=name,custom_number=number,product=dict(row)))
    if not 3 <= total <= 2147483647:
        raise ValueError('Total inválido para três parcelas.')
    base=total//3
    return {'total_cents':total,'installments':[{'installment_number':n,'amount_cents':amount,'due_date':(today()+timedelta(days=offset)).isoformat()} for n,amount,offset in ((1,base,0),(2,base,30),(3,total-2*base,60))]},normalized


def create_checkout(db, player_id, items, key, notes=''):
    try:
        key=str(UUID(str(key)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError('Chave de compra inválida.') from exc
    reference=f'sportscheckout_{player_id}_{key}'
    with db:
        if not db.is_postgres and not db.conn.in_transaction:
            db.execute('BEGIN IMMEDIATE')
        lock=' FOR UPDATE' if db.is_postgres else ''
        player=db.execute('SELECT id,email FROM players WHERE id=? AND active=1'+lock,(player_id,)).fetchone()
        if not player or '@' not in str(player['email'] or ''):
            raise ValueError('Cadastre um e-mail válido para o peladeiro.')
        existing=db.execute('SELECT id FROM sales WHERE external_reference=? AND player_id=?',(reference,player_id)).fetchone()
        if existing:
            # A checkout key identifies one immutable cart, not a new purchase.
            stored=db.execute("""SELECT si.product_id,d.variant_id,si.quantity,d.custom_name,d.custom_number,d.order_mode
                                 FROM sale_items si JOIN sports_sale_item_details d ON d.sale_item_id=si.id
                                 WHERE si.sale_id=? ORDER BY si.id""",(existing['id'],)).fetchall()
            try:
                submitted=[(int(i['product_id']),int(i['variant_id']),int(i['quantity']),
                            ' '.join(str(i.get('custom_name') or '').split()),
                            ' '.join(str(i.get('custom_number') or '').split()),i['order_mode']) for i in items]
            except (KeyError,TypeError,ValueError) as exc:
                raise ValueError('Carrinho inválido para esta compra.') from exc
            if sorted(submitted) != sorted((r['product_id'],r['variant_id'],r['quantity'],r['custom_name'] or '',r['custom_number'] or '',r['order_mode']) for r in stored):
                raise ValueError('Esta chave já identifica outra compra.')
            return existing['id']
        preview,normalized=preview_checkout(db,items)
        sale_id=db.execute("""INSERT INTO sales(player_id,payment_method,total_cents,paid,payment_status,external_reference,notes)
                              VALUES(?,'Pix',?,0,'pending',?,?)""",(player_id,preview['total_cents'],reference,str(notes)[:500])).lastrowid
        for item in normalized:
            p=item['product']
            updated=db.execute('UPDATE sports_product_variants SET stock=stock-?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active AND stock>=?',(item['quantity'],item['variant_id'],item['quantity']))
            if updated.rowcount != 1:
                raise ValueError('O estoque mudou. Atualize a compra.')
            item_id=db.execute('INSERT INTO sale_items(sale_id,product_id,quantity,unit_price_cents,unit_cost_cents) VALUES(?,?,?,?,?)',(sale_id,item['product_id'],item['quantity'],p['price_cents'],p['cost_cents'])).lastrowid
            db.execute("""INSERT INTO sports_sale_item_details(sale_item_id,variant_id,variant_size,custom_name,custom_number,order_mode,fulfillment_status)
                          VALUES(?,?,?,?,?,'ready','reserved') RETURNING sale_item_id""",(item_id,item['variant_id'],p['size'],item['custom_name'],item['custom_number']))
            db.execute("INSERT INTO sports_stock_reservations(sale_item_id,variant_id,quantity,status) VALUES(?,?,?,'reserved')",(item_id,item['variant_id'],item['quantity']))
        create_installment_plan(db,sale_id,preview['total_cents'],today())
        return sale_id


def plan_summary(db, sale_id, player_id):
    plan=db.execute('''SELECT p.* FROM sports_installment_plans p JOIN sales s ON s.id=p.sale_id
                       WHERE p.sale_id=? AND s.player_id=?''',(sale_id,player_id)).fetchone()
    if not plan:
        return None
    installments=db.execute('SELECT id,installment_number,amount_cents,due_date,status FROM sports_installments WHERE plan_id=? ORDER BY installment_number',(plan['id'],)).fetchall()
    paid=sum(i['amount_cents'] for i in installments if i['status']=='paid')
    return dict(sale_id=sale_id,total_cents=plan['total_cents'],paid_cents=paid,balance_cents=plan['total_cents']-paid,
                status=plan['status'],withdrawal_allowed=installment_withdrawal_allowed(db,sale_id),
                installments=[dict(i,display_status='Pago' if i['status']=='paid' else 'Vencido' if str(i['due_date'])<today().isoformat() else 'Pendente') for i in installments])


def public_attempt(attempt):
    return {key:attempt[key] for key in ('status','qr_code','qr_code_base64','ticket_url','expires_at')}
