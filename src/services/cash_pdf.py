from datetime import date
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from src.utils import local_datetime, money


NAVY = colors.HexColor("#073B5C")
BLUE = colors.HexColor("#0D6E9E")
LIGHT_BLUE = colors.HexColor("#EAF4F8")
LIGHT_GRAY = colors.HexColor("#F4F6F7")


def _date_br(value):
    try:
        return date.fromisoformat(str(value)).strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return "-"


def _datetime_br(value):
    try:
        return local_datetime(value).strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return "-"


def _table(rows, widths, header_rows=1):
    table = Table(rows, colWidths=widths, repeatRows=header_rows, hAlign="LEFT")
    rules = [
        ("BACKGROUND", (0, 0), (-1, header_rows - 1), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, header_rows - 1), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CFD8DC")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for index in range(header_rows, len(rows)):
        if index % 2 == 0:
            rules.append(("BACKGROUND", (0, index), (-1, index), LIGHT_GRAY))
    table.setStyle(TableStyle(rules))
    return table


def build_cash_pdf(data, start_date, end_date, filter_text, account_labels, category_labels, issued_on):
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        leftMargin=13 * mm,
        rightMargin=13 * mm,
        topMargin=13 * mm,
        bottomMargin=17 * mm,
        title=f"Relatório de Caixa - {start_date} a {end_date}",
        author="PELADEIROS GPCTA",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="CashTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=19, leading=22, textColor=NAVY, alignment=TA_CENTER, spaceAfter=3))
    styles.add(ParagraphStyle(name="CashHeading", parent=styles["Heading2"], fontSize=12, leading=15, textColor=BLUE, spaceBefore=5, spaceAfter=5))
    styles.add(ParagraphStyle(name="CashSub", parent=styles["Normal"], fontSize=8.5, leading=11, textColor=colors.HexColor("#5E6B73"), alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="CashCell", parent=styles["Normal"], fontSize=7.2, leading=9, textColor=colors.HexColor("#183042")))
    styles.add(ParagraphStyle(name="CashCenter", parent=styles["CashCell"], alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="CashRight", parent=styles["CashCell"], alignment=TA_RIGHT))
    styles.add(ParagraphStyle(name="CashHeader", parent=styles["CashCenter"], fontName="Helvetica-Bold", textColor=colors.white))

    totals = data["totals"]
    story = [
        Paragraph("PELADEIROS GPCTA", styles["CashTitle"]),
        Paragraph("Relatório de Caixa", styles["CashHeading"]),
        Paragraph(
            f"Período: {_date_br(start_date)} a {_date_br(end_date)} - Emitido em {issued_on.strftime('%d/%m/%Y')}"
            + (f" - Filtros: {escape(filter_text)}" if filter_text else ""),
            styles["CashSub"],
        ),
        Spacer(1, 4 * mm),
    ]
    summary = Table(
        [
            [Paragraph(label, styles["CashCenter"]) for label in ("VENDAS", "OUTRAS ENTRADAS", "SAÍDAS", "MOVIMENTO LÍQUIDO")],
            [Paragraph(f"<b>{money(value)}</b>", styles["CashCenter"]) for value in (totals["sales"], totals["in"], totals["out"], totals["net"])],
        ],
        colWidths=[67.75 * mm] * 4,
    )
    summary.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), LIGHT_BLUE), ("BOX", (0, 0), (-1, -1), .5, colors.HexColor("#B9D5E1")), ("INNERGRID", (0, 0), (-1, -1), .5, colors.HexColor("#B9D5E1")), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story.extend([summary, Spacer(1, 5 * mm), Paragraph("Fechamentos", styles["CashHeading"])])

    session_rows = [[Paragraph(v, styles["CashHeader"]) for v in ("Data", "Status", "Dinheiro esperado", "Dinheiro conferido", "Conta esperada", "Conta conferida", "Diferença total", "Responsável")]]
    for row in data["sessions"]:
        pending = row["status"] == "closed" and row["counted_cash_cents"] is None
        difference = int(row["cash_difference_cents"] or 0) + int(row["bank_difference_cents"] or 0)
        session_rows.append([
            Paragraph(_date_br(row["business_date"]), styles["CashCenter"]), Paragraph("Aberto" if row["status"] == "open" else ("Pendente" if pending else "Fechado"), styles["CashCenter"]),
            Paragraph(money(row["expected_cash_cents"]) if row["status"] == "closed" else "Em andamento", styles["CashRight"]), Paragraph("Pendente" if pending else (money(row["counted_cash_cents"]) if row["status"] == "closed" else "-"), styles["CashRight"]),
            Paragraph(money(row["expected_bank_cents"]) if row["status"] == "closed" else "Em andamento", styles["CashRight"]), Paragraph("Pendente" if pending else (money(row["counted_bank_cents"]) if row["status"] == "closed" else "-"), styles["CashRight"]),
            Paragraph("Pendente" if pending else money(difference), styles["CashRight"]), Paragraph(escape(row["closed_by_name"] or row["opened_by_name"] or "-"), styles["CashCell"]),
        ])
    if len(session_rows) == 1:
        session_rows.append([Paragraph("Nenhum caixa no período.", styles["CashCenter"])] + [""] * 7)
    story.extend([_table(session_rows, [24*mm, 21*mm, 34*mm, 34*mm, 34*mm, 34*mm, 30*mm, 59*mm]), Spacer(1, 5*mm), Paragraph("Movimentações e transferências", styles["CashHeading"])])

    movement_rows = [[Paragraph(v, styles["CashHeader"]) for v in ("Data", "Conta", "Tipo", "Categoria", "Descrição", "Responsável", "Valor")]]
    for row in data["movements"]:
        movement_rows.append([
            Paragraph(_datetime_br(row["created_at"]), styles["CashCenter"]), Paragraph(account_labels.get(row["account"], row["account"]), styles["CashCell"]),
            Paragraph("Entrada" if row["direction"] == "in" else "Saída", styles["CashCenter"]), Paragraph(category_labels.get(row["category"], row["category"]), styles["CashCell"]),
            Paragraph(escape(row["description"]), styles["CashCell"]), Paragraph(escape(row["user_name"] or "Sistema"), styles["CashCell"]),
            Paragraph(("+ " if row["direction"] == "in" else "- ") + money(row["amount_cents"]), styles["CashRight"]),
        ])
    if len(movement_rows) == 1:
        movement_rows.append([Paragraph("Nenhuma movimentação encontrada.", styles["CashCenter"])] + [""] * 6)
    story.extend([_table(movement_rows, [32*mm, 31*mm, 20*mm, 34*mm, 78*mm, 43*mm, 33*mm]), Spacer(1, 5*mm), Paragraph("Vendas contabilizadas", styles["CashHeading"])])

    sale_rows = [[Paragraph(v, styles["CashHeader"]) for v in ("Data", "Pedido", "Peladeiro", "Pagamento", "Conta", "Valor")]]
    for row in data["sales"]:
        account = "cash" if row["payment_method"] == "Dinheiro" else "bank"
        sale_rows.append([
            Paragraph(_datetime_br(row["payment_date"]), styles["CashCenter"]), Paragraph(f"#{row['id']}", styles["CashCenter"]),
            Paragraph(escape(row["player_name"]), styles["CashCell"]), Paragraph(row["payment_method"], styles["CashCenter"]),
            Paragraph(account_labels[account], styles["CashCell"]), Paragraph(money(row["total_cents"]), styles["CashRight"]),
        ])
    if len(sale_rows) == 1:
        sale_rows.append([Paragraph("Nenhuma venda encontrada.", styles["CashCenter"])] + [""] * 5)
    story.append(_table(sale_rows, [38*mm, 25*mm, 91*mm, 34*mm, 45*mm, 38*mm]))

    def footer(canvas, doc):
        canvas.saveState(); canvas.setStrokeColor(colors.HexColor("#D9E1E5")); canvas.line(13*mm, 12*mm, landscape(A4)[0]-13*mm, 12*mm)
        canvas.setFont("Helvetica", 7.5); canvas.setFillColor(colors.HexColor("#6C757D")); canvas.drawString(13*mm, 8*mm, "PELADEIROS GPCTA - Controle de Caixa"); canvas.drawRightString(landscape(A4)[0]-13*mm, 8*mm, f"Página {doc.page}"); canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    output.seek(0)
    return output
