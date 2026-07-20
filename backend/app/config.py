from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    APP_ENV: str = "dev"
    APP_NAME: str = "repair-mail-agent"
    LOG_LEVEL: str = "INFO"

    DB_NAME: str = "repair_system_test"
    DATABASE_URL: str = "mysql+asyncmy://root:change-me-root@127.0.0.1:13307/repair_system_test"
    DEV_DATABASE_URL: str = "mysql+asyncmy://root:change-me-root@127.0.0.1:13307/repair_system_dev"
    DB_SMOKE_DATABASE_URL: str = ""

    JWT_SECRET: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 480

    IMAP_HOST: str = "imap.example.com"
    IMAP_PORT: int = 993
    IMAP_USER: str = "repair@example.com"
    IMAP_PASSWORD: str = ""
    IMAP_POLL_INTERVAL_MINUTES: int = 5
    IMAP_FETCH_ENABLED: bool = True
    IMAP_FOLDER: str = "INBOX"
    IMAP_FETCH_LIMIT: int = 10
    IMAP_UNSEEN_ONLY: bool = True
    IMAP_ARCHIVE_TO_OSS: bool = True
    IMAP_MAX_RETRIES: int = 5
    AUTO_FOLLOWUP_INTERVAL_MINUTES: int = 5

    SMTP_HOST: str = "smtp.example.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = "repair@example.com"
    SMTP_PASSWORD: str = ""

    OSS_ENDPOINT: str = "https://oss-cn-shanghai.aliyuncs.com"
    OSS_BUCKET: str = "acco-repair-mail-file"
    OSS_ACCESS_KEY: str = ""
    OSS_SECRET_KEY: str = ""

    AI_PROVIDER: str = "deepseek"
    AI_API_KEY: str = ""
    AI_MODEL: str = "deepseek-v4-flash"
    AI_BASE_URL: str = "https://api.deepseek.com"
    AI_TIMEOUT_SECONDS: float = 30.0
    AI_MAX_RETRIES: int = 2
    AI_RETRY_BASE_DELAY_SECONDS: float = 1.0
    AI_MAX_INPUT_CHARS: int = 12000
    AI_PROMPT_VERSION: str = "deepseek-v4-json-v1"
    AI_FULL_LOG_ENABLED: bool = True
    AI_FULL_LOG_RETENTION_DAYS: int = 30
    AI_LOG_DIR: str = str(BACKEND_DIR / "logs" / "ai")
    MAIL_PRECHECK_IRRELEVANT_MIN_CONFIDENCE: float = 0.85
    ATTACHMENT_MAX_AUTO_PARSE_BYTES: int = 50 * 1024 * 1024
    ATTACHMENT_TEXT_MAX_CHARS: int = 20000
    PDF_MAX_PARSE_PAGES: int = 15
    PDF_PREVIEW_MAX_PAGES: int = 5
    ATTACHMENT_MAX_ARCHIVE_BYTES: int = 200 * 1024 * 1024
    EMAIL_MAX_ARCHIVE_BYTES: int = 500 * 1024 * 1024
    EMAIL_MAX_ATTACHMENTS: int = 50
    INLINE_ATTACHMENT_MAX_BYTES: int = 10 * 1024 * 1024
    INLINE_IMAGE_MIN_PARSE_WIDTH: int = 256
    INLINE_IMAGE_MIN_PARSE_HEIGHT: int = 128
    OSS_ORPHAN_MIN_AGE_HOURS: int = 24
    OSS_IO_CONCURRENCY: int = 4
    MAIL_IO_CONCURRENCY: int = 2
    FILE_PARSE_CONCURRENCY: int = 2

    MULTIMODAL_PROVIDER: str = "qwen"
    QWEN_API_KEY: str = ""
    QWEN_MODEL: str = "qwen-plus"
    QWEN_VL_MODEL: str = "qwen-vl-plus"
    QWEN_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    RELAY_BASE_URL: str = ""
    RELAY_API_KEY: str = ""
    RELAY_SN_SYNC_ENABLED: bool = False
    RELAY_PUSH_ENABLED: bool = False
    RELAY_TIMEOUT_SECONDS: float = 10.0

    RELAY_SQLSERVER_ENABLED: bool = False
    RELAY_SQLSERVER_HOST: str = ""
    RELAY_SQLSERVER_PORT: int = 1433
    RELAY_SQLSERVER_DATABASE: str = ""
    RELAY_SQLSERVER_USER: str = ""
    RELAY_SQLSERVER_PASSWORD: str = ""
    RELAY_SQLSERVER_DRIVER: str = "ODBC Driver 18 for SQL Server"
    RELAY_SQLSERVER_ENCRYPT: bool = True
    RELAY_SQLSERVER_TRUST_SERVER_CERTIFICATE: bool = False
    RELAY_SQLSERVER_SN_SCHEMA: str = "dbo"
    RELAY_SQLSERVER_SN_TABLE: str = ""
    RELAY_SQLSERVER_SN_PRIMARY_KEY: str = ""
    RELAY_SQLSERVER_SN_UPDATED_AT_COLUMN: str = ""
    RELAY_SQLSERVER_SN_COLUMN_MAP: dict[str, str] = {
        "sn": "sn",
        "customer_code": "customer_code",
        "customer_name": "customer_name",
        "material_code": "material_code",
        "material_name": "material_name",
        "asset_status": "asset_status",
    }
    RELAY_SQLSERVER_RESULT_MODE: str = "table"
    RELAY_SQLSERVER_RESULT_SCHEMA: str = "dbo"
    RELAY_SQLSERVER_RESULT_TARGET: str = ""
    RELAY_SQLSERVER_RESULT_UNIQUE_COLUMN: str = ""
    RELAY_SQLSERVER_RESULT_UNIQUE_PAYLOAD_KEY: str = "ticket_no"
    RELAY_SQLSERVER_RESULT_COLUMN_MAP: dict[str, str] = {}
    RELAY_SQLSERVER_BATCH_SIZE: int = 500
    RELAY_SQLSERVER_SYNC_INTERVAL_MINUTES: int = 5
    RELAY_SQLSERVER_FULL_SYNC_HOUR: int = 2

    INTERNAL_EMAIL_DOMAINS: list[str] = ["accotest.com"]
    DEVICE_RECEIPT_TRUSTED_SENDERS: list[str] = []
    ROUTING_DOMESTIC_USERNAME: str = "miya"
    ROUTING_FOREIGN_USERNAME: str = "demi"
    RMA_AUTO_SEND_ENABLED: bool = True

    EMAIL_ASYNC_ENABLED: bool = False
    SMTP_ASYNC_ENABLED: bool = False
    IMPORT_EXPORT_ASYNC_ENABLED: bool = False
    ASYNC_JOB_POLL_SECONDS: int = 5
    ASYNC_JOB_STALE_SECONDS: int = 900

    AUTO_SEND_ENABLED: bool = False
    AUTO_FOLLOWUP_ENABLED: bool = False
    REPLY_SEND_MODE: str = "human_review"
    AUTO_APPLY_MIN_CONFIDENCE: float = 0.85
    AUTO_SEND_MIN_CONFIDENCE: float = 0.85
    SMTP_RECIPIENT_WHITELIST: list[str] = []
    MAX_FOLLOW_UP: int = 3
    CONFIDENCE_THRESHOLD: float = 0.7
    RUNTIME_CONFIG_PATH: str = str(BACKEND_DIR / "config" / "runtime_config.json")

    RMA_AUTHORIZATION_ENABLED: bool = True
    RMA_PDF_TEMPLATE_PATH: str = str(
        BACKEND_DIR / "app" / "resources" / "rma_pdf" / "rma_authorization_auto_v3_1.pdf"
    )
    RMA_PDF_LAYOUT_PATH: str = str(
        BACKEND_DIR / "app" / "resources" / "rma_pdf" / "layout_v3_1_auto.yaml"
    )
    RMA_CJK_FONT_PATH: str = ""
    RMA_PDF_DEFAULT_CURRENCY: str = ""
    RMA_PDF_DEFAULT_DELIVERY_FEE: str = "one-way charge/单次收费"
    RMA_PDF_DEFAULT_REPAIR_FEE: str = "free of charge/免费"
    RMA_PDF_DEFAULT_TOTAL_COST: str = "0"

    DEFAULT_ADMIN_USERNAME: str = "admin"
    DEFAULT_ADMIN_PASSWORD: str = "change-me-admin"
    DEFAULT_ADMIN_REAL_NAME: str = "System Administrator"
    DEFAULT_ADMIN_EMAIL: str = "admin@example.com"

    model_config = SettingsConfigDict(env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore")


settings = Settings()
