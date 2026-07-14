import html
import re
import smtplib
import ssl
from email.message import EmailMessage

from src.utils import money


MONTH_NAMES = (
    "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
)

DEFAULT_REMINDER_SUBJECT = "Pendência financeira - Peladeiros GPCTA"
DEFAULT_REMINDER_BODY = """Prezado Peladeiro **{{ nome }}**,

Identificamos pendências financeiras. Segue o detalhamento:

**Débito {{ ano }} (até {{ mes }}):** **{{ debito }}**
**Total a pagar:** **{{ total }}**

**Gentileza realizar o pagamento o mais breve possível** e enviar o comprovante respondendo este e-mail.

----DADOS-PARA-PAGAMENTO------------------------
BANCO DO BRASIL
Agência: 5899-8
C/C: 19118-3
Poupança, variação 51
Titular: Mário Paulo Alves Júnior
Chave PIX: diretoriagpcta@gmail.com"""


def get_reminder_settings(db):
    settings = db.execute("SELECT * FROM reminder_settings ORDER BY id LIMIT 1").fetchone()
    if settings:
        return settings
    db.execute(
        "INSERT INTO reminder_settings(enabled,schedule_day,subject,body) VALUES(0,5,?,?)",
        (DEFAULT_REMINDER_SUBJECT, DEFAULT_REMINDER_BODY),
    )
    db.commit()
    return db.execute("SELECT * FROM reminder_settings ORDER BY id LIMIT 1").fetchone()


def outstanding_players(db, today, monthly_fee=1500):
    players = db.execute(
        "SELECT id,name,email FROM players "
        "WHERE active=1 AND membership_type='regular' ORDER BY name"
    ).fetchall()
    paid_rows = db.execute(
        "SELECT player_id,month FROM membership_months WHERE month>=? AND month<=?",
        (f"{today.year}-01", f"{today.year}-{today.month:02d}"),
    ).fetchall()
    paid = {}
    for row in paid_rows:
        paid.setdefault(row["player_id"], set()).add(int(row["month"][-2:]))

    debtors = []
    for player in players:
        missing = [month for month in range(1, today.month + 1) if month not in paid.get(player["id"], set())]
        if not missing:
            continue
        amount = len(missing) * monthly_fee
        debtors.append({
            "id": player["id"],
            "name": player["name"],
            "email": (player["email"] or "").strip(),
            "missing_months": missing,
            "missing_month_names": ", ".join(MONTH_NAMES[month - 1] for month in missing),
            "amount_cents": amount,
        })
    return debtors


def render_template_text(template, context):
    rendered = template
    for key, value in context.items():
        rendered = rendered.replace("{{ " + key + " }}", str(value))
        rendered = rendered.replace("{{" + key + "}}", str(value))
    return rendered


def reminder_context(debtor, today):
    amount = money(debtor["amount_cents"])
    return {
        "nome": debtor["name"],
        "ano": today.year,
        "mes": MONTH_NAMES[today.month - 1],
        "meses": debtor["missing_month_names"],
        "debito": amount,
        "total": amount,
    }


def markdown_email_html(body):
    escaped = html.escape(body)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    return "<div style=\"font-family:Arial,sans-serif;font-size:16px;line-height:1.55;color:#183042\">" + escaped.replace("\n", "<br>\n") + "</div>"


def send_gmail(sender, app_password, recipient, subject, body):
    sender = sender.strip()
    app_password = app_password.replace(" ", "").strip()
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body.replace("**", ""))
    message.add_alternative(markdown_email_html(body), subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=20) as smtp:
        smtp.login(sender, app_password)
        smtp.send_message(message)


def dispatch_reminders(db, settings, sender, app_password, today, send_func=send_gmail):
    result = {"sent": 0, "failed": 0, "skipped": 0, "without_email": 0}
    period = today.strftime("%Y-%m")
    for debtor in outstanding_players(db, today):
        if not debtor["email"]:
            result["without_email"] += 1
            continue
        existing = db.execute(
            "SELECT status FROM reminder_dispatches WHERE player_id=? AND period=?",
            (debtor["id"], period),
        ).fetchone()
        if existing and existing["status"] == "sent":
            result["skipped"] += 1
            continue

        context = reminder_context(debtor, today)
        subject = render_template_text(settings["subject"], context)
        body = render_template_text(settings["body"], context)
        try:
            send_func(sender, app_password, debtor["email"], subject, body)
            status, error = "sent", ""
            result["sent"] += 1
        except Exception as exc:
            status, error = "failed", str(exc)[:500]
            result["failed"] += 1

        db.execute(
            """INSERT INTO reminder_dispatches
               (player_id,period,recipient_email,status,error_message)
               VALUES(?,?,?,?,?)
               ON CONFLICT(player_id,period) DO UPDATE SET
                 recipient_email=?,status=?,error_message=?,sent_at=CURRENT_TIMESTAMP""",
            (debtor["id"], period, debtor["email"], status, error,
             debtor["email"], status, error),
        )
        db.commit()
    return result
