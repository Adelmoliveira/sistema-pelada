"""Read-only administrative view of ready-stock sports installment plans."""
from datetime import date

from src.catalog import SPORTS_MATERIAL_CATEGORY
from src.services.sports_installment_client import today


ELIGIBILITY = """s.payment_method='Pix'
    AND s.payment_status NOT IN ('canceled','expired','failed','refunded')
    AND EXISTS (SELECT 1 FROM sports_installments confirmed
                WHERE confirmed.plan_id=p.id AND confirmed.status='paid')
    AND NOT EXISTS (SELECT 1 FROM pix_checkout_closures closure
                    WHERE closure.sale_id=s.id AND closure.status='completed')
    AND EXISTS (SELECT 1 FROM sale_items si WHERE si.sale_id=s.id)
    AND NOT EXISTS (
        SELECT 1 FROM sale_items si JOIN products prod ON prod.id=si.product_id
        LEFT JOIN sports_sale_item_details d ON d.sale_item_id=si.id
        WHERE si.sale_id=s.id AND (prod.category<>? OR d.order_mode IS NULL OR d.order_mode<>'ready')
    )"""


def parse_filters(args):
    filters = {name: str(args.get(name) or '').strip() for name in ('player_id', 'situation', 'due_from', 'due_to')}
    if filters['situation'] not in {'', 'open', 'overdue', 'paid'}:
        raise ValueError('Situação inválida.')
    if filters['player_id']:
        try:
            if int(filters['player_id']) <= 0:
                raise ValueError
        except ValueError as exc:
            raise ValueError('Peladeiro inválido.') from exc
    for field in ('due_from', 'due_to'):
        if filters[field]:
            try:
                filters[field] = date.fromisoformat(filters[field]).isoformat()
            except ValueError as exc:
                raise ValueError('Vencimento inválido.') from exc
    if filters['due_from'] and filters['due_to'] and filters['due_from'] > filters['due_to']:
        raise ValueError('O início do período deve preceder o fim.')
    return filters


def _plans(db, filters, plan_id=None):
    where = [ELIGIBILITY]
    params = [SPORTS_MATERIAL_CATEGORY]
    if plan_id is not None:
        where.append('p.id=?')
        params.append(plan_id)
    if filters.get('player_id'):
        where.append('s.player_id=?')
        params.append(int(filters['player_id']))
    due_conditions = []
    for field, operator in (('due_from', '>='), ('due_to', '<=')):
        if filters.get(field):
            due_conditions.append(f'i.due_date{operator}?')
            params.append(filters[field])
    if due_conditions:
        where.append('EXISTS (SELECT 1 FROM sports_installments i WHERE i.plan_id=p.id AND ' + ' AND '.join(due_conditions) + ')')
    plans = db.execute(f"""SELECT p.*,s.player_id,s.created_at purchase_date,s.delivered_at,s.payment_status,
                            COALESCE(NULLIF(pl.war_name,''),pl.name,'Peladeiro') player_name
                           FROM sports_installment_plans p JOIN sales s ON s.id=p.sale_id
                           LEFT JOIN players pl ON pl.id=s.player_id
                           WHERE {' AND '.join(where)} ORDER BY s.created_at DESC,p.id DESC""", tuple(params)).fetchall()
    if not plans:
        return []
    placeholders = ','.join('?' for _ in plans)
    installments = db.execute(f'SELECT * FROM sports_installments WHERE plan_id IN ({placeholders}) ORDER BY installment_number', tuple(p['id'] for p in plans)).fetchall()
    items = db.execute(f"""SELECT si.sale_id,si.quantity,prod.name,d.variant_size,d.custom_name,d.custom_number,
                                  d.fulfillment_status,
                                  COALESCE((SELECT SUM(del.quantity-COALESCE(r.quantity,0))
                                            FROM sale_item_deliveries del
                                            LEFT JOIN sale_item_delivery_restorations r ON r.delivery_id=del.id
                                            WHERE del.sale_item_id=si.id),0) delivered_quantity
                           FROM sale_items si JOIN products prod ON prod.id=si.product_id
                           JOIN sports_sale_item_details d ON d.sale_item_id=si.id
                           WHERE si.sale_id IN ({placeholders}) ORDER BY si.id""", tuple(p['sale_id'] for p in plans)).fetchall()
    result = []
    current_date = today().isoformat()
    for row in plans:
        plan = dict(row)
        parts = [dict(i) for i in installments if i['plan_id'] == plan['id']]
        for part in parts:
            part['display_status'] = 'Pago' if part['status'] == 'paid' else 'Vencido' if str(part['due_date']) < current_date else 'Pendente'
        pending = [i for i in parts if i['status'] == 'pending']
        overdue = [i for i in pending if str(i['due_date']) < current_date]
        plan.update(installments=parts,paid_count=sum(i['status']=='paid' for i in parts),
                    received_cents=sum(i['amount_cents'] for i in parts if i['status']=='paid'),
                    balance_cents=sum(i['amount_cents'] for i in pending),
                    overdue_cents=sum(i['amount_cents'] for i in overdue),overdue_count=len(overdue),
                    next_due_date=min((str(i['due_date']) for i in pending),default=None))
        plan['situation'] = 'paid' if len(parts) == 3 and plan['paid_count'] == 3 else 'overdue' if overdue else 'open'
        plan['situation_label'] = {'paid':'Quitado','overdue':'Vencido','open':'Em aberto'}[plan['situation']]
        plan['items'] = [dict(i) for i in items if i['sale_id'] == plan['sale_id']]
        first_paid = any(i['installment_number'] == 1 and i['status'] == 'paid' for i in parts)
        plan['withdrawal_allowed'] = bool(first_paid and plan['first_installment_paid_at'] and plan['status'] in {'active','paid'} and plan['payment_status'] not in {'canceled','refunded','failed','expired'})
        delivered = sum(i['quantity'] if i['fulfillment_status']=='delivered' or plan['delivered_at'] else min(i['quantity'],i['delivered_quantity']) for i in plan['items'])
        bought = sum(i['quantity'] for i in plan['items'])
        plan['withdrawal_label'] = 'Retirado' if bought and delivered >= bought else 'Retirada parcial' if delivered else 'Liberada' if plan['withdrawal_allowed'] else 'Bloqueada — primeira parcela pendente'
        if not filters.get('situation') or filters['situation'] == plan['situation']:
            result.append(plan)
    return result


def receivables(db, args):
    filters = parse_filters(args)
    plans = _plans(db, filters)
    summary = dict(receivable_cents=sum(p['balance_cents'] for p in plans),
                   overdue_cents=sum(p['overdue_cents'] for p in plans),
                   upcoming_cents=sum(p['balance_cents']-p['overdue_cents'] for p in plans),
                   received_cents=sum(p['received_cents'] for p in plans),
                   players_with_balance=len({p['player_id'] for p in plans if p['player_id'] is not None and p['balance_cents']>0}),
                   overdue_count=sum(p['overdue_count'] for p in plans))
    players = db.execute(f"""SELECT DISTINCT pl.id,COALESCE(NULLIF(pl.war_name,''),pl.name) name FROM players pl
                             JOIN sales s ON s.player_id=pl.id JOIN sports_installment_plans p ON p.sale_id=s.id
                             WHERE {ELIGIBILITY} ORDER BY name,pl.id""", (SPORTS_MATERIAL_CATEGORY,)).fetchall()
    return dict(plans=plans,summary=summary,filters=filters,players=players)


def receivable_detail(db, plan_id):
    plans = _plans(db, {}, plan_id)
    return plans[0] if plans else None
