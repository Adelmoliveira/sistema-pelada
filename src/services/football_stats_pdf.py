from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def build_football_stats_pdf(rows, totals, filters=None, issued_on=None):
    filters = filters or {}
    output = BytesIO()
    document = SimpleDocTemplate(output, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=15 * mm, title="Estatísticas do futebol", author="PELADEIROS GPCTA")
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="StatsTitle", parent=styles["Title"], fontSize=18, textColor=colors.HexColor("#073B5C"), alignment=1))
    styles.add(ParagraphStyle(name="StatsSub", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#405563"), alignment=1))
    styles.add(ParagraphStyle(name="StatsCell", parent=styles["Normal"], fontSize=8, leading=10))
    styles.add(ParagraphStyle(name="StatsHead", parent=styles["StatsCell"], fontName="Helvetica-Bold", textColor=colors.white, alignment=1))
    period = "Todos os períodos"
    if filters.get("year") and filters.get("month"):
        period = f"{filters['month']}/{filters['year']}"
    elif filters.get("year"):
        period = str(filters["year"])
    player_filter = filters.get("player_name") or "Todos os peladeiros"
    own_goals_total = int(totals["gols_contra"] or 0)
    story = [Paragraph("PELADEIROS GPCTA", styles["StatsTitle"]), Paragraph("Estatísticas do futebol", styles["StatsSub"]), Paragraph(f"Período: {escape(period)} · Peladeiro: {escape(player_filter)} · Súmulas: {totals['sumulas'] or 0} · Partidas: {totals['partidas'] or 0} · Gols: {totals['gols'] or 0} · Gols contra: {-own_goals_total}", styles["StatsSub"]), Spacer(1, 5 * mm)]
    headers = ("Peladeiro", "Frequência", "Jogos", "Vitórias", "Empates", "Derrotas", "Gols", "Gols contra", "Assistências")
    table_rows = [[Paragraph(value, styles["StatsHead"]) for value in headers]]
    for row in rows:
        table_rows.append([Paragraph(escape(str(value)), styles["StatsCell"]) for value in ((row["war_name"] or row["name"]), f"{row['frequencia']}%", row["jogos"], row["vitorias"], row["empates"], row["derrotas"], row["gols"], -int(row["gols_contra"] or 0), row["assistencias"])])
    if len(table_rows) == 1:
        table_rows.append([Paragraph("Nenhuma estatística encontrada para os filtros informados.", styles["StatsCell"])] + [""] * (len(headers) - 1))
    table = Table(table_rows, colWidths=[51 * mm, 23 * mm, 17 * mm, 18 * mm, 18 * mm, 18 * mm, 15 * mm, 23 * mm, 23 * mm], repeatRows=1)
    rules = [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#073B5C")), ("GRID", (0, 0), (-1, -1), .35, colors.HexColor("#CFD8DC")), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]
    for index in range(2, len(table_rows), 2):
        rules.append(("BACKGROUND", (0, index), (-1, index), colors.HexColor("#F4F6F7")))
    if len(table_rows) == 2:
        rules.append(("SPAN", (0, 1), (-1, 1)))
    table.setStyle(TableStyle(rules))
    story.append(table)
    document.build(story)
    output.seek(0)
    return output
