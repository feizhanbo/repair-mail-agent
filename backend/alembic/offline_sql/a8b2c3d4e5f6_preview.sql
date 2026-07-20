-- REVIEW ONLY. Do not apply directly without the database migration authorization checkpoint.
-- Current expected test database revision before upgrade: f7a1b2c3d4e5
-- Target application migration head: a8b2c3d4e5f6
-- Alembic delta to target: a8b2c3d4e5f6

ALTER TABLE mail_fetch_records
  ADD COLUMN uid_validity BIGINT UNSIGNED NOT NULL DEFAULT 0;

ALTER TABLE mail_fetch_records
  DROP INDEX uk_mail_fetch_records,
  ADD CONSTRAINT uk_mail_fetch_records
    UNIQUE (mailbox_account, folder_name, uid_validity, imap_uid);

-- Lock impact: both ALTER TABLE statements can acquire metadata/table locks;
-- the unique-index rebuild cost grows with mail_fetch_records row count.
-- Backup: take a verified database backup and record the current row count/index definition.
-- Rollback limitation: rows from different UIDVALIDITY epochs may share the same numeric UID.
-- Deduplicate those rows before recreating the old three-column unique key.
