from datetime import date
from io import BytesIO
import base64
import re
import textwrap

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, send_file, url_for

from src.db import get_db
from src.routes.auth import roles_allowed
from src.routes.sales import event_public_token
from src.services.pix import generate_qrcode_base64


bp = Blueprint("events", __name__, url_prefix="/eventos")


@bp.get("")
@roles_allowed("manager", "staff")
def index():
    db = get_db()
    events = db.execute(
        """SELECT e.*, u.name created_by_name,
                  COUNT(DISTINCT s.id) sale_count,
                  COALESCE(SUM(CASE WHEN s.paid=1 THEN s.total_cents ELSE 0 END),0) total_cents
           FROM bar_events e
           LEFT JOIN users u ON u.id=e.created_by
           LEFT JOIN sales s ON s.event_id=e.id
           GROUP BY e.id,u.name ORDER BY CASE WHEN e.status='open' THEN 0 ELSE 1 END,e.event_date DESC,e.id DESC"""
    ).fetchall()
    active_events = [event for event in events if event["status"] == "open"]
    event_qr = {}
    for event in active_events:
        token = event_public_token(event["id"])
        guest_url = url_for("sales.guest_event_sale", token=token, _external=True)
        event_qr[event["id"]] = {
            "url": guest_url,
            "image": "data:image/png;base64," + generate_qrcode_base64(guest_url),
        }
    return render_template("events.html", events=events, active_events=active_events, event_qr=event_qr)


@bp.post("")
@roles_allowed("manager")
def create():
    name = request.form.get("name", "").strip()
    event_date = request.form.get("event_date", "").strip()
    description = request.form.get("description", "").strip()
    if not name:
        flash("Informe o nome do evento ou festa.", "danger")
        return redirect(url_for("events.index"))
    if event_date:
        try:
            event_date = date.fromisoformat(event_date).isoformat()
        except ValueError:
            flash("Informe uma data válida.", "danger")
            return redirect(url_for("events.index"))
    db = get_db()
    with db:
        db.execute(
            "INSERT INTO bar_events(name,event_date,description,created_by) VALUES(?,?,?,?)",
            (name[:150], event_date, description[:2000], g.user["id"]),
        )
    flash("Evento criado e aberto para vendas.", "success")
    return redirect(url_for("events.index"))


@bp.post("/<int:event_id>/close")
@roles_allowed("manager")
def close(event_id):
    db = get_db()
    with db:
        updated = db.execute(
            "UPDATE bar_events SET status='closed',closed_at=CURRENT_TIMESTAMP,closed_by=? WHERE id=? AND status='open'",
            (g.user["id"], event_id),
        )
    flash("Evento encerrado." if updated.rowcount else "Evento não encontrado ou já encerrado.", "success" if updated.rowcount else "warning")
    return redirect(url_for("events.index"))


@bp.get("/<int:event_id>")
@roles_allowed("manager", "staff")
def detail(event_id):
    db = get_db()
    event = db.execute("SELECT * FROM bar_events WHERE id=?", (event_id,)).fetchone()
    if not event:
        flash("Evento não encontrado.", "warning")
        return redirect(url_for("events.index"))
    sales = db.execute(
        """SELECT s.*,p.name player_name,p.war_name
           FROM sales s LEFT JOIN players p ON p.id=s.player_id
           WHERE s.event_id=? ORDER BY s.id DESC""", (event_id,)
    ).fetchall()
    return render_template("event_detail.html", event=event, sales=sales)


