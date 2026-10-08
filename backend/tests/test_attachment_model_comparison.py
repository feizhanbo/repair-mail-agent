"""Standalone test: compare qwen3.7-plus vs qwen3.8-flash on real Excel attachment parsing."""
from __future__ import annotations

import asyncio
import imaplib
import io
import json
import sys
from email import policy
from email.parser import BytesParser
from pathlib import Path

# Add backend to path so `app.*` imports work when running from tests/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.ai.schemas import (
    AttachmentContentEvidence,
    AttachmentParseInput,
    AttachmentMetadata,
    AttachmentFileType,
)
from app.ai.prompts import ATTACHMENT_TEXT
from app.integrations.llm_gateway import load_llm_routes, LlmTask
from app.services.attachment_parser import _extract_xlsx


TARGET_MESSAGE_ID = "202609201350414344885@accotest.com"


def fetch_excel_attachment_from_imap() -> tuple[str, bytes]:
    """Connect to IMAP, find email by message_id, extract Excel attachment.

    Returns (filename, content_bytes).
    """
    print(f"[IMAP] Connecting to {settings.IMAP_HOST}:{settings.IMAP_PORT}...")
    imap = imaplib.IMAP4_SSL(settings.IMAP_HOST, settings.IMAP_PORT)
    try:
        imap.login(settings.IMAP_USER, settings.IMAP_PASSWORD)
        print(f"[IMAP] Logged in as {settings.IMAP_USER}")
        imap.select("INBOX", readonly=True)

        # Search by message_id (try with angle brackets first, then without)
        status, data = imap.search(None, f'HEADER Message-ID "<{TARGET_MESSAGE_ID}>"')
        if status != "OK" or not data[0]:
            status, data = imap.search(None, f'HEADER Message-ID "{TARGET_MESSAGE_ID}"')

        if status != "OK" or not data[0]:
            raise RuntimeError(f"Email with message_id <{TARGET_MESSAGE_ID}> not found in INBOX")

        uid_list = data[0].split()
        print(f"[IMAP] Found {len(uid_list)} matching email(s), using first")
        uid = uid_list[0]

        status, msg_data = imap.fetch(uid, "(RFC822)")
        if status != "OK":
            raise RuntimeError(f"Failed to fetch email uid={uid}")

        eml_bytes = msg_data[0][1]
        print(f"[IMAP] Downloaded {len(eml_bytes)} bytes")
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    # Parse eml and extract Excel attachment
    parser = BytesParser(policy=policy.default)
    msg = parser.parsebytes(eml_bytes)

    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = part.get_filename()
        content_type = part.get_content_type()
        if filename and (
            "spreadsheetml" in content_type
            or content_type == "application/vnd.ms-excel"
            or filename.lower().endswith((".xlsx", ".xls"))
        ):
            content_bytes = part.get_content()
            if isinstance(content_bytes, str):
                content_bytes = content_bytes.encode("utf-8")
            print(f"[IMAP] Found Excel attachment: {filename} ({len(content_bytes)} bytes)")
            return filename, content_bytes

    raise RuntimeError("No Excel attachment found in the email")


def build_parse_input(file_name: str, text: str) -> AttachmentParseInput:
    """Build the AttachmentParseInput for the model."""
    lines = [line for line in text.splitlines() if line.strip()][:8]
    local_summary = "\n".join(lines)[:1000]
    key_points = lines[:5]
    metadata = AttachmentMetadata(
        attachment_id=0,
        file_name=file_name,
        file_type=AttachmentFileType.XLSX,
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        truncated=False,
    )
    return AttachmentParseInput(
        metadata=metadata,
        local_summary=local_summary,
        local_key_points=key_points,
        content=text,
    )


async def call_model(
    model_name: str,
    api_key: str,
    base_url: str,
    parse_input: AttachmentParseInput,
) -> dict:
    """Call a specific model and return parsed result or error."""
    import time
    import traceback
    from langchain_openai import ChatOpenAI

    print(f"\n[MODEL] Calling {model_name}...")
    start_time = time.time()
    try:
        llm = ChatOpenAI(
            model=model_name,
            api_key=api_key,
            base_url=base_url,
            temperature=0.0,
            timeout=120,
            max_retries=1,
            model_kwargs={"extra_body": {"enable_thinking": False}},
        )
        structured = llm.with_structured_output(
            schema=AttachmentContentEvidence,
            method="json_schema",
            include_raw=True,
            strict=True,
        )
        messages = [
            {"role": "system", "content": ATTACHMENT_TEXT.system},
            {"role": "user", "content": parse_input.model_dump_json()},
        ]
        result = await structured.ainvoke(messages)
        elapsed = time.time() - start_time

        # with include_raw=True, result is a dict with 'parsed', 'raw', 'parsing_error'
        if isinstance(result, dict):
            parsed = result.get("parsed")
            parsing_error = result.get("parsing_error")
            if parsing_error:
                raise RuntimeError(f"Structured output parsing error: {parsing_error}")
            if isinstance(parsed, AttachmentContentEvidence):
                data = parsed.model_dump(mode="json")
            elif isinstance(parsed, dict):
                data = parsed
            else:
                raise RuntimeError(f"Unexpected parsed type: {type(parsed)}")
        else:
            data = result.model_dump(mode="json")

        print(f"[MODEL] {model_name} returned successfully in {elapsed:.2f}s")
        return {"status": "ok", "model": model_name, "result": data, "elapsed_seconds": elapsed}
    except Exception as exc:
        elapsed = time.time() - start_time
        error_type = type(exc).__name__
        error_detail = str(exc)
        print(f"[MODEL] {model_name} failed after {elapsed:.2f}s")
        print(f"[MODEL]   Error type: {error_type}")
        print(f"[MODEL]   Error detail: {error_detail}")
        print(f"[MODEL]   Traceback:\n{traceback.format_exc()}")
        return {
            "status": "error",
            "model": model_name,
            "error": error_detail,
            "error_type": error_type,
            "elapsed_seconds": elapsed,
        }


