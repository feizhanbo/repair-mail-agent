/*
AIRMA SQL Server handoff contract for:
  dbo.oins_rma  = SN master (read-only)
  dbo.oscl_rma  = RMA1 request facts (AIRMA writes)
  dbo.oscl_print = RMA2 result facts (AIRMA reads)

Run as a DBA in the relay database. The script is idempotent for the two
nullable changes and RequestID indexes. It intentionally does not rewrite the
SIS_* gateway trigger because its CBD event-field semantics are vendor-owned.
The final gate fails until that trigger has been changed to use RequestID as
its replication key.
*/

SET XACT_ABORT ON;

IF OBJECT_ID(N'dbo.oins_rma', N'U') IS NULL THROW 51000, 'dbo.oins_rma is missing', 1;
IF OBJECT_ID(N'dbo.oscl_rma', N'U') IS NULL THROW 51001, 'dbo.oscl_rma is missing', 1;
IF OBJECT_ID(N'dbo.oscl_print', N'U') IS NULL THROW 51002, 'dbo.oscl_print is missing', 1;

IF EXISTS (
    SELECT 1 FROM sys.columns c
    WHERE c.object_id = OBJECT_ID(N'dbo.oscl_rma') AND c.name = N'RequestID'
      AND (TYPE_NAME(c.user_type_id) <> N'char' OR c.max_length <> 36 OR c.is_nullable = 1)
) THROW 51003, 'dbo.oscl_rma.RequestID must be CHAR(36) NOT NULL', 1;

IF EXISTS (
    SELECT 1 FROM sys.columns c
    WHERE c.object_id = OBJECT_ID(N'dbo.oscl_print') AND c.name = N'RequestID'
      AND (TYPE_NAME(c.user_type_id) <> N'char' OR c.max_length <> 36 OR c.is_nullable = 1)
) THROW 51004, 'dbo.oscl_print.RequestID must be CHAR(36) NOT NULL', 1;

IF EXISTS (
    SELECT RequestID FROM dbo.oscl_rma
    GROUP BY RequestID HAVING COUNT_BIG(*) > 1
) THROW 51005, 'dbo.oscl_rma contains duplicate RequestID values', 1;

IF EXISTS (
    SELECT RequestID FROM dbo.oscl_print
    GROUP BY RequestID HAVING COUNT_BIG(*) > 1
) THROW 51006, 'dbo.oscl_print contains duplicate RequestID values', 1;

BEGIN TRANSACTION;

IF EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID(N'dbo.oscl_rma') AND name = N'callID' AND is_nullable = 0
)
    ALTER TABLE dbo.oscl_rma ALTER COLUMN callID int NULL;

IF EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID(N'dbo.oscl_rma') AND name = N'U_ModVersion' AND is_nullable = 0
)
    ALTER TABLE dbo.oscl_rma ALTER COLUMN U_ModVersion varchar(1) NULL;

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID(N'dbo.oscl_rma')
      AND name = N'UQ_oscl_rma_RequestID' AND is_unique = 1 AND is_disabled = 0
)
    CREATE UNIQUE NONCLUSTERED INDEX UQ_oscl_rma_RequestID ON dbo.oscl_rma(RequestID);

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID(N'dbo.oscl_print')
      AND name = N'UQ_oscl_print_RequestID' AND is_unique = 1 AND is_disabled = 0
)
    CREATE UNIQUE NONCLUSTERED INDEX UQ_oscl_print_RequestID ON dbo.oscl_print(RequestID);

COMMIT TRANSACTION;

/* Vendor/DBA handoff gate: update every active oscl_rma INSERT trigger so the
   CBD_DB_EVENT key is derived from inserted.RequestID, not callID/编号. */
IF EXISTS (
    SELECT 1
    FROM sys.triggers t
    JOIN sys.sql_modules m ON m.object_id = t.object_id
    WHERE t.parent_id = OBJECT_ID(N'dbo.oscl_rma')
      AND t.is_disabled = 0
      AND OBJECTPROPERTY(t.object_id, 'ExecIsInsertTrigger') = 1
      AND m.definition NOT LIKE N'%RequestID%'
)
    THROW 51007, 'Gateway INSERT trigger still uses the legacy key; vendor must change it to inserted.RequestID', 1;

SELECT N'PASS' AS contract_status,
       N'dbo.oins_rma -> dbo.oscl_rma(RequestID) -> dbo.oscl_print(RequestID)' AS contract_path;
