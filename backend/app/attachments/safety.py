from __future__ import annotations

import gzip
import io
import re
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class ArchiveSafetyResult:
    status: str
    safe: bool
    warnings: tuple[str, ...] = ()
    member_count: int | None = None
    expanded_size: int | None = None


@dataclass(frozen=True)
class ArchiveMemberInfo:
    name: str
    size: int
    compressed_size: int
    encrypted: bool = False
    is_link: bool = False


def inspect_archive_safety(
    content: bytes | None,
    archive_format: str,
    *,
    max_members: int = 1000,
    max_expanded_bytes: int = 100 * 1024 * 1024,
    max_compression_ratio: int = 100,
) -> ArchiveSafetyResult:
    """Inspect archive metadata without writing members to the filesystem."""
    if content is None:
        return ArchiveSafetyResult(status="unscanned_archive", safe=False, warnings=("ARCHIVE_CONTENT_UNAVAILABLE",))
    try:
        members = _member_infos(content, archive_format, max_expanded_bytes=max_expanded_bytes)
    except _ArchiveInspectionUnavailable:
        return ArchiveSafetyResult(
            status="inspection_unavailable",
            safe=False,
            warnings=("ARCHIVE_INSPECTION_UNAVAILABLE",),
        )
    except _ArchiveEncrypted:
        return ArchiveSafetyResult(status="unsafe", safe=False, warnings=("ARCHIVE_ENCRYPTED",))
    except Exception:
        return ArchiveSafetyResult(status="unsafe", safe=False, warnings=("ARCHIVE_INVALID",))

    warnings: list[str] = []
    if not members:
        warnings.append("ARCHIVE_EMPTY")
    if len(members) > max_members:
        warnings.append("ARCHIVE_MEMBER_LIMIT_EXCEEDED")
    expanded_size = sum(member.size for member in members)
    compressed_size = sum(max(1, member.compressed_size) for member in members)
    if expanded_size > max_expanded_bytes:
        warnings.append("ARCHIVE_EXPANDED_SIZE_EXCEEDED")
    if expanded_size / max(1, compressed_size) > max_compression_ratio:
        warnings.append("ARCHIVE_COMPRESSION_RATIO_EXCEEDED")
    if any(member.encrypted for member in members):
        warnings.append("ARCHIVE_ENCRYPTED")
    if any(member.is_link for member in members):
        warnings.append("ARCHIVE_LINK_MEMBER")
    if any(_unsafe_member_path(member.name) for member in members):
        warnings.append("ARCHIVE_PATH_TRAVERSAL")
    normalized_names = [member.name.replace("\\", "/").casefold() for member in members]
    if len(normalized_names) != len(set(normalized_names)):
        warnings.append("ARCHIVE_DUPLICATE_MEMBER")
    if any(_looks_like_archive(member.name) for member in members):
        warnings.append("ARCHIVE_NESTED_MEMBER")

    blocking = {
        "ARCHIVE_EMPTY",
        "ARCHIVE_MEMBER_LIMIT_EXCEEDED",
        "ARCHIVE_EXPANDED_SIZE_EXCEEDED",
        "ARCHIVE_COMPRESSION_RATIO_EXCEEDED",
        "ARCHIVE_ENCRYPTED",
        "ARCHIVE_LINK_MEMBER",
        "ARCHIVE_PATH_TRAVERSAL",
        "ARCHIVE_DUPLICATE_MEMBER",
    }
    safe = not any(item in blocking for item in warnings)
    return ArchiveSafetyResult(
        status="inspected_safe" if safe else "unsafe",
        safe=safe,
        warnings=tuple(warnings),
        member_count=len(members),
        expanded_size=expanded_size,
    )


def _member_infos(content: bytes, archive_format: str, *, max_expanded_bytes: int) -> list[ArchiveMemberInfo]:
    if archive_format == "zip":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            corrupt = archive.testzip()
            if corrupt is not None:
                raise ValueError("ARCHIVE_CORRUPT")
            return [
                ArchiveMemberInfo(
                    name=item.filename,
                    size=item.file_size,
                    compressed_size=item.compress_size,
                    encrypted=bool(item.flag_bits & 0x1),
                    is_link=((item.external_attr >> 16) & 0o170000) == 0o120000,
                )
                for item in members
                if not item.is_dir()
            ]
    if archive_format in {"tar", "tar_gz"}:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as archive:
            return [
                ArchiveMemberInfo(
                    name=item.name,
                    size=item.size,
                    compressed_size=max(1, item.size),
                    is_link=item.issym() or item.islnk() or item.isdev(),
                )
                for item in archive.getmembers()
                if not item.isdir()
            ]
    if archive_format == "gzip":
        with gzip.GzipFile(fileobj=io.BytesIO(content)) as archive:
            expanded = archive.read(max_expanded_bytes + 1)
        return [ArchiveMemberInfo(name="payload", size=len(expanded), compressed_size=len(content))]
    if archive_format == "7z":
        try:
            import py7zr
        except ImportError as exc:
            raise _ArchiveInspectionUnavailable from exc
        with py7zr.SevenZipFile(io.BytesIO(content), mode="r") as archive:
            if archive.needs_password():
                raise _ArchiveEncrypted
            return [
                ArchiveMemberInfo(
                    name=item.filename,
                    size=int(item.uncompressed or 0),
                    compressed_size=int(item.compressed or 0),
                    is_link=bool(item.is_symlink),
                )
                for item in archive.list()
                if not item.is_directory
            ]
    if archive_format == "rar":
        try:
            import rarfile
        except ImportError as exc:
            raise _ArchiveInspectionUnavailable from exc
        with rarfile.RarFile(io.BytesIO(content)) as archive:
            return [
                ArchiveMemberInfo(
                    name=item.filename,
                    size=item.file_size,
                    compressed_size=item.compress_size,
                    encrypted=item.needs_password(),
                    is_link=item.is_symlink(),
                )
                for item in archive.infolist()
                if not item.isdir()
            ]
    raise _ArchiveInspectionUnavailable


class _ArchiveInspectionUnavailable(Exception):
    pass


class _ArchiveEncrypted(Exception):
    pass


def _unsafe_member_path(name: str) -> bool:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    return path.is_absolute() or ".." in path.parts or bool(re.match(r"^[A-Za-z]:", normalized))


def _looks_like_archive(name: str) -> bool:
    lower = name.casefold()
    return lower.endswith((".zip", ".rar", ".7z", ".tar", ".tar.gz", ".tgz", ".gz"))
