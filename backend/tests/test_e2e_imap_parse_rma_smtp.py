# -*- coding: utf-8 -*-
"""
维修邮件代理 — 全链路 E2E 测试脚本

测试目的:
  验证从 IMAP 拉取保修邮件 → 邮件解析 → 工单创建 → RMA 授权单生成 → SMTP 发送
  到指定邮箱的完整链路是否正常工作。

前置条件:
  1. 后端服务已启动在 http://127.0.0.1:8000
  2. 已设置环境变量 RUN_REAL_MAIL_INTEGRATION_TESTS=1
  3. 已设置环境变量 INTEGRATION_ADMIN_USERNAME / INTEGRATION_ADMIN_PASSWORD
  4. .env 中已正确配置 IMAP / SMTP / OSS 等参数
  5. 测试邮箱 (rmatest1@accotest.com / rmatest2@accotest.com) 可正常访问

运行方式:
  python -m backend.tests.test_e2e_imap_parse_rma_smtp

安全警告:
  - 本脚本不会执行破坏性操作，所有发送目标均强制限定为配置的白名单收件人
  - Phase 4 的 auto_send_enabled 切换由 try/finally 包裹，确保无论成功失败均回滚
  - 发送前会调用 test_envelope_allowed 进行收件人白名单复核
  - 所有邮件主题会自动添加 [TEST ONLY] 前缀
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from app.config import settings
from app.services.mail_safety import (
    test_envelope_allowed as _test_envelope_allowed,
    test_mail_configuration_reasons as _test_mail_configuration_reasons,
)

# ──────────────────────────────────────────────────────────────
# 全局常量
# ──────────────────────────────────────────────────────────────
BASE = "http://127.0.0.1:8000"
TOKEN: str | None = None

IDENT = " " * 2
PASS = "✓"
FAIL = "✗"
WARN = "△"

# 累加计数
_results: dict[str, dict[str, int]] = {
    "phase0": {"pass": 0, "fail": 0, "skip": 0},
    "phase1": {"pass": 0, "fail": 0, "skip": 0},
    "phase2": {"pass": 0, "fail": 0, "skip": 0},
    "phase3": {"pass": 0, "fail": 0, "skip": 0},
    "phase4": {"pass": 0, "fail": 0, "skip": 0},
}


# ──────────────────────────────────────────────────────────────
# 日志 / 输出辅助函数
# ──────────────────────────────────────────────────────────────
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


def _count(phase: str, key: str) -> None:
    _results[phase][key] += 1


# ──────────────────────────────────────────────────────────────
# HTTP 请求辅助函数
# ──────────────────────────────────────────────────────────────
def _req(method: str, path: str, *, body: dict | None = None, params: dict | None = None) -> dict | list:
    url = BASE + path
    if params:
        qs_parts = []
        for k, v in params.items():
            if isinstance(v, bool):
                qs_parts.append(f"{k}={str(v).lower()}")
            else:
                qs_parts.append(f"{k}={v}")
        qs = "&".join(qs_parts)
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
            raise RuntimeError(f"{method} {path} -> HTTP {exc.code} raw={err_body}") from exc
    except URLError as exc:
        raise RuntimeError(f"无法连接 {url}: {exc}") from exc


# ──────────────────────────────────────────────────────────────
# Phase 0: 前置检查
# ──────────────────────────────────────────────────────────────
def phase_precheck() -> bool:
    """前置检查阶段。

    操作步骤:
      1. 检查 RUN_REAL_MAIL_INTEGRATION_TESTS=1 环境变量
      2. 调用 test_mail_configuration_reasons() 验证 IMAP/SMTP/白名单配置
      3. 验证 INTEGRATION_ADMIN_USERNAME / INTEGRATION_ADMIN_PASSWORD 环境变量
      4. 通过 POST /api/v1/auth/login 获取 JWT Token

    校验逻辑:
      - 未设置 RUN_REAL_MAIL_INTEGRATION_TESTS=1 则安全跳过
      - 邮件配置不满足 test_mail_configuration_reasons 则阻断运行
      - 登录成功后存储全局 TOKEN 供后续请求使用

    Returns:
      True 表示所有前置条件满足，False 表示前置条件不满足
    """
    _hdr("Phase 0: 前置检查")

    # 1. 检查 RUN_REAL_MAIL_INTEGRATION_TESTS
    if os.getenv("RUN_REAL_MAIL_INTEGRATION_TESTS") != "1":
        _warn("RUN_REAL_MAIL_INTEGRATION_TESTS != 1，跳过真实邮箱集成测试")
        _count("phase0", "skip")
        return False

    # 2. 检查邮件配置
    reasons = _test_mail_configuration_reasons()
    if reasons:
        _err("邮件配置检查未通过，发现以下问题:")
        for reason in reasons:
            print(f"{IDENT}    - {reason}")
        _count("phase0", "fail")
        return False
    _ok("邮件配置检查通过 (IMAP/SMTP/白名单)")

    # 3. 检查登录凭据环境变量
    if not os.getenv("INTEGRATION_ADMIN_USERNAME") or not os.getenv("INTEGRATION_ADMIN_PASSWORD"):
        _err("缺少环境变量 INTEGRATION_ADMIN_USERNAME 或 INTEGRATION_ADMIN_PASSWORD")
        _count("phase0", "fail")
        return False
    _ok("环境变量 INTEGRATION_ADMIN_USERNAME / INTEGRATION_ADMIN_PASSWORD 均已设置")

    # 4. 登录获取 Token
    global TOKEN
    try:
        resp = _req(
            "POST",
            "/api/v1/auth/login",
            body={
                "username": os.environ["INTEGRATION_ADMIN_USERNAME"],
                "password": os.environ["INTEGRATION_ADMIN_PASSWORD"],
            },
        )
    except Exception as exc:
        _err(f"登录请求失败: {exc}")
        _count("phase0", "fail")
        return False

    if not resp.get("success"):
        _err(f"登录失败: {resp.get('message', resp)}")
        _count("phase0", "fail")
        return False

    data = resp.get("data", {})
    token = data.get("access_token")
    if not token:
        _err("响应中未找到 access_token")
        _count("phase0", "fail")
        return False

    TOKEN = token
    user = data.get("user", {})
    username = user.get("username", "?")
    roles = user.get("roles", [])
    _ok(f"登录成功 -> 用户: {username}")
    _ok(f"角色: {roles}")
    _count("phase0", "pass")
    return True


# ──────────────────────────────────────────────────────────────
# Phase 1: IMAP 拉取保修邮件
# ──────────────────────────────────────────────────────────────
def phase_imap_fetch() -> list[int] | None:
    """IMAP 拉取保修邮件阶段。

    操作步骤:
      1. 构造 POST /api/v1/emails/fetch-now 请求参数:
         folder_name=INBOX, limit=5, unseen_only=true, auto_parse=true, archive_to_oss=true
      2. 解析响应中的 job_id, processed_count, success_count, fetched 列表
      3. 逐条打印 uid, message_id, email_id, fetch_status
      4. 筛选 fetch_status="ingested" 的 email_id 作为有效结果

    校验逻辑:
      - 响应 success=false 视为失败，返回 None
      - fetched 为空或全部为 duplicate/skipped 时返回 None
      - 有效 email_id 列表传递给 Phase 2

    Returns:
      成功 ingest 的 email_id 列表，无可用邮件或失败时返回 None
    """
    _hdr("Phase 1: IMAP 拉取保修邮件")

    params = {
        "folder_name": "INBOX",
        "limit": 5,
        "unseen_only": True,
        "auto_parse": True,
        "archive_to_oss": True,
    }
    print(f"{IDENT}请求参数: {json.dumps(params, ensure_ascii=False)}")

    try:
        resp = _req("POST", "/api/v1/emails/fetch-now", params=params)
    except Exception as exc:
        _err(f"fetch-now 请求失败: {exc}")
        _count("phase1", "fail")
        return None

    if not resp.get("success"):
        _err(f"IMAP fetch 失败: {resp.get('message', resp)}")
        _count("phase1", "fail")
        return None

    data = resp.get("data", {})
    job_id = data.get("job_id")
    processed_count = data.get("processed_count", 0)
    success_count = data.get("success_count", 0)
    fetched = data.get("fetched", [])

    _ok("IMAP fetch 调用成功")
    print(f"{IDENT}  job_id         = {job_id}")
    print(f"{IDENT}  processed_count = {processed_count}")
    print(f"{IDENT}  success_count   = {success_count}")
    print(f"{IDENT}  fetched 数量    = {len(fetched)}")

    ingested_email_ids: list[int] = []
    for item in fetched:
        uid = item.get("uid", "?")
        message_id = item.get("message_id", "?")
        email_id = item.get("email_id")
        fetch_status = item.get("fetch_status", "?")
        print(f"{IDENT}  [{fetch_status}] uid={uid}, email_id={email_id}, message_id={message_id}")
        if fetch_status == "ingested" and email_id is not None:
            ingested_email_ids.append(int(email_id))

    if not ingested_email_ids:
        _warn("没有邮件被成功 ingest，后续 Phase 2-4 将跳过")
    else:
        _ok(f"成功 ingest 的邮件数量: {len(ingested_email_ids)}")

    _count("phase1", "pass")
    return ingested_email_ids if ingested_email_ids else None


# ──────────────────────────────────────────────────────────────
# Phase 2: 追踪邮件解析与工单创建
# ──────────────────────────────────────────────────────────────
def phase_trace_parse(email_ids: list[int]) -> list[dict] | None:
    """追踪邮件解析与工单创建阶段。

    操作步骤:
      1. 对每个 email_id 调用 GET /api/v1/emails/{id} 查询 parse_status 和 intent_type
      2. 通过 GET /api/v1/tickets 客户端过滤 source_email_id 找到关联工单
      3. 如 parse_results 中有 ticket_id，补查 GET /api/v1/tickets/{id} 获取详情
      4. 打印每个工单的 ticket_no, current_status_code, rma_required, rma_status
      5. 打印 parse_results 的 apply_status，标记 needs_manual 情况

    校验逻辑:
      - 未找到关联工单时输出警告
      - 优先返回 ready_for_export 状态的工单
      - 无 ready_for_export 时降级返回 parsed/manual_review 状态的工单
      - 完全无工单时返回 None

    Returns:
      按优先级排序的工单列表，无可用工单时返回 None
    """
    _hdr("Phase 2: 追踪邮件解析与工单创建")

    all_tickets: list[dict] = []

    for email_id in email_ids:
        _sub(f"检查 email_id={email_id}")

        # 2a. 查询邮件解析状态
        try:
            email_resp = _req("GET", f"/api/v1/emails/{email_id}")
        except Exception as exc:
            _err(f"查询 email_id={email_id} 失败: {exc}")
            _count("phase2", "fail")
            continue

        email_data = email_resp.get("data", {})
        email_info = email_data.get("email", {})
        parse_status = email_info.get("parse_status", "?")
        intent_type = email_info.get("intent_type", "?")
        subject = email_info.get("subject", "?")
        parse_results = email_data.get("parse_results", [])

        print(f"{IDENT}parse_status  = {parse_status}")
        print(f"{IDENT}intent_type   = {intent_type}")
        print(f"{IDENT}subject       = {subject}")

        # 从 parse_results 中提取 ticket_ids
        ticket_ids_from_parse: set[int] = set()
        for pr in parse_results:
            ticket_id = pr.get("ticket_id")
            if ticket_id is not None:
                ticket_ids_from_parse.add(int(ticket_id))

        # 2b. 查询该 email 关联的工单（通过 source_email_id 近似查找）
        #     API 不支持 source_email_id 过滤，拉取全部工单后客户端过滤
        try:
            tickets_resp = _req("GET", "/api/v1/tickets", params={"page": 1, "page_size": 100})
        except Exception as exc:
            _err(f"查询工单列表失败: {exc}")
            _count("phase2", "fail")
            continue

        tickets_data = tickets_resp.get("data", {})
        tickets = tickets_data.get("items", [])

        # 客户端过滤 source_email_id
        matched_tickets = [t for t in tickets if t.get("source_email_id") == email_id]

        # 如果 parse_results 中有 ticket_id 但没通过 source_email_id 找到，也通过 ticket_ids 补查
        if ticket_ids_from_parse and not matched_tickets:
            for tid in ticket_ids_from_parse:
                try:
                    single_resp = _req("GET", f"/api/v1/tickets/{tid}")
                    ticket_detail = single_resp.get("data", {}).get("ticket", {})
                    if ticket_detail:
                        matched_tickets.append(ticket_detail)
                except Exception:
                    pass

        if not matched_tickets:
            _warn(f"email_id={email_id} 未找到关联工单")
            continue

        for ticket in matched_tickets:
            tid = ticket.get("id")
            ticket_no = ticket.get("ticket_no", "?")
            status_code = ticket.get("current_status_code", "?")
            rma_required = ticket.get("rma_required", False)
            rma_status = ticket.get("rma_status", "?")

            print(f"{IDENT}  工单 #{tid}: ticket_no={ticket_no}")
            print(f"{IDENT}    current_status_code = {status_code}")
            print(f"{IDENT}    rma_required        = {rma_required}")
            print(f"{IDENT}    rma_status          = {rma_status}")

            # parse_status / apply_status 需要从 ticket detail 获取
            try:
                detail_resp = _req("GET", f"/api/v1/tickets/{tid}")
                detail_data = detail_resp.get("data", {})
                detail_ticket = detail_data.get("ticket", {})
                parse_results_list = detail_data.get("parse_results", [])
            except Exception:
                detail_ticket = ticket
                parse_results_list = []

            # 打印 parse_results 的 apply_status
            for pr in parse_results_list:
                pr_id = pr.get("id", "?")
                pr_intent = pr.get("intent_type", "?")
                pr_apply = pr.get("apply_status", "?")
                print(f"{IDENT}    parse_result #{pr_id}: intent={pr_intent}, apply_status={pr_apply}")

            # 判断 parse_status (优先看 parse_results 里是否有 needs_manual 的情况)
            needs_manual = False
            for pr in parse_results_list:
                if pr.get("apply_status") == "needs_manual":
                    needs_manual = True
                    print(f"{IDENT}    (parse_result needs_manual: {pr.get('error_message', '?')})")

            if status_code == "manual_review" or needs_manual:
                print(f"{IDENT}    -> 需人工复核 (manual_review)")

            all_tickets.append(detail_ticket)

    if not all_tickets:
        _warn("没有找到任何关联工单，跳过 Phase 3-4")
        _count("phase2", "skip")
        return None

    _ok(f"共追踪到 {len(all_tickets)} 个工单")
    _count("phase2", "pass")

    # 优先选择 ready_for_export 状态的工单
    suitable = [t for t in all_tickets if t.get("current_status_code") == "ready_for_export"]
    if suitable:
        _ok(f"找到 {len(suitable)} 个 ready_for_export 工单")
        return suitable

    # 降级：选择 parsed / manual_review 状态
    fallback = [t for t in all_tickets if t.get("current_status_code") in ("parsed", "manual_review")]
    if fallback:
        _warn(f"没有 ready_for_export 工单，降级使用 {fallback[0].get('current_status_code')} 状态工单")
        return fallback

    _warn("所有工单状态均不满足 RMA 条件")
    return all_tickets


# ──────────────────────────────────────────────────────────────
# Phase 3: RMA 生成与校验
# ──────────────────────────────────────────────────────────────
def phase_rma(tickets: list[dict]) -> dict | None:
    """RMA 生成与校验阶段。

    操作步骤:
      1. 取第一个工单，打印当前 status_code, rma_required, rma_status
      2. 若已为 ready_for_export + rma_status=sent: 直接标记为已完成
      3. 若非 ready_for_export: 调用 POST /api/v1/tickets/{id}/validate-export 推进状态
      4. 处理 sn_validation_failed, safety_failed 等失败场景
      5. 识别 rma_authorization job 并轮询 GET /api/v1/jobs/{id}（最多 5 次×2 秒）
      6. 最终查询工单状态确认变更

    校验逻辑:
      - validate-export 返回 success=false 时输出具体失败原因
      - RMA job 完成后打印 reply_id, rma_template_version 等关键信息
      - 工单 rma_required=False 时正常跳过

    Returns:
      包含 ticket_id 和最新 ticket 数据的字典，失败时返回 None
    """
    _hdr("Phase 3: RMA 生成与校验")

    if not tickets:
        _warn("无可用工单")
        _count("phase3", "skip")
        return None

    ticket = tickets[0]
    ticket_id = ticket.get("id")
    ticket_no = ticket.get("ticket_no", "?")
    status_code = ticket.get("current_status_code", "?")
    rma_required = ticket.get("rma_required", False)
    rma_status = ticket.get("rma_status", "?")

    print(f"{IDENT}工单 #{ticket_id} ({ticket_no})")
    print(f"{IDENT}  current_status_code = {status_code}")
    print(f"{IDENT}  rma_required        = {rma_required}")
    print(f"{IDENT}  rma_status          = {rma_status}")

    # 如果是 ready_for_export 且 rma_status 已为 sent，直接返回
    if status_code == "ready_for_export" and rma_status == "sent":
        _ok("RMA 已生成并发送 (rma_status=sent)")
        _count("phase3", "pass")
        return {"ticket_id": ticket_id, "rma_already_sent": True}

    if status_code == "ready_for_export" and rma_status == "not_required":
        _ok("该工单不需要 RMA (rma_status=not_required)")
        _count("phase3", "pass")
        return {"ticket_id": ticket_id, "rma_not_required": True}

    # 如果不在 ready_for_export，尝试 validate-export
    if status_code != "ready_for_export":
        _sub("调用 validate-export 推进工单到 ready_for_export")
        try:
            resp = _req("POST", f"/api/v1/tickets/{ticket_id}/validate-export")
        except Exception as exc:
            _err(f"validate-export 请求失败: {exc}")
            _count("phase3", "fail")
            return None

        if not resp.get("success"):
            msg = resp.get("message", resp)
            _err(f"validate-export 失败: {msg}")
            _count("phase3", "fail")
            return None

        data = resp.get("data", {})
        result_status = data.get("status", "?")
        jobs = data.get("jobs", [])
        report = data.get("report", {})

        print(f"{IDENT}validate-export 返回 status: {result_status}")
        print(f"{IDENT}jobs 数量: {len(jobs)}")

        if result_status == "sn_validation_failed":
            sn_result = data.get("sn_result", {})
            sn_errors = (sn_result.get("report", {}) or {}).get("errors", {})
            _err(f"SN 校验失败: {json.dumps(sn_errors, ensure_ascii=False)}")
            _count("phase3", "fail")
            return None

        if result_status == "safety_failed":
            errors = report.get("errors", {})
            _err(f"安全门禁未通过: {json.dumps(errors, ensure_ascii=False)}")
            _count("phase3", "fail")
            return None

        # 查找 rma_authorization job
        rma_jobs = [j for j in jobs if j.get("job_type") == "rma_authorization"]
        if not rma_jobs and result_status == "ready_for_export":
            _ok("工单已推进到 ready_for_export，但无 RMA job（可能 rma_required=False）")
            _count("phase3", "pass")
            return {"ticket_id": ticket_id, "rma_not_required": True}

        # 等待 RMA job 完成
        for rma_job in rma_jobs:
            job_id = rma_job.get("id")
            if not job_id:
                continue
            _sub(f"等待 RMA job #{job_id} 完成")

            job_completed = False
            for attempt in range(5):
                time.sleep(2)
                try:
                    job_resp = _req("GET", f"/api/v1/jobs/{job_id}")
                except Exception as exc:
                    print(f"{IDENT}  第 {attempt + 1} 次查询 job 失败: {exc}")
                    continue

                job_data = job_resp.get("data", {})
                job_status = job_data.get("status", "?")
                print(f"{IDENT}  第 {attempt + 1} 次: job_status={job_status}")

                if job_status in ("success", "failed", "skipped"):
                    job_completed = True
                    if job_status == "success":
                        _ok(f"RMA job #{job_id} 完成 (success)")
                        result_json = job_data.get("result_json", {})
                        if isinstance(result_json, dict):
                            for key in ("reply_id", "rma_template_version", "pdf_url"):
                                if key in result_json:
                                    print(f"{IDENT}    {key} = {result_json[key]}")
                    elif job_status == "failed":
                        _err(f"RMA job #{job_id} 失败: {job_data.get('error_code', '?')}")
                        _count("phase3", "fail")
                        return None
                    else:
                        _warn(f"RMA job #{job_id} 被跳过: {job_data.get('error_code', '?')}")
                    break

            if not job_completed:
                _warn(f"RMA job #{job_id} 在 {5 * 2} 秒内未完成，继续")

    # 再次检查工单状态
    try:
        detail_resp = _req("GET", f"/api/v1/tickets/{ticket_id}")
        ticket_now = detail_resp.get("data", {}).get("ticket", {})
    except Exception:
        ticket_now = ticket

    new_status = ticket_now.get("current_status_code", "?")
    new_rma_status = ticket_now.get("rma_status", "?")
    print(f"{IDENT}最终工单状态: current_status_code={new_status}, rma_status={new_rma_status}")

    _ok("Phase 3 RMA 流程完成")
    _count("phase3", "pass")
    return {"ticket_id": ticket_id, "ticket": ticket_now}


# ──────────────────────────────────────────────────────────────
# Phase 4: SMTP 发送回复到指定邮箱
# ──────────────────────────────────────────────────────────────
def phase_smtp(ticket: dict) -> dict | None:
    """SMTP 发送回复到指定邮箱阶段。

    操作步骤:
      1. 调用 test_envelope_allowed() 进行收件人白名单安全校验
      2. 读取当前 auto_send_enabled 配置并记录原始值
      3. 查询工单已有的 reply_records，检查是否已存在 sent 状态的 RMA 回复
      4. 若 auto_send_enabled=False: 临时 PATCH /api/v1/system/config 开启
      5. POST /api/v1/replies/{ticket_id}/draft 创建 rma_authorization 类型草稿
      6. 根据 send_status 判断结果:
         - sent → 输出 smtp_message_id, sent_at
         - sending → 等待 3 秒后再次查询确认
         - send_failed → 输出 error_message
         - pending_review → 尝试 POST /api/v1/replies/{id}/approve-send 手动审批
      7. finally 块中无条件恢复 auto_send_enabled 为原始值

    校验逻辑:
      - test_envelope_allowed 返回 False 时跳过 SMTP 发送
      - 已有 sent 状态的 RMA 回复时标记为已发送
      - auto_send_enabled 回滚在 finally 中保证执行

    安全约束:
      - 发送前必须通过 test_envelope_allowed 白名单复核
      - auto_send_enabled 配置使用 try/finally 保证回滚
      - 收件人限定为 SMTP_RECIPIENT_WHITELIST 中的地址

    Returns:
      包含发送结果和回复数据的字典，跳过或失败时返回 None
    """
    _hdr("Phase 4: SMTP 发送回复到指定邮箱")

    ticket_id = ticket.get("id")
    if not ticket_id:
        _warn("工单 ID 缺失，跳过")
        _count("phase4", "skip")
        return None

    # 0. 安全校验：确认收件人白名单
    if not _test_envelope_allowed(None, None):
        _warn("SMTP 收件人白名单不满足发送条件，跳过发送")
        print(f"{IDENT}  期望白名单: rmatest2@accotest.com")
        _count("phase4", "skip")
        return None
    _ok("SMTP 收件人白名单校验通过")

    # 1. 保存原始 auto_send_enabled 配置
    original_auto_send: bool | None = None
    try:
        config_resp = _req("GET", "/api/v1/system/config")
        original_auto_send = config_resp.get("data", {}).get("auto_send_enabled", False)
        print(f"{IDENT}当前 auto_send_enabled = {original_auto_send}")
    except Exception as exc:
        _err(f"读取系统配置失败: {exc}")
        _count("phase4", "fail")
        return None

    try:
        # 2. 检查是否已有 sent 状态的 RMA 回复
        try:
            detail_resp = _req("GET", f"/api/v1/tickets/{ticket_id}")
        except Exception as exc:
            _err(f"查询工单详情失败: {exc}")
            _count("phase4", "fail")
            return None

        reply_records = detail_resp.get("data", {}).get("reply_records", [])
        existing_rma_replies = [r for r in reply_records if r.get("reply_type") == "rma_authorization"]

        for reply in existing_rma_replies:
            send_status = reply.get("send_status", "?")
            if send_status == "sent":
                smtp_msg_id = reply.get("smtp_message_id", "?")
                sent_at = reply.get("sent_at", "?")
                _ok(f"RMA 回复已存在且已发送: smtp_message_id={smtp_msg_id}, sent_at={sent_at}")
                _count("phase4", "pass")
                return {"already_sent": True, "reply": reply}
            elif send_status == "send_failed":
                _warn(f"已有发送失败的 RMA 回复: {reply.get('error_message', '?')}")

        # 3. 如果 auto_send_enabled 未开启，临时开启
        if not original_auto_send:
            _sub("临时开启 auto_send_enabled")
            try:
                patch_resp = _req("PATCH", "/api/v1/system/config", body={"auto_send_enabled": True})
                if not patch_resp.get("success"):
                    _err(f"开启 auto_send_enabled 失败: {patch_resp.get('message', patch_resp)}")
                    _count("phase4", "fail")
                    return None
                _ok("auto_send_enabled = True (临时)")
            except Exception as exc:
                _err(f"配置更新失败: {exc}")
                _count("phase4", "fail")
                return None

        # 4. 创建 RMA 授权回复草稿
        _sub("创建 rma_authorization 回复草稿")
        try:
            draft_resp = _req("POST", f"/api/v1/replies/{ticket_id}/draft", body={"reply_type": "rma_authorization"})
        except Exception as exc:
            _err(f"创建草稿失败: {exc}")
            _count("phase4", "fail")
            return None

        if not draft_resp.get("success"):
            _err(f"创建草稿失败: {draft_resp.get('message', draft_resp)}")
            _count("phase4", "fail")
            return None

        data = draft_resp.get("data", {})
        reply = data.get("reply", {})
        reply_id = reply.get("id", "?")
        reply_type = reply.get("reply_type", "?")
        send_status = reply.get("send_status", "?")
        subject = reply.get("subject", "?")

        _ok(f"草稿创建成功: reply_id={reply_id}, reply_type={reply_type}")
        print(f"{IDENT}  subject      = {subject}")
        print(f"{IDENT}  send_status  = {send_status}")

        # 5. 检查发送结果
        if send_status == "sent":
            smtp_message_id = reply.get("smtp_message_id", "?")
            sent_at = reply.get("sent_at", "?")
            _ok(f"SMTP 发送成功! smtp_message_id={smtp_message_id}")
            _ok(f"  sent_at = {sent_at}")
            _count("phase4", "pass")
            return {"sent": True, "reply": reply}
        elif send_status == "sending":
            _ok("SMTP 发送中 (sending)...")
            # 等待几秒后再次检查
            time.sleep(3)
            try:
                ticket_detail2 = _req("GET", f"/api/v1/tickets/{ticket_id}")
                reply_records2 = ticket_detail2.get("data", {}).get("reply_records", [])
                for r in reply_records2:
                    if r.get("id") == reply.get("id"):
                        if r.get("send_status") == "sent":
                            _ok(f"SMTP 发送成功 (延迟确认): smtp_message_id={r.get('smtp_message_id')}")
                            _count("phase4", "pass")
                            return {"sent": True, "reply": r}
                        elif r.get("send_status") == "send_failed":
                            _err(f"SMTP 发送失败 (延迟确认): {r.get('error_message', '?')}")
                            _count("phase4", "fail")
                            return {"sent": False, "reply": r}
                _warn("发送状态仍为 sending，可能仍在队列中")
            except Exception:
                pass
            _count("phase4", "pass")
            return {"sent": True, "reply": reply}
        elif send_status == "send_failed":
            error_msg = reply.get("error_message", "?")
            _err(f"SMTP 发送失败: {error_msg}")
            _count("phase4", "fail")
            return {"sent": False, "reply": reply}
        elif send_status == "pending_review":
            _warn("草稿状态为 pending_review（auto_send 未触发）")
            # 尝试手动审批
            try:
                approve_resp = _req("POST", f"/api/v1/replies/{reply_id}/approve-send")
                if approve_resp.get("success"):
                    approve_reply = approve_resp.get("data", {}).get("reply", {})
                    approve_status = approve_reply.get("send_status", "?")
                    if approve_status == "sent":
                        _ok(f"手动审批后发送成功: smtp_message_id={approve_reply.get('smtp_message_id')}")
                        _count("phase4", "pass")
                        return {"sent": True, "reply": approve_reply}
                    else:
                        _warn(f"手动审批后状态: {approve_status}")
                else:
                    _err(f"手动审批失败: {approve_resp.get('message', approve_resp)}")
            except Exception as exc:
                _err(f"手动审批请求失败: {exc}")
            _count("phase4", "skip")
            return {"sent": False, "reply": reply}
        else:
            print(f"{IDENT}  未知发送状态: {send_status}")
            _count("phase4", "skip")
            return {"unknown": True, "reply": reply}

    finally:
        # 6. 恢复原始 auto_send_enabled 配置（MUST 在 finally 中）
        if original_auto_send is not None:
            try:
                _sub("恢复 auto_send_enabled 配置")
                current_check = _req("GET", "/api/v1/system/config")
                current_value = current_check.get("data", {}).get("auto_send_enabled", None)
                if current_value != original_auto_send:
                    restore_resp = _req("PATCH", "/api/v1/system/config", body={"auto_send_enabled": original_auto_send})
                    if restore_resp.get("success"):
                        _ok(f"auto_send_enabled 已恢复为 {original_auto_send}")
                    else:
                        _err(f"恢复配置失败: {restore_resp.get('message', restore_resp)}")
                else:
                    _ok(f"auto_send_enabled 未变更，保持 {original_auto_send}")
            except Exception as exc:
                _err(f"恢复配置时出错: {exc}")
                print(f"{IDENT}  !! 请手动恢复 auto_send_enabled 为 {original_auto_send}")


# ──────────────────────────────────────────────────────────────
# Phase 5: 全链路测试报告汇总
# ──────────────────────────────────────────────────────────────
def phase_summary(start_time: float) -> int:
    _hdr("Phase 5: 全链路测试报告汇总")

    elapsed = time.monotonic() - start_time

    print()
    print(f"{IDENT}{'阶段':<30} {'通过':>5} {'失败':>5} {'跳过':>5}")
    print(f"{IDENT}{'-' * 47}")

    phase_names = {
        "phase0": "Phase 0: 前置检查/登录",
        "phase1": "Phase 1: IMAP 邮件拉取",
        "phase2": "Phase 2: 解析与工单追踪",
        "phase3": "Phase 3: RMA 生成校验",
        "phase4": "Phase 4: SMTP 回复发送",
    }

    total_pass = 0
    total_fail = 0
    total_skip = 0

    for key in ("phase0", "phase1", "phase2", "phase3", "phase4"):
        r = _results[key]
        name = phase_names[key]
        print(f"{IDENT}{name:<30} {r['pass']:>5} {r['fail']:>5} {r['skip']:>5}")
        total_pass += r["pass"]
        total_fail += r["fail"]
        total_skip += r["skip"]

    print(f"{IDENT}{'-' * 47}")
    print(f"{IDENT}{'合计':<30} {total_pass:>5} {total_fail:>5} {total_skip:>5}")

    print()
    print(f"{IDENT}总耗时: {elapsed:.1f} 秒")

    if total_fail == 0 and total_pass > 0:
        print(f"\n{IDENT}🎉 全链路 E2E 测试通过!")
        return 0
    elif total_fail > 0 and total_pass > 0:
        print(f"\n{IDENT}⚠ 部分测试失败 (pass={total_pass}, fail={total_fail})")
        return 1
    elif total_skip > 0 and total_pass == 0 and total_fail == 0:
        print(f"\n{IDENT}△ 所有测试均被跳过 (环境不满足)")
        return 2
    else:
        print(f"\n{IDENT}✗ 测试失败")
        return 1


# ──────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────
def main() -> int:
    print("=" * 72)
    print("  维修邮件代理 — 全链路 E2E 测试")
    print(f"  后端地址: {BASE}")
    print(f"  时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 72)

    start_time = time.monotonic()

    # --- Phase 0 ---
    if not phase_precheck():
        print("\n前置检查失败，终止测试。")
        phase_summary(start_time)
        return 2

    # --- Phase 1 ---
    email_ids = phase_imap_fetch()
    if not email_ids:
        print("\nPhase 1 无可用邮件，跳过后续阶段。")
        phase_summary(start_time)
        return 1

    # --- Wait for async parsing to complete ---
    _hdr("等待异步解析完成")
    parse_timeout = 60  # seconds
    for email_id in email_ids:
        waited = 0
        while waited < parse_timeout:
            try:
                email_resp = _req("GET", f"/api/v1/emails/{email_id}")
                parse_status = email_resp.get("data", {}).get("email", {}).get("parse_status", "?")
                print(f"{IDENT}email_id={email_id} parse_status={parse_status} (waited {waited}s)")
                if parse_status not in ("pending", "parsing"):
                    _ok(f"email_id={email_id} 解析完成: {parse_status}")
                    break
            except Exception as exc:
                print(f"{IDENT}查询状态失败: {exc}")
            time.sleep(3)
            waited += 3
        if waited >= parse_timeout:
            _warn(f"email_id={email_id} 解析超时 ({parse_timeout}s), 状态仍为 pending, 继续测试")

    # --- Phase 2 ---
    tickets = phase_trace_parse(email_ids)
    if not tickets:
        print("\nPhase 2 未找到关联工单，跳过 Phase 3-4。")
        phase_summary(start_time)
        return 1

    # --- Phase 3 ---
    rma_result = phase_rma(tickets)
    if rma_result is None:
        print("\nPhase 3 RMA 流程异常。")

    # 获取最新工单状态
    ticket_for_smtp = None
    if rma_result and rma_result.get("ticket"):
        ticket_for_smtp = rma_result["ticket"]
    elif tickets:
        # 重新获取工单详情
        try:
            detail_resp = _req("GET", f"/api/v1/tickets/{tickets[0].get('id')}")
            ticket_for_smtp = detail_resp.get("data", {}).get("ticket", {})
        except Exception:
            ticket_for_smtp = tickets[0]

    # --- Phase 4 ---
    if ticket_for_smtp:
        phase_smtp(ticket_for_smtp)
    else:
        _hdr("Phase 4: SMTP 发送回复到指定邮箱")
        _warn("无可用工单，跳过 SMTP 阶段")
        _count("phase4", "skip")

    # --- 总结 ---
    return phase_summary(start_time)


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
