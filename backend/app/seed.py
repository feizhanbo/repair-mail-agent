from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

import bcrypt
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.database import AsyncSessionLocal, engine
from app.models import ReplyTemplate, Role, User, UserRole, WorkflowStatus, WorkflowTransition


WORKFLOW_STATUSES: tuple[dict[str, Any], ...] = (
    {
        "status_code": "new_email",
        "status_name": "新邮件",
        "status_category": "intake",
        "description": "邮件已进入系统，等待解析。",
        "sort_order": 10,
    },
    {
        "status_code": "parsed",
        "status_name": "已解析",
        "status_category": "processing",
        "description": "邮件内容已完成结构化解析。",
        "sort_order": 20,
    },
    {
        "status_code": "need_customer_info",
        "status_name": "待客户补充",
        "status_category": "waiting",
        "description": "缺少 SN、故障描述或联系人等必要字段，需要客户补充。",
        "sort_order": 30,
    },
    {
        "status_code": "auto_replied",
        "status_name": "已自动回复",
        "status_category": "waiting",
        "description": "系统已按模板向客户发送补充信息邮件。",
        "sort_order": 40,
    },
    {
        "status_code": "manual_review",
        "status_name": "人工审核",
        "status_category": "review",
        "description": "解析置信度不足、字段冲突或规则命中，需要人工处理。",
        "sort_order": 50,
    },
    {
        "status_code": "ready_for_export",
        "status_name": "待导出",
        "status_category": "export",
        "description": "工单字段已确认，可以进入导出或下游同步。",
        "sort_order": 60,
    },
    {
        "status_code": "closed",
        "status_name": "已关闭",
        "status_category": "terminal",
        "description": "工单处理流程结束。",
        "is_terminal": True,
        "sort_order": 70,
    },
    {
        "status_code": "error",
        "status_name": "异常",
        "status_category": "error",
        "description": "系统处理失败，等待排查或重试。",
        "sort_order": 80,
    },
)


WORKFLOW_TRANSITIONS: tuple[dict[str, Any], ...] = (
    {
        "from_status_code": "new_email",
        "to_status_code": "parsed",
        "trigger_event": "parse_completed",
        "condition_desc": "邮件解析成功。",
    },
    {
        "from_status_code": "new_email",
        "to_status_code": "manual_review",
        "trigger_event": "parse_low_confidence",
        "condition_desc": "初次解析置信度低于阈值。",
        "require_manual": True,
    },
    {
        "from_status_code": "new_email",
        "to_status_code": "error",
        "trigger_event": "system_error",
        "condition_desc": "邮件入库或解析阶段发生系统异常。",
        "require_manual": True,
    },
    {
        "from_status_code": "parsed",
        "to_status_code": "need_customer_info",
        "trigger_event": "missing_required_fields",
        "condition_desc": "缺少创建报修所需关键字段。",
    },
    {
        "from_status_code": "parsed",
        "to_status_code": "manual_review",
        "trigger_event": "field_conflict",
        "condition_desc": "字段冲突、SN 校验失败或规则命中人工审核。",
        "require_manual": True,
    },
    {
        "from_status_code": "parsed",
        "to_status_code": "ready_for_export",
        "trigger_event": "validation_passed",
        "condition_desc": "字段完整且校验通过。",
    },
    {
        "from_status_code": "parsed",
        "to_status_code": "error",
        "trigger_event": "system_error",
        "condition_desc": "结构化处理阶段发生系统异常。",
        "require_manual": True,
    },
    {
        "from_status_code": "need_customer_info",
        "to_status_code": "auto_replied",
        "trigger_event": "reply_draft_created",
        "condition_desc": "补充信息邮件已生成并发送。",
    },
    {
        "from_status_code": "need_customer_info",
        "to_status_code": "manual_review",
        "trigger_event": "followup_limit_exceeded",
        "condition_desc": "补充信息轮次达到上限。",
        "require_manual": True,
    },
    {
        "from_status_code": "auto_replied",
        "to_status_code": "parsed",
        "trigger_event": "customer_replied",
        "condition_desc": "客户回复后重新解析补充内容。",
    },
    {
        "from_status_code": "auto_replied",
        "to_status_code": "manual_review",
        "trigger_event": "followup_limit_exceeded",
        "condition_desc": "自动追问达到上限。",
        "require_manual": True,
    },
    {
        "from_status_code": "manual_review",
        "to_status_code": "parsed",
        "trigger_event": "review_completed",
        "condition_desc": "人工修正后回到解析完成态继续校验。",
        "require_manual": True,
    },
    {
        "from_status_code": "manual_review",
        "to_status_code": "need_customer_info",
        "trigger_event": "need_customer_info",
        "condition_desc": "人工判断仍需客户补充信息。",
        "require_manual": True,
    },
    {
        "from_status_code": "manual_review",
        "to_status_code": "ready_for_export",
        "trigger_event": "review_approved",
        "condition_desc": "人工审核通过。",
        "require_manual": True,
    },
    {
        "from_status_code": "manual_review",
        "to_status_code": "error",
        "trigger_event": "system_error",
        "condition_desc": "人工处理阶段发生系统异常。",
        "require_manual": True,
    },
    {
        "from_status_code": "ready_for_export",
        "to_status_code": "closed",
        "trigger_event": "export_completed",
        "condition_desc": "导出或下游同步完成。",
    },
)


