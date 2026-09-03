from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import MailboxSyncState
from app.services.common import utcnow


class MailboxSyncConfigurationError(RuntimeError):
    pass


async def get_or_create_sync_state(
    session: AsyncSession,
    *,
    mailbox_account: str,
    folder_name: str,
    for_update: bool = False,
) -> MailboxSyncState:
    statement = select(MailboxSyncState).where(
        MailboxSyncState.mailbox_account == mailbox_account,
        MailboxSyncState.folder_name == folder_name,
    )
    if for_update:
        statement = statement.with_for_update()
    state = await session.scalar(statement)
    if state is not None:
        return state
    start_at = settings.IMAP_INITIAL_SYNC_START_AT
    if start_at is None:
        raise MailboxSyncConfigurationError("IMAP_INITIAL_SYNC_START_AT_REQUIRED")
    state = MailboxSyncState(
        mailbox_account=mailbox_account,
        folder_name=folder_name,
        sync_mode="initializing",
        initial_sync_start_at=start_at,
        version=1,
    )
    session.add(state)
    await session.flush()
    return state


def apply_uid_validity(state: MailboxSyncState, uid_validity: int) -> str:
    if state.uid_validity is None:
        state.uid_validity = uid_validity
        return "initialized"
    if int(state.uid_validity) == int(uid_validity):
        return "unchanged"
    # The old cursor has no meaning in the new UID namespace. Re-run the
    # configured bounded initial window and rely on RFC identity/raw hashes for
    # business deduplication.
    state.uid_validity = uid_validity
    state.sync_mode = "rebaseline"
    state.last_discovered_uid = None
    state.last_fetched_uid = None
    state.initial_sync_completed_at = None
    state.version = int(state.version or 0) + 1
    state.last_error_code = "IMAP_UIDVALIDITY_CHANGED"
    return "changed"


def record_discovery(state: MailboxSyncState, uids: list[str]) -> None:
    numeric = [int(uid) for uid in uids if str(uid).isdigit()]
    if numeric:
        state.last_discovered_uid = max(int(state.last_discovered_uid or 0), max(numeric))
    state.last_sync_at = utcnow()


def mark_initial_complete(state: MailboxSyncState) -> None:
    state.sync_mode = "incremental"
    state.initial_sync_completed_at = state.initial_sync_completed_at or utcnow()
    state.last_success_at = utcnow()
    state.last_error_code = None


def imap_since_date(value: datetime) -> str:
    # IMAP SEARCH dates are day-granular and interpreted against InternalDate.
    return value.strftime("%d-%b-%Y")
