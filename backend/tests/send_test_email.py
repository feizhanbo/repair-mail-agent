# -*- coding: utf-8 -*-
"""Standalone, explicitly invoked SMTP smoke-test helper.

Credentials and message endpoints must be supplied through environment
variables. This file is not collected as an automated pytest test and must
never contain production or test-account secrets.
"""

from __future__ import annotations

import os
import smtplib
import sys
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def main() -> None:
    smtp_host = _required_env("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))
    smtp_user = _required_env("SMTP_USER")
    smtp_password = _required_env("SMTP_PASSWORD")
    sender = os.environ.get("SMTP_TEST_FROM", smtp_user).strip()
    recipient = _required_env("SMTP_TEST_TO")
    attachment_path = _required_env("SMTP_TEST_ATTACHMENT_PATH")

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = os.environ.get("SMTP_TEST_SUBJECT", "Repair mail SMTP smoke test")
    msg.attach(MIMEText("This is an explicitly requested SMTP smoke test.", "plain", "utf-8"))

    filename = os.path.basename(attachment_path)
    with open(attachment_path, "rb") as file_handle:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(file_handle.read())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=("utf-8", "", filename))
    msg.attach(part)

    with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
        server.login(smtp_user, smtp_password)
        server.send_message(msg)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"SMTP smoke test failed: {exc}")
        sys.exit(1)
