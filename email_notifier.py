"""
notify.py — shared email alerting for agent-dashboard scripts.

Any agent (excursion checker, wacky-wire-watcher, future ones) imports
send_alert() instead of rolling its own SMTP logic.

Config comes from environment variables so no credentials live in code:

    NOTIFY_SMTP_HOST      e.g. smtp.gmail.com
    NOTIFY_SMTP_PORT      e.g. 587
    NOTIFY_SMTP_USER      login/username for the SMTP account
    NOTIFY_SMTP_PASSWORD  app password / SMTP password
    NOTIFY_FROM_ADDR      From: header (defaults to NOTIFY_SMTP_USER)
    NOTIFY_DEFAULT_TO     default recipient if send_alert() isn't given one

Set these in a .env file (loaded via python-dotenv) or your shell/cron
environment — never hardcode them in an agent script.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

logger = logging.getLogger("notify")


class NotifyConfigError(RuntimeError):
    """Raised when required SMTP config is missing."""


def _get_config() -> dict:
    host = os.environ.get("NOTIFY_SMTP_HOST")
    port = os.environ.get("NOTIFY_SMTP_PORT", "587")
    user = os.environ.get("NOTIFY_SMTP_USER")
    password = os.environ.get("NOTIFY_SMTP_PASSWORD")
    from_addr = os.environ.get("NOTIFY_FROM_ADDR", user)
    default_to = os.environ.get("NOTIFY_DEFAULT_TO")

    missing = [
        name
        for name, val in [
            ("NOTIFY_SMTP_HOST", host),
            ("NOTIFY_SMTP_USER", user),
            ("NOTIFY_SMTP_PASSWORD", password),
        ]
        if not val
    ]
    if missing:
        raise NotifyConfigError(
            f"Missing required env vars: {', '.join(missing)}"
        )

    return {
        "host": host,
        "port": int(port),
        "user": user,
        "password": password,
        "from_addr": from_addr,
        "default_to": default_to,
    }


def send_alert(
    subject: str,
    body: str,
    to: str | None = None,
    attachments: list[str] | None = None,
    dry_run: bool = False,
) -> bool:
    """
    Send a plain-text email alert.

    Returns True on success, False on failure. Never raises for send
    failures (network/auth issues) — logs instead, so a flaky SMTP
    connection doesn't crash a scheduled agent run. Raises
    NotifyConfigError only for missing configuration, since that's a
    setup bug worth surfacing loudly.

    dry_run=True skips the actual send and just logs what would have
    been sent — useful when testing an agent's formatting logic.
    """
    config = _get_config()
    recipient = to or config["default_to"]
    if not recipient:
        raise NotifyConfigError(
            "No recipient given and NOTIFY_DEFAULT_TO is not set"
        )

    if dry_run:
        logger.info(
            "[DRY RUN] Would send to %s | subject=%r | body=\n%s",
            recipient,
            subject,
            body,
        )
        return True

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config["from_addr"]
    msg["To"] = recipient
    msg.set_content(body)

    for path in (attachments or []):
        data = Path(path).read_bytes()
        mime, _ = mimetypes.guess_type(path)
        maintype, subtype = (mime or "application/octet-stream").split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype,
                           filename=os.path.basename(path))

    try:
        with smtplib.SMTP(config["host"], config["port"], timeout=15) as smtp:
            smtp.starttls()
            smtp.login(config["user"], config["password"])
            smtp.send_message(msg)
        logger.info("Alert sent to %s: %s", recipient, subject)
        return True
    except Exception:
        logger.exception("Failed to send alert to %s", recipient)
        return False


if __name__ == "__main__":
    # Quick manual smoke test: python notify.py
    logging.basicConfig(level=logging.INFO)
    ok = send_alert(
        subject="notify.py test",
        body="If you're reading this, the shared email module works.",
        dry_run=True,
    )
    print("dry-run send_alert() returned:", ok)