ROLES: tuple[dict[str, str], ...] = (
    {"role_code": "admin", "role_name": "系统管理员", "description": "拥有系统配置、用户管理和全部数据操作权限。"},
    {"role_code": "reviewer", "role_name": "审核员", "description": "处理人工审核队列，修正解析结果并审批回复。"},
    {"role_code": "service_agent", "role_name": "客服处理员", "description": "查看报修邮件、工单和客户补充信息。"},
    {"role_code": "viewer", "role_name": "只读观察员", "description": "只读查看系统数据、统计和处理记录。"},
)


REPLY_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "template_code": "missing_info_zh",
        "template_name": "补充报修信息",
        "template_type": "missing_fields",
        "language": "zh-CN",
        "version": "v1",
        "subject_template": "请补充报修信息：{{ ticket_no }}",
        "body_template": (
            "您好，\n\n"
            "我们已收到您的报修邮件，但还需要补充以下信息后才能继续处理：\n"
            "{{ missing_fields }}\n\n"
            "请直接回复本邮件补充上述内容。谢谢。"
        ),
    },
    {
        "template_code": "sn_invalid_zh",
        "template_name": "SN 信息需确认",
        "template_type": "sn_invalid",
        "language": "zh-CN",
        "version": "v1",
        "subject_template": "请确认设备 SN：{{ ticket_no }}",
        "body_template": (
            "您好，\n\n"
            "您提供的设备 SN 暂未通过系统校验，请确认 SN 是否完整、是否存在大小写或字符录入错误。\n"
            "请回复正确 SN 后，我们会继续处理您的报修申请。"
        ),
    },
    {
        "template_code": "manual_review_notice_zh",
        "template_name": "人工审核提醒",
        "template_type": "manual_review",
        "language": "zh-CN",
        "version": "v1",
        "subject_template": "报修邮件需人工确认：{{ ticket_no }}",
        "body_template": (
            "您好，\n\n"
            "您的报修邮件已进入人工确认流程。我们会尽快核对信息，并在需要时联系您补充材料。"
        ),
    },
)


def _hash_password(password: str) -> str:
    password_bytes = password.encode("utf-8")
    if len(password_bytes) > 72:
        raise ValueError("DEFAULT_ADMIN_PASSWORD must be 72 bytes or fewer for bcrypt.")
    return bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode("utf-8")


async def _get_one(session: AsyncSession, model: type[Any], *conditions: Any) -> Any | None:
    result = await session.execute(select(model).where(and_(*conditions)))
    return result.scalar_one_or_none()


def _apply_values(instance: Any, values: dict[str, Any], keys: Iterable[str]) -> None:
    for key in keys:
        setattr(instance, key, values[key])


