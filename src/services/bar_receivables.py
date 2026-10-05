"""Read-only receivables for case-only Bar plans; amounts come from installments."""
from datetime import date
from src.utils import local_today



ELIGIBILITY = """p.checkout_origin='case_only'
    AND s.payment_method='Pix'
    AND s.payment_status NOT IN ('canceled','expired','failed','refunded')
    AND EXISTS (SELECT 1 FROM bar_installments confirmed
                WHERE confirmed.plan_id=p.id AND confirmed.status='paid')
    AND NOT EXISTS (SELECT 1 FROM pix_checkout_closures closure
                    WHERE closure.sale_id=s.id AND closure.status='completed')"""


def parse_filters(args):
    filters={key:str(args.get(key) or '').strip() for key in ('player_id','situation','due_from','due_to')}
    if filters['situation'] not in {'','open','overdue','paid'}: raise ValueError('Situação inválida.')
    if filters['player_id']:
        try:
            if int(filters['player_id'])<=0: raise ValueError
        except ValueError as exc: raise ValueError('Peladeiro inválido.') from exc
    for key in ('due_from','due_to'):
        if filters[key]:
            try: filters[key]=date.fromisoformat(filters[key]).isoformat()
            except ValueError as exc: raise ValueError('Vencimento inválido.') from exc
    if filters['due_from'] and filters['due_to'] and filters['due_from']>filters['due_to']:
        raise ValueError('Período inválido.')
    return filters


def _plans(db,filters,plan_id=None):
    where=[ELIGIBILITY]
    params=[]
    if plan_id is not None: where.append('p.id=?');params.append(plan_id)
    if filters.get('player_id'): where.append('s.player_id=?');params.append(int(filters['player_id']))
    due=[]
    for field,operator in (('due_from','>='),('due_to','<=')):
        if filters.get(field): due.append('i.due_date'+operator+'?');params.append(filters[field])
    if due: where.append('EXISTS(SELECT 1 FROM bar_installments i WHERE i.plan_id=p.id AND '+' AND '.join(due)+')')
    rows=db.execute('''SELECT p.*,s.player_id,s.created_at purchase_date,
                       COALESCE(NULLIF(pl.war_name,''),pl.name,'Peladeiro') player_name
                       FROM bar_installment_plans p JOIN sales s ON s.id=p.sale_id
                       LEFT JOIN players pl ON pl.id=s.player_id WHERE '''+' AND '.join(where)+' ORDER BY p.id DESC',tuple(params)).fetchall()
    if not rows: return []
    placeholders=','.join('?' for _ in rows)
    parts=db.execute(f'SELECT * FROM bar_installments WHERE plan_id IN ({placeholders}) ORDER BY installment_number',tuple(p['id'] for p in rows)).fetchall()
    today=local_today().isoformat()
    result=[]
    for row in rows:
        plan=dict(row)
        installments=[dict(i) for i in parts if i['plan_id']==plan['id']]
        for i in installments:
            i['display_status']='Pago' if i['status']=='paid' else 'Vencido' if str(i['due_date'])<today else 'Pendente'
        pending=[i for i in installments if i['status']=='pending']
        overdue=[i for i in pending if str(i['due_date'])<today]
        plan.update(installments=installments,paid_count=sum(i['status']=='paid' for i in installments),
                    received_cents=sum(i['amount_cents'] for i in installments if i['status']=='paid'),
                    balance_cents=sum(i['amount_cents'] for i in pending),overdue_cents=sum(i['amount_cents'] for i in overdue),
                    overdue_count=len(overdue),next_due_date=min((str(i['due_date']) for i in pending),default=None))
        plan['situation']='paid' if len(installments)==2 and plan['paid_count']==2 else 'overdue' if overdue else 'open'
        plan['situation_label']={'paid':'Quitado','overdue':'Vencido','open':'Em aberto'}[plan['situation']]
        if not filters.get('situation') or filters['situation']==plan['situation']: result.append(plan)
    return result


def receivables(db,args):
    filters=parse_filters(args)
    plans=_plans(db,filters)
    summary=dict(receivable_cents=sum(p['balance_cents'] for p in plans),overdue_cents=sum(p['overdue_cents'] for p in plans),
                 upcoming_cents=sum(p['balance_cents']-p['overdue_cents'] for p in plans),received_cents=sum(p['received_cents'] for p in plans),
                 players_with_balance=len({p['player_id'] for p in plans if p['player_id'] is not None and p['balance_cents']>0}),
                 overdue_count=sum(p['overdue_count'] for p in plans))
    players=db.execute(f"""SELECT DISTINCT pl.id,COALESCE(NULLIF(pl.war_name,''),pl.name) name
                           FROM players pl JOIN sales s ON s.player_id=pl.id JOIN bar_installment_plans p ON p.sale_id=s.id
                           WHERE {ELIGIBILITY} ORDER BY name,pl.id""").fetchall()
    return dict(plans=plans,summary=summary,filters=filters,players=players)


def receivable_detail(db,plan_id):
    plans=_plans(db,{},plan_id)
    return plans[0] if plans else None
