from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from src.utils import money

NAVY = colors.HexColor("#073B5C")


def _pdf(title, subtitle, headers, rows):
    output = BytesIO()
    document = SimpleDocTemplate(output, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=15 * mm, title=title, author="PELADEIROS GPCTA")
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="ReportTitle", parent=styles["Title"], fontSize=18, textColor=NAVY, alignment=1))
    styles.add(ParagraphStyle(name="ReportSub", parent=styles["Normal"], fontSize=9, textColor=NAVY, alignment=1))
    styles.add(ParagraphStyle(name="ReportCell", parent=styles["Normal"], fontSize=8, leading=10))
    styles.add(ParagraphStyle(name="ReportHead", parent=styles["ReportCell"], fontName="Helvetica-Bold", textColor=colors.white, alignment=1))
    story = [Paragraph("PELADEIROS GPCTA", styles["ReportTitle"]), Paragraph(escape(title), styles["ReportSub"]), Paragraph(escape(subtitle), styles["ReportSub"]), Spacer(1, 5 * mm)]
    table_rows = [[Paragraph(escape(str(header)), styles["ReportHead"]) for header in headers]]
    for row in rows:
        table_rows.append([Paragraph(escape(str(value if value not in (None, "") else "—")), styles["ReportCell"]) for value in row])
    if len(table_rows) == 1:
        table_rows.append([Paragraph("Nenhum registro encontrado.", styles["ReportCell"])] + [""] * (len(headers) - 1))
    table = Table(table_rows, repeatRows=1, hAlign="LEFT")
    rules = [("BACKGROUND", (0, 0), (-1, 0), NAVY), ("GRID", (0, 0), (-1, -1), .35, colors.HexColor("#CFD8DC")), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]
    for index in range(2, len(table_rows), 2):
        rules.append(("BACKGROUND", (0, index), (-1, index), colors.HexColor("#F4F6F7")))
    if len(table_rows) == 2:
        rules.append(("SPAN", (0, 1), (-1, 1)))
    table.setStyle(TableStyle(rules))
    story.append(table)
    document.build(story)
    output.seek(0)
    return output


def build_membership_report_pdf(rows, summary, period):
    data = [(row["player_name"], row["created_at"], row["months_count"], row["payment_method"], money(row["amount_cents"]), row["notes"]) for row in rows]
    subtitle = f"Período: {period} · Recebido: {money(summary['received'])} · Registros: {summary['count']} · Isentos: {summary['exempt']}"
    return _pdf("Relatório de mensalidades", subtitle, ("Peladeiro", "Data", "Meses", "Forma de pagamento", "Valor", "Observação"), data)


def build_sale_detail_pdf(sale, items):
    data = [(item["product_name"], item["quantity"], money(item["unit_price_cents"]), money(item["quantity"] * item["unit_price_cents"])) for item in items]
    subtitle = f"Pedido #{sale['id']} · {sale['player_name']} · {sale['sale_date']} · Pagamento: {sale['payment_method']} · Total: {money(sale['total_cents'])}"
    return _pdf("Detalhe da venda", subtitle, ("Produto", "Quantidade", "Preço unitário", "Total"), data)