async def _seed_workflow_statuses(session: AsyncSession) -> int:
    for values in WORKFLOW_STATUSES:
        status = await _get_one(session, WorkflowStatus, WorkflowStatus.status_code == values["status_code"])
        payload = {
            "status_name": values["status_name"],
            "status_category": values["status_category"],
            "description": values.get("description"),
            "is_terminal": values.get("is_terminal", False),
            "sort_order": values["sort_order"],
            "enabled": values.get("enabled", True),
        }
        if status is None:
            session.add(WorkflowStatus(status_code=values["status_code"], **payload))
        else:
            _apply_values(status, payload, payload.keys())
    return len(WORKFLOW_STATUSES)


async def _seed_workflow_transitions(session: AsyncSession) -> int:
    for values in WORKFLOW_TRANSITIONS:
        transition = await _get_one(
            session,
            WorkflowTransition,
            WorkflowTransition.from_status_code == values["from_status_code"],
            WorkflowTransition.to_status_code == values["to_status_code"],
            WorkflowTransition.trigger_event == values["trigger_event"],
        )
        payload = {
            "condition_desc": values.get("condition_desc"),
            "require_manual": values.get("require_manual", False),
            "enabled": values.get("enabled", True),
        }
        if transition is None:
            session.add(WorkflowTransition(**values))
        else:
            _apply_values(transition, payload, payload.keys())
    return len(WORKFLOW_TRANSITIONS)


async def _seed_roles(session: AsyncSession) -> dict[str, Role]:
    roles: dict[str, Role] = {}
    for values in ROLES:
        role = await _get_one(session, Role, Role.role_code == values["role_code"])
        payload = {"role_name": values["role_name"], "description": values.get("description")}
        if role is None:
            role = Role(role_code=values["role_code"], **payload)
            session.add(role)
        else:
            _apply_values(role, payload, payload.keys())
        roles[values["role_code"]] = role
    await session.flush()
    return roles


async def _seed_default_admin(session: AsyncSession, admin_role: Role) -> User:
    username = settings.DEFAULT_ADMIN_USERNAME
    user = await _get_one(session, User, User.username == username)
    payload = {
        "real_name": settings.DEFAULT_ADMIN_REAL_NAME,
        "email": settings.DEFAULT_ADMIN_EMAIL,
        "status": "active",
    }
    if user is None:
        user = User(
            username=username,
            password_hash=_hash_password(settings.DEFAULT_ADMIN_PASSWORD),
            phone=None,
            department="System",
            **payload,
        )
        session.add(user)
        await session.flush()
    else:
        _apply_values(user, payload, payload.keys())
        await session.flush()

    existing_role = await _get_one(session, UserRole, UserRole.user_id == user.id, UserRole.role_id == admin_role.id)
    if existing_role is None:
        session.add(UserRole(user_id=user.id, role_id=admin_role.id))
    return user


async def _seed_reply_templates(session: AsyncSession, creator_user_id: int) -> int:
    for values in REPLY_TEMPLATES:
        template = await _get_one(
            session,
            ReplyTemplate,
            ReplyTemplate.template_code == values["template_code"],
            ReplyTemplate.version == values["version"],
        )
        payload = {
            "template_name": values["template_name"],
            "template_type": values["template_type"],
            "language": values["language"],
            "subject_template": values.get("subject_template"),
            "body_template": values["body_template"],
            "enabled": values.get("enabled", True),
            "created_by_user_id": creator_user_id,
        }
        if template is None:
            session.add(ReplyTemplate(**values, created_by_user_id=creator_user_id))
        else:
            _apply_values(template, payload, payload.keys())
    return len(REPLY_TEMPLATES)


async def seed_database() -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            status_count = await _seed_workflow_statuses(session)
            transition_count = await _seed_workflow_transitions(session)
            roles = await _seed_roles(session)
            admin_user = await _seed_default_admin(session, roles["admin"])
            template_count = await _seed_reply_templates(session, admin_user.id)

    return {
        "workflow_statuses": status_count,
        "workflow_transitions": transition_count,
        "roles": len(roles),
        "reply_templates": template_count,
        "default_admin": settings.DEFAULT_ADMIN_USERNAME,
    }


async def _main() -> None:
    try:
        result = await seed_database()
        for key, value in result.items():
            print(f"{key}: {value}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
