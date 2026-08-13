from __future__ import annotations

import gzip
import io
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePath

from app.attachments.safety import ArchiveSafetyResult, inspect_archive_safety
from app.services.attachment_precheck import detect_archive_format


@dataclass(frozen=True)
class ExtractedArchiveMember:
    path: str
    content: bytes
    archive_depth: int


@dataclass(frozen=True)
class ArchiveExtractionResult:
    members: tuple[ExtractedArchiveMember, ...]
    safety: ArchiveSafetyResult
    warnings: tuple[str, ...] = ()


def extract_archive_members(
    content: bytes,
    archive_format: str,
    *,
    max_depth: int = 2,
    max_members: int = 1000,
    max_expanded_bytes: int = 100 * 1024 * 1024,
    max_compression_ratio: int = 100,
) -> ArchiveExtractionResult:
    """Extract bounded regular-file members into memory and recurse safely."""
    extracted: list[ExtractedArchiveMember] = []
    warnings: list[str] = []
    budget = {"members": 0, "bytes": 0}
    root_safety = _extract_recursive(
        content,
        archive_format,
        prefix="",
        depth=0,
        max_depth=max_depth,
        max_members=max_members,
        max_expanded_bytes=max_expanded_bytes,
        max_compression_ratio=max_compression_ratio,
        budget=budget,
        extracted=extracted,
        warnings=warnings,
    )
    return ArchiveExtractionResult(tuple(extracted), root_safety, tuple(dict.fromkeys(warnings)))


def _extract_recursive(
    content: bytes,
    archive_format: str,
    *,
    prefix: str,
    depth: int,
    max_depth: int,
    max_members: int,
    max_expanded_bytes: int,
    max_compression_ratio: int,
    budget: dict[str, int],
    extracted: list[ExtractedArchiveMember],
    warnings: list[str],
) -> ArchiveSafetyResult:
    safety = inspect_archive_safety(
        content,
        archive_format,
        max_members=max_members,
        max_expanded_bytes=max_expanded_bytes,
        max_compression_ratio=max_compression_ratio,
    )
    if not safety.safe:
        raise ValueError(safety.warnings[0] if safety.warnings else "ARCHIVE_UNSAFE")
    raw_members = _read_members(content, archive_format)
    for name, member_content in raw_members:
        budget["members"] += 1
        budget["bytes"] += len(member_content)
        if budget["members"] > max_members:
            raise ValueError("ARCHIVE_MEMBER_LIMIT_EXCEEDED")
        if budget["bytes"] > max_expanded_bytes:
            raise ValueError("ARCHIVE_EXPANDED_SIZE_EXCEEDED")
        normalized_name = name.replace("\\", "/").lstrip("/")
        path = f"{prefix}/{normalized_name}".strip("/")
        nested_format, _ = detect_archive_format(
            file_name=normalized_name,
            content_type=None,
            content=member_content,
        )
        if nested_format:
            if depth >= max_depth:
                raise ValueError("ARCHIVE_MAX_DEPTH_EXCEEDED")
            warnings.append("ARCHIVE_NESTED_MEMBER")
            _extract_recursive(
                member_content,
                nested_format,
                prefix=path,
                depth=depth + 1,
                max_depth=max_depth,
                max_members=max_members,
                max_expanded_bytes=max_expanded_bytes,
                max_compression_ratio=max_compression_ratio,
                budget=budget,
                extracted=extracted,
                warnings=warnings,
            )
            continue
        extracted.append(ExtractedArchiveMember(path, member_content, depth))
    return safety


def _read_members(content: bytes, archive_format: str) -> list[tuple[str, bytes]]:
    if archive_format == "zip":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            return [(item.filename, archive.read(item)) for item in archive.infolist() if not item.is_dir()]
    if archive_format in {"tar", "tar_gz"}:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as archive:
            result: list[tuple[str, bytes]] = []
            for item in archive.getmembers():
                if not item.isfile():
                    continue
                handle = archive.extractfile(item)
                if handle is not None:
                    result.append((item.name, handle.read()))
            return result
    if archive_format == "gzip":
        return [("payload", gzip.decompress(content))]
    if archive_format == "7z":
        import py7zr

        with tempfile.TemporaryDirectory(prefix="repair-archive-") as directory:
            root = Path(directory).resolve()
            with py7zr.SevenZipFile(io.BytesIO(content), mode="r") as archive:
                archive.extractall(path=root)
            return _read_extracted_tree(root)
    if archive_format == "rar":
        import rarfile

        with rarfile.RarFile(io.BytesIO(content)) as archive:
            return [(item.filename, archive.read(item)) for item in archive.infolist() if not item.isdir()]
    raise ValueError("ARCHIVE_FORMAT_UNSUPPORTED")


def _read_extracted_tree(root: Path) -> list[tuple[str, bytes]]:
    result: list[tuple[str, bytes]] = []
    for path in root.rglob("*"):
        resolved = path.resolve()
        if root != resolved and root not in resolved.parents:
            raise ValueError("ARCHIVE_PATH_TRAVERSAL")
        if path.is_symlink():
            raise ValueError("ARCHIVE_LINK_MEMBER")
        if path.is_file():
            result.append((str(PurePath(path.relative_to(root))).replace("\\", "/"), path.read_bytes()))
    return result
