# -*- coding: utf-8 -*-
"""
Standalone script: send a test warranty email from rmatest2 to rmatest1
bypassing the app's mail_safety security gates.

Usage:
    cd backend
    python tests\send_test_email.py
"""

from __future__ import annotations

import os
import smtplib
import sys
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ---------------------------------------------------------------------------
# SMTP credentials — rmatest2 独立凭据（不可用 rmatest1 代发）
# ---------------------------------------------------------------------------
_SMTP_HOST = "smtphz.qiye.163.com"
_SMTP_PORT = 465
_SMTP_USER = "rmatest2@accotest.com"
_SMTP_PASSWORD = "SL@M9S5@zXWJ2nM1"

_FROM = "rmatest2@accotest.com"
_TO = "rmatest1@accotest.com"

_ATTACHMENT_PATH = r"D:\refile\emlattachment\05_15个SN_动态扩展测试.xlsx"

# ---------------------------------------------------------------------------
# Email body (plain text, Chinese)
# ---------------------------------------------------------------------------
_BODY = (
    "您好，\n"
    "\n"
    "我司有3块同型号设备出现故障需要报修，详细信息如下：\n"
    "\n"
    "客户名称: 成都保鼎科技有限公司\n"
    "联系人: 张三\n"
    "联系电话: 13800138000\n"
    "邮箱: zhangsan@test.com\n"
    "寄件地址: 北京市朝阳区测试路100号\n"
    "\n"
    "故障描述: 设备上电后无任何反应，电源指示灯不亮，疑似电源模块损坏。\n"
    "\n"
    "详见附件清单。"
)


# ===================================================================
def main() -> None:
    """Construct the MIME message, attach the Excel file, and send."""

    # -- Build multipart message ------------------------------------
    msg = MIMEMultipart()
    msg["From"] = _FROM
    msg["To"] = _TO
    msg["Subject"] = "报修申请 - 3块STM32测试板故障 [E2E TEST]"

    msg.attach(MIMEText(_BODY, "plain", "utf-8"))

    # -- Attach Excel file ------------------------------------------
    filename = os.path.basename(_ATTACHMENT_PATH)
    with open(_ATTACHMENT_PATH, "rb") as fh:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(fh.read())
    encoders.encode_base64(part)
    # Use RFC 2231 encoding for the Chinese filename
    part.add_header(
        "Content-Disposition",
        "attachment",
        filename=("utf-8", "", filename),
    )
    msg.attach(part)

    # -- Send via SMTP_SSL (port 465) --------------------------------
    print(f"Sending from {_FROM} to {_TO} via {_SMTP_HOST}:{_SMTP_PORT} ...")
    with smtplib.SMTP_SSL(_SMTP_HOST, _SMTP_PORT) as server:
        server.login(_SMTP_USER, _SMTP_PASSWORD)
        server.send_message(msg)

    print("Email sent successfully!")


# ===================================================================
if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}")
        sys.exit(1)
