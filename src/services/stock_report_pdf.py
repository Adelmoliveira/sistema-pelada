from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from src.utils import local_today


NAVY = colors.HexColor("#073B5C")
BLUE = colors.HexColor("#0D6E9E")
LIGHT_BLUE = colors.HexColor("#EAF4F8")
LIGHT_GRAY = colors.HexColor("#F4F6F7")


def stock_report_data(db, start_date="", end_date=""):
    """Return current stock and period movement totals for every product."""
    conditions = []
    params = []
    if start_date:
        conditions.append("date(r.created_at) >= ?")
    if end_date:
        conditions.append("date(r.created_at) <= ?")
    restock_where = (" AND " + " AND ".join(conditions)) if conditions else ""
    restock_params = tuple(value for value in (start_date, end_date) if value)
    restocks = db.execute(
        f"""SELECT r.product_id,
        COALESCE(c.corrected_quantity, r.quantity) quantity
        FROM restocks r
        LEFT JOIN restock_corrections c ON c.id=(
            SELECT MAX(c2.id) FROM restock_corrections c2 WHERE c2.restock_id=r.id
        )
        WHERE 1=1{restock_where}""",
        restock_params,
    ).fetchall()

    sale_conditions = ["s.paid=1", "s.payment_status NOT IN ('canceled','refunded','failed','expired')"]
    sale_params = []
    if start_date:
        sale_conditions.append("date(COALESCE(s.paid_at,s.created_at)) >= ?")
        sale_params.append(start_date)
    if end_date:
        sale_conditions.append("date(COALESCE(s.paid_at,s.created_at)) <= ?")
        sale_params.append(end_date)
    sales = db.execute(
        f"""SELECT i.product_id, COALESCE(SUM(i.quantity),0) quantity
        FROM sale_items i JOIN sales s ON s.id=i.sale_id
        WHERE {' AND '.join(sale_conditions)}
        GROUP BY i.product_id""",
        tuple(sale_params),
    ).fetchall()
    entries = {}
    for row in restocks:
        entries[row["product_id"]] = entries.get(row["product_id"], 0) + int(row["quantity"] or 0)
    exits = {row["product_id"]: int(row["quantity"] or 0) for row in sales}
    # LOWER(name) funciona tanto no SQLite local quanto no PostgreSQL/Supabase.
    products = db.execute("SELECT id,name,stock FROM products ORDER BY LOWER(name),name").fetchall()
    rows = []
    for product in products:
        incoming = entries.get(product["id"], 0)
        outgoing = exits.get(product["id"], 0)
        rows.append({
            "name": product["name"], "stock": int(product["stock"] or 0),
            "entries": incoming, "exits": outgoing, "net": incoming - outgoing,
        })
    return rows


