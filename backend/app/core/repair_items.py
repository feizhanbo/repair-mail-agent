from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable


_SN_LIKE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{3,99}$")


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def normalize_board_code(value: Any) -> str:
    return unicodedata.normalize("NFKC", _text(value)).strip().upper()


def normalize_board_name(value: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", _text(value))).strip()


def _serial_number_is_sn(value: Any) -> bool:
    candidate = _text(value)
    return bool(candidate and not candidate.isdigit() and _SN_LIKE_PATTERN.fullmatch(candidate))


def normalize_repair_item(payload: dict[str, Any], *, default_line_no: int | None = None) -> dict[str, Any]:
    """Normalize one item without treating an Excel row number as an SN."""
    normalized = dict(payload)
    serial_number = _text(normalized.get("serial_number"))

    line_no = normalized.get("line_no")
    if line_no in {None, ""} and serial_number.isdigit():
        line_no = int(serial_number)
    if line_no in {None, ""} and default_line_no is not None:
        line_no = default_line_no
    if line_no not in {None, ""}:
        try:
            normalized["line_no"] = int(line_no)
        except (TypeError, ValueError):
            if default_line_no is not None:
                normalized["line_no"] = default_line_no

    sn = next(
        (
            _text(normalized.get(alias))
            for alias in ("sn", "part_serial_no", "serial_no", "device_sn")
            if _text(normalized.get(alias))
        ),
        "",
    )
    if not sn and _serial_number_is_sn(serial_number):
        sn = serial_number
    if sn:
        normalized["sn"] = sn
    else:
        normalized.pop("sn", None)

    if not _text(normalized.get("board_code")):
        board_code = next(
            (
                _text(normalized.get(alias))
                for alias in ("board_model", "board_type")
                if _text(normalized.get(alias))
            ),
            "",
        )
        if board_code:
            normalized["board_code"] = board_code
    if _text(normalized.get("board_code")):
        normalized["board_code"] = normalize_board_code(normalized["board_code"])
    if _text(normalized.get("board_name")):
        normalized["board_name"] = normalize_board_name(normalized["board_name"])

    if not _text(normalized.get("failure_description")):
        failure_description = next(
            (
                _text(normalized.get(alias))
                for alias in ("fault_description", "problem_description", "failure_information")
                if _text(normalized.get(alias))
            ),
            "",
        )
        if failure_description:
            normalized["failure_description"] = failure_description
    return normalized


def canonical_sn(payload: dict[str, Any]) -> str:
    return _text(normalize_repair_item(payload).get("sn")).upper()


def normalize_repair_items(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize and de-duplicate by canonical SN while retaining first-seen order."""
    normalized_items: list[dict[str, Any]] = []
    by_sn: dict[str, dict[str, Any]] = {}
    for index, raw_item in enumerate(items, start=1):
        item = normalize_repair_item(raw_item, default_line_no=index)
        sn = canonical_sn(item)
        if sn and sn in by_sn:
            existing = by_sn[sn]
            for name, value in item.items():
                if _blank(existing.get(name)) and not _blank(value):
                    existing[name] = value
            continue
        normalized_items.append(item)
        if sn:
            by_sn[sn] = item
    return normalized_items
