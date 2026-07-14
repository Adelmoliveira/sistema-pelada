from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from src.utils import brdate


NAVY = colors.HexColor("#073B5C")
BLUE = colors.HexColor("#0D6E9E")
LIGHT_BLUE = colors.HexColor("#EAF4F8")
LIGHT_GRAY = colors.HexColor("#F4F6F7")
TEXT = colors.HexColor("#183042")


def build_load_relation_pdf(entries, today, query=""):
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=16 * mm,
        bottomMargin=17 * mm,
        title=f"Relação de Carga - {today.strftime('%d/%m/%Y')}",
        author="PELADEIROS GPCTA",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="LoadTitle", parent=styles["Title"], fontName="Helvetica-Bold",
        fontSize=19, leading=23, textColor=NAVY, alignment=TA_CENTER, spaceAfter=4,
    ))
    styles.add(ParagraphStyle(
        name="LoadHeading", parent=styles["Heading2"], fontSize=13, leading=16,
        textColor=BLUE, alignment=TA_CENTER, spaceAfter=3,
    ))
    styles.add(ParagraphStyle(
        name="LoadSubtitle", parent=styles["Normal"], fontSize=9.5, leading=13,
        textColor=colors.HexColor("#5E6B73"), alignment=TA_CENTER,
    ))
    styles.add(ParagraphStyle(
        name="LoadCell", parent=styles["Normal"], fontSize=7.8, leading=10,
        textColor=TEXT,
    ))
    styles.add(ParagraphStyle(
        name="LoadCenter", parent=styles["LoadCell"], alignment=TA_CENTER,
    ))
    styles.add(ParagraphStyle(
        name="LoadHeader", parent=styles["LoadCenter"], fontName="Helvetica-Bold",
        textColor=colors.white,
    ))

    total_photos = sum(int(entry["photo_count"] or 0) for entry in entries)
    story = [
        Paragraph("PELADEIROS GPCTA", styles["LoadTitle"]),
        Paragraph("Relação de Carga", styles["LoadHeading"]),
        Paragraph(
            f"Emitido em {today.strftime('%d/%m/%Y')}" + (f" - Filtro: {escape(query)}" if query else ""),
            styles["LoadSubtitle"],
        ),
        Spacer(1, 6 * mm),
    ]
    summary = Table([
        [Paragraph("ITENS CADASTRADOS", styles["LoadCenter"]), Paragraph("FOTOS VINCULADAS", styles["LoadCenter"])],
        [Paragraph(f"<b>{len(entries)}</b>", styles["LoadCenter"]), Paragraph(f"<b>{total_photos}</b>", styles["LoadCenter"])],
    ], colWidths=[127.5 * mm, 127.5 * mm])
    summary.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), LIGHT_BLUE),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#B9D5E1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#B9D5E1")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.extend([summary, Spacer(1, 6 * mm)])

    headers = ["BMP", "Material", "Nº de série", "Localização", "Observações", "Cadastro"]
    rows = [[Paragraph(header, styles["LoadHeader"]) for header in headers]]
    for entry in entries:
        rows.append([
            Paragraph(escape(entry["bmp"]), styles["LoadCenter"]),
            Paragraph(escape(entry["material_description"]), styles["LoadCell"]),
            Paragraph(escape(entry["serial_number"] or "-"), styles["LoadCell"]),
            Paragraph(escape(entry["location"] or "-"), styles["LoadCell"]),
            Paragraph(escape(entry["notes"] or "-"), styles["LoadCell"]),
            Paragraph(escape(brdate(entry["created_at"]).split(" ")[0]), styles["LoadCenter"]),
        ])
    if not entries:
        rows.append([Paragraph("Nenhuma carga encontrada.", styles["LoadCenter"]), "", "", "", "", ""])

    table = Table(
        rows,
        colWidths=[33 * mm, 79 * mm, 35 * mm, 34 * mm, 48 * mm, 26 * mm],
        repeatRows=1,
    )
    table_style = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CFD8DC")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]
    for row_number in range(1, len(rows)):
        if row_number % 2 == 0:
            table_style.append(("BACKGROUND", (0, row_number), (-1, row_number), LIGHT_GRAY))
    if not entries:
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