def build_stock_report_pdf(rows, start_date, end_date, issued_on=None):
    issued_on = issued_on or local_today()
    output = BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=landscape(A4), leftMargin=13 * mm, rightMargin=13 * mm,
        topMargin=13 * mm, bottomMargin=17 * mm,
        title="Relatório de Estoque", author="PELADEIROS GPCTA",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="StockTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=19, leading=22, textColor=NAVY, alignment=TA_CENTER, spaceAfter=3))
    styles.add(ParagraphStyle(name="StockHeading", parent=styles["Heading2"], fontSize=12, leading=15, textColor=BLUE, spaceBefore=5, spaceAfter=5))
    styles.add(ParagraphStyle(name="StockSub", parent=styles["Normal"], fontSize=8.5, leading=11, textColor=colors.HexColor("#5E6B73"), alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="StockCell", parent=styles["Normal"], fontSize=8.5, leading=10.5, textColor=colors.HexColor("#183042")))
    styles.add(ParagraphStyle(name="StockCenter", parent=styles["StockCell"], alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="StockRight", parent=styles["StockCell"], alignment=TA_RIGHT))
    styles.add(ParagraphStyle(name="StockHeader", parent=styles["StockCenter"], fontName="Helvetica-Bold", textColor=colors.white))
    period = f"{start_date.strftime('%d/%m/%Y') if start_date else 'Todos os registros'} a {end_date.strftime('%d/%m/%Y') if end_date else 'hoje'}"
    story = [
        Paragraph("PELADEIROS GPCTA", styles["StockTitle"]),
        Paragraph("Relatório de Estoque", styles["StockHeading"]),
        Paragraph(f"Período das movimentações: {period} - Emitido em {issued_on.strftime('%d/%m/%Y')}", styles["StockSub"]),
        Spacer(1, 5 * mm),
    ]
    headers = ("Produto", "Estoque atual", "Entradas", "Saídas", "Movimentação líquida")
    table_rows = [[Paragraph(value, styles["StockHeader"]) for value in headers]]
    for row in rows:
        table_rows.append([
            Paragraph(escape(row["name"]), styles["StockCell"]),
            Paragraph(str(row["stock"]), styles["StockCenter"]),
            Paragraph(f"+{row['entries']}", styles["StockCenter"]),
            Paragraph(f"-{row['exits']}", styles["StockCenter"]),
            Paragraph(f"{row['net']:+d}", styles["StockCenter"]),
        ])
    if not rows:
        table_rows.append([Paragraph("Nenhum produto cadastrado.", styles["StockCenter"])] + [""] * 4)
    table = Table(table_rows, colWidths=[122 * mm, 36 * mm, 36 * mm, 36 * mm, 49 * mm], repeatRows=1, hAlign="LEFT")
    rules = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("GRID", (0, 0), (-1, -1), .35, colors.HexColor("#CFD8DC")),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    for index in range(2, len(table_rows), 2):
        rules.append(("BACKGROUND", (0, index), (-1, index), LIGHT_GRAY))
    if not rows:
        rules.append(("SPAN", (0, 1), (-1, 1)))
    table.setStyle(TableStyle(rules))
    story.extend([table, Spacer(1, 5 * mm), Paragraph(f"Produtos listados: <b>{len(rows)}</b> - Os totais de entradas e saídas consideram apenas o período informado.", styles["StockSub"])])

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#D9E1E5")); canvas.line(13 * mm, 12 * mm, landscape(A4)[0] - 13 * mm, 12 * mm)
        canvas.setFont("Helvetica", 7.5); canvas.setFillColor(colors.HexColor("#6C757D"))
        canvas.drawString(13 * mm, 8 * mm, "PELADEIROS GPCTA - Relatório de Estoque")
        canvas.drawRightString(landscape(A4)[0] - 13 * mm, 8 * mm, f"Página {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    output.seek(0)
    return output


def low_stock_report_data(db):
    rows = db.execute(
        """SELECT name,stock,min_stock,supplier_email FROM products
           WHERE active=1 AND stock<=min_stock ORDER BY stock,LOWER(name),name"""
    ).fetchall()
    return [dict(row) for row in rows]


def build_low_stock_pdf(rows, issued_on=None):
    issued_on = issued_on or local_today()
    output = BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=15 * mm, bottomMargin=18 * mm,
        title="Relatório de Estoque Baixo", author="PELADEIROS GPCTA",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="LowTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=19, leading=22, textColor=NAVY, alignment=TA_CENTER, spaceAfter=4))
    styles.add(ParagraphStyle(name="LowSub", parent=styles["Normal"], fontSize=9, leading=12, textColor=colors.HexColor("#5E6B73"), alignment=TA_CENTER, spaceAfter=8))
    styles.add(ParagraphStyle(name="LowCell", parent=styles["Normal"], fontSize=9, leading=11, textColor=colors.HexColor("#183042")))
    styles.add(ParagraphStyle(name="LowCenter", parent=styles["LowCell"], alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="LowHeader", parent=styles["LowCenter"], fontName="Helvetica-Bold", textColor=colors.white))
    story = [
        Paragraph("PELADEIROS GPCTA", styles["LowTitle"]),
        Paragraph("Relatório de estoque baixo", styles["LowSub"]),
        Paragraph(f"Emitido em {issued_on.strftime('%d/%m/%Y')}", styles["LowSub"]),
        Spacer(1, 3 * mm),
    ]
    headers = ("Produto", "Estoque atual", "Limite", "Fornecedor")
    table_rows = [[Paragraph(value, styles["LowHeader"]) for value in headers]]
    for row in rows:
        table_rows.append([
            Paragraph(escape(str(row["name"])), styles["LowCell"]),
            Paragraph(str(row["stock"]), styles["LowCenter"]),
            Paragraph(str(row["min_stock"]), styles["LowCenter"]),
            Paragraph(escape(str(row["supplier_email"] or "Não informado")), styles["LowCell"]),
        ])
    if not rows:
        table_rows.append([Paragraph("Nenhum produto está abaixo do limite.", styles["LowCenter"])] + [""] * 3)
    table = Table(table_rows, colWidths=[75 * mm, 30 * mm, 25 * mm, 50 * mm], repeatRows=1)
    rules = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), .35, colors.HexColor("#CFD8DC")),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]
    for index in range(2, len(table_rows), 2):
        rules.append(("BACKGROUND", (0, index), (-1, index), LIGHT_GRAY))
    if not rows:
        rules.append(("SPAN", (0, 1), (-1, 1)))
    table.setStyle(TableStyle(rules))
    story.extend([table, Spacer(1, 5 * mm), Paragraph(f"Produtos abaixo do limite: <b>{len(rows)}</b>", styles["LowSub"])])

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#D9E1E5")); canvas.line(15 * mm, 13 * mm, A4[0] - 15 * mm, 13 * mm)
        canvas.setFont("Helvetica", 7.5); canvas.setFillColor(colors.HexColor("#6C757D"))
        canvas.drawString(15 * mm, 9 * mm, "PELADEIROS GPCTA - Estoque baixo")
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f"Página {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    output.seek(0)
    return output
