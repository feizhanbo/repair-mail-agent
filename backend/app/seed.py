from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.database import AsyncSessionLocal, engine
from app.core.security import hash_password
from app.models import ReplyTemplate, Role, User, UserRole, WorkflowStatus, WorkflowTransition


WORKFLOW_STATUSES: tuple[dict[str, Any], ...] = (
    {
        "status_code": "new_email",
        "status_name": "邮件已入库",
        "status_category": "intake",
        "description": "邮件和附件已归档，尚未解析。",
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
        "status_name": "人工复核",
        "status_category": "review",
        "description": "解析置信度不足、字段冲突或规则命中，需要人工处理。",
        "sort_order": 50,
    },
    {
        "status_code": "ready_for_export",
        "status_name": "可导出",
        "status_category": "export",
        "description": "工单字段已确认，可以进入导出或下游同步。",
        "sort_order": 60,
    },
    {
        "status_code": "rma_sent",
        "status_name": "RMA已发送",
        "status_category": "rma",
        "description": "SMTP已明确发送RMA成功，等待完成PDF与出站邮件归档核验。",
        "sort_order": 70,
    },
    {
        "status_code": "error",
        "status_name": "异常",
        "status_category": "error",
        "description": "系统处理失败，等待排查或重试。",
        "sort_order": 80,
    },
    {
        "status_code": "closed",
        "status_name": "已关闭",
        "status_category": "terminal",
        "description": "工单完成或终止。",
        "is_terminal": True,
        "sort_order": 90,
    },
    {
        "status_code": "resolved",
        "status_name": "人工业务已完成",
        "status_category": "terminal",
        "description": "SECOND 人工业务已经处理并记录结果，不代表签发 RMA。",
        "is_terminal": True,
        "sort_order": 95,
    },
)


BASE_WORKFLOW_TRANSITIONS: tuple[dict[str, Any], ...] = (
    {
        "from_status_code": "manual_review",
        "to_status_code": "resolved",
        "trigger_event": "manual_business_resolved",
        "condition_desc": "SECOND 人工业务已通过现有业务渠道处理并记录结果。",
        "require_manual": True,
    },
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
        "from_status_code": "parsed",
        "to_status_code": "ready_for_export",
        "trigger_event": "validation_passed",
        "condition_desc": "字段完整且 SN 校验通过。",
    },
    {
        "from_status_code": "parsed",
        "to_status_code": "need_customer_info",
        "trigger_event": "missing_fields_detected",
        "condition_desc": "关键字段缺失且可追问。",
    },
    {
        "from_status_code": "parsed",
        "to_status_code": "manual_review",
        "trigger_event": "field_conflict",
        "condition_desc": "字段冲突、SN 异常或置信度不足。",
        "require_manual": True,
    },
    {
        "from_status_code": "need_customer_info",
        "to_status_code": "auto_replied",
        "trigger_event": "reply_sent",
        "condition_desc": "补充信息邮件已成功发送。",
    },
    {
        "from_status_code": "need_customer_info",
        "to_status_code": "parsed",
        "trigger_event": "customer_info_completed",
        "condition_desc": "Required customer fields were completed during a deterministic reparse.",
    },
    {
        "from_status_code": "manual_review",
        "to_status_code": "auto_replied",
        "trigger_event": "reply_sent",
        "condition_desc": "人工审核后的补充信息邮件已成功发送。",
    },
    {
        "from_status_code": "auto_replied",
        "to_status_code": "parsed",
        "trigger_event": "customer_reply_received",
        "condition_desc": "客户回复后重新解析补充内容。",
    },
    {
        "from_status_code": "manual_review",
        "to_status_code": "ready_for_export",
        "trigger_event": "manual_resolved",
        "condition_desc": "人工修正且校验通过。",
        "require_manual": True,
    },
    {
        "from_status_code": "manual_review",
        "to_status_code": "parsed",
        "trigger_event": "manual_resolved",
        "condition_desc": "人工修正后回到解析态。",
        "require_manual": True,
    },
    {
        "from_status_code": "manual_review",
        "to_status_code": "need_customer_info",
        "trigger_event": "manual_resolved",
        "condition_desc": "人工判定需要客户补充信息。",
        "require_manual": True,
    },
    {
        "from_status_code": "error",
        "to_status_code": "parsed",
        "trigger_event": "manual_resolved",
        "condition_desc": "异常修复后人工恢复。",
        "require_manual": True,
    },
    {
        "from_status_code": "ready_for_export",
        "to_status_code": "manual_review",
        "trigger_event": "validated_data_changed",
        "condition_desc": "已验证字段或明细发生变化，原导出快照失效。",
        "require_manual": True,
    },
    {
        "from_status_code": "ready_for_export",
        "to_status_code": "rma_sent",
        "trigger_event": "rma_reply_sent",
        "condition_desc": "SAP回填合法RMA编号且模板回复实际发送成功。",
    },
    {
        "from_status_code": "rma_sent",
        "to_status_code": "closed",
        "trigger_event": "rma_issued_and_archived",
        "condition_desc": "RMA回复发送成功且PDF与出站EML归档核验完成。",
    },
)


