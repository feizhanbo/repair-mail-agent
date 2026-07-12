# -*- coding: utf-8 -*-
"""
IMAP & SMTP 集成测试脚本

测试对象: http://127.0.0.1:8000
Phase B: IMAP 邮件拉取
Phase C: SMTP 自动发送（含配置切换 + 回复草稿 + 状态检查 + 配置回滚）
"""

from __future__ import annotations

import json
import sys
import traceback
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE = "http://127.0.0.1:8000"
TOKEN: str | None = None

IDENT = " " * 2
PASS = "✓"
FAIL = "✗"
WARN = "△"


def _hdr(label: str) -> None:
    print()
    print("=" * 72)
    print(f"  {label}")
    print("=" * 72)


def _sub(label: str) -> None:
    print(f"\n--- {label} ---")


def _ok(label: str) -> None:
    print(f"{IDENT}{PASS} {label}")


def _err(label: str) -> None:
    print(f"{IDENT}{FAIL} {label}")


def _warn(label: str) -> None:
    print(f"{IDENT}{WARN} {label}")


def _req(method: str, path: str, *, body: dict | None = None, params: dict | None = None) -> dict | list:
    url = BASE + path
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    data_bytes = json.dumps(body).encode("utf-8") if body else None
    req = Request(url, data=data_bytes, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            content = resp.read()
            if not content:
                return {}
            return json.loads(content.decode("utf-8"))
    except HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(err_body)
        except json.JSONDecodeError:
            raise RuntimeError(f"{method} {path} → HTTP {exc.code} raw={err_body}") from exc
    except URLError as exc:
        raise RuntimeError(f"无法连接 {url}: {exc}") from exc


# ──────────────────────────────────────────────────────────────
# Phase A: 登录获取 Token
# ──────────────────────────────────────────────────────────────
def phase_login() -> bool:
    global TOKEN
    _hdr("Phase A: 登录获取 JWT Token")

    try:
        resp = _req("POST", "/api/v1/auth/login", body={"username": "admin", "password": "repair-admin-2026"})
    except Exception as exc:
        _err(f"登录请求失败: {exc}")
        return False

    if not resp.get("success"):
        _err(f"登录失败: {resp.get('message', resp)}")
        print(f"{IDENT}   完整响应: {json.dumps(resp, ensure_ascii=False, indent=2)}")
        return False

    data = resp.get("data", {})
    token = data.get("access_token")
    if not token:
        _err("响应中未找到 access_token")
        return False

    TOKEN = token
    user = data.get("user", {})
    _ok(f"登录成功 → 用户: {user.get('username')} ({user.get('real_name')})")
    _ok(f"角色: {user.get('roles')}")
    _ok(f"Token 前缀: {token[:30]}...")
    return True


# ──────────────────────────────────────────────────────────────
# Phase B: IMAP 邮件拉取测试
# ──────────────────────────────────────────────────────────────
def phase_imap_fetch() -> dict | None:
    _hdr("Phase B: IMAP 邮件拉取测试")

    params = {
        "folder_name": "INBOX",
        "limit": 5,
        "unseen_only": "true",
        "auto_parse": "true",
        "archive_to_oss": "true",
    }
    print(f"{IDENT}请求参数: {json.dumps(params, ensure_ascii=False)}")

    try:
        resp = _req("POST", "/api/v1/emails/fetch-now", params=params)
    except Exception as exc:
        _err(f"fetch-now 请求失败: {exc}")
        return None

    print(f"{IDENT}响应: {json.dumps(resp, ensure_ascii=False, indent=2)}")

    if not resp.get("success"):
        _err(f"IMAP fetch 失败: {resp.get('message', resp)}")
        return None

    data = resp.get("data", {})
    _ok("IMAP fetch 调用成功")
    for key in ("job_id", "processed_count", "success_count", "failed_count"):
        value = data.get(key)
        icon = PASS if value is not None else FAIL
        print(f"{IDENT}{icon} {key} = {value}")

    return data


def phase_check_job_logs() -> None:
    _hdr("Phase B.2: 查看 job_run_logs (IMAP 任务日志)")

    try:
        resp = _req("GET", "/api/v1/db-browser/tables/job_run_logs/rows", params={"page": 1, "page_size": 10})
    except Exception as exc:
        _err(f"查询 job_run_logs 失败: {exc}")
        return

    success = resp.get("success")
    data = resp.get("data", {})

    if not success:
        _err(f"查询失败: {resp.get('message', resp)}")
        return

    items = data.get("items", data.get("rows", []))
    total = data.get("total", len(items))

    print(f"{IDENT}最近任务日志 (共 {total} 条):")
    if not items:
        _warn("无任务日志记录")
        return

    for i, row in enumerate(items):
        print(f"\n{IDENT}--- 日志 #{i + 1} ---")
        if isinstance(row, dict):
            for key in ("id", "job_type", "job_status", "trigger_source", "started_at", "completed_at", "result_summary", "error_message"):
                if key in row:
                    print(f"{IDENT}    {key}: {row[key]}")
        else:
            print(f"{IDENT}    {row}")

    _ok("job_run_logs 查询完成")


# ──────────────────────────────────────────────────────────────
# Phase C: SMTP 自动发送测试
# ──────────────────────────────────────────────────────────────
def phase_config_enable_auto_send() -> bool:
    _hdr("Phase C.1: 开启自动发送配置")

    body = {"auto_send_enabled": True, "reply_send_mode": "auto_send"}
    print(f"{IDENT}请求: {json.dumps(body, ensure_ascii=False)}")

    try:
        resp = _req("PATCH", "/api/v1/system/config", body=body)
    except Exception as exc:
        _err(f"配置更新失败: {exc}")
        return False

    print(f"{IDENT}响应: {json.dumps(resp, ensure_ascii=False, indent=2)}")

    if not resp.get("success"):
        _err(f"配置更新失败: {resp.get('message', resp)}")
        return False

    data = resp.get("data", {})
    _ok(f"auto_send_enabled = {data.get('auto_send_enabled')}")
    _ok(f"reply_send_mode  = {data.get('reply_send_mode')}")
    return True


def phase_find_ticket() -> dict | None:
    _hdr("Phase C.2: 查找可测试的工单")

    statuses = ["need_customer_info", "parsed", "manual_review", "auto_replied", "ready_for_export"]
    for status_code in statuses:
        _sub(f"查询 status_code={status_code}")
        try:
            resp = _req("GET", "/api/v1/tickets", params={"status_code": status_code, "page": 1, "page_size": 1})
        except Exception as exc:
            _err(f"查询失败: {exc}")
            continue

        if not resp.get("success"):
            _warn(f"查询返回失败: {resp.get('message', resp)}")
            continue

        data = resp.get("data", {})
        items = data.get("items", [])
        total = data.get("total", 0)

        print(f"{IDENT}total={total}, 返回 items={len(items)}")

        if items:
            ticket = items[0]
            print(f"{IDENT}找到工单: id={ticket.get('id')}, ticket_no={ticket.get('ticket_no')}, status={ticket.get('current_status_code')}")
            _ok(f"使用工单 id={ticket['id']}")
            return ticket
        else:
            _warn(f"未找到 status_code={status_code} 的工单")

    _sub("查询全部工单（不限状态）")
    try:
        resp = _req("GET", "/api/v1/tickets", params={"page": 1, "page_size": 1})
    except Exception as exc:
        _err(f"查询失败: {exc}")
        return None

    if not resp.get("success"):
        _err(f"查询返回失败: {resp.get('message', resp)}")
        return None

    data = resp.get("data", {})
    items = data.get("items", [])
    total = data.get("total", 0)

    print(f"{IDENT}total={total}, 返回 items={len(items)}")

    if items:
        ticket = items[0]
        print(f"{IDENT}找到工单: id={ticket.get('id')}, ticket_no={ticket.get('ticket_no')}, status={ticket.get('current_status_code')}")
        _ok(f"使用工单 id={ticket['id']} (状态: {ticket.get('current_status_code')})")
        return ticket

    _err("未找到任何工单")
    return None


def phase_create_draft(ticket: dict) -> dict | None:
    _hdr("Phase C.3: 创建回复草稿")

    ticket_id = ticket["id"]
    body = {"reply_type": "missing_fields"}
    print(f"{IDENT}ticket_id = {ticket_id}")
    print(f"{IDENT}请求: {json.dumps(body, ensure_ascii=False)}")

    try:
        resp = _req("POST", f"/api/v1/replies/{ticket_id}/draft", body=body)
    except Exception as exc:
        _err(f"创建草稿失败: {exc}")
        return None

    print(f"{IDENT}响应: {json.dumps(resp, ensure_ascii=False, indent=2)}")

    if not resp.get("success"):
        _err(f"创建草稿失败: {resp.get('message', resp)}")
        return None

    data = resp.get("data", {})
    reply = data.get("reply", {})

    _ok(f"草稿创建成功")
    _ok(f"  reply.id          = {reply.get('id')}")
    _ok(f"  reply_type        = {reply.get('reply_type')}")
    _ok(f"  review_status     = {reply.get('review_status')}")
    _ok(f"  send_status       = {reply.get('send_status')}")
    _ok(f"  generate_source   = {reply.get('generate_source')}")
    _ok(f"  auto_send_enabled = {data.get('auto_send_enabled')}")
    _ok(f"  reply_send_mode   = {data.get('reply_send_mode')}")

    if reply.get("send_status") == "sent":
        _ok("🎉 草稿已自动发送 (auto_send 生效)!")
        _ok(f"  smtp_message_id = {reply.get('smtp_message_id')}")
        _ok(f"  sent_at         = {reply.get('sent_at')}")
    elif reply.get("send_status") == "send_failed":
        _warn(f"自动发送失败: {reply.get('error_message')}")
    elif reply.get("send_status") == "pending_review":
        _warn("草稿状态为 pending_review，未触发自动发送")

    return data


def phase_config_reset() -> bool:
    _hdr("Phase C.5: 恢复配置为 human_review")

    body = {"auto_send_enabled": False, "reply_send_mode": "human_review"}
    print(f"{IDENT}请求: {json.dumps(body, ensure_ascii=False)}")

    try:
        resp = _req("PATCH", "/api/v1/system/config", body=body)
    except Exception as exc:
        _err(f"配置恢复失败: {exc}")
        return False

    if not resp.get("success"):
        _err(f"配置恢复失败: {resp.get('message', resp)}")
        return False

    data = resp.get("data", {})
    _ok(f"auto_send_enabled = {data.get('auto_send_enabled')}")
    _ok(f"reply_send_mode  = {data.get('reply_send_mode')}")
    return True


def phase_verify_config() -> None:
    _hdr("Phase C.6: 验证配置确已恢复")

    try:
        resp = _req("GET", "/api/v1/system/config")
    except Exception as exc:
        _err(f"查询配置失败: {exc}")
        return

    data = resp.get("data", {})
    print(f"{IDENT}当前配置:")
    print(f"{IDENT}  auto_send_enabled = {data.get('auto_send_enabled')}")
    print(f"{IDENT}  reply_send_mode   = {data.get('reply_send_mode')}")
    print(f"{IDENT}  smtp_configured   = {data.get('integrations', {}).get('smtp_configured')}")
    print(f"{IDENT}  imap_configured   = {data.get('integrations', {}).get('imap_configured')}")


# ──────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────
def main() -> int:
    print("=" * 72)
    print("  IMAP & SMTP 集成测试")
    print(f"  后端地址: {BASE}")
    print("=" * 72)

    # --- Phase A ---
    if not phase_login():
        print("\n登录失败，后续测试无法继续。")
        return 1

    # --- Phase B ---
    phase_imap_fetch()
    phase_check_job_logs()

    # --- Phase C ---
    if not phase_config_enable_auto_send():
        print("\n无法启用 auto_send，跳过 SMTP 测试。")
        try:
            phase_config_reset()
        except Exception:
            pass
        return 2

    ticket = phase_find_ticket()
    if ticket is None:
        print("\n未找到可测试工单，跳过草稿测试，恢复配置。")
        phase_config_reset()
        phase_verify_config()
        return 3

    phase_create_draft(ticket)

    # --- 恢复配置 ---
    phase_config_reset()
    phase_verify_config()

    # --- 总结 ---
    _hdr("测试总结")
    print(f"{IDENT}所有测试阶段已完成。")
    print(f"{IDENT}请在上方日志中查看各阶段详细结果。")
    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
        sys.exit(exit_code)
    except Exception:
        print("\n" + "!" * 72)
        print("  未预期的异常:")
        traceback.print_exc()
        print("!" * 72)
        sys.exit(99)
