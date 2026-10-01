from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from src.utils import brdate


FOOTER = "Documento de consulta gerado a partir dos registros do sistema."


def _text(value, fallback="—"):
    return escape(str(value)) if value not in (None, "") else fallback


def _date(value):
    if not value:
        return "Não informado"
    try:
        return brdate(value)
    except (TypeError, ValueError):
        return str(value)


def _table(headers, rows, widths, styles):
    values = [[Paragraph(escape(header), styles["StatementHead"]) for header in headers]]
    values.extend([
        [Paragraph(_text(value), styles["StatementCell"]) for value in row]
        for row in rows
    ])
    if not rows:
        values.append([Paragraph("Nenhum registro encontrado.", styles["StatementCell"])] + [""] * (len(headers) - 1))
    table = Table(values, colWidths=widths, repeatRows=1, splitByRow=1)
    rules = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#073B5C")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CFD8DC")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for index in range(2, len(values), 2):
        rules.append(("BACKGROUND", (0, index), (-1, index), colors.HexColor("#F4F6F7")))
    if not rows:
        rules.append(("SPAN", (0, 1), (-1, 1)))
    table.setStyle(TableStyle(rules))
    return table


def _footer(canvas, document):
    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(colors.HexColor("#607D8B"))
    canvas.drawCentredString(landscape(A4)[0] / 2, 8 * mm, FOOTER)
    canvas.drawRightString(landscape(A4)[0] - 12 * mm, 8 * mm, f"Página {document.page}")
    canvas.restoreState()


def build_player_statement_pdf(context, generated_at):
    output = BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm,
        topMargin=12 * mm, bottomMargin=16 * mm,
        title="Extrato do Peladeiro", author="PELADEIROS GPCTA",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="StatementTitle", parent=styles["Title"], fontSize=17, textColor=colors.HexColor("#073B5C"), alignment=1))
    styles.add(ParagraphStyle(name="StatementSub", parent=styles["Normal"], fontSize=9, leading=12, alignment=1))
    styles.add(ParagraphStyle(name="StatementSection", parent=styles["Heading2"], fontSize=11, textColor=colors.HexColor("#073B5C"), spaceBefore=8, spaceAfter=5))
    styles.add(ParagraphStyle(name="StatementCell", parent=styles["Normal"], fontSize=7, leading=9))
    styles.add(ParagraphStyle(name="StatementHead", parent=styles["StatementCell"], fontName="Helvetica-Bold", textColor=colors.white, alignment=1))

    player = context["selected_player"]
    filters = context["filters"]
    product_name = next(
        (row["name"] for row in context["products"] if str(row["id"]) == filters["product_id"]),
        "Todos",
    )
    situation_name = {
        "": "Todos", "pending": "A retirar", "delivered": "Retirado",
        "restored": "Restaurado", "canceled": "Cancelado/Estornado",
    }[filters["situation"]]
    period = f"{filters['start_date'] or 'início'} a {filters['end_date'] or 'hoje'}"
    player_name = player["war_name"] or player["name"]
    story = [
        Paragraph("EXTRATO DO PELADEIRO", styles["StatementTitle"]),
        Paragraph(f"Peladeiro: <b>{_text(player_name)}</b>", styles["StatementSub"]),
        Paragraph(f"Gerado em: {_text(_date(generated_at))}", styles["StatementSub"]),
        Paragraph(
            f"Filtros — Período da compra: {_text(period)} · Produto: {_text(product_name)} · Situação: {_text(situation_name)}",
            styles["StatementSub"],
        ),
        Spacer(1, 3 * mm),
    ]
    summary = context["summary"]
    summary_table = _table(
        ("Compras", "Itens comprados", "Retirados líquidos", "A retirar"),
        [(summary["sales"], summary["bought"], summary["net_delivered"], summary["pending"])],
        [45 * mm] * 4, styles,
    )
    story.extend([Paragraph("RESUMO", styles["StatementSection"]), summary_table])

    balance_rows = []
    for row in context["balances"]:
        product = row["product_name"] + (f" · {row['variant_label']}" if row["variant_label"] else "")
        net = str(row["net_delivered"])
        if row["has_legacy"]:
            net += " (+ legado não detalhado)"
        balance_rows.append((product, row["bought"], net, row["pending"]))
    story.extend([
        Paragraph("SALDO A RETIRAR", styles["StatementSection"]),
        _table(("Produto/variante", "Comprado", "Retirado líquido", "A retirar"), balance_rows,
               [105 * mm, 30 * mm, 50 * mm, 30 * mm], styles),
    ])

    purchase_rows = []
    for row in context["purchases"]:
        product = row["product_name"] + (f" · {row['variant_label']}" if row["variant_label"] else "")
        net = "Não detalhado (legado)" if row["net_delivered"] is None else row["net_delivered"]
        purchase_rows.append((f"#{row['sale_id']}", _date(row["sale_created_at"]), product,
                              row["bought"], net, row["pending"], row["status_label"]))
    story.extend([
        Paragraph("COMPRAS/VENDAS", styles["StatementSection"]),
        _table(("Venda", "Data", "Produto/variante", "Comprado", "Retirado líquido", "A retirar", "Situação"),
               purchase_rows, [18 * mm, 31 * mm, 78 * mm, 25 * mm, 35 * mm, 22 * mm, 43 * mm], styles),
    ])

    movement_rows = []
    for row in context["movements"]:
        product = row["product_name"] + (f" · {row['variant_size']}" if row["variant_size"] else "")
        movement_rows.append((_date(row["occurred_at"]), row["kind"], product,
                              row["quantity"] if row["quantity"] is not None else "Não informado",
                              f"#{row['sale_id']}", row["operator_name"] or "Não informado",
                              row["reason"] or "—"))
    story.extend([
        Paragraph("HISTÓRICO DE MOVIMENTAÇÕES", styles["StatementSection"]),
        _table(("Data/hora", "Evento", "Produto/variante", "Quantidade", "Venda", "Operador", "Motivo"),
               movement_rows, [32 * mm, 39 * mm, 65 * mm, 25 * mm, 18 * mm, 33 * mm, 40 * mm], styles),
    ])
    document.build(story, onFirstPage=_footer, onLaterPages=_footer)
    output.seek(0)
    return output