def _build_workflow_transitions() -> tuple[dict[str, Any], ...]:
    statuses = [status["status_code"] for status in WORKFLOW_STATUSES]
    terminal_statuses = {status["status_code"] for status in WORKFLOW_STATUSES if status.get("is_terminal")}
    non_terminal_statuses = [status for status in statuses if status not in terminal_statuses]
    transitions = list(BASE_WORKFLOW_TRANSITIONS)

    for from_status in non_terminal_statuses:
        if from_status != "manual_review":
            transitions.append(
                {
                    "from_status_code": from_status,
                    "to_status_code": "manual_review",
                    "trigger_event": "manual_review_required",
                    "condition_desc": "需要人工介入。",
                    "require_manual": True,
                }
            )
        if from_status != "error":
            transitions.append(
                {
                    "from_status_code": from_status,
                    "to_status_code": "error",
                    "trigger_event": "system_error",
                    "condition_desc": "系统处理异常。",
                    "require_manual": True,
                }
            )
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for transition in transitions:
        key = (transition["from_status_code"], transition["to_status_code"], transition["trigger_event"])
        unique[key] = transition
    return tuple(unique.values())


WORKFLOW_TRANSITIONS = _build_workflow_transitions()


ROLES: tuple[dict[str, str], ...] = (
    {"role_code": "admin", "role_name": "系统管理员", "description": "用户管理、角色分配、系统配置、基础资料维护、权限配置、全部数据查看和全部业务兜底操作。"},
    {"role_code": "operator", "role_name": "一般操作员", "description": "处理本人可见或已领取任务，修正字段、SN 校验、采纳解析、生成追问和提交回复草稿。"},
)


