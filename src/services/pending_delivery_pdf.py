from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from src.utils import brdate


def build_pending_delivery_pdf(orders):
    """Build a read-only inventory of items awaiting pickup."""
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=15 * mm,
        title="Pedidos aguardando retirada",
        author="PELADEIROS GPCTA",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="PendingTitle", parent=styles["Title"], fontSize=18, textColor=colors.HexColor("#073B5C"), alignment=1))
    styles.add(ParagraphStyle(name="PendingSub", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#405563"), alignment=1))
    styles.add(ParagraphStyle(name="PendingCell", parent=styles["Normal"], fontSize=8, leading=10))
    styles.add(ParagraphStyle(name="PendingHead", parent=styles["PendingCell"], fontName="Helvetica-Bold", textColor=colors.white, alignment=1))

    total_items = sum(order["pending_quantity"] for order in orders)
    story = [
        Paragraph("PELADEIROS GPCTA", styles["PendingTitle"]),
        Paragraph("Pedidos aguardando retirada", styles["PendingSub"]),
        Paragraph(f"Pedidos: {len(orders)} · Itens pendentes: {total_items}", styles["PendingSub"]),
        Spacer(1, 5 * mm),
    ]
    headers = ("Pedido", "Peladeiro / convidado", "Data do pagamento", "Pagamento", "Produtos aguardando retirada", "Total pendente")
    rows = [[Paragraph(escape(str(header)), styles["PendingHead"]) for header in headers]]
    for order in orders:
        products = "<br/>".join(
            f"{item['pending_quantity']}× {escape(str(item['name']))}"
            for item in order["items"] if item["pending_quantity"] > 0
        ) or "—"
        date_value = order.get("paid_at") or ""
        try:
            date_value = brdate(date_value)
        except Exception:
            pass
        player = order.get("player_war_name") or order.get("player_name") or "Convidado"
        full = order.get("player_full_name") or ""
        if full and full != player:
            player = f"{player} ({full})"
        if order.get("event_name"):
            player = f"{player} · Evento: {order['event_name']}"
        rows.append([
            Paragraph(f"#{order['id']}", styles["PendingCell"]),
            Paragraph(escape(str(player)), styles["PendingCell"]),
            Paragraph(escape(str(date_value or "—")), styles["PendingCell"]),
            Paragraph(escape(str(order.get("payment_method") or "—")), styles["PendingCell"]),
            Paragraph(products, styles["PendingCell"]),
            Paragraph(str(order["pending_quantity"]), styles["PendingCell"]),
        ])
    if len(rows) == 1:
        rows.append([Paragraph("Nenhum pedido aguardando retirada.", styles["PendingCell"])] + [""] * (len(headers) - 1))
    table = Table(rows, colWidths=[18 * mm, 56 * mm, 37 * mm, 29 * mm, 90 * mm, 25 * mm], repeatRows=1)
    rules = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#073B5C")),
        ("GRID", (0, 0), (-1, -1), .35, colors.HexColor("#CFD8DC")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    for index in range(2, len(rows), 2):
        rules.append(("BACKGROUND", (0, index), (-1, index), colors.HexColor("#F4F6F7")))
    if len(rows) == 2:
        rules.append(("SPAN", (0, 1), (-1, 1)))
    table.setStyle(TableStyle(rules))
    story.append(table)
    document.build(story)
    output.seek(0)
    return output
