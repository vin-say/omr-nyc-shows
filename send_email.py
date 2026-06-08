"""
Email sender for the NYC Concert Tracker newsletter.

Sends an HTML email via Gmail SMTP using an App Password.

Required .env variables:
    GMAIL_SENDER          - the Gmail address sending the email
    GMAIL_APP_PASSWORD    - a 16-character App Password (not your real password)
    NEWSLETTER_RECIPIENT  - the email address to receive the newsletter
"""

import smtplib
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


def _load_email_config() -> dict:
    """Load email credentials from .env."""
    load_dotenv()
    sender = os.getenv("GMAIL_SENDER")
    password = os.getenv("GMAIL_APP_PASSWORD")
    recipient = os.getenv("NEWSLETTER_RECIPIENT")

    missing = []
    if not sender:
        missing.append("GMAIL_SENDER")
    if not password:
        missing.append("GMAIL_APP_PASSWORD")
    if not recipient:
        missing.append("NEWSLETTER_RECIPIENT")

    if missing:
        raise RuntimeError(
            f"Missing required .env variable(s): {', '.join(missing)}. "
            f"See send_email.py docstring for setup instructions."
        )

    return {
        "sender": sender.strip(),
        "password": password.strip(),
        "recipient": recipient.strip(),
    }


def send_newsletter_email(subject: str, html_body: str, plain_body: str | None = None):
    """
    Send the newsletter as an HTML email via Gmail SMTP.

    Args:
        subject: Email subject line.
        html_body: Full HTML content of the newsletter.
        plain_body: Optional plain-text fallback. If not provided, a minimal
                    fallback is generated.
    """
    config = _load_email_config()

    if plain_body is None:
        plain_body = (
            "Your NYC Concert Tracker newsletter is ready.\n"
            "View this email in an HTML-capable client for the full experience."
        )

    # Build the email
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config["sender"]
    msg["To"] = config["recipient"]

    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    # Send via Gmail SMTP over SSL
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
        server.login(config["sender"], config["password"])
        server.sendmail(config["sender"], config["recipient"], msg.as_string())

    print(f"Email sent to {config['recipient']} with subject: {subject}")
