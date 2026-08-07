from collections import defaultdict
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


NAVY = colors.HexColor("#073B5C")
BLUE = colors.HexColor("#0D6E9E")
LIGHT_BLUE = colors.HexColor("#EAF4F8")
LIGHT_GRAY = colors.HexColor("#F4F6F7")
TEXT = colors.HexColor("#183042")


def build_load_quantity_pdf(entries, today, query=""):
    """Build a compact, consolidated report with one row per material.

    Each load entry represents one physical unit, so the quantity is the number
    of matching entries after the relation filters have been applied.
    """
    grouped = defaultdict(lambda: {"fcg": "", "quantity": 0, "areas": set()})
    for entry in entries:
        description = entry["material_description"] or "Material sem descrição"
        item = grouped[description]
        item["fcg"] = entry["material_fcg"] or item["fcg"]
        item["quantity"] += 1
        if entry["area_code"]:
            item["areas"].add(entry["area_code"])

    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=17 * mm,
        title=f"Quantidade por material - {today.strftime('%d/%m/%Y')}",
        author="PELADEIROS GPCTA",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="QuantityTitle", parent=styles["Title"], fontName="Helvetica-Bold",
        fontSize=19, leading=23, textColor=NAVY, alignment=TA_CENTER, spaceAfter=4,
    ))
    styles.add(ParagraphStyle(
        name="QuantitySubtitle", parent=styles["Normal"], fontSize=9.5, leading=13,
        textColor=colors.HexColor("#5E6B73"), alignment=TA_CENTER,
    ))
    styles.add(ParagraphStyle(
        name="QuantityCell", parent=styles["Normal"], fontSize=9, leading=11,
        textColor=TEXT,
    ))
    styles.add(ParagraphStyle(
        name="QuantityCenter", parent=styles["QuantityCell"], alignment=TA_CENTER,
    ))
    styles.add(ParagraphStyle(
        name="QuantityHeader", parent=styles["QuantityCenter"], fontName="Helvetica-Bold",
        textColor=colors.white,
    ))

    total_units = sum(item["quantity"] for item in grouped.values())
    story = [
        Paragraph("PELADEIROS GPCTA", styles["QuantityTitle"]),
        Paragraph("Relação de Carga — Quantidade por material", styles["QuantityTitle"]),
        Paragraph(
            f"Emitido em {today.strftime('%d/%m/%Y')} · {len(grouped)} material(is) · "
            f"{total_units} unidade(s)" + (f" · Filtro: {escape(query)}" if query else ""),
            styles["QuantitySubtitle"],
        ),
        Spacer(1, 7 * mm),
    ]
    headers = ["Material", "FCG", "Quantidade", "Áreas"]
    rows = [[Paragraph(header, styles["QuantityHeader"]) for header in headers]]
    for description in sorted(grouped, key=str.casefold):
        item = grouped[description]
        rows.append([
            Paragraph(escape(description), styles["QuantityCell"]),
            Paragraph(escape(item["fcg"] or "—"), styles["QuantityCenter"]),
            Paragraph(f"<b>{item['quantity']}</b>", styles["QuantityCenter"]),
            Paragraph(escape(", ".join(sorted(item["areas"])) or "—"), styles["QuantityCenter"]),
        ])
    if not grouped:
        rows.append([Paragraph("Nenhuma carga encontrada.", styles["QuantityCenter"]), "", "", ""])

    table = Table(rows, colWidths=[112 * mm, 35 * mm, 35 * mm, 55 * mm], repeatRows=1)
    table_style = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CFD8DC")),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]
    for row_number in range(1, len(rows)):
        if row_number % 2 == 0:
            table_style.append(("BACKGROUND", (0, row_number), (-1, row_number), LIGHT_GRAY))
    if not grouped:
        table_style.append(("SPAN", (0, 1), (-1, 1)))
    table.setStyle(TableStyle(table_style))
    story.append(table)

    def draw_footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#68757C"))
        canvas.drawCentredString(
            landscape(A4)[0] / 2,
            9 * mm,
            f"PELADEIROS GPCTA · Página {doc.page}",
        )
        canvas.restoreState()

    document.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    output.seek(0)
    return output
