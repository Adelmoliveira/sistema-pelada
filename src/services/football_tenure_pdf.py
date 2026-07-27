from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def build_football_tenure_pdf(rows, issued_on=None):
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=15 * mm,
        title="Tempo de futebol",
        author="PELADEIROS GPCTA",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="TenureTitle", parent=styles["Title"], fontSize=18, textColor=colors.HexColor("#073B5C"), alignment=1))
    styles.add(ParagraphStyle(name="TenureSub", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#405563"), alignment=1))
    styles.add(ParagraphStyle(name="TenureCell", parent=styles["Normal"], fontSize=8, leading=10))
    styles.add(ParagraphStyle(name="TenureHead", parent=styles["TenureCell"], fontName="Helvetica-Bold", textColor=colors.white, alignment=1))

    issued = issued_on.strftime("%d/%m/%Y") if issued_on else ""
    story = [
        Paragraph("PELADEIROS GPCTA", styles["TenureTitle"]),
        Paragraph("Tempo de futebol", styles["TenureSub"]),
        Paragraph(f"Peladeiros ativos · Emitido em {escape(issued)}", styles["TenureSub"]),
        Spacer(1, 5 * mm),
    ]
    headers = ("#", "Peladeiro", "Posição", "Data de apresentação", "Tempo no grupo", "Condecorações")
    table_rows = [[Paragraph(value, styles["TenureHead"]) for value in headers]]
    for index, row in enumerate(rows, 1):
        name = row.get("war_name") or row.get("name") or ""
        if row.get("war_name") and row.get("name"):
            name = f"{name} ({row['name']})"
        medals = row.get("service_medals") or []
        decoration = ", ".join(medal.get("name", "") for medal in medals) if medals else "Nenhuma"
        table_rows.append([
            Paragraph(str(index), styles["TenureCell"]),
            Paragraph(escape(name), styles["TenureCell"]),
            Paragraph(escape(row.get("position_label", "Não definida")), styles["TenureCell"]),
            Paragraph(escape(row.get("join_date_label", "Não informada")), styles["TenureCell"]),
            Paragraph(escape(row.get("tenure_label", "Não informada")), styles["TenureCell"]),
            Paragraph(escape(decoration), styles["TenureCell"]),
        ])
    if len(table_rows) == 1:
        table_rows.append([Paragraph("Nenhum peladeiro apto cadastrado.", styles["TenureCell"])] + [""] * (len(headers) - 1))
    table = Table(table_rows, colWidths=[10 * mm, 65 * mm, 35 * mm, 42 * mm, 45 * mm, 70 * mm], repeatRows=1)
    rules = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#073B5C")),
        ("GRID", (0, 0), (-1, -1), .35, colors.HexColor("#CFD8DC")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    for index, row in enumerate(rows, 1):
        if not row.get("football_join_date"):
            rules.append(("BACKGROUND", (0, index), (-1, index), colors.HexColor("#FFF3CD")))
        elif index % 2 == 0:
            rules.append(("BACKGROUND", (0, index), (-1, index), colors.HexColor("#F4F6F7")))
    if len(table_rows) == 2:
        rules.append(("SPAN", (0, 1), (-1, 1)))
    table.setStyle(TableStyle(rules))
    story.append(table)
    document.build(story)
    output.seek(0)
    return output