async def main() -> None:
    print("=" * 70)
    print("Attachment Model Comparison Test")
    print(f"Target message_id: <{TARGET_MESSAGE_ID}>")
    print("=" * 70)

    # Step 1: Fetch from IMAP
    try:
        file_name, content_bytes = fetch_excel_attachment_from_imap()
    except Exception as exc:
        print(f"\n[ERROR] {exc}")
        sys.exit(1)

    # Step 2: Extract text from Excel
    try:
        text = _extract_xlsx(content_bytes)
        print(f"\n[PARSE] Extracted {len(text)} chars from Excel")
        print(f"[PARSE] Preview:\n{text[:500]}")
    except Exception as exc:
        print(f"\n[ERROR] Failed to extract Excel text: {exc}")
        sys.exit(1)

    # Step 3: Build input
    parse_input = build_parse_input(file_name, text)

    # Step 4: Load route config to get API keys / base_urls per model
    routes = load_llm_routes()
    route = routes[LlmTask.ATTACHMENT_TEXT_PARSE]

    models_to_test: list[tuple[str, str, str]] = [
        (route.primary.model, route.primary.api_key, route.primary.base_url),
    ]
    if route.fallback:
        models_to_test.append(
            (route.fallback.model, route.fallback.api_key, route.fallback.base_url)
        )

    print(f"\n[TEST] Testing {len(models_to_test)} models: {[m[0] for m in models_to_test]}")

    # Step 5: Call each model sequentially
    results: list[dict] = []
    for model_name, api_key, base_url in models_to_test:
        result = await call_model(model_name, api_key, base_url, parse_input)
        results.append(result)

    # Step 6: Print comparison
    print("\n" + "=" * 70)
    print("RESULTS COMPARISON")
    print("=" * 70)

    for r in results:
        print(f"\n--- {r['model']} (status: {r['status']}) ---")
        if r["status"] == "ok":
            res = r["result"]
            print(f"  summary: {str(res.get('summary', ''))[:200]}")
            print(f"  key_points: {res.get('key_points', [])}")
            print(f"  candidate_fields ({len(res.get('candidate_fields', []))}):")
            for cf in res.get("candidate_fields", []):
                print(
                    f"    - {cf.get('field')}: {cf.get('value')} "
                    f"(confidence={cf.get('source', {}).get('source_type', 'N/A')})"
                )
            print(f"  candidate_items ({len(res.get('candidate_items', []))}):")
            for ci in res.get("candidate_items", []):
                print(f"    - index={ci.get('candidate_index')}: {ci.get('values', [])}")
            print(f"  warnings: {res.get('warnings', [])}")
        else:
            print(f"  ERROR: {r.get('error', 'unknown')}")

    # Diff if both succeeded
    ok_results = [r for r in results if r["status"] == "ok"]
    if len(ok_results) >= 2:
        r1, r2 = ok_results[0], ok_results[1]
        print(f"\n--- DIFF: {r1['model']} vs {r2['model']} ---")
        res1, res2 = r1["result"], r2["result"]

        # Compare summary
        if res1.get("summary") == res2.get("summary"):
            print("  summary: IDENTICAL")
        else:
            print("  summary DIFFERS:")
            print(f"    {r1['model']}: {str(res1.get('summary', ''))[:100]}")
            print(f"    {r2['model']}: {str(res2.get('summary', ''))[:100]}")

        # Compare candidate_fields
        fields1 = {
            cf.get("field"): cf.get("value")
            for cf in res1.get("candidate_fields", [])
        }
        fields2 = {
            cf.get("field"): cf.get("value")
            for cf in res2.get("candidate_fields", [])
        }
        all_fields = sorted(set(fields1) | set(fields2))
        for f in all_fields:
            v1, v2 = fields1.get(f), fields2.get(f)
            if v1 == v2:
                print(f"  {f}: SAME ({v1})")
            else:
                print(f"  {f}: DIFFERS ({r1['model']}={v1}, {r2['model']}={v2})")

        # Compare candidate_items count
        items1 = len(res1.get("candidate_items", []))
        items2 = len(res2.get("candidate_items", []))
        if items1 == items2:
            print(f"  candidate_items count: SAME ({items1})")
        else:
            print(f"  candidate_items count: DIFFERS ({r1['model']}={items1}, {r2['model']}={items2})")

        # Compare warnings
        w1 = res1.get("warnings", [])
        w2 = res2.get("warnings", [])
        if w1 == w2:
            print(f"  warnings: SAME ({len(w1)})")
        else:
            print(f"  warnings DIFFERS ({r1['model']}={len(w1)}, {r2['model']}={len(w2)})")
    elif len(ok_results) == 1:
        print(f"\n  Only {ok_results[0]['model']} succeeded; cannot diff.")
    else:
        print("\n  Both models failed; no diff available.")

    print("\n" + "=" * 70)
    print("DONE")


if __name__ == "__main__":
    asyncio.run(main())
