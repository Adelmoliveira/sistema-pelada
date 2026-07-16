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


def _table(rows, widths):
    table = Table(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
    rules = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CFD8DC")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for index in range(2, len(rows), 2):
        rules.append(("BACKGROUND", (0, index), (-1, index), LIGHT_GRAY))
    table.setStyle(TableStyle(rules))
    return table


def build_finance_ledger_pdf(
    ledger, finance, bar, start_date, end_date, filter_text,
    account_labels, category_labels, issued_on,
):
    output = BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=landscape(A4), leftMargin=13 * mm, rightMargin=13 * mm,
        topMargin=13 * mm, bottomMargin=17 * mm,
        title=f"Livro-caixa Financeiro - {start_date} a {end_date}",
        author="PELADEIROS GPCTA",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="LedgerTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=19, leading=22, textColor=NAVY, alignment=TA_CENTER, spaceAfter=3))
    styles.add(ParagraphStyle(name="LedgerHeading", parent=styles["Heading2"], fontSize=12, leading=15, textColor=BLUE, spaceBefore=5, spaceAfter=5))
    styles.add(ParagraphStyle(name="LedgerSub", parent=styles["Normal"], fontSize=8.5, leading=11, textColor=colors.HexColor("#5E6B73"), alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="LedgerCell", parent=styles["Normal"], fontSize=7.2, leading=9, textColor=colors.HexColor("#183042")))
    styles.add(ParagraphStyle(name="LedgerCenter", parent=styles["LedgerCell"], alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="LedgerRight", parent=styles["LedgerCell"], alignment=TA_RIGHT))
    styles.add(ParagraphStyle(name="LedgerHeader", parent=styles["LedgerCenter"], fontName="Helvetica-Bold", textColor=colors.white))

    consolidated_bank = finance["bank"] + bar["bank"]
    balances = (
        ("BANCO FINANCEIRO", finance["bank"]), ("DINHEIRO FINANCEIRO", finance["cash"]),
        ("BANCO DO BAR", bar["bank"]), ("DINHEIRO DO BAR", bar["cash"]),
        ("BANCO CONSOLIDADO", consolidated_bank),
    )
    story = [
        Paragraph("PELADEIROS GPCTA", styles["LedgerTitle"]),
        Paragraph("Livro-caixa Financeiro", styles["LedgerHeading"]),
        Paragraph(
            f"Período: {_date_br(start_date)} a {_date_br(end_date)} - Emitido em {issued_on.strftime('%d/%m/%Y')}"
            + (f" - Filtros: {escape(filter_text)}" if filter_text else ""),
            styles["LedgerSub"],
        ),
        Spacer(1, 4 * mm),
    ]
    balance_table = Table(
        [[Paragraph(label, styles["LedgerCenter"]) for label, _ in balances],
         [Paragraph(f"<b>{money(value)}</b>", styles["LedgerCenter"]) for _, value in balances]],
        colWidths=[54.2 * mm] * 5,
    )
    balance_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), LIGHT_BLUE), ("BOX", (0, 0), (-1, -1), .5, colors.HexColor("#B9D5E1")), ("INNERGRID", (0, 0), (-1, -1), .5, colors.HexColor("#B9D5E1")), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    totals = ledger["totals"]
    period_table = Table(
        [[Paragraph(label, styles["LedgerCenter"]) for label in ("ENTRADAS NO PERÍODO", "SAÍDAS NO PERÍODO", "MOVIMENTO LÍQUIDO")],
         [Paragraph(f"<b>{money(value)}</b>", styles["LedgerCenter"]) for value in (totals["in"], totals["out"], totals["net"])]],
        colWidths=[90.33 * mm] * 3,
    )
    period_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EDF6ED")), ("BOX", (0, 0), (-1, -1), .5, colors.HexColor("#C7DDC7")), ("INNERGRID", (0, 0), (-1, -1), .5, colors.HexColor("#C7DDC7")), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story.extend([balance_table, Spacer(1, 3 * mm), period_table, Spacer(1, 5 * mm), Paragraph("Movimentações financeiras", styles["LedgerHeading"])])

    rows = [[Paragraph(value, styles["LedgerHeader"]) for value in ("Data", "Conta", "Tipo", "Categoria", "Descrição", "Responsável", "Valor")]]
    for row in ledger["movements"]:
        rows.append([
            Paragraph(_datetime_br(row["created_at"]), styles["LedgerCenter"]),
            Paragraph(account_labels.get(row["account"], row["account"]), styles["LedgerCell"]),
            Paragraph("Entrada" if row["direction"] == "in" else "Saída", styles["LedgerCenter"]),
            Paragraph(category_labels.get(row["category"], row["category"]), styles["LedgerCell"]),
            Paragraph(escape(row["description"]), styles["LedgerCell"]),
            Paragraph(escape(row["user_name"] or "Sistema"), styles["LedgerCell"]),
            Paragraph(("+ " if row["direction"] == "in" else "- ") + money(row["amount_cents"]), styles["LedgerRight"]),
        ])
    if len(rows) == 1:
        rows.append([Paragraph("Nenhuma movimentação encontrada no período.", styles["LedgerCenter"])] + [""] * 6)
    story.append(_table(rows, [32 * mm, 38 * mm, 22 * mm, 38 * mm, 70 * mm, 42 * mm, 29 * mm]))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#D9E1E5")); canvas.line(13 * mm, 12 * mm, landscape(A4)[0] - 13 * mm, 12 * mm)
        canvas.setFont("Helvetica", 7.5); canvas.setFillColor(colors.HexColor("#6C757D"))
        canvas.drawString(13 * mm, 8 * mm, "PELADEIROS GPCTA - Livro-caixa Financeiro")
        canvas.drawRightString(landscape(A4)[0] - 13 * mm, 8 * mm, f"Página {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    output.seek(0)
    return output
