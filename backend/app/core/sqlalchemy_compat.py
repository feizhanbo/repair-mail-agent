from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine


def configure_asyncmy_pre_ping(engine: AsyncEngine) -> None:
    """Work around SQLAlchemy 2.0.35 inspecting PyMySQL for asyncmy ping semantics.

    SQLAlchemy's asyncmy adapter exposes ``ping(reconnect)`` and requires
    ``False``.  The inherited dialect detector inspects an unrelated PyMySQL
    installation and can incorrectly call it without that argument.  Keep the
    workaround explicit while the project pins this SQLAlchemy/asyncmy pair.
    """
    if engine.url.drivername == "mysql+asyncmy":
        engine.sync_engine.dialect._send_false_to_ping = True
