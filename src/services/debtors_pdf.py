from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from src.utils import money


NAVY = colors.HexColor("#073B5C")
BLUE = colors.HexColor("#0D6E9E")
LIGHT_BLUE = colors.HexColor("#EAF4F8")
LIGHT_GRAY = colors.HexColor("#F4F6F7")
TEXT = colors.HexColor("#183042")


def build_debtors_pdf(debtors, today, monthly_fee=1500):
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=16 * mm,
        bottomMargin=17 * mm,
        title=f"Relatório de devedores - {today.strftime('%m/%Y')}",
        author="PELADEIROS GPCTA",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="ReportTitle", parent=styles["Title"], fontName="Helvetica-Bold",
        fontSize=19, leading=23, textColor=NAVY, alignment=TA_CENTER, spaceAfter=4,
    ))
    styles.add(ParagraphStyle(
        name="ReportSubtitle", parent=styles["Normal"], fontSize=9.5, leading=13,
        textColor=colors.HexColor("#5E6B73"), alignment=TA_CENTER,
    ))
    styles.add(ParagraphStyle(
        name="ReportHeading", parent=styles["Heading2"], fontSize=13, leading=16,
        textColor=BLUE, alignment=TA_CENTER, spaceAfter=3,
    ))
    styles.add(ParagraphStyle(
        name="Cell", parent=styles["Normal"], fontSize=8.5, leading=11, textColor=TEXT,
    ))
    styles.add(ParagraphStyle(
        name="CellRight", parent=styles["Cell"], alignment=2,
    ))
    styles.add(ParagraphStyle(
        name="CellCenter", parent=styles["Cell"], alignment=TA_CENTER,
    ))
    styles.add(ParagraphStyle(
        name="HeaderCell", parent=styles["CellCenter"], fontName="Helvetica-Bold",
        textColor=colors.white,
    ))
    styles.add(ParagraphStyle(
        name="MissingEmail", parent=styles["Cell"], fontName="Helvetica-Bold",
        textColor=colors.HexColor("#B02A37"),
    ))

    total_due = sum(debtor["amount_cents"] for debtor in debtors)
    without_email = sum(not debtor["email"] for debtor in debtors)
    story = [
        Paragraph("PELADEIROS GPCTA", styles["ReportTitle"]),
        Paragraph("Relatório de mensalidades pendentes", styles["ReportHeading"]),
        Paragraph(
            f"Posição em {today.strftime('%d/%m/%Y')} - mensalidade de {money(monthly_fee)}",
            styles["ReportSubtitle"],
        ),
        Spacer(1, 6 * mm),
    ]

    summary = Table([
        [Paragraph("PELADEIROS PENDENTES", styles["CellCenter"]),
         Paragraph("VALOR TOTAL PENDENTE", styles["CellCenter"]),
         Paragraph("SEM E-MAIL CADASTRADO", styles["CellCenter"])],
        [Paragraph(f"<b>{len(debtors)}</b>", styles["CellCenter"]),
         Paragraph(f"<b>{money(total_due)}</b>", styles["CellCenter"]),
         Paragraph(f"<b>{without_email}</b>", styles["CellCenter"])],
    ], colWidths=[85 * mm, 85 * mm, 85 * mm])
    summary.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), LIGHT_BLUE),
        ("TEXTCOLOR", (0, 0), (-1, 0), BLUE),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#B9D5E1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#B9D5E1")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.extend([summary, Spacer(1, 6 * mm)])

    header = ["#", "Peladeiro", "E-mail", "Meses pendentes", "Qtd.", "Total"]
    rows = [[Paragraph(value, styles["HeaderCell"]) for value in header]]
    for position, debtor in enumerate(debtors, 1):
        email = debtor["email"] or "Não cadastrado"
        rows.append([
            Paragraph(str(position), styles["CellCenter"]),
            Paragraph(escape(debtor["name"]), styles["Cell"]),
            Paragraph(escape(email), styles["Cell"] if debtor["email"] else styles["MissingEmail"]),
            Paragraph(escape(debtor["missing_month_names"]), styles["Cell"]),
            Paragraph(str(len(debtor["missing_months"])), styles["CellCenter"]),
            Paragraph(f"<b>{money(debtor['amount_cents'])}</b>", styles["CellRight"]),
        ])
    if not debtors:
        rows.append([Paragraph("Nenhuma mensalidade pendente na data deste relatório.", styles["CellCenter"]), "", "", "", "", ""])

    table = Table(rows, colWidths=[10 * mm, 48 * mm, 62 * mm, 92 * mm, 14 * mm, 29 * mm], repeatRows=1)
    table_style = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CFD8DC")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]
    for row_number in range(1, len(rows)):
        if row_number % 2 == 0:
            table_style.append(("BACKGROUND", (0, row_number), (-1, row_number), LIGHT_GRAY))
    if not debtors:
        table_style.append(("SPAN", (0, 1), (-1, 1)))
    table.setStyle(TableStyle(table_style))
    story.append(table)

    def draw_footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#D9E1E5"))
        canvas.line(15 * mm, 12 * mm, landscape(A4)[0] - 15 * mm, 12 * mm)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#6C757D"))
        canvas.drawString(15 * mm, 8 * mm, "Documento gerado pelo PELADEIROS GPCTA")
        canvas.drawRightString(landscape(A4)[0] - 15 * mm, 8 * mm, f"Página {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    output.seek(0)
    return output
