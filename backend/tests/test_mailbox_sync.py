from __future__ import annotations

from datetime import datetime

import pytest

from app.models import MailboxSyncState
from app.services.mailbox_sync import (
    MailboxSyncConfigurationError,
    apply_uid_validity,
    get_or_create_sync_state,
    imap_since_date,
    mark_initial_complete,
    record_discovery,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeSession:
    def __init__(self) -> None:
        self.added = []

    async def scalar(self, _statement):
        return None

    def add(self, row) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        for index, row in enumerate(self.added, start=1):
            row.id = row.id or index


@pytest.mark.anyio
async def test_new_mailbox_requires_explicit_initial_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.mailbox_sync.settings.IMAP_INITIAL_SYNC_START_AT", None)
    with pytest.raises(MailboxSyncConfigurationError, match="IMAP_INITIAL_SYNC_START_AT_REQUIRED"):
        await get_or_create_sync_state(
            FakeSession(), mailbox_account="repair@example.com", folder_name="INBOX"
        )


@pytest.mark.anyio
async def test_new_mailbox_starts_initializing_from_configured_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    boundary = datetime(2026, 8, 1)
    monkeypatch.setattr("app.services.mailbox_sync.settings.IMAP_INITIAL_SYNC_START_AT", boundary)
    state = await get_or_create_sync_state(
        FakeSession(), mailbox_account="repair@example.com", folder_name="INBOX"
    )
    assert state.sync_mode == "initializing"
    assert state.initial_sync_start_at == boundary
    assert imap_since_date(boundary) == "01-Aug-2026"


def test_uidvalidity_change_invalidates_cursor_and_enters_rebaseline() -> None:
    state = MailboxSyncState(
        mailbox_account="repair@example.com",
        folder_name="INBOX",
        uid_validity=100,
        sync_mode="incremental",
        last_discovered_uid=900,
        last_fetched_uid=899,
        version=3,
    )
    assert apply_uid_validity(state, 101) == "changed"
    assert state.sync_mode == "rebaseline"
    assert state.last_discovered_uid is None
    assert state.last_fetched_uid is None
    assert state.last_error_code == "IMAP_UIDVALIDITY_CHANGED"
    assert state.version == 4


def test_discovery_cursor_and_initial_completion_are_separate() -> None:
    state = MailboxSyncState(
        mailbox_account="repair@example.com",
        folder_name="INBOX",
        sync_mode="initializing",
        version=1,
    )
    record_discovery(state, ["10", "12", "11"])
    assert state.last_discovered_uid == 12
    assert state.initial_sync_completed_at is None
    mark_initial_complete(state)
    assert state.sync_mode == "incremental"
    assert state.initial_sync_completed_at is not None
