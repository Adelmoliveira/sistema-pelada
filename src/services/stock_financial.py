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


def stock_financial_data(db, product_id=None):
    # One statement gives balances, prices and purchase history a common read
    # snapshot on both engines. No mutation, DDL or transaction ownership here.
    selection = '' if product_id is None else ' AND p.id=?'
    sources = db.execute('''
        SELECT p.id,p.name,p.category,p.active,p.stock,p.price_cents,p.cost_cents,
               p.cost_reference_revision,
               f.id reference_id,f.unit_cost_cents reference_cost,f.reference_stock,
               f.reference_revision,f.created_at reference_created_at,
               r.id restock_id,r.notes,
               COALESCE(c.corrected_quantity,r.quantity) acquisition_quantity,
               COALESCE(c.corrected_unit_cost_cents,r.unit_cost_cents) acquisition_cost,
               EXISTS(SELECT 1 FROM stock_adjustments a
                      WHERE a.product_id=p.id AND a.difference>0
                        AND a.reason NOT LIKE 'Correção da reposição #%%') unvalued_increase
        FROM products p
        LEFT JOIN stock_cost_references f ON f.id=(
            SELECT MAX(f2.id) FROM stock_cost_references f2 WHERE f2.product_id=p.id)
        LEFT JOIN restocks r ON r.product_id=p.id
        LEFT JOIN restock_corrections c ON c.id=(
            SELECT MAX(c2.id) FROM restock_corrections c2 WHERE c2.restock_id=r.id)
        WHERE p.category<>? AND (p.active=1 OR p.stock>0)''' + selection + '''
        ORDER BY LOWER(p.name),p.name,p.id,r.id
    ''', (SPORTS_MATERIAL_CATEGORY,) + (() if product_id is None else (product_id,))).fetchall()
    grouped = {}
    for source in sources:
        product = grouped.setdefault(source['id'], {
            'id': source['id'], 'name': source['name'], 'category': source['category'],
            'active': bool(source['active']), 'stock': int(source['stock']),
            'price_cents': int(source['price_cents']), 'purchase_units': 0,
            'purchase_count': 0, 'purchase_value': 0, 'reason_codes': set(),
            'catalog_cost_cents': int(source['cost_cents']),
            'reference_revision': int(source['cost_reference_revision']),
            'reference_id': source['reference_id'] or 0,
            'reference_cost': source['reference_cost'],
            'reference_stock': source['reference_stock'],
            'approved_revision': source['reference_revision'],
            'reference_created_at': source['reference_created_at'],
            'correctable_restock_ids': [],
        })
        if source['unvalued_increase']:
            product['reason_codes'].add('unvalued_increase')
        if source['restock_id'] is None:
            continue
        quantity, cost = source['acquisition_quantity'], source['acquisition_cost']
        allocated = (source['notes'] or '').startswith('Reposição via solicitação #')
        if not allocated and (quantity is None or cost is None or quantity < 0 or cost < 0 or (quantity > 0 and cost == 0)):
            product['correctable_restock_ids'].append(source['restock_id'])
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
        if allocated:
            product['reason_codes'].add('allocated_cost')

    products = []
    for product in grouped.values():
        if not product['purchase_units']:
            product['reason_codes'].add('no_history')
        elif product['stock'] > product['purchase_units']:
            product['reason_codes'].add('uncovered_stock')
        product['revenue_cents'] = product['stock'] * product['price_cents']
        available = not product['reason_codes']
        valid_reference = bool(product['reference_id'] and product['active']
                               and product['approved_revision'] == product['reference_revision']
                               and product['reference_stock'] == product['stock'])
        product['reference_valid'] = valid_reference
        product['can_regularize'] = bool(product['active'] and not available and not product['correctable_restock_ids'])
        product['cost_status'] = ('historical_reference' if available else
                                  'manager_reference' if valid_reference else 'unavailable')
        product['reasons'] = [REASONS[code] for code in sorted(product['reason_codes'])]
        product['reason_codes'] = sorted(product['reason_codes'])
        if available or valid_reference:
            average = (Decimal(product['purchase_value']) / Decimal(product['purchase_units'])
                       if available else Decimal(product['reference_cost']))
            product['unit_cost_cents'] = _cents(average)
            product['stock_value_cents'] = _cents(average * product['stock'])
            product['margin_cents'] = product['revenue_cents'] - product['stock_value_cents']
        else:
            product['unit_cost_cents'] = None
            product['stock_value_cents'] = None
            product['margin_cents'] = None
        product['margin_percent'] = _percentage(product['margin_cents'], product['revenue_cents'])
        products.append(product)

    covered = [p for p in products if p['cost_status'] != 'unavailable' and p['stock'] > 0]
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
        'historical_value_cents': sum(p['stock_value_cents'] for p in covered if p['cost_status']=='historical_reference'),
        'regularized_value_cents': sum(p['stock_value_cents'] for p in covered if p['cost_status']=='manager_reference'),
        'covered_units': sum(p['stock'] for p in covered),
        'coverage_percent': _percentage(sum(p['stock'] for p in covered), sum(p['stock'] for p in products)),
    }}