@bp.get("/<int:event_id>/qr.pdf")
@roles_allowed("manager", "staff")
def qr_pdf(event_id):
    """Create a print-ready poster for the event's guest-sale QR code.

    The QR is intentionally generated only while the event is open. Once the
    event is closed, the signed guest URL is no longer valid and no new poster
    can be downloaded from the management screen.
    """
    event = get_db().execute("SELECT * FROM bar_events WHERE id=?", (event_id,)).fetchone()
    if not event:
        flash("Evento não encontrado.", "warning")
        return redirect(url_for("events.index"))
    if event["status"] != "open":
        flash("O QR Code só pode ser baixado enquanto o evento estiver aberto.", "warning")
        return redirect(url_for("events.index"))

    try:
        from reportlab.lib.colors import HexColor, white
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas
    except ImportError:
        current_app.logger.exception("PDF do QR indisponível: reportlab não instalado")
        flash("Não foi possível gerar o PDF neste momento.", "danger")
        return redirect(url_for("events.index"))

    token = event_public_token(event_id)
    guest_url = url_for("sales.guest_event_sale", token=token, _external=True)
    qr_bytes = base64.b64decode(generate_qrcode_base64(guest_url))
    output = BytesIO()
    page_width, page_height = A4
    pdf = canvas.Canvas(output, pagesize=A4)

    # A strong, high-contrast header keeps the poster readable from a wall.
    header_height = 145
    pdf.setFillColor(HexColor("#07558c"))
    pdf.rect(0, page_height - header_height, page_width, header_height, fill=1, stroke=0)

    logo_path = current_app.static_folder and (current_app.static_folder + "/logo-gpcta.jpeg")
    if logo_path:
        try:
            logo = ImageReader(logo_path)
            logo_size = 76
            pdf.drawImage(logo, 40, page_height - 108, width=logo_size, height=logo_size, preserveAspectRatio=True, mask="auto")
        except (OSError, ValueError):
            current_app.logger.warning("Logo do GPCTA não pôde ser carregada no PDF do evento", exc_info=True)

    pdf.setFillColor(white)
    pdf.setFont("Helvetica-Bold", 25)
    pdf.drawString(135, page_height - 62, "PELADEIROS GPCTA")
    pdf.setFont("Helvetica", 12)
    pdf.drawString(138, page_height - 84, "Venda para convidados")

    event_name = str(event["name"] or "Evento/Festa").strip()
    pdf.setFillColor(HexColor("#12324a"))
    pdf.setFont("Helvetica-Bold", 24)
    # Keep the title inside the printable width even for long event names.
    title = event_name if len(event_name) <= 48 else event_name[:45].rstrip() + "..."
    pdf.drawCentredString(page_width / 2, page_height - 190, title)
    if event["event_date"]:
        pdf.setFont("Helvetica", 12)
        pdf.setFillColor(HexColor("#5f7482"))
        pdf.drawCentredString(page_width / 2, page_height - 212, str(event["event_date"]))

    # Center a large QR with generous quiet space for reliable scanning.
    qr_size = 310
    qr_x = (page_width - qr_size) / 2
    qr_y = page_height - 570
    pdf.setFillColor(white)
    pdf.roundRect(qr_x - 18, qr_y - 18, qr_size + 36, qr_size + 36, 12, fill=1, stroke=0)
    pdf.drawImage(ImageReader(BytesIO(qr_bytes)), qr_x, qr_y, width=qr_size, height=qr_size, preserveAspectRatio=True, mask="auto")

    pdf.setFillColor(HexColor("#12324a"))
    pdf.setFont("Helvetica-Bold", 15)
    pdf.drawCentredString(page_width / 2, qr_y - 52, "Aponte a câmera e faça seu pedido")
    pdf.setFont("Helvetica", 10)
    pdf.setFillColor(HexColor("#5f7482"))
    pdf.drawCentredString(page_width / 2, qr_y - 70, "O QR Code é válido enquanto o evento estiver aberto.")
    pdf.setStrokeColor(HexColor("#d9e3e9"))
    pdf.line(55, 70, page_width - 55, 70)
    pdf.setFont("Helvetica", 7)
    pdf.setFillColor(HexColor("#78909c"))
    for index, line in enumerate(textwrap.wrap(guest_url, width=100)):
        pdf.drawCentredString(page_width / 2, 54 - (index * 9), line)
    pdf.showPage()
    pdf.save()
    output.seek(0)

    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "-", event_name).strip("-") or "evento"
    return send_file(output, mimetype="application/pdf", as_attachment=True, download_name=f"qr-{safe_name}.pdf")
