from datetime import datetime
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def build_sports_supplier_pdf(items, generated_at=None):
    """Build the read-only list of pending sports requests for the supplier."""
    generated_at = generated_at or datetime.now()
    output = BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=landscape(A4), leftMargin=10 * mm, rightMargin=10 * mm,
        topMargin=10 * mm, bottomMargin=12 * mm,
        title="Encomendas de Material Esportivo", author="PELADEIROS GPCTA",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="SupplierCell", parent=styles["Normal"], fontSize=8, leading=10))
    styles.add(ParagraphStyle(name="SupplierHead", parent=styles["SupplierCell"], fontName="Helvetica-Bold", textColor=colors.white, alignment=1))
    total = sum(int(item["quantity"] or 0) for item in items)
    story = [
        Paragraph("PELADEIROS GPCTA", styles["Title"]),
        Paragraph("Encomendas de Material Esportivo para o fornecedor", styles["Heading2"]),
        Paragraph(f"Gerado em {generated_at.strftime('%d/%m/%Y %H:%M')} · Total de itens: {total}", styles["Normal"]),
        Spacer(1, 5 * mm),
    ]
    headers = ("Peladeiro", "Produto", "Tamanho", "Nome personalizado", "Número", "Quantidade")
    rows = [[Paragraph(escape(header), styles["SupplierHead"]) for header in headers]]
    for item in items:
        rows.append([
            Paragraph(escape(str(item["player_name"] or "—")), styles["SupplierCell"]),
            Paragraph(escape(str(item["product_name"] or "—")), styles["SupplierCell"]),
            Paragraph(escape(str(item["variant_size"] or "Único")), styles["SupplierCell"]),
            Paragraph(escape(str(item["custom_name"] or "—")), styles["SupplierCell"]),
            Paragraph(escape(str(item["custom_number"] or "—")), styles["SupplierCell"]),
            Paragraph(str(int(item["quantity"] or 0)), styles["SupplierCell"]),
        ])
    if len(rows) == 1:
        rows.append([Paragraph("Nenhuma solicitação pendente.", styles["SupplierCell"])] + [""] * 5)
    table = Table(rows, colWidths=[52 * mm, 65 * mm, 28 * mm, 58 * mm, 25 * mm, 28 * mm], repeatRows=1)
    rules = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#073B5C")),
        ("GRID", (0, 0), (-1, -1), .35, colors.HexColor("#CFD8DC")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    if len(rows) == 2 and not items:
        rules.append(("SPAN", (0, 1), (-1, 1)))
    table.setStyle(TableStyle(rules))
    story.append(table)
    document.build(story)
    output.seek(0)
    return output
