"""Email-уведомления о новых «теневых» устройствах.

Отправка через SMTP. Для Яндекса: smtp.yandex.ru:465 (SSL),
логин — полный адрес, пароль — «пароль приложения» (не пароль аккаунта):
yandex.ru/id → Безопасность → Пароли приложений.

ВАЖНО: многие сети (например, университетские) блокируют исходящий
SMTP (порты 25/465/587) — тогда письма не уходят. Рабочий путь: сервер
в сети без такой блокировки.

Если SMTP_PASSWORD не задан — уведомления молча выключены, остальной
функционал агента не затронут. Ошибки отправки не роняют цикл.
"""
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage

from . import config


def enabled() -> bool:
    return bool(config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASSWORD
                and config.MAIL_TO)


def send_email(subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = config.SMTP_USER
    msg["To"] = ", ".join(config.MAIL_TO)
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT,
                          timeout=30) as smtp:
        smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
        smtp.send_message(msg)


def send_shadow_alert(devices: list[dict]) -> bool:
    """Одно письмо со списком новых живых устройств без DHCP lease."""
    if not enabled():
        print("[notify] SMTP не настроен (SMTP_PASSWORD и т.д.) — "
              "уведомление пропущено")
        return False
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"Обнаружено {len(devices)} новых устройств БЕЗ DHCP lease,",
        "прямо сейчас присутствующих в сети лаборатории:",
        "",
    ]
    for d in devices:
        lines.append(
            f"  • IP {d.get('ip', '?')}  MAC {d.get('mac', '?')}  "
            f"hostname: {d.get('hostname') or '—'}  "
            f"категория: {d.get('category', '—')}")
    lines += ["", f"Время опроса: {now}",
              "Дашборд: http://<IP-сервера>:8000"]
    try:
        send_email(f"[NetAgent] Новые теневые устройства: {len(devices)} шт.",
                   "\n".join(lines))
        print(f"[notify] отправлено письмо: {len(devices)} новых устройств")
        return True
    except Exception as exc:
        print(f"[notify] ошибка отправки: {exc}")
        return False
