from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence
from uuid import UUID


class SapMiddlewareError(RuntimeError):
    """Base error translated at the SQL Server integration boundary."""


class SapMiddlewareConfigurationError(SapMiddlewareError):
    pass


class SapSchemaMismatchError(SapMiddlewareError):
    pass


class SapSnapshotUnstableError(SapMiddlewareError):
    pass


class SapTransactionError(SapMiddlewareError):
    pass


class SapUnknownCommitStateError(SapMiddlewareError):
    """The caller must reconcile by SourceRequestID before retrying."""


@dataclass(frozen=True)
class ConnectionHealth:
    configured: bool
    status: str
    missing: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExternalSnRecord:
    sn: str
    customer_code: str
    customer_name: str
    material_code: str
    material_name: str | None = None
    values: dict[str, Any] = field(default_factory=dict)
    raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExternalRmaSubmissionItem:
    source_request_id: UUID
    sn: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ExternalRmaResult:
    source_request_id: UUID
    sn: str | None
    rma_no: str | None
    raw_data: dict[str, Any] = field(default_factory=dict)


class SapMiddlewareAdapter(Protocol):
    async def check_connection(self) -> ConnectionHealth: ...

    async def fetch_all_sn_records(self) -> Sequence[ExternalSnRecord]: ...

    async def submit_rma_batch(self, items: Sequence[ExternalRmaSubmissionItem]) -> None: ...

    async def find_records_by_source_request_ids(
        self, source_request_ids: Sequence[UUID]
    ) -> Sequence[ExternalRmaResult]: ...
