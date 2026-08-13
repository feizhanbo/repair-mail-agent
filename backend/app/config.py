from __future__ import annotations

from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    APP_ENV: str = "dev"
    APP_NAME: str = "repair-mail-agent"
    LOG_LEVEL: str = "INFO"
    CORS_ALLOWED_ORIGINS: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    TRUSTED_HOSTS: list[str] = ["localhost", "127.0.0.1", "testserver"]
    API_DOCS_ENABLED: bool = False

    DB_NAME: str = "repair_system_test"
    DATABASE_URL: str = "mysql+asyncmy://root:change-me-root@127.0.0.1:13307/repair_system_test"
    DEV_DATABASE_URL: str = "mysql+asyncmy://root:change-me-root@127.0.0.1:13307/repair_system_dev"
    DB_SMOKE_DATABASE_URL: str = ""
    LANGGRAPH_CHECKPOINT_DATABASE_URL: str = ""
    LANGGRAPH_CHECKPOINT_SMOKE_DATABASE_URL: str = ""
    LANGGRAPH_CHECKPOINT_AUTO_SETUP: bool = False
    LANGGRAPH_STRICT_MSGPACK: bool = True
    LANGGRAPH_RELEASE_EVIDENCE_FILE: str = ""
    LANGGRAPH_RELEASE_EVIDENCE_ROOT: str = "/app/release-evidence"
    LANGGRAPH_RELEASE_EVIDENCE_MAX_AGE_HOURS: int = 168
    APP_RELEASE_COMMIT: str = ""

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
    AI_MAX_TOKENS: int = 4096
    AI_STRUCTURED_OUTPUT_METHOD: str = "json_mode"
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
    ARCHIVE_MAX_MEMBERS: int = 1000
    ARCHIVE_MAX_EXPANDED_BYTES: int = 100 * 1024 * 1024
    ARCHIVE_MAX_COMPRESSION_RATIO: int = 100
    ARCHIVE_MAX_DEPTH: int = 2
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
    RELAY_ADAPTER: str = "sqlserver"
    TEST_RELAY_BASE_URL: str = ""
    TEST_RELAY_TOKEN: str = ""
    RUN_REAL_MAIL_INTEGRATION_TESTS: bool = False
    E2E_GOLD_RUN_ENABLED: bool = False
    E2E_RMATEST2_IMAP_HOST: str = "imaphz.qiye.163.com"
    E2E_RMATEST2_IMAP_PORT: int = 993
    E2E_RMATEST2_IMAP_USE_SSL: bool = True
    E2E_RMATEST2_IMAP_USER: str = "rmatest2@accotest.com"
    E2E_RMATEST2_IMAP_PASSWORD: str = ""
    E2E_RMATEST2_IMAP_FOLDER: str = "INBOX"
    E2E_RMATEST2_SMTP_HOST: str = "smtphz.qiye.163.com"
    E2E_RMATEST2_SMTP_PORT: int = 465
    E2E_RMATEST2_SMTP_USE_SSL: bool = True
    E2E_RMATEST2_SMTP_USER: str = "rmatest2@accotest.com"
    E2E_RMATEST2_SMTP_PASSWORD: str = ""

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
    RELAY_SQLSERVER_SOURCE_REQUEST_ID_COLUMN: str = "SourceRequestID"
    # Legacy audit-only configuration. New submissions and result queries never use CallID.
    RELAY_SQLSERVER_CALL_ID_COLUMN: str = "CallID"
    RELAY_SQLSERVER_RMA_COLUMN: str = "U_CustomerNum"
    RELAY_SQLSERVER_RESULT_COLUMN_MAP: dict[str, str] = {
        "sn": "internalSN",
        "customer_code": "customer",
        "customer_name": "custmrName",
        "material_code": "itemCode",
        "material_name": "itemName",
        "email_subject": "subject",
        "contact_person": "BPContact",
        "contact_phone": "Telephone",
        "problem_description": "U_FailurePhenomena",
        "repair_requested_at": "U_BXDate",
        "mailing_address": "BPShipAddr",
        "currency": "U_cur",
        "shipping_fee": "U_DeliveryPaid",
        "repair_fee": "U_WSPrice",
        "charge_status": "U_RepairPaid",
    }
    RELAY_SQLSERVER_BATCH_SIZE: int = 500
    RELAY_SQLSERVER_SYNC_INTERVAL_MINUTES: int = 5
    RELAY_SQLSERVER_FULL_SYNC_HOUR: int = 2
    RELAY_SQLSERVER_RMA_POLL_INTERVAL_SECONDS: int = 300
    RELAY_SQLSERVER_RMA_TIMEOUT_WORKING_HOURS: int = 8
    RELAY_SUBMIT_UNKNOWN_CONFIRM_SECONDS: int = 300
    RELAY_SN_SYNC_CRON: str = ""
    RELAY_SN_COUNT_CHANGE_GUARD_PERCENT: float = 5.0
    RELAY_SN_SNAPSHOT_MAX_AGE_HOURS: int = 36

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
    WORKFLOW_ENGINE: str = "legacy"
    LANGGRAPH_EMAIL_ALLOWLIST: list[int] = []
    LANGGRAPH_ROLLOUT_PERCENT: int = 100

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
        BACKEND_DIR / "app" / "resources" / "rma_pdf" / "layout_v3_2_reference.yaml"
    )
    RMA_CJK_FONT_PATH: str = ""
    RMA_PDF_DEFAULT_CURRENCY: str = ""
    RMA_PDF_DEFAULT_DELIVERY_FEE: str = "one-way charge/单次收费"
    RMA_PDF_DEFAULT_REPAIR_FEE: str = "free of charge/免费"
    RMA_PDF_DEFAULT_TOTAL_COST: str = "0"
    RMA_PDF_MAILING_CONTACT_PERSON: str = ""
    RMA_PDF_MAILING_CONTACT_PHONE: str = ""
    RMA_PDF_TYPICAL_MAX_BYTES: int = 500_000
    RMA_PDF_MAX_BYTES: int = 2_000_000
    RMA_DEFAULT_BEIJING_COMPANY: str = "北京华峰测控技术股份有限公司"
    RMA_DEFAULT_BEIJING_ADDRESS: str = "北京市海淀区丰豪东路9号院5号楼"
    RMA_DEFAULT_BEIJING_CONTACT: str = "李连荣"
    RMA_DEFAULT_BEIJING_PHONE: str = "010-63725600-193"
    RMA_DEFAULT_BEIJING_POSTAL_CODE: str = "100094"
    RMA_DEFAULT_TIANJIN_COMPANY: str = "华峰测控技术（天津）有限责任公司"
    RMA_DEFAULT_TIANJIN_ADDRESS: str = "天津市滨海新区生态城川博道华峰测控1201号"
    RMA_DEFAULT_TIANJIN_CONTACT: str = "郭洋（收）"
    RMA_DEFAULT_TIANJIN_PHONE: str = "022-67253518-8108"
    RMA_DEFAULT_TIANJIN_POSTAL_CODE: str = ""
    RMA_OVERSEAS_BEIJING_ADDRESS_BLOCK: str = (
        "Beijing Huafeng Test & Control Technology Co., Ltd.\n"
        "Attention: Li Lian Rong\n"
        "Address: Building 5, IC PARK, No. 9 Fenghao East Road, "
        "Haidian District (100094), Beijing\n"
        "Phone: +86-15811322137"
    )

    DEFAULT_ADMIN_USERNAME: str = "admin"
    DEFAULT_ADMIN_PASSWORD: str = "change-me-admin"
    DEFAULT_ADMIN_REAL_NAME: str = "System Administrator"
    DEFAULT_ADMIN_EMAIL: str = "admin@example.com"

    @field_validator("DATABASE_URL", "DEV_DATABASE_URL", "DB_SMOKE_DATABASE_URL", mode="before")
    @classmethod
    def normalize_legacy_mysql_async_driver(cls, value: object) -> object:
        if isinstance(value, str) and value.startswith("mysql+aiomysql://"):
            return value.replace("mysql+aiomysql://", "mysql+asyncmy://", 1)
        return value

    @field_validator("WORKFLOW_ENGINE")
    @classmethod
    def validate_workflow_engine(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"legacy", "shadow", "langgraph"}:
            raise ValueError("WORKFLOW_ENGINE must be legacy, shadow, or langgraph")
        return normalized

    @field_validator("LANGGRAPH_CHECKPOINT_DATABASE_URL", "LANGGRAPH_CHECKPOINT_SMOKE_DATABASE_URL")
    @classmethod
    def validate_checkpoint_database_url(cls, value: str) -> str:
        normalized = value.strip()
        if normalized and not normalized.lower().startswith(("postgresql://", "postgres://")):
            raise ValueError("LANGGRAPH_CHECKPOINT_DATABASE_URL must be a PostgreSQL URL")
        return normalized

    @field_validator("LANGGRAPH_ROLLOUT_PERCENT")
    @classmethod
    def validate_langgraph_rollout_percent(cls, value: int) -> int:
        if not 0 <= value <= 100:
            raise ValueError("LANGGRAPH_ROLLOUT_PERCENT must be between 0 and 100")
        return value

    @field_validator("ASYNC_JOB_STALE_SECONDS")
    @classmethod
    def validate_async_job_stale_seconds(cls, value: int) -> int:
        if value < 30:
            raise ValueError("ASYNC_JOB_STALE_SECONDS must be at least 30")
        return value

    @field_validator("LANGGRAPH_RELEASE_EVIDENCE_MAX_AGE_HOURS")
    @classmethod
    def validate_release_evidence_max_age(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("LANGGRAPH_RELEASE_EVIDENCE_MAX_AGE_HOURS must be positive")
        return value

    @model_validator(mode="after")
    def reject_insecure_production_defaults(self) -> "Settings":
        if self.WORKFLOW_ENGINE == "langgraph" and not self.LANGGRAPH_CHECKPOINT_DATABASE_URL.strip():
            raise ValueError(
                "LANGGRAPH_CHECKPOINT_DATABASE_URL is required when WORKFLOW_ENGINE=langgraph"
            )
        if self.APP_ENV.strip().lower() not in {"prod", "production"}:
            return self
        insecure: list[str] = []
        if len(self.JWT_SECRET) < 32 or "change-me" in self.JWT_SECRET.lower():
            insecure.append("JWT_SECRET")
        if "change-me" in self.DATABASE_URL.lower():
            insecure.append("DATABASE_URL")
        if self.WORKFLOW_ENGINE == "langgraph" and "change-me" in self.LANGGRAPH_CHECKPOINT_DATABASE_URL.lower():
            insecure.append("LANGGRAPH_CHECKPOINT_DATABASE_URL")
        if len(self.DEFAULT_ADMIN_PASSWORD) < 12 or "change-me" in self.DEFAULT_ADMIN_PASSWORD.lower():
            insecure.append("DEFAULT_ADMIN_PASSWORD")
        if not self.CORS_ALLOWED_ORIGINS or "*" in self.CORS_ALLOWED_ORIGINS:
            insecure.append("CORS_ALLOWED_ORIGINS")
        if not self.TRUSTED_HOSTS or "*" in self.TRUSTED_HOSTS:
            insecure.append("TRUSTED_HOSTS")
        if insecure:
            raise ValueError(f"insecure production settings: {', '.join(insecure)}")
        return self

    model_config = SettingsConfigDict(env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore")


settings = Settings()
