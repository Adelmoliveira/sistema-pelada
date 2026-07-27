import html
import re
import smtplib
import ssl
from datetime import date
from email.message import EmailMessage

from src.utils import alphabetical_key, money
from src.services.push_notifications import send_player_push


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
        "SELECT id,name,email,football_join_date FROM players "
        "WHERE active=1 AND membership_type='regular'"
    ).fetchall()
    players = sorted(players, key=lambda player: alphabetical_key(player["name"]))
    paid_rows = db.execute(
        "SELECT player_id,month FROM membership_months WHERE month>=? AND month<=?",
        (f"{today.year}-01", f"{today.year}-{today.month:02d}"),
    ).fetchall()
    paid = {}
    for row in paid_rows:
        paid.setdefault(row["player_id"], set()).add(int(row["month"][-2:]))

    debtors = []
    for player in players:
        raw_join = (player["football_join_date"] or "").strip()
        try:
            joined = date.fromisoformat(raw_join + "-01" if len(raw_join) == 7 else raw_join[:10])
            if joined.year > today.year:
                continue
            first_month = joined.month if joined.year == today.year else 1
        except (TypeError, ValueError):
            # Mantém compatibilidade com cadastros antigos sem data; novos
            # cadastros devem informar a apresentação antes da cobrança.
            first_month = 1
        missing = [month for month in range(first_month, today.month + 1) if month not in paid.get(player["id"], set())]
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
    html_body = f"""<div style="margin:0;background:#f2f6f9;padding:24px;font-family:Arial,sans-serif;color:#183247">
      <div style="max-width:620px;margin:auto;background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 3px 12px #1232">
        <div style="background:#07558c;padding:20px;text-align:center"><img src="https://sistema-pelada-one.vercel.app/static/logo-gpcta.jpeg" alt="Logo GPCTA" style="max-width:110px;max-height:90px;object-fit:contain"><h1 style="color:#fff;font-size:22px;margin:10px 0 0">PELADEIROS GPCTA</h1></div>
        <div style="padding:24px"><h2 style="margin-top:0;color:#07558c">Lembrete de pendência financeira</h2>{markdown_email_html(body)}
        <p style="margin-top:24px;color:#607d8b;font-size:13px">Mensagem enviada pelo sistema PELADEIROS GPCTA.</p></div>
      </div>
    </div>"""
    message.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=20) as smtp:
        smtp.login(sender, app_password)
        smtp.send_message(message)


def send_gmail_html(sender, app_password, recipient, subject, body, html_body):
    """Send a message with a custom HTML layout and a plain-text fallback."""
    sender = sender.strip()
    app_password = app_password.replace(" ", "").strip()
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    message.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=20) as smtp:
        smtp.login(sender, app_password)
        smtp.send_message(message)


def dispatch_reminders(db, settings, sender, app_password, today, send_func=send_gmail):
    result = {"sent": 0, "failed": 0, "skipped": 0, "without_email": 0}
    period = today.strftime("%Y-%m")
    for debtor in outstanding_players(db, today):
        if not debtor["email"]:
            if int(settings["push_enabled"] or 0):
                send_player_push(db, debtor["id"], "Mensalidade pendente", f"Você possui uma pendência de mensalidade no valor de {money(debtor['amount_cents'])}.", "/notificacoes")
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
            if int(settings["push_enabled"] or 0):
                send_player_push(db, debtor["id"], "Mensalidade pendente", f"Você possui uma pendência de mensalidade no valor de {money(debtor['amount_cents'])}.", "/notificacoes")
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
