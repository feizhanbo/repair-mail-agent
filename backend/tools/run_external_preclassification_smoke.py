from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

from sqlalchemy import select

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings
from app.core.database import AsyncSessionLocal, engine
from app.integrations.llm_gateway import public_llm_routes
from app.models import AiCallLog
from app.schemas.business import EmailIngestRequest
from app.services.mail_preclassification import classify_mail
from app.services.storage import (
    delete_oss_object,
    download_oss_object_bytes,
    upload_bytes_to_oss,
)


async def main() -> None:
    if settings.IMAP_FETCH_ENABLED or settings.AUTO_SEND_ENABLED or settings.RMA_AUTO_SEND_ENABLED:
        raise RuntimeError("EXTERNAL_SMOKE_REQUIRES_ALL_MAIL_SEND_AND_FETCH_SWITCHES_OFF")

    marker = uuid.uuid4().hex
    payload = EmailIngestRequest(
        mailbox_account="external-smoke@example.test",
        folder_name="INBOX",
        message_id=f"<preclassification-smoke-{marker}@example.test>",
        from_address="customer@example.test",
        to_addresses="rma@example.test",
        subject="[TEST ONLY] 新设备报修申请",
        text_body=(
            "设备无法启动，申请返厂维修。序列号 SN-TEST-20260820，"
            "联系人测试用户，电话 13800000000。"
        ),
        attachments=[],
    )
    content = f"AIRMA external OSS smoke {marker}".encode()
    oss_identity: tuple[str, str, str | None] | None = None
    try:
        async with AsyncSessionLocal() as session:
            decision = await classify_mail(
                payload,
                session=session,
                mail_fetch_record_id=None,
                thread_summary={"test_only": True, "active_ticket": None},
            )
            if decision.reason_code == "PRECLASSIFICATION_PROVIDER_FAILED":
                failed_log = await session.scalar(
                    select(AiCallLog).order_by(AiCallLog.id.desc()).limit(1)
                )
                detail = ":".join(filter(None, (
                    getattr(failed_log, "error_code", None),
                    getattr(failed_log, "provider_name", None),
                    getattr(failed_log, "model_name", None),
                )))
                raise RuntimeError(f"TEXT_MODEL_SMOKE_FAILED:{detail or 'UNKNOWN'}")
            obj = await upload_bytes_to_oss(
                session,
                content=content,
                original_file_name=f"external-smoke-{marker}.txt",
                content_type="text/plain",
                source_type="integration_smoke",
            )
            downloaded = await download_oss_object_bytes(session, oss_object_id=obj.id)
            if downloaded != content:
                raise RuntimeError("OSS_SMOKE_CONTENT_MISMATCH")
            oss_identity = (obj.bucket, obj.object_key, obj.endpoint)
            await session.rollback()

        if oss_identity is None:
            raise RuntimeError("OSS_SMOKE_IDENTITY_MISSING")
        deleted = await delete_oss_object(
            bucket=oss_identity[0], object_key=oss_identity[1], endpoint=oss_identity[2]
        )
        if not deleted.deleted:
            raise RuntimeError("OSS_SMOKE_DELETE_NOT_CONFIRMED")

        routes = public_llm_routes()
        print(json.dumps({
            "status": "passed",
            "database": "AIRMA_test",
            "send_and_fetch_switches": "disabled",
            "classification": {
                "intent_type": decision.intent_type,
                "handling_level": decision.handling_level,
                "confidence": decision.confidence,
                "reason_code": decision.reason_code,
            },
            "mail_classification_route": routes["mail_classification"],
            "oss": {"upload": "passed", "download": "passed", "delete": "passed"},
            "database_transaction": "rolled_back",
        }, ensure_ascii=False, default=str))
    finally:
        if oss_identity is not None:
            try:
                await delete_oss_object(
                    bucket=oss_identity[0], object_key=oss_identity[1], endpoint=oss_identity[2]
                )
            except Exception:
                pass
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
