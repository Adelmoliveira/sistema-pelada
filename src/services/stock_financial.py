"""Read-only historical reference valuation, never an accounting inventory ledger."""
from decimal import Decimal, ROUND_HALF_UP

from src.catalog import SPORTS_MATERIAL_CATEGORY


REASONS = {
    'no_history': 'Sem histórico de aquisição individual utilizável.',
    'missing_cost': 'Há aquisições com custo zero ou desconhecido.',
    'allocated_cost': 'Há compra por solicitação com rateio global sem custo individual comprovado.',
    'invalid_history': 'Há quantidade ou custo inconsistente no histórico.',
    'unvalued_increase': 'Há acréscimo de estoque sem custo de aquisição vinculado.',
    'uncovered_stock': 'O saldo supera as unidades das aquisições rastreadas.',
}


def _cents(value):
    return int(value.quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def _percentage(margin, revenue):
    if not revenue or margin is None:
        return None
    return (Decimal(margin) * 100 / Decimal(revenue)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def stock_financial_data(db):
    # One statement gives balances, prices and purchase history a common read
    # snapshot on both engines. No mutation, DDL or transaction ownership here.
    sources = db.execute('''
        SELECT p.id,p.name,p.category,p.active,p.stock,p.price_cents,
               r.id restock_id,r.notes,
               COALESCE(c.corrected_quantity,r.quantity) acquisition_quantity,
               COALESCE(c.corrected_unit_cost_cents,r.unit_cost_cents) acquisition_cost,
               EXISTS(SELECT 1 FROM stock_adjustments a
                      WHERE a.product_id=p.id AND a.difference>0
                        AND a.reason NOT LIKE 'Correção da reposição #%') unvalued_increase
        FROM products p
        LEFT JOIN restocks r ON r.product_id=p.id
        LEFT JOIN restock_corrections c ON c.id=(
            SELECT MAX(c2.id) FROM restock_corrections c2 WHERE c2.restock_id=r.id)
        WHERE p.category<>? AND (p.active=1 OR p.stock>0)
        ORDER BY LOWER(p.name),p.name,p.id,r.id
    ''', (SPORTS_MATERIAL_CATEGORY,)).fetchall()
    grouped = {}
    for source in sources:
        product = grouped.setdefault(source['id'], {
            'id': source['id'], 'name': source['name'], 'category': source['category'],
            'active': bool(source['active']), 'stock': int(source['stock']),
            'price_cents': int(source['price_cents']), 'purchase_units': 0,
            'purchase_count': 0, 'purchase_value': 0, 'reason_codes': set(),
        })
        if source['unvalued_increase']:
            product['reason_codes'].add('unvalued_increase')
        if source['restock_id'] is None:
            continue
        quantity, cost = source['acquisition_quantity'], source['acquisition_cost']
        if quantity is None or cost is None or quantity < 0 or cost < 0:
            product['reason_codes'].add('invalid_history')
            continue
        if quantity == 0:  # fully corrected-away purchase is not an acquisition
            continue
        product['purchase_count'] += 1
        product['purchase_units'] += quantity
        product['purchase_value'] += quantity * cost
        if cost == 0:
            product['reason_codes'].add('missing_cost')
        # Legacy requests store their provenance in this system-generated note.
        # Even a later correction is not proof of an individually invoiced cost.
        if (source['notes'] or '').startswith('Reposição via solicitação #'):
            product['reason_codes'].add('allocated_cost')

    products = []
    for product in grouped.values():
        if not product['purchase_units']:
            product['reason_codes'].add('no_history')
        elif product['stock'] > product['purchase_units']:
            product['reason_codes'].add('uncovered_stock')
        product['revenue_cents'] = product['stock'] * product['price_cents']
        available = not product['reason_codes']
        product['cost_status'] = 'historical_reference' if available else 'unavailable'
        product['reasons'] = [REASONS[code] for code in sorted(product['reason_codes'])]
        product['reason_codes'] = sorted(product['reason_codes'])
        if available:
            average = Decimal(product['purchase_value']) / Decimal(product['purchase_units'])
            product['unit_cost_cents'] = _cents(average)
            product['stock_value_cents'] = _cents(average * product['stock'])
            product['margin_cents'] = product['revenue_cents'] - product['stock_value_cents']
        else:
            product['unit_cost_cents'] = None
            product['stock_value_cents'] = None
            product['margin_cents'] = None
        product['margin_percent'] = _percentage(product['margin_cents'], product['revenue_cents'])
        products.append(product)

    covered = [p for p in products if p['cost_status'] == 'historical_reference' and p['stock'] > 0]
    missing = [p for p in products if p['cost_status'] == 'unavailable' and p['stock'] > 0]
    covered_revenue = sum(p['revenue_cents'] for p in covered)
    stock_value = sum(p['stock_value_cents'] for p in covered)
    margin = covered_revenue - stock_value
    # With only unvalued stock, never render an apparent zero valuation/profit.
    if missing and not covered:
        stock_value = margin = None
    return {'products': products, 'summary': {
        'products': len(products), 'units': sum(p['stock'] for p in products),
        'revenue_cents': sum(p['revenue_cents'] for p in products),
        'stock_value_cents': stock_value, 'margin_cents': margin,
        'margin_percent': _percentage(margin, covered_revenue),
        'covered_revenue_cents': covered_revenue, 'covered_products': len(covered),
        'missing_products': len(missing), 'missing_units': sum(p['stock'] for p in missing),
        'missing_revenue_cents': sum(p['revenue_cents'] for p in missing),
        'partial': bool(missing),
        'inactive_units': sum(p['stock'] for p in products if not p['active']),
    }}
