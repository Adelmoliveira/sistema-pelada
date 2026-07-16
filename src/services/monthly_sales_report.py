from datetime import date
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from src.utils import money, month_bounds


NAVY = colors.HexColor("#073B5C")
BLUE = colors.HexColor("#0D6E9E")
LIGHT_BLUE = colors.HexColor("#EAF4F8")
LIGHT_GRAY = colors.HexColor("#F4F6F7")
MONTH_NAMES = ("", "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho", "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro")


def monthly_sales_data(db, requested_month=None):
    month, start, end = month_bounds(requested_month)
    summary = db.execute(
        """SELECT
        COUNT(CASE WHEN payment_method<>'Cortesia' THEN 1 END) sales_count,
        COALESCE(SUM(CASE WHEN payment_method<>'Cortesia' THEN total_cents ELSE 0 END),0) revenue,
        COUNT(CASE WHEN payment_method='Cortesia' THEN 1 END) courtesy_sales
        FROM sales WHERE paid=1 AND COALESCE(paid_at,created_at)>=? AND COALESCE(paid_at,created_at)<?""",
        (start, end),
    ).fetchone()
    item_summary = db.execute(
        """SELECT
        COALESCE(SUM(CASE WHEN s.payment_method<>'Cortesia' THEN i.quantity ELSE 0 END),0) items_sold,
        COALESCE(SUM(CASE WHEN s.payment_method='Cortesia' THEN i.quantity ELSE 0 END),0) courtesy_items,
        COALESCE(SUM(CASE WHEN s.payment_method<>'Cortesia' THEN i.quantity*i.unit_cost_cents ELSE 0 END),0) cost,
        COALESCE(SUM(CASE WHEN s.payment_method<>'Cortesia' THEN i.quantity*(i.unit_price_cents-i.unit_cost_cents) ELSE 0 END),0) profit
        FROM sale_items i JOIN sales s ON s.id=i.sale_id
        WHERE s.paid=1 AND COALESCE(s.paid_at,s.created_at)>=? AND COALESCE(s.paid_at,s.created_at)<?""",
        (start, end),
    ).fetchone()
    payments = db.execute(
        """SELECT payment_method,COUNT(*) sales_count,COALESCE(SUM(total_cents),0) total
        FROM sales WHERE paid=1 AND payment_method<>'Cortesia'
        AND COALESCE(paid_at,created_at)>=? AND COALESCE(paid_at,created_at)<?
        GROUP BY payment_method ORDER BY sales_count DESC,total DESC""",
        (start, end),
    ).fetchall()
    products = db.execute(
        """SELECT p.name,
        COALESCE(SUM(CASE WHEN s.payment_method<>'Cortesia' THEN i.quantity ELSE 0 END),0) quantity,
        COALESCE(SUM(CASE WHEN s.payment_method='Cortesia' THEN i.quantity ELSE 0 END),0) courtesy_quantity,
        COALESCE(SUM(CASE WHEN s.payment_method<>'Cortesia' THEN i.quantity*i.unit_price_cents ELSE 0 END),0) revenue,
        COALESCE(SUM(CASE WHEN s.payment_method<>'Cortesia' THEN i.quantity*i.unit_cost_cents ELSE 0 END),0) cost,
        COALESCE(SUM(CASE WHEN s.payment_method<>'Cortesia' THEN i.quantity*(i.unit_price_cents-i.unit_cost_cents) ELSE 0 END),0) profit
        FROM sale_items i JOIN sales s ON s.id=i.sale_id JOIN products p ON p.id=i.product_id
        WHERE s.paid=1 AND COALESCE(s.paid_at,s.created_at)>=? AND COALESCE(s.paid_at,s.created_at)<?
        GROUP BY p.id,p.name ORDER BY quantity DESC,revenue DESC,p.name""",
        (start, end),
    ).fetchall()
    consumers = db.execute(
        """SELECT p.name,COUNT(s.id) purchases,COALESCE(SUM(s.total_cents),0) total,
        COALESCE(SUM((SELECT COALESCE(SUM(i.quantity),0) FROM sale_items i WHERE i.sale_id=s.id)),0) items
        FROM sales s JOIN players p ON p.id=s.player_id
        WHERE s.paid=1 AND s.payment_method<>'Cortesia'
        AND COALESCE(s.paid_at,s.created_at)>=? AND COALESCE(s.paid_at,s.created_at)<?
        GROUP BY p.id,p.name ORDER BY total DESC,purchases DESC,p.name""",
        (start, end),
    ).fetchall()
    daily = db.execute(
        """SELECT date(COALESCE(paid_at,created_at)) business_date,COUNT(*) sales_count,
        COALESCE(SUM(total_cents),0) revenue
        FROM sales WHERE paid=1 AND payment_method<>'Cortesia'
        AND COALESCE(paid_at,created_at)>=? AND COALESCE(paid_at,created_at)<?
        GROUP BY date(COALESCE(paid_at,created_at)) ORDER BY business_date""",
        (start, end),
    ).fetchall()
    sales_count = int(summary["sales_count"] or 0)
    revenue = int(summary["revenue"] or 0)
    return {
        "month": month,
        "start": start,
        "end": end,
        "summary": {
            "sales_count": sales_count,
            "revenue": revenue,
            "ticket_average": round(revenue / sales_count) if sales_count else 0,
            "items_sold": int(item_summary["items_sold"] or 0),
            "courtesy_sales": int(summary["courtesy_sales"] or 0),
            "courtesy_items": int(item_summary["courtesy_items"] or 0),
            "cost": int(item_summary["cost"] or 0),
            "profit": int(item_summary["profit"] or 0),
        },
        "payments": payments,
        "products": products,
        "consumers": consumers,
        "daily": daily,
        "most_used_payment": payments[0]["payment_method"] if payments else "Sem vendas",
    }


