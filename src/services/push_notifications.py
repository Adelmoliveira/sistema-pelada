import json
import os


def public_key():
    return (os.environ.get("VAPID_PUBLIC_KEY") or "").strip()


def send_player_push(db, player_id, title, body, url="/", image_url=""):
    """Envia uma notificação Web Push, quando VAPID está configurado."""
    private_key = (os.environ.get("VAPID_PRIVATE_KEY") or "").strip()
    subject = (os.environ.get("VAPID_SUBJECT") or "mailto:diretoriagpcta@gmail.com").strip()
    if not private_key:
        return {"sent": 0, "skipped": 1, "reason": "VAPID não configurado"}
    try:
        from pywebpush import webpush
    except ImportError:
        return {"sent": 0, "skipped": 1, "reason": "pywebpush não instalado"}
    subscriptions = db.execute("SELECT id,endpoint,p256dh,auth FROM push_subscriptions WHERE player_id=?", (player_id,)).fetchall()
    unread = int(db.execute("SELECT COUNT(*) FROM push_inbox WHERE player_id=? AND read_at IS NULL", (player_id,)).fetchone()[0] or 0)
    badge_count = unread + 1
    sent = 0
    for subscription in subscriptions:
        info = {"endpoint": subscription["endpoint"], "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]}}
        try:
            payload = {"title": title, "body": body, "url": url, "badge": badge_count}
            if image_url:
                payload["image"] = image_url
            webpush(subscription_info=info, data=json.dumps(payload), vapid_private_key=private_key, vapid_claims={"sub": subject})
            sent += 1
        except Exception as exc:
            # Assinaturas expiradas devem ser removidas para não gerar falhas futuras.
            if getattr(exc, "response", None) is not None and getattr(exc.response, "status_code", 0) in (404, 410):
                db.execute("DELETE FROM push_subscriptions WHERE id=?", (subscription["id"],))
    if sent:
        db.execute("INSERT INTO push_inbox(player_id,title,body,image_url) VALUES(?,?,?,?)", (player_id, title, body, image_url or ""))
    db.commit()
    return {"sent": sent, "skipped": 0}


def send_player_push_once(db, player_id, kind, period, title, body, url="/", image_url=""):
    existing = db.execute("SELECT 1 FROM push_dispatches WHERE player_id=? AND kind=? AND period=?", (player_id, kind, period)).fetchone()
    if existing:
        return {"sent": 0, "skipped": 1, "reason": "já enviado"}
    result = send_player_push(db, player_id, title, body, url, image_url)
    if result.get("reason") not in ("VAPID não configurado", "pywebpush não instalado"):
        db.execute("INSERT INTO push_dispatches(player_id,kind,period) VALUES(?,?,?) ON CONFLICT(player_id,kind,period) DO NOTHING", (player_id, kind, period))
        db.commit()
    return result


def send_transfer_window_notifications(db, year):
    """Notifica uma vez os peladeiros ativos no início da janela de fevereiro."""
    period = str(year)
    players = db.execute("SELECT id FROM players WHERE active=1 AND gender!='female' AND membership_type!='veteran'").fetchall()
    sent = 0
    for player in players:
        result = send_player_push_once(db, player["id"], "transfer_window", period, "Janela de transferência aberta", f"A janela de transferência de {year} está aberta durante fevereiro.", "/futebol/transferencia")
        sent += int(result.get("sent", 0))
    return sent


def send_birthday_notifications(db, today):
    """Envia uma vez por dia os avisos dos aniversariantes para os peladeiros."""
    birthdays = db.execute(
        """SELECT id,name,war_name,gender FROM players
           WHERE active=1 AND birth_date<>'' AND substr(birth_date,6,5)=?""",
        (today.strftime("%m-%d"),),
    ).fetchall()
    recipients = db.execute("SELECT id FROM players WHERE active=1").fetchall()
    sent = 0
    for birthday in birthdays:
        display_name = birthday["war_name"] or birthday["name"]
        for recipient in recipients:
            if recipient["id"] == birthday["id"]:
                prefix = "" if birthday["gender"] == "female" else "Peladeiro "
                title = f"Parabéns, {prefix}{display_name}! ⚽🍻"
                body = "Hoje é dia de comemorar! Desejamos a você muita saúde, felicidade, paz e sucesso. Que não faltem bons momentos, grandes amizades e, claro, muitas resenhas e gols na nossa pelada.\n\nFeliz aniversário! Aproveite o seu dia!"
            elif birthday["gender"] == "female":
                title = f"Aniversário da {display_name}"
                body = f"Hoje é aniversário da {display_name}! Deseje parabéns à aniversariante."
            else:
                title = f"Aniversário do peladeiro {display_name}"
                body = f"Hoje é aniversário do peladeiro {display_name}! Deseje parabéns ao aniversariante."
            result = send_player_push_once(
                db, recipient["id"], f"birthday:{birthday['id']}", today.isoformat(),
                title, body, "/notificacoes",
            )
            sent += int(result.get("sent", 0))
    return sent
