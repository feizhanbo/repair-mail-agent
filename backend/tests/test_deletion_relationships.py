from sqlalchemy import inspect
from sqlalchemy.orm import configure_mappers

from app.models import Base, Email, EmailAttachment, EmailThread, RepairTicket


def test_domain_relationships_configure_without_delete_cascade() -> None:
    configure_mappers()
    assert "emails" in inspect(EmailThread).relationships
    assert "attachments" in inspect(Email).relationships
    assert "email_links" in inspect(RepairTicket).relationships
    assert "oss_object" in inspect(EmailAttachment).relationships

    dangerous = []
    for mapper in Base.registry.mappers:
        for relation in mapper.relationships:
            if "delete" in relation.cascade:
                dangerous.append(f"{mapper.class_.__name__}.{relation.key}")
    assert dangerous == []


def test_ticket_email_and_attachment_oss_are_not_delete_orphans() -> None:
    ticket_links = inspect(RepairTicket).relationships["email_links"]
    attachment_oss = inspect(EmailAttachment).relationships["oss_object"]
    assert "delete" not in ticket_links.cascade
    assert "delete-orphan" not in ticket_links.cascade
    assert "delete" not in attachment_oss.cascade
    assert "delete-orphan" not in attachment_oss.cascade
