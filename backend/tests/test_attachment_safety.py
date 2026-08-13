from __future__ import annotations

import io
import gzip
import tarfile
import zipfile

import pytest

from app.attachments.detector import detect_file_type
from app.attachments.safety import inspect_archive_safety
from app.attachments.archive import extract_archive_members
from app.services import attachment_parser


def _zip(entries: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return buffer.getvalue()


def test_detector_is_deterministic_and_keeps_office_containers_distinct() -> None:
    assert detect_file_type(file_name="repair.docx", content_type="application/zip") == "docx"
    assert detect_file_type(file_name="scan.bin", content_type="application/pdf") == "pdf"
    assert detect_file_type(file_name="unknown.bin", content_type="application/octet-stream") is None


@pytest.mark.parametrize(
    ("entries", "warning"),
    [
        ([('../escape.txt', b'x')], "ARCHIVE_PATH_TRAVERSAL"),
        ([('a.txt', b'a'), ('A.TXT', b'b')], "ARCHIVE_DUPLICATE_MEMBER"),
        ([('nested.zip', b'PK\x03\x04')], "ARCHIVE_NESTED_MEMBER"),
    ],
)
def test_zip_safety_reports_member_risks(entries: list[tuple[str, bytes]], warning: str) -> None:
    result = inspect_archive_safety(_zip(entries), "zip")

    assert warning in result.warnings
    assert result.safe is (warning not in {"ARCHIVE_PATH_TRAVERSAL", "ARCHIVE_DUPLICATE_MEMBER"})


def test_zip_bomb_limits_block_processing() -> None:
    result = inspect_archive_safety(
        _zip([("large.txt", b"0" * 5000)]),
        "zip",
        max_expanded_bytes=1000,
        max_compression_ratio=10,
    )

    assert result.safe is False
    assert "ARCHIVE_EXPANDED_SIZE_EXCEEDED" in result.warnings


def test_invalid_zip_requires_manual_handling() -> None:
    result = inspect_archive_safety(b"PK\x03\x04not-a-zip", "zip")

    assert result.safe is False
    assert result.status == "unsafe"
    assert result.warnings == ("ARCHIVE_INVALID",)


def test_binary_parser_returns_normalized_content_without_ai() -> None:
    result = attachment_parser.parse_binary_content("txt", b"SN001 needs repair", max_pdf_pages=15)

    assert result.file_type == "txt"
    assert result.parser == "txt"
    assert result.text == "SN001 needs repair"
    assert result.semantic_mode == "text"


def test_nested_zip_is_bounded_and_flattens_leaf_members() -> None:
    nested = _zip([("inner.txt", b"SN-NESTED")])
    result = extract_archive_members(_zip([("nested.zip", nested)]), "zip", max_depth=1)

    assert [(item.path, item.content, item.archive_depth) for item in result.members] == [
        ("nested.zip/inner.txt", b"SN-NESTED", 1)
    ]
    assert "ARCHIVE_NESTED_MEMBER" in result.warnings


def test_nested_zip_exceeding_depth_is_rejected() -> None:
    level_two = _zip([("leaf.txt", b"leaf")])
    level_one = _zip([("two.zip", level_two)])

    with pytest.raises(ValueError, match="ARCHIVE_MAX_DEPTH_EXCEEDED"):
        extract_archive_members(_zip([("one.zip", level_one)]), "zip", max_depth=1)


def test_tar_and_gzip_members_are_read_without_shell_extraction() -> None:
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w") as archive:
        info = tarfile.TarInfo("repair.txt")
        payload = b"SN-TAR"
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    tar_result = extract_archive_members(tar_buffer.getvalue(), "tar")
    gzip_result = extract_archive_members(gzip.compress(b"SN-GZIP"), "gzip")

    assert tar_result.members[0].content == b"SN-TAR"
    assert gzip_result.members[0].content == b"SN-GZIP"


def test_7z_members_are_inspected_and_read_in_bounded_directory() -> None:
    import py7zr

    buffer = io.BytesIO()
    with py7zr.SevenZipFile(buffer, mode="w") as archive:
        archive.writestr(b"SN-7Z", "repair.txt")
    result = extract_archive_members(buffer.getvalue(), "7z")

    assert result.safety.safe is True
    assert [(item.path, item.content) for item in result.members] == [("repair.txt", b"SN-7Z")]


def test_encrypted_7z_requires_manual_handling() -> None:
    import py7zr

    buffer = io.BytesIO()
    with py7zr.SevenZipFile(buffer, mode="w", password="secret") as archive:
        archive.writestr(b"SN-SECRET", "repair.txt")
    safety = inspect_archive_safety(buffer.getvalue(), "7z")

    assert safety.safe is False
    assert safety.warnings == ("ARCHIVE_ENCRYPTED",)
