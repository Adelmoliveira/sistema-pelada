"""Explicit managerial estimates, never purchases or accounting movements."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import re

from src.catalog import SPORTS_MATERIAL_CATEGORY
from src.services.stock_conferences import conference_transaction
from src.services.stock_financial import stock_financial_data


def parse_reference_cost(value):
    text = (value or '').strip()
    if len(text) > 24 or not re.fullmatch(r'\d+(?:\.\d{3})*(?:,\d{1,4})?', text):
        raise ValueError('Informe um custo válido em reais, usando vírgula decimal.')
    amount = Decimal(text.replace('.', '').replace(',', '.')) * 100
    if amount > 2147483647:
        raise ValueError('O custo informado excede o limite permitido.')
    result = int(amount.quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    if result <= 0:
        raise ValueError('O custo deve ser positivo após o arredondamento em centavos.')
    return result


def reference_snapshot(product):
    return {key: product[key] for key in ('id', 'stock', 'reference_revision', 'reference_id', 'catalog_cost_cents')}


def register_cost_reference(db, product_id, user_id, expected, unit_cost_cents,
                            justification, value_origin, confirmed=False, support_document=''):
    if confirmed is not True:
        raise ValueError('Confirme explicitamente a referência de custo.')
    if isinstance(unit_cost_cents, bool) or not isinstance(unit_cost_cents, int) or not 0 < unit_cost_cents <= 2147483647:
        raise ValueError('O custo deve ser positivo e estar dentro do limite permitido.')
    justification, support_document = justification.strip(), support_document.strip()
    if not 5 <= len(justification) <= 2000:
        raise ValueError('Informe uma justificativa entre 5 e 2000 caracteres.')
    if len(support_document) > 500 or value_origin not in ('catalog_confirmed', 'manager_entered'):
        raise ValueError('Origem do valor ou documento de suporte inválido.')
    with conference_transaction(db):
        actor = db.execute('SELECT id,name,role,active FROM users WHERE id=?' +
                           (' FOR SHARE' if db.is_postgres else ''), (user_id,)).fetchone()
        if not actor or not actor['active'] or actor['role'] != 'manager':
            raise PermissionError('Somente gestores ativos podem regularizar custos.')
        product = db.execute('SELECT id,active,category FROM products WHERE id=?' +
                             (' FOR UPDATE' if db.is_postgres else ''), (product_id,)).fetchone()
        if not product or not product['active'] or product['category'] == SPORTS_MATERIAL_CATEGORY:
            raise ValueError('Produto inexistente, inativo ou fora do Bar.')
        current = stock_financial_data(db, product_id)['products'][0]
        if reference_snapshot(current) != expected:
            raise ValueError('O produto, o histórico ou a referência mudou. Recarregue e revise a confirmação.')
        if current['cost_status'] == 'historical_reference':
            raise ValueError('O produto já possui custo histórico apurado; preserve a média histórica.')
        if current['correctable_restock_ids']:
            raise ValueError('Corrija primeiro a reposição pelo fluxo existente.')
        if value_origin == 'catalog_confirmed' and unit_cost_cents != current['catalog_cost_cents']:
            raise ValueError('O valor difere do cadastro; informe a origem como valor informado pelo gestor.')
        return db.execute('''INSERT INTO stock_cost_references
            (product_id,product_name,unit_cost_cents,reference_stock,reference_revision,previous_reference_id,
             responsible_id,responsible_name,justification,value_origin,support_document,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
            (product_id, current['name'], unit_cost_cents, current['stock'], current['reference_revision'],
             current['reference_id'], actor['id'], actor['name'], justification, value_origin,
             support_document, datetime.now(timezone.utc).isoformat())).lastrowid
