"""Deterministic monthly purchase forecast for bar products.

Physical consumption is measured by deliveries, not by payment or order creation.
The service is read-only and deliberately does not create restock requests.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal, ROUND_CEILING

from src.catalog import SPORTS_MATERIAL_CATEGORY
from src.utils import SAO_PAULO, local_today


VALID_HISTORY_WINDOWS = (3, 6)
DEFAULT_SAFETY_MARGIN_PERCENT = 10
TERMINAL_PAYMENT_STATUSES = {"canceled", "cancelled", "refunded", "failed", "expired"}


def _first_day(value):
    if value is None:
        value = local_today()
    if isinstance(value, str):
        try:
            value = datetime.strptime(value, "%Y-%m").date()
        except ValueError as exc:
            raise ValueError("planned_month deve usar o formato YYYY-MM") from exc
    if isinstance(value, datetime):
        value = value.date()
    if not isinstance(value, date):
        raise ValueError("planned_month deve ser uma data ou YYYY-MM")
    return value.replace(day=1)


def _shift_month(value, offset):
    absolute = value.year * 12 + value.month - 1 + offset
    return date(absolute // 12, absolute % 12 + 1, 1)


def _utc_boundary(value):
    return datetime.combine(value, time.min, SAO_PAULO).astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")


def _as_utc(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _month_key(value):
    return _as_utc(value).astimezone(SAO_PAULO).strftime("%Y-%m")


def _ceil(value):
    return int(Decimal(str(value)).to_integral_value(rounding=ROUND_CEILING))


def _trend(values):
    """Return an informational least-squares slope; it never changes the forecast."""
    if len(values) < 3:
        return None, "insufficient_data"
    count = Decimal(len(values))
    mean_x = Decimal(len(values) - 1) / Decimal(2)
    mean_y = sum(Decimal(value) for value in values) / count
    denominator = sum((Decimal(index) - mean_x) ** 2 for index in range(len(values)))
    if not denominator:
        return 0.0, "stable"
    slope = sum(
        (Decimal(index) - mean_x) * (Decimal(value) - mean_y)
        for index, value in enumerate(values)
    ) / denominator
    numeric = float(slope)
    direction = "up" if numeric > 0 else "down" if numeric < 0 else "stable"
    return round(numeric, 4), direction


def purchase_forecast(
    db,
    planned_month=None,
    history_months=3,
    safety_margin_percent=DEFAULT_SAFETY_MARGIN_PERCENT,
):
    """Build a read-only bar purchase forecast from physical withdrawals.

    ``history_months`` accepts only 3 or 6 complete months. Restorations are
    attributed to the original withdrawal month and clamped at zero if corrupt
    data restores more than was delivered.
    """
    if history_months not in VALID_HISTORY_WINDOWS:
        raise ValueError("history_months deve ser 3 ou 6")
    try:
        margin = Decimal(str(safety_margin_percent))
    except Exception as exc:
        raise ValueError("safety_margin_percent deve ser numérico") from exc
    if margin < 0 or margin > 30:
        raise ValueError("safety_margin_percent deve estar entre 0 e 30")

    planned = _first_day(planned_month)
    history_start = _shift_month(planned, -history_months)
    months = [_shift_month(history_start, index).strftime("%Y-%m") for index in range(history_months)]
    start, end = _utc_boundary(history_start), _utc_boundary(planned)

    products = db.execute(
        """SELECT id,name,category,units_per_case,stock,active,created_at
           FROM products WHERE category<>? ORDER BY name,id""",
        (SPORTS_MATERIAL_CATEGORY,),
    ).fetchall()
    product_ids = {int(row["id"]) for row in products}
    consumption = {product_id: {month: 0 for month in months} for product_id in product_ids}
    anomalies = []

    deliveries = db.execute(
        """SELECT sid.id delivery_id,si.product_id,sid.quantity,sid.delivered_at,
                  COALESCE(r.quantity,0) restored_quantity
           FROM sale_item_deliveries sid
           JOIN sale_items si ON si.id=sid.sale_item_id
           JOIN products p ON p.id=si.product_id
           LEFT JOIN sale_item_delivery_restorations r ON r.delivery_id=sid.id
           WHERE sid.delivered_at>=? AND sid.delivered_at<? AND p.category<>?
           ORDER BY sid.delivered_at,sid.id""",
        (start, end, SPORTS_MATERIAL_CATEGORY),
    ).fetchall()
    for row in deliveries:
        delivered = int(row["quantity"] or 0)
        restored = int(row["restored_quantity"] or 0)
        if restored > delivered:
            anomalies.append({
                "code": "restoration_exceeds_delivery",
                "delivery_id": int(row["delivery_id"]),
                "product_id": int(row["product_id"]),
            })
        effective = max(0, delivered - restored)
        key = _month_key(row["delivered_at"])
        product_id = int(row["product_id"])
        if product_id in consumption and key in consumption[product_id]:
            consumption[product_id][key] += effective

    legacy_rows = db.execute(
        """SELECT s.id sale_id,s.delivered_at,s.payment_status,
                  CASE WHEN sc.id IS NULL THEN 0 ELSE 1 END canceled,
                  si.id sale_item_id,si.product_id,si.quantity
           FROM sales s
           JOIN sale_items si ON si.sale_id=s.id
           JOIN products p ON p.id=si.product_id
           LEFT JOIN sale_cancellations sc ON sc.sale_id=s.id
           WHERE s.delivered_at>=? AND s.delivered_at<? AND p.category<>?
             AND NOT EXISTS (
                 SELECT 1 FROM sale_items all_items
                 JOIN sale_item_deliveries sid ON sid.sale_item_id=all_items.id
                 WHERE all_items.sale_id=s.id
             )
           ORDER BY s.delivered_at,s.id,si.id""",
        (start, end, SPORTS_MATERIAL_CATEGORY),
    ).fetchall()
    legacy_anomalies = {}
    for row in legacy_rows:
        sale_id = int(row["sale_id"])
        terminal = str(row["payment_status"] or "").strip().lower() in TERMINAL_PAYMENT_STATUSES
        invalid_quantity = int(row["quantity"] or 0) <= 0
        if bool(row["canceled"]) or terminal or invalid_quantity:
            if sale_id not in legacy_anomalies:
                legacy_anomalies[sale_id] = {
                    "code": "ambiguous_legacy_delivery",
                    "sale_id": sale_id,
                    "reason": "canceled_or_terminal" if bool(row["canceled"]) or terminal else "invalid_quantity",
                    "product_ids": [],
                }
            product_id = int(row["product_id"])
            if product_id not in legacy_anomalies[sale_id]["product_ids"]:
                legacy_anomalies[sale_id]["product_ids"].append(product_id)
            continue
        product_id = int(row["product_id"])
        key = _month_key(row["delivered_at"])
        if product_id in consumption and key in consumption[product_id]:
            consumption[product_id][key] += int(row["quantity"])
    anomalies.extend(legacy_anomalies.values())

    results = []
    for product in products:
        product_id = int(product["id"])
        monthly = consumption[product_id]
        values = [monthly[month] for month in months]
        total = sum(values)
        average = Decimal(total) / Decimal(history_months)
        safety_stock = average * margin / Decimal(100)
        gross_requirement = _ceil(average + safety_stock)
        stock = max(0, int(product["stock"] or 0))
        net_required = max(0, gross_requirement - stock)
        units_per_case = max(0, int(product["units_per_case"] or 0))
        active = bool(product["active"])
        suggested_quantity = _ceil(Decimal(net_required) / Decimal(units_per_case)) if units_per_case and net_required else net_required
        suggested_units = suggested_quantity * units_per_case if units_per_case else suggested_quantity
        if not active:
            suggested_quantity = 0
            suggested_units = 0

        created_at = _as_utc(product["created_at"]) if product["created_at"] else None
        history_started_at = _as_utc(start)
        partial_history = bool(created_at and created_at > history_started_at)
        insufficient = total == 0 or partial_history
        limitations = []
        if total == 0:
            limitations.append("no_consumption_history")
        if partial_history:
            limitations.append("partial_history")
        if not active:
            limitations.append("inactive_product")
        slope, direction = _trend(values) if not insufficient else (None, "insufficient_data")
        results.append({
            "product_id": product_id,
            "name": product["name"],
            "category": product["category"],
            "active": active,
            "monthly_consumption": [{"month": month, "quantity": monthly[month]} for month in months],
            "average_monthly_consumption": round(float(average), 4),
            "trend_units_per_month": slope,
            "trend_direction": direction,
            "forecast_units": round(float(average), 4),
            "safety_margin_percent": float(margin),
            "safety_stock_units": round(float(safety_stock), 4),
            "gross_requirement_units": gross_requirement,
            "current_stock_units": stock,
            "net_required_units": net_required,
            "units_per_case": units_per_case,
            "suggested_purchase_quantity": suggested_quantity,
            "purchase_unit": "cases" if units_per_case else "units",
            "suggested_purchase_units": suggested_units,
            "insufficient_history": insufficient,
            "limitations": limitations,
        })

    return {
        "planned_month": planned.strftime("%Y-%m"),
        "history_months": history_months,
        "history_period": {"start": months[0], "end": months[-1]},
        "safety_margin_percent": float(margin),
        "methodology": {
            "consumption_source": "physical_deliveries_net_of_restorations",
            "legacy_fallback": "sales_delivered_at_without_any_detailed_delivery",
            "forecast_base": "monthly_average_including_zero_months",
            "trend_applied": False,
            "stock_source": "products.stock",
            "pending_restock_deducted": False,
        },
        "limitations": ["historical_stockouts_cannot_be_detected_from_the_available_data"],
        "anomalies": anomalies,
        "products": results,
    }
