from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from src.utils import local_today


def _value(value, fallback="Não informado"):
    return escape(str(value)) if value else fallback


def build_players_pdf(players, generated_on=None, query=""):
    generated_on = generated_on or local_today()
    output = BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=15 * mm, bottomMargin=17 * mm,
        title="Cadastro completo de peladeiros", author="PELADEIROS GPCTA",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="ReportTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=colors.HexColor("#073B5C"), spaceAfter=4))
    styles.add(ParagraphStyle(name="Subtitle", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#5E6B73"), spaceAfter=10))
    styles.add(ParagraphStyle(name="Name", parent=styles["Heading2"], fontSize=13, leading=16, textColor=colors.HexColor("#0D6E9E"), spaceAfter=3))
    styles.add(ParagraphStyle(name="Cell", parent=styles["Normal"], fontSize=8.5, leading=11, textColor=colors.HexColor("#183042")))
    styles.add(ParagraphStyle(name="Label", parent=styles["Cell"], fontName="Helvetica-Bold", textColor=colors.HexColor("#5E6B73")))
    story = [Paragraph("PELADEIROS GPCTA", styles["ReportTitle"]), Paragraph("Cadastro completo dos peladeiros" + (f" - filtro: {escape(query)}" if query else ""), styles["Subtitle"]), Paragraph(f"Gerado em {generated_on.strftime('%d/%m/%Y')} - {len(players)} cadastro(s)", styles["Subtitle"])]
    for index, player in enumerate(players, 1):
        display_name = player["war_name"] or player["name"]
        address = " - ".join(part for part in (player["address_street"], player["address_number"], player["address_complement"]) if part) or "Não informado"
        location = " - ".join(part for part in (player["address_neighborhood"], player["address_city"], player["address_state"]) if part) or "Não informado"
        status = "Ativo" if player["active"] else "Inativo"
        story.append(Paragraph(f"{index}. {escape(display_name)}", styles["Name"]))
        rows = [
            [Paragraph("Nome completo", styles["Label"]), Paragraph(_value(player["name"]), styles["Cell"]), Paragraph("Nome de guerra", styles["Label"]), Paragraph(_value(player["war_name"]), styles["Cell"])],
            [Paragraph("CPF", styles["Label"]), Paragraph(_value(player["cpf"]), styles["Cell"]), Paragraph("Nascimento", styles["Label"]), Paragraph(_value(player["birth_date"]), styles["Cell"])],
            [Paragraph("E-mail", styles["Label"]), Paragraph(_value(player["email"]), styles["Cell"]), Paragraph("Contato", styles["Label"]), Paragraph(_value(player["phone"]), styles["Cell"])],
            [Paragraph("Emergência", styles["Label"]), Paragraph(_value(player["emergency_phone"]), styles["Cell"]), Paragraph("Situação", styles["Label"]), Paragraph(status, styles["Cell"])],
            [Paragraph("Endereço", styles["Label"]), Paragraph(_value(address), styles["Cell"]), Paragraph("Localidade", styles["Label"]), Paragraph(_value(location), styles["Cell"])],
            [Paragraph("CEP", styles["Label"]), Paragraph(_value(player["postal_code"]), styles["Cell"]), Paragraph("Mensalidade", styles["Label"]), Paragraph(_value(player["membership_type"]), styles["Cell"])],
        ]
        table = Table(rows, colWidths=[27 * mm, 62 * mm, 27 * mm, 62 * mm])
        table.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#EAF4F8")), ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#EAF4F8")), ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CFD8DC")), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
        story.extend([table, Spacer(1, 7 * mm)])
    if not players:
        story.append(Paragraph("Nenhum peladeiro encontrado para o filtro informado.", styles["Cell"]))

    def footer(canvas, doc):
        canvas.saveState(); canvas.setStrokeColor(colors.HexColor("#D9E1E5")); canvas.line(15 * mm, 11 * mm, A4[0] - 15 * mm, 11 * mm); canvas.setFont("Helvetica", 7.5); canvas.setFillColor(colors.HexColor("#6C757D")); canvas.drawString(15 * mm, 7 * mm, "Documento gerado pelo PELADEIROS GPCTA"); canvas.drawRightString(A4[0] - 15 * mm, 7 * mm, f"Página {doc.page}"); canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    output.seek(0)
    return output
