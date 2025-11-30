import logging
import smtplib
from email.message import EmailMessage
from typing import Iterable

from .config import settings

logger = logging.getLogger(__name__)


def send_email(to: Iterable[str], subject: str, body: str) -> None:
    recipients = list(to)
    if not recipients:
        return
    if not settings.smtp_host or not settings.smtp_from:
        logger.warning("SMTP not configured; skipping email send")
        return

    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        if settings.smtp_use_tls:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
                smtp.starttls()
                if settings.smtp_username and settings.smtp_password:
                    smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
                if settings.smtp_username and settings.smtp_password:
                    smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(msg)
    except Exception:
        logger.exception("Failed to send email")


def send_sms(to: Iterable[str], body: str) -> None:
    recipients = list(to)
    if not recipients:
        return

    provider = (settings.sms_provider or "stub").lower()
    if provider == "twilio":
        if not (settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_from_number):
            logger.warning("Twilio SMS not configured; skipping send")
            return
        try:
            from twilio.rest import Client  # type: ignore
        except Exception:
            logger.exception("Twilio dependency missing; install twilio to enable SMS")
            return

        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        for dest in recipients:
            try:
                client.messages.create(body=body, from_=settings.twilio_from_number, to=dest)
            except Exception:
                logger.exception("Failed to send SMS to %s via Twilio", dest)
        return

    # Default stub implementation when no provider is configured
    logger.info("[SMS:%s] Would send to %s: %s", provider, recipients, body)