def _date_br(value):
    try:
        return date.fromisoformat(str(value)).strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return "-"


def _styled_table(rows, widths, empty_span=False):
    table = Table(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
    rules = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), .35, colors.HexColor("#CFD8DC")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for row_index in range(1, len(rows)):
        if row_index % 2 == 0:
            rules.append(("BACKGROUND", (0, row_index), (-1, row_index), LIGHT_GRAY))
    if empty_span:
        rules.append(("SPAN", (0, 1), (-1, 1)))
    table.setStyle(TableStyle(rules))
    return table


def build_monthly_sales_pdf(data, issued_on):
    output = BytesIO()
    month_date = date.fromisoformat(f"{data['month']}-01")
    month_label = f"{MONTH_NAMES[month_date.month]} de {month_date.year}"
    document = SimpleDocTemplate(
        output, pagesize=landscape(A4), leftMargin=13*mm, rightMargin=13*mm,
        topMargin=13*mm, bottomMargin=17*mm,
        title=f"Relatório Mensal de Vendas - {month_label}", author="PELADEIROS GPCTA",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="SalesTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=19, leading=22, textColor=NAVY, alignment=TA_CENTER, spaceAfter=3))
    styles.add(ParagraphStyle(name="SalesHeading", parent=styles["Heading2"], fontSize=12, leading=15, textColor=BLUE, spaceBefore=5, spaceAfter=5))
    styles.add(ParagraphStyle(name="SalesSub", parent=styles["Normal"], fontSize=8.5, leading=11, textColor=colors.HexColor("#5E6B73"), alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="SalesCell", parent=styles["Normal"], fontSize=7.5, leading=9.5, textColor=colors.HexColor("#183042")))
    styles.add(ParagraphStyle(name="SalesCenter", parent=styles["SalesCell"], alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="SalesRight", parent=styles["SalesCell"], alignment=TA_RIGHT))
    styles.add(ParagraphStyle(name="SalesHeader", parent=styles["SalesCenter"], fontName="Helvetica-Bold", textColor=colors.white))
    summary = data["summary"]
    story = [
        Paragraph("PELADEIROS GPCTA", styles["SalesTitle"]),
        Paragraph("Relatório Mensal de Vendas", styles["SalesHeading"]),
        Paragraph(f"Competência: {month_label} - Emitido em {issued_on.strftime('%d/%m/%Y')}", styles["SalesSub"]),
        Spacer(1, 4*mm),
    ]
    cards = Table([
        [Paragraph(label, styles["SalesCenter"]) for label in ("FATURAMENTO", "VENDAS", "ITENS VENDIDOS", "TICKET MÉDIO", "LUCRO ESTIMADO")],
        [Paragraph(f"<b>{value}</b>", styles["SalesCenter"]) for value in (money(summary["revenue"]), summary["sales_count"], summary["items_sold"], money(summary["ticket_average"]), money(summary["profit"]))],
    ], colWidths=[54.2*mm]*5)
    cards.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), LIGHT_BLUE), ("BOX", (0,0), (-1,-1), .5, colors.HexColor("#B9D5E1")), ("INNERGRID", (0,0), (-1,-1), .5, colors.HexColor("#B9D5E1")), ("TOPPADDING", (0,0), (-1,-1), 5), ("BOTTOMPADDING", (0,0), (-1,-1), 5)]))
    story.extend([
        cards, Spacer(1, 3*mm),
        Paragraph(f"Cortesias: <b>{summary['courtesy_sales']} registro(s), {summary['courtesy_items']} item(ns)</b> - Custo estimado das vendas: <b>{money(summary['cost'])}</b>", styles["SalesSub"]),
        Spacer(1, 3*mm), Paragraph(f"Forma de pagamento mais utilizada: <b>{escape(data['most_used_payment'])}</b>", styles["SalesHeading"]), Paragraph("Formas de pagamento", styles["SalesHeading"]),
    ])

    payment_rows = [[Paragraph(v, styles["SalesHeader"]) for v in ("Forma", "Quantidade de vendas", "% das vendas", "Valor recebido", "% do faturamento")]]
    for row in data["payments"]:
        sales_share = (int(row["sales_count"]) / summary["sales_count"] * 100) if summary["sales_count"] else 0
        revenue_share = (int(row["total"]) / summary["revenue"] * 100) if summary["revenue"] else 0
        payment_rows.append([Paragraph(row["payment_method"], styles["SalesCell"]), Paragraph(str(row["sales_count"]), styles["SalesCenter"]), Paragraph(f"{sales_share:.1f}%", styles["SalesCenter"]), Paragraph(money(row["total"]), styles["SalesRight"]), Paragraph(f"{revenue_share:.1f}%", styles["SalesCenter"])])
    payment_empty = len(payment_rows) == 1
    if payment_empty:
        payment_rows.append([Paragraph("Nenhuma venda paga no mês.", styles["SalesCenter"])] + [""]*4)
    story.extend([_styled_table(payment_rows, [51*mm,55*mm,45*mm,65*mm,55*mm], payment_empty), Spacer(1,4*mm), Paragraph("Ranking de quem mais consumiu", styles["SalesHeading"])])

    consumer_rows = [[Paragraph(v, styles["SalesHeader"]) for v in ("Posição", "Peladeiro", "Compras", "Itens", "Valor consumido", "Ticket médio")]]
    for position, row in enumerate(data["consumers"], 1):
        ticket = round(int(row["total"] or 0) / int(row["purchases"])) if row["purchases"] else 0
        consumer_rows.append([
            Paragraph(f"{position}º", styles["SalesCenter"]), Paragraph(escape(row["name"]), styles["SalesCell"]),
            Paragraph(str(row["purchases"]), styles["SalesCenter"]), Paragraph(str(row["items"]), styles["SalesCenter"]),
            Paragraph(money(row["total"]), styles["SalesRight"]), Paragraph(money(ticket), styles["SalesRight"]),
        ])
    consumer_empty = len(consumer_rows) == 1
    if consumer_empty:
        consumer_rows.append([Paragraph("Nenhum consumo registrado no mês.", styles["SalesCenter"])] + [""]*5)
    story.extend([_styled_table(consumer_rows, [22*mm,89*mm,35*mm,31*mm,49*mm,45*mm], consumer_empty), Spacer(1,4*mm), Paragraph("Produtos vendidos", styles["SalesHeading"])])

    product_rows = [[Paragraph(v, styles["SalesHeader"]) for v in ("Produto", "Unidades", "Cortesias", "Faturamento", "Custo estimado", "Lucro estimado")]]
    for row in data["products"]:
        product_rows.append([Paragraph(escape(row["name"]), styles["SalesCell"]), Paragraph(str(row["quantity"]), styles["SalesCenter"]), Paragraph(str(row["courtesy_quantity"]), styles["SalesCenter"]), Paragraph(money(row["revenue"]), styles["SalesRight"]), Paragraph(money(row["cost"]), styles["SalesRight"]), Paragraph(money(row["profit"]), styles["SalesRight"])])
    product_empty = len(product_rows) == 1
    if product_empty:
        product_rows.append([Paragraph("Nenhum produto vendido no mês.", styles["SalesCenter"])] + [""]*5)
    story.extend([_styled_table(product_rows, [91*mm,32*mm,32*mm,39*mm,39*mm,39*mm], product_empty), Spacer(1,4*mm), Paragraph("Vendas por dia", styles["SalesHeading"])])

    daily_rows = [[Paragraph(v, styles["SalesHeader"]) for v in ("Data", "Quantidade de vendas", "Faturamento")]]
    for row in data["daily"]:
        daily_rows.append([Paragraph(_date_br(row["business_date"]), styles["SalesCenter"]), Paragraph(str(row["sales_count"]), styles["SalesCenter"]), Paragraph(money(row["revenue"]), styles["SalesRight"])])
    daily_empty = len(daily_rows) == 1
    if daily_empty:
        daily_rows.append([Paragraph("Nenhuma venda no mês.", styles["SalesCenter"]), "", ""])
    story.append(_styled_table(daily_rows, [75*mm,95*mm,101*mm], daily_empty))

    def footer(canvas, doc):
        canvas.saveState(); canvas.setStrokeColor(colors.HexColor("#D9E1E5")); canvas.line(13*mm,12*mm,landscape(A4)[0]-13*mm,12*mm)
        canvas.setFont("Helvetica",7.5); canvas.setFillColor(colors.HexColor("#6C757D")); canvas.drawString(13*mm,8*mm,"PELADEIROS GPCTA - Relatório Mensal de Vendas"); canvas.drawRightString(landscape(A4)[0]-13*mm,8*mm,f"Página {doc.page}"); canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    output.seek(0)
    return output
