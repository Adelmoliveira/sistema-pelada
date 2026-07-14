from io import BytesIO
from datetime import date
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


def _date_br(value):
    try:
        return date.fromisoformat(str(value)).strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return "-"


def build_maintenance_pdf(entries, today, filters, categories, priorities, statuses):
    output = BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=landscape(A4), leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=15 * mm, bottomMargin=17 * mm,
        title=f"Relatório de Manutenção - {today.strftime('%d/%m/%Y')}",
        author="PELADEIROS GPCTA",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="MaintTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=19, leading=22, textColor=NAVY, alignment=TA_CENTER, spaceAfter=4))
    styles.add(ParagraphStyle(name="MaintHeading", parent=styles["Heading2"], fontSize=13, leading=16, textColor=BLUE, alignment=TA_CENTER, spaceAfter=3))
    styles.add(ParagraphStyle(name="MaintSub", parent=styles["Normal"], fontSize=9, leading=12, textColor=colors.HexColor("#5E6B73"), alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="MaintCell", parent=styles["Normal"], fontSize=7.5, leading=9.5, textColor=colors.HexColor("#183042")))
    styles.add(ParagraphStyle(name="MaintCenter", parent=styles["MaintCell"], alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="MaintHeader", parent=styles["MaintCenter"], fontName="Helvetica-Bold", textColor=colors.white))

    open_count = sum(entry["status"] != "completed" for entry in entries)
    urgent_count = sum(entry["priority"] == "urgent" and entry["status"] != "completed" for entry in entries)
    total_cost = sum(int(entry["cost_cents"] or 0) for entry in entries)
    story = [
        Paragraph("PELADEIROS GPCTA", styles["MaintTitle"]),
        Paragraph("Relatório de Manutenção Predial", styles["MaintHeading"]),
        Paragraph(
            f"Emitido em {today.strftime('%d/%m/%Y')}" + (f" - Filtros: {escape(filters)}" if filters else ""),
            styles["MaintSub"],
        ), Spacer(1, 5 * mm),
    ]
    summary = Table([
        [Paragraph("CHAMADOS", styles["MaintCenter"]), Paragraph("EM ABERTO", styles["MaintCenter"]), Paragraph("URGENTES", styles["MaintCenter"]), Paragraph("CUSTO REGISTRADO", styles["MaintCenter"])],
        [Paragraph(f"<b>{len(entries)}</b>", styles["MaintCenter"]), Paragraph(f"<b>{open_count}</b>", styles["MaintCenter"]), Paragraph(f"<b>{urgent_count}</b>", styles["MaintCenter"]), Paragraph(f"<b>{money(total_cost)}</b>", styles["MaintCenter"])],
    ], colWidths=[63.75 * mm] * 4)
    summary.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), LIGHT_BLUE), ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#B9D5E1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#B9D5E1")),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.extend([summary, Spacer(1, 5 * mm)])

    headers = ["Código", "Chamado", "Área", "Tipo", "Prioridade", "Status", "Responsável", "Previsão", "Custo"]
    rows = [[Paragraph(header, styles["MaintHeader"]) for header in headers]]
    for entry in entries:
        rows.append([
            Paragraph(escape(entry["code"]), styles["MaintCenter"]),
            Paragraph(escape(entry["title"]), styles["MaintCell"]),
            Paragraph(escape(entry["area_code"]), styles["MaintCenter"]),
            Paragraph(escape(categories.get(entry["category"], entry["category"])), styles["MaintCell"]),
            Paragraph(escape(priorities.get(entry["priority"], entry["priority"])), styles["MaintCenter"]),
            Paragraph(escape(statuses.get(entry["status"], entry["status"])), styles["MaintCell"]),
            Paragraph(escape(entry["responsible"] or "-"), styles["MaintCell"]),
            Paragraph(_date_br(entry["due_on"]), styles["MaintCenter"]),
            Paragraph(escape(money(entry["cost_cents"])), styles["MaintCenter"]),
        ])
    if not entries:
        rows.append([Paragraph("Nenhum chamado encontrado.", styles["MaintCenter"])] + [""] * 8)
    table = Table(rows, colWidths=[24 * mm, 55 * mm, 14 * mm, 25 * mm, 24 * mm, 29 * mm, 32 * mm, 25 * mm, 27 * mm], repeatRows=1)
    table_style = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CFD8DC")),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    for row_number in range(1, len(rows)):
        if row_number % 2 == 0:
            table_style.append(("BACKGROUND", (0, row_number), (-1, row_number), LIGHT_GRAY))
    if not entries:
        table_style.append(("SPAN", (0, 1), (-1, 1)))
    table.setStyle(TableStyle(table_style))
    story.append(table)

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#D9E1E5"))
        canvas.line(15 * mm, 12 * mm, landscape(A4)[0] - 15 * mm, 12 * mm)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#6C757D"))
        canvas.drawString(15 * mm, 8 * mm, "PELADEIROS GPCTA - Manutenção Predial")
        canvas.drawRightString(landscape(A4)[0] - 15 * mm, 8 * mm, f"Página {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    output.seek(0)
    return output