REPLY_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "template_code": "all_replies_neutral_base_zh",
        "template_name": "特殊客户中性基础模板（不含公司名称）",
        "template_type": "neutral_base",
        "language": "zh-CN",
        "version": "v1",
        "subject_template": None,
        "body_template": (
            "{{ content }}\n\n"
            "Best Regards!\n\n"
            "本邮件及其附件可能包含保密信息，仅供指定收件方使用。"
            "如您并非指定收件方，请删除本邮件及全部附件，并通知发件方；"
            "不得泄露、复制、散发本邮件或依赖本邮件采取任何行动。\n"
            "The information contained in and accompanying this email may be confidential "
            "and is intended solely for the intended recipient(s). If received in error, "
            "please delete all copies and notify the sender."
        ),
    },
    {
        "template_code": "all_replies_domestic_company_base_zh",
        "template_name": "所有回复邮件基础模板（国内公司信息）",
        "template_type": "domestic_company_base",
        "language": "zh-CN",
        "version": "v1",
        "subject_template": None,
        "body_template": (
            "{{ content }}\n\n"
            "Best Regards!\n"
            "------------------------------------------------------------\n"
            "AccoTEST Business Unit of Beijing Huafeng Test & Control Technology Co., Ltd.\n"
            "Web: www.accotest.com\n\n"
            "本邮件及其附件可能包含保密信息，仅供指定收件方使用。"
            "如您并非指定收件方，请删除本邮件及全部附件，并通知发件方；"
            "不得泄露、复制、散发本邮件或依赖本邮件采取任何行动。\n"
            "The information contained in and accompanying this email may be confidential "
            "and is intended solely for the intended recipient(s). If received in error, "
            "please delete all copies and notify the sender."
        ),
    },
    {
        "template_code": "all_replies_domestic_company_base_zh",
        "template_name": "所有回复邮件基础模板（Miya 完整签名）",
        "template_type": "domestic_company_base",
        "language": "zh-CN",
        "version": "v2",
        "subject_template": None,
        "body_template": (
            "{{ content }}\n\n\n"
            "Best Regards!\n"
            "-----------------------------------------------------\n"
            "Miya Fang (方菲)\n\n"
            "AccoTEST Business Unit of Beijing Huafeng Test & Control Technology Co.,Ltd\n"
            "Tel: +86-512-67678157/62982753*801\n"
            "Cell Phone: 86-15001161080\n"
            "E-mail:miya.fang@accotest.com\n"
            "Address: Room309, A1 Building, #388 XinPing Street, Suzhou, 215123 P.R. China\n"
            "地址:江苏省苏州市工业园区新平街388号 腾飞创新园 塔楼A1-309室\n"
            "www.accotest.com\n"
            "本电邮所包含或其随附的信息可能属于保密信息，仅供指定收件方使用。如阁下并非本电邮指明的收件方，"
            "请将阁下拥有的本电邮及其所有备份（包括所有附件）删除并销毁，并将阁下误收本电邮一事通知发件方或华峰测控；"
            "在此特提请阁下注意不得泄露、复制或散发本电邮，并不得倚赖本电邮而采取任何行动。\n"
            "The information contained in and accompanying this email may be confidential, and is intended solely for the use of the intended recipient(s).  "
            "If you are not the intended recipient of this email, please delete and destroy all copies (including any attachments) in your possession, "
            "notify the sender or Beijing Huafeng Test & Control Technology Co., Ltd. that you have received this email in error, and you are hereby notified "
            "that any disclosure, duplication or dissemination of, or the taking of any action in reliance on, this email is expressly prohibited."
        ),
        "html_body_template": (
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5">\n'
            '{{ content }}<br><br><br>\n\n<div style="color:#6666CC;">\n'
            'Best Regards!<br>\n-----------------------------------------------------<br>\n'
            'Miya Fang (方菲)<br><br>\n\n'
            'AccoTEST Business Unit of Beijing Huafeng Test &amp; Control Technology Co.,Ltd<br>'
            '\nTel: +86-512-67678157/62982753*801<br>\nCell Phone: 86-15001161080<br>\n'
            'E-mail:miya.fang@accotest.com<br>\nAddress: Room309, A1 Building, #388 XinPing Street, Suzhou, 215123 P.R. China<br>\n'
            '地址:江苏省苏州市工业园区新平街388号 腾飞创新园 塔楼A1-309室<br>\n'
            '<img src="cid:accotest_logo" alt="AccoTEST" width="110" style="display:block;border:0;outline:none;text-decoration:none;margin-top:2px;margin-bottom:0;">\n'
            '<a href="https://www.accotest.com" style="color:#6666CC;text-decoration:none;">www.accotest.com</a>\n</div>\n\n'
            '<div style="color:#808080;">\n'
            '本电邮所包含或其随附的信息可能属于保密信息，仅供指定收件方使用。如阁下并非本电邮指明的收件方，请将阁下拥有的本电邮及其所有备份（包括所有附件）删除并销毁，并将阁下误收本电邮一事通知发件方或华峰测控；在此特提请阁下注意不得泄露、复制或散发本电邮，并不得倚赖本电邮而采取任何行动。<br>'
            'The information contained in and accompanying this email may be confidential, and is intended solely for the use of the intended recipient(s).  '
            'If you are not the intended recipient of this email, please delete and destroy all copies (including any attachments) in your possession, '
            'notify the sender or Beijing Huafeng Test &amp; Control Technology Co., Ltd. that you have received this email in error, and you are hereby notified '
            'that any disclosure, duplication or dissemination of, or the taking of any action in reliance on, this email is expressly prohibited.\n'
            '</div>\n</div>'
        ),
    },
    {
        "template_code": "all_replies_international_company_base_en",
        "template_name": "All replies base template (Demi full signature)",
        "template_type": "international_company_base",
        "language": "en-US",
        "version": "v1",
        "subject_template": None,
        "body_template": (
            "{{ content }}\n\n\nBest Regards!\n-----------------------------------------------------\n\n"
            "Demi Wang(王佳慧)\n\n\nAccoTEST Business Unit of Beijing Huafeng Test & Control Technology Co.,Ltd\n\n"
            "Tel:+86-21-60875371\n\nCell Phone:+86-18721190315\n\nE-mail: demi.wang@accotest.com\n\n"
            "Address: 10F,#7 Building,#88 ShengRong Road,PuDong District, Shanghai, 201203 P.R.China (New)\n\n"
            "地址:上海市浦东新区盛荣路88号源创谷7号楼10层\n\n\nwww.accotest.com\n\n"
            "本电邮所包含或其随附的信息可能属于保密信息，仅供指定收件方使用。如阁下并非本电邮指明的收件方，请将阁下拥有的本电邮及其所有备份（包括所有附件）删除并销毁，并将阁下误收本电邮一事通知发件方或华峰测控；在此特提请阁下注意不得泄露、复制或散发本电邮，并不得倚赖本电邮而采取任何行动。\n"
            "The information contained in and accompanying this email may be confidential, and is intended solely for the use of the intended recipient(s).  If you are not the intended recipient of this email, please delete and destroy all copies (including any attachments) in your possession, notify the sender or Beijing Huafeng Test & Control Technology Co., Ltd. that you have received this email in error, and you are hereby notified that any disclosure, duplication or dissemination of, or the taking of any action in reliance on, this email is expressly prohibited."
        ),
        "html_body_template": (
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5">\n{{ content }}<br><br><br>\n\n'
            '<div style="color:#000099;">\nBest Regards!<br><br>\n-----------------------------------------------------<br><br>\n'
            '<span style="background-color:#ffff00;color:#000099;">Demi</span> Wang(王佳慧)<br><br><br>\n'
            'AccoTEST Business Unit of Beijing Huafeng Test &amp; Control Technology Co.,Ltd<br><br>\nTel:+86-21-60875371<br><br>\n'
            'Cell Phone:+86-18721190315<br><br>\nE-mail: <span style="background-color:#ffff00;color:#000099;">demi</span>.wang@accotest.com<br><br>\n'
            'Address: <span style="color:#ff0000;font-weight:bold;">10F,#7 Building,</span>#88 ShengRong Road,PuDong District, Shanghai, 201203 P.R.China <span style="color:#ff0000;font-weight:bold;">(New)</span><br><br>\n'
            '地址:上海市浦东新区盛荣路88号源创谷<span style="color:#ff0000;font-weight:bold;">7号楼10层</span><br><br>\n'
            '<img src="cid:accotest_logo" alt="AccoTEST" width="145" style="display:block;border:0;outline:none;text-decoration:none;margin:0 0 2px 0;">\n'
            '<a href="https://www.accotest.com" style="color:#3366cc;text-decoration:none;">www.accotest.com</a>\n</div>\n\n<br>\n\n<div style="color:#666666;">\n'
            '本电邮所包含或其随附的信息可能属于保密信息，仅供指定收件方使用。如阁下并非本电邮指明的收件方，请将阁下拥有的本电邮及其所有备份（包括所有附件）删除并销毁，并将阁下误收本电邮一事通知发件方或华峰测控；在此特提请阁下注意不得泄露、复制或散发本电邮，并不得倚赖本电邮而采取任何行动。<br>\n'
            'The information contained in and accompanying this email may be confidential, and is intended solely for the use of the intended recipient(s).  If you are not the intended recipient of this email, please delete and destroy all copies (including any attachments) in your possession, notify the sender or Beijing Huafeng Test &amp; Control Technology Co., Ltd. that you have received this email in error, and you are hereby notified that any disclosure, duplication or dissemination of, or the taking of any action in reliance on, this email is expressly prohibited.\n</div>\n</div>'
        ),
    },
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
    {
        "template_code": "repair_receipt_zh",
        "template_name": "报修受理确认",
        "template_type": "receipt",
        "language": "zh-CN",
        "version": "v1",
        "subject_template": "Re: {{ original_subject }}",
        "body_template": (
            "您好，{{ contact_person }}：\n\n"
            "我们已收到您的报修邮件并生成工单 {{ ticket_no }}。"
            "系统已完成初步信息核对，后续处理进展会继续通过本邮件回复链同步。\n\n谢谢。"
        ),
    },
    {
        "template_code": "followup_zh",
        "template_name": "通用报修信息追问",
        "template_type": "followup",
        "language": "zh-CN",
        "version": "v1",
        "subject_template": "Re: {{ original_subject }}",
        "body_template": (
            "您好，{{ contact_person }}：\n\n"
            "为继续处理工单 {{ ticket_no }}，请补充以下信息：\n"
            "{{ missing_fields }}\n\n请直接回复本邮件补充。谢谢。"
        ),
    },
    {
        "template_code": "rma_authorization_domestic_zh",
        "template_name": "国内 RMA 维修授权回复",
        "template_type": "rma_authorization_domestic",
        "language": "zh-CN",
        "version": "rma_reply_zh_v1",
        "enabled": False,
        "subject_template": "Re: {{ original_subject }}",
        "body_template": (
            "您好：\n\nRMA维修授权表见附件。\n"
            "为了不耽误贵司维修进度，请注意以下事项：\n"
            "1. 请务必打印 RMA 表，并与报修板一同寄出。\n"
            "2. 请妥善包装设备，并寄送至以下地址：\n"
            "{{ return_address_block }}\n"
            "3. 维修工期预计为 10 个工作日，实际进度以维修检测结果为准。\n\n谢谢。"
        ),
    },
    {
        "template_code": "rma_authorization_domestic_in_warranty_zh",
        "template_name": "国内在保 RMA 维修授权回复",
        "template_type": "rma_authorization_domestic_in_warranty",
        "language": "zh-CN",
        "version": "domestic_in_warranty_v1",
        "subject_template": "{{ rma_attachment_basename }}",
        "body_template": (
            "Dear {{ contact_person }} ：\n"
            "RMA表格见附件。\n"
            "为了不耽误贵司维修进度，请注意以下事项：\n"
            "1.请务必打印出RMA表，并与报修板一起寄到{{ city }}质量部 。\n"
            "2.请务必注意板子的包装良好及核对板子的返回地址是否正确,如若中途改地址请提前告知\n"
            "3.维修工期：10个工作日\n"
            "请寄如下地址：(寄错会延长返修时效）\n"
            "{{ return_address_block }}"
        ),
        "html_body_template": (
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5">'
            'Dear {{ contact_person }} ：<br>'
            'RMA表格见附件。<br>'
            '为了不耽误贵司维修进度，请注意以下事项：<br>'
            '<span style="color:red;">1.请务必打印出RMA表，并与报修板一起寄到{{ city }}质量部 。</span><br>'
            '<span style="color:red;">2.请务必注意板子的包装良好及核对板子的返回地址是否正确,如若中途改地址请提前告知</span><br>'
            '<span style="color:red;">3.维修工期：10个工作日</span><br>'
            '请寄如下地址：(寄错会延长返修时效）<br>'
            '<span style="white-space:pre-line">{{ return_address_block }}</span></div>'
        ),
    },
    {
        "template_code": "rma_authorization_domestic_out_of_warranty_zh",
        "template_name": "国内超保 RMA 维修授权回复",
        "template_type": "rma_authorization_domestic_out_of_warranty",
        "language": "zh-CN",
        "version": "domestic_out_warranty_v1",
        "subject_template": "{{ rma_attachment_basename }}",
        "body_template": (
            "Dear {{ contact_person }} ：\n为了不耽误贵司维修进度，请注意以下事项：\n"
            "1.请打印出附件RMA表，并与报修板一起寄到{{ city }}质量部。(请核对板子的返回地址，如若中途改地址请提前告知）\n"
            "2.已超保板卡，请将维修订单盖章，邮件提供电子版（我司收到有效订单后，SAP系统放行）,请尽快安排付款\n"
            "邮寄地址：\n{{ return_address_block }}\n维修工期：10个工作日。维修后板卡质保期：90天\n"
            "维修费：{{ repair_fee }}{{ currency_unit }}/块（含13%VAT税票与单程快递费），维修发票单独邮寄。"
        ),
        "html_body_template": (
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5">'
            'Dear {{ contact_person }} ：<br>为了不耽误贵司维修进度，请注意以下事项：<br>'
            '1.请打印出附件RMA表，并与报修板一起寄到{{ city }}质量部。(请核对板子的返回地址，如若中途改地址请提前告知）<br>'
            '2.<span style="background-color:yellow;">已超保板卡，请将维修订单盖章，邮件提供电子版</span>（我司收到有效订单后，SAP系统放行）,请尽快安排付款<br>'
            '邮寄地址：<br><span style="white-space:pre-line">{{ return_address_block }}</span><br>'
            '维修工期：10个工作日。维修后板卡质保期：90天<br>'
            '维修费：{{ repair_fee }}{{ currency_unit }}/块（含13%VAT税票与单程快递费），维修发票单独邮寄。\n</div>'
        ),
    },
    {
        "template_code": "rma_attachment_disabled_receipt_zh",
        "template_name": "RMA 附件未启用受理回复",
        "template_type": "rma_attachment_disabled_receipt",
        "language": "zh-CN",
        "version": "v1",
        "subject_template": "Re: {{ original_subject }}",
        "body_template": (
            "您好，{{ contact_person }}：\n\n"
            "我们已收到并确认您的报修申请。RMA 维修授权单附件当前未启用自动发送，"
            "后续将由工作人员继续处理。\n\n谢谢。"
        ),
    },
    {
        "template_code": "rma_overseas_in_warranty_en",
        "template_name": "Overseas RMA - In Warranty",
        "template_type": "rma_authorization_overseas_in_warranty",
        "language": "en-US",
        "version": "overseas_in_warranty_v1",
        "subject_template": "Re: {{ original_subject }}",
        "body_template": (
            "Dear Customer,\n\n"
            "The RMA authorization form is attached for your review. Please print it and include it "
            "in the package sent to AccoTEST.\n"
            "Please ensure that the board is securely packed and that the return address on the RMA form is correct.\n\n"
            "Please ship the faulty board to:\n"
            "{{ return_address_block }}\n\n"
            "Please note:\n"
            "1. Please attach the fault data to the email.\n"
            "2. Before shipment, please provide photos of the physical goods and outer packaging by email. "
            "The nameplate information must be clear for import customs clearance.\n"
            "3. On your shipping invoice, please state \"No commercial value as sample\".\n"
            "4. Invoices and packing lists should avoid the following words: old, repaired, returned, used, and national.\n"
            "5. The recommended declared value is between USD 50 and USD 100.\n"
            "6. If DHL is used and the value of the goods is less than RMB 5,000, please state \"NO KJ3\" in the commodity name.\n"
            "7. Please pack the boards separately. Place one or two boards in each box.\n\n"
            "Thank you for your cooperation!"
        ),
    },
    {
        "template_code": "rma_overseas_out_of_warranty_en",
        "template_name": "Overseas RMA - Out of Warranty",
        "template_type": "rma_authorization_overseas_out_of_warranty",
        "language": "en-US",
        "version": "overseas_out_warranty_v1",
        "subject_template": "Re: {{ original_subject }}",
        "body_template": (
            "Dear Customer,\n\nThe board is out of warranty.\n"
            "The RMA authorization form is attached for your review. Please print it and include it "
            "in the package sent to AccoTEST.\n"
            "Please ensure that the board is securely packed and that the return address on the RMA form is correct.\n\n"
            "Please ship the faulty board to:\n"
            "{{ return_address_block }}\n\n"
            "Please note:\n"
            "1. Please attach the fault data to the email.\n"
            "2. Before shipment, please provide photos of the physical goods and outer packaging by email. "
            "The nameplate information must be clear for import customs clearance.\n"
            "3. On your shipping invoice, please state \"No commercial value as sample\".\n"
            "4. Invoices and packing lists should avoid the following words: old, repaired, returned, used, and national.\n"
            "5. The recommended declared value is between USD 50 and USD 100.\n"
            "6. If DHL is used and the value of the goods is less than RMB 5,000, please state \"NO KJ3\" in the commodity name.\n"
            "7. Please pack the boards separately. Place one or two boards in each box.\n\n"
            "Thank you for your cooperation!"
        ),
    },
    {
        "template_code": "rma_overseas_st_pickup_en",
        "template_name": "Overseas RMA - ST Reverse Pick-up",
        "template_type": "rma_authorization_overseas_st_pickup",
        "language": "en-US",
        "version": "overseas_st_pickup_v1",
        "subject_template": "Re: {{ original_subject }}",
        "body_template": (
            "Dear Customer,\n\n"
            "The RMA authorization form is attached for your review. Please print it and include it "
            "in the package sent to AccoTEST.\n"
            "Please ensure that the board is securely packed and that the return address on the RMA form is correct.\n\n"
            "The following information is required to arrange reverse pick-up:\n"
            "1. The specific pick-up date and time window. After confirmation, we will arrange for SF Express to collect the package.\n"
            "2. The detailed pick-up address, contact name, and contact phone number.\n"
            "3. Package details: total number of boxes, gross weight of each box with units, number of boards in each box, and the SN of every board.\n"
            "4. Please print the RMA authorization form and place it inside the package.\n\n"
            "Return address:\n"
            "{{ return_address_block }}\n\n"
            "Before shipment, please provide photos of the physical goods and outer packaging by email. "
            "The nameplate information must be clear for import customs clearance.\n\n"
            "Thank you for your cooperation!"
        ),
    },
    {
        "template_code": "repair_receipt_en",
        "template_name": "Repair Request Receipt",
        "template_type": "receipt",
        "language": "en-US",
        "version": "v1",
        "subject_template": "Re: {{ original_subject }}",
        "body_template": (
            "Dear {{ contact_person }},\n\nWe have received your repair request and created ticket {{ ticket_no }}. "
            "Further updates will be sent in this email thread.\n\nThank you."
        ),
    },
    {
        "template_code": "missing_info_en",
        "template_name": "Missing Repair Information",
        "template_type": "missing_fields",
        "language": "en-US",
        "version": "v1",
        "subject_template": "Re: {{ original_subject }}",
        "body_template": (
            "Dear {{ contact_person }},\n\nPlease provide the following information for ticket {{ ticket_no }}:\n"
            "{{ missing_fields }}\n\nPlease reply directly to this email. Thank you."
        ),
    },
    {
        "template_code": "followup_en",
        "template_name": "Repair Follow-up",
        "template_type": "followup",
        "language": "en-US",
        "version": "v1",
        "subject_template": "Re: {{ original_subject }}",
        "body_template": (
            "Dear {{ contact_person }},\n\nPlease provide the following information for ticket {{ ticket_no }}:\n"
            "{{ missing_fields }}\n\nPlease reply directly to this email. Thank you."
        ),
    },
    {
        "template_code": "sn_invalid_en",
        "template_name": "SN Confirmation Required",
        "template_type": "sn_invalid",
        "language": "en-US",
        "version": "v1",
        "subject_template": "Re: {{ original_subject }}",
        "body_template": (
            "Dear {{ contact_person }},\n\nThe supplied device SN did not pass validation. "
            "Please reply with the complete and correct SN so we can continue ticket {{ ticket_no }}."
        ),
    },
    {
        "template_code": "manual_review_notice_en",
        "template_name": "Manual Review Notice",
        "template_type": "manual_review",
        "language": "en-US",
        "version": "v1",
        "subject_template": "Re: {{ original_subject }}",
        "body_template": (
            "Dear {{ contact_person }},\n\nYour repair request {{ ticket_no }} is under manual review. "
            "We will contact you in this email thread if additional information is required."
        ),
    },
    {
        "template_code": "rma_attachment_disabled_receipt_en",
        "template_name": "RMA Attachment Disabled Receipt",
        "template_type": "rma_attachment_disabled_receipt",
        "language": "en-US",
        "version": "v1",
        "subject_template": "Re: {{ original_subject }}",
        "body_template": (
            "Dear {{ contact_person }},\n\nWe have received your repair request. Automatic delivery of the "
            "RMA authorization attachment is currently disabled and our team will continue handling it."
        ),
    },
)


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
            password_hash=hash_password(settings.DEFAULT_ADMIN_PASSWORD),
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
            "html_body_template": values.get("html_body_template"),
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
