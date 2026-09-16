from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

BACKEND_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    APP_ENV: str
    APP_NAME: str = "repair-mail-agent"
    APP_VERSION: str = "0.1.0"
    COMMIT_SHA: str = "unknown"
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "json"
    LOG_STDOUT_ENABLED: bool = True
    LOG_FILE_ENABLED: bool = False
    LOG_DIR: str = str(BACKEND_DIR / "logs" / "runtime")
    LOG_ROTATION_WHEN: str = "midnight"
    LOG_RETENTION_DAYS: int = 30
    LOG_MAX_MESSAGE_LENGTH: int = 8192
    LOG_INCLUDE_TRACEBACK: bool = True
    HTTP_ACCESS_LOG_ENABLED: bool = True
    SLOW_REQUEST_THRESHOLD_MS: int = 3000
    SLOW_DB_THRESHOLD_MS: int = 1000
    SLOW_EXTERNAL_THRESHOLD_MS: int = 5000
    TRUSTED_PROXY_CIDRS: list[str] = []
    CORS_ALLOWED_ORIGINS: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    TRUSTED_HOSTS: list[str] = ["localhost", "127.0.0.1", "testserver"]
    API_DOCS_ENABLED: bool = False

    DATABASE_URL: str = "mysql+asyncmy://root:change-me-root@127.0.0.1:13307/repair_system_test"
    DEV_DATABASE_URL: str = "mysql+asyncmy://root:change-me-root@127.0.0.1:13307/repair_system_dev"
    DB_SMOKE_DATABASE_URL: str = ""
    DESTRUCTIVE_TEST_DATABASE_ALLOWLIST: list[str] = ["repair_system_test", "AIRMA_test"]
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_TIMEOUT_SECONDS: int = 30
    DB_POOL_RECYCLE_SECONDS: int = 1800

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
    IMAP_INITIAL_SYNC_START_AT: datetime | None = None
    IMAP_INITIAL_BATCH_SIZE: int = 50
    IMAP_INCREMENTAL_LIMIT: int = 20
    IMAP_FETCH_BATCH_SIZE: int = 10
    IMAP_UNSEEN_ONLY: bool = True
    IMAP_ARCHIVE_TO_OSS: bool = True
    IMAP_MAX_RETRIES: int = 5
    MAIL_PRECLASSIFICATION_MIN_CONFIDENCE: float = 0.7
    MAIL_PRECLASSIFICATION_ATTACHMENT_MAX_BYTES: int = 2 * 1024 * 1024
    MAIL_PRECLASSIFICATION_MAX_ATTACHMENTS: int = 3
    MAIL_PRECLASSIFICATION_LATEST_REPLY_CHARS: int = 6000
    MAIL_PRECLASSIFICATION_BODY_CHARS: int = 12000
    MAIL_PRECLASSIFICATION_ATTACHMENT_TEXT_CHARS: int = 8000
    # 链路 smoke 测试开关：非空时强制邮件分类最终 intent（例如 "new_repair"），
    # 仅用于链路连通性验证，仍走完整 classify_mail 代码路径；默认空=关闭。
    MAIL_INTENT_FORCE: str = ""
    AUTO_FOLLOWUP_INTERVAL_MINUTES: int = 5

    SMTP_HOST: str = "smtp.example.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = "repair@example.com"
    SMTP_PASSWORD: str = ""
    SMTP_MAX_CONNECTIONS: int = 2
    SMTP_MESSAGES_PER_CONNECTION: int = 20
    SMTP_CONNECTION_MAX_AGE_SECONDS: int = 600
    SMTP_IDLE_TIMEOUT_SECONDS: int = 60
    SMTP_CONNECT_TIMEOUT_SECONDS: int = 20
    SMTP_SEND_TIMEOUT_SECONDS: int = 60
    SMTP_RETRY_LIMIT: int = 3
    SMTP_RETRY_BACKOFF_SECONDS: list[int] = [30, 120, 300]
    SMTP_RATE_LIMIT_PER_MINUTE: int = 60
    SMTP_SENT_FOLDER: str = "Sent"

    OSS_ENDPOINT: str = "https://oss-cn-shanghai.aliyuncs.com"
    OSS_BUCKET: str = "acco-repair-mail-file"
    OSS_ACCESS_KEY: str = ""
    OSS_SECRET_KEY: str = ""

    AI_PROVIDER: str = "deepseek"
    AI_API_KEY: str = ""
    AI_MODEL: str = "deepseek-chat"
    AI_BASE_URL: str = "https://api.deepseek.com"
    AI_TIMEOUT_SECONDS: float = 30.0
    AI_MAX_RETRIES: int = 2
    AI_RETRY_BASE_DELAY_SECONDS: float = 1.0
    AI_MAX_INPUT_CHARS: int = 12000
    AI_PROMPT_VERSION: str = "deepseek-v4-json-v1"
    AI_FULL_LOG_ENABLED: bool = False
    AI_FULL_LOG_RETENTION_DAYS: int = 30
    AI_LOG_DIR: str = str(BACKEND_DIR / "logs" / "ai")
    LLM_ROUTES_FILE: str = str(BACKEND_DIR / "config" / "llm_routes.yaml")
    SYSTEM_SENDER_ADDRESSES: list[str] = []
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
    MASTER_DATA_IMPORT_MAX_BYTES: int = 20 * 1024 * 1024

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
    RELAY_SQLSERVER_SN_TABLE: str = "oins_rma"
    RELAY_SQLSERVER_SN_PRIMARY_KEY: str = ""
    RELAY_SQLSERVER_SN_UPDATED_AT_COLUMN: str = ""
    RELAY_SQLSERVER_SN_COLUMN_MAP: dict[str, str] = {
        "ins_id": "insID",
        "sn": "internalSN",
        "customer_code": "customer",
        "customer_name": "custmrName",
        "material_code": "ITEMCODE",
        "material_name": "ITEMNAME",
        "parent_material_code": "U_FatherItem",
        "parent_sn": "U_FatherSerialNum",
        "top_material_code": "U_topitemcode",
        "top_sn": "U_TOPSN",
        "warranty_end_date": "ExpDate",
    }
    RELAY_SQLSERVER_REQUEST_SCHEMA: str = "dbo"
    RELAY_SQLSERVER_REQUEST_TABLE: str = "oscl_rma"
    RELAY_SQLSERVER_RESULT_SCHEMA: str = "dbo"
    RELAY_SQLSERVER_RESULT_TABLE: str = "oscl_print"
    RELAY_SQLSERVER_REQUEST_ID_COLUMN: str = "RequestID"
    # oscl_print CallID is persisted locally and printed as the PDF Part No.
    RELAY_SQLSERVER_CALL_ID_COLUMN: str = "CallID"
    RELAY_SQLSERVER_RMA_COLUMN: str = "U_CustomerNum"
    RELAY_SQLSERVER_BATCH_SIZE: int = 500
    RELAY_SQLSERVER_SYNC_INTERVAL_MINUTES: int = 5
    RELAY_SQLSERVER_FULL_SYNC_HOUR: int = 2
    RELAY_SQLSERVER_RMA_POLL_INTERVAL_SECONDS: int = 300
    RELAY_SQLSERVER_RMA_TIMEOUT_WORKING_HOURS: int = 8
    RELAY_SUBMIT_UNKNOWN_CONFIRM_SECONDS: int = 300
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
    AIRMA_WORKER_ENVIRONMENT: str = "development"
    AIRMA_WORKER_QUEUE: str = "default"
    AIRMA_WORKER_LEASE_SECONDS: int = 60
    AIRMA_WORKER_HEARTBEAT_SECONDS: int = 15
    AIRMA_JOB_HEARTBEAT_SECONDS: int = 30
    AIRMA_WATCHDOG_INTERVAL_SECONDS: int = 60
    AIRMA_SHUTDOWN_GRACE_SECONDS: int = 30
    AIRMA_EVENT_LOOP_WATCHDOG_SECONDS: int = 90

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

    @field_validator("APP_ENV", "AIRMA_WORKER_ENVIRONMENT", mode="before")
    @classmethod
    def validate_environment_name(cls, value: object) -> str:
        normalized = str(value or "").strip().lower()
        if normalized not in {"development", "test", "production"}:
            raise ValueError("environment must be one of: development, test, production")
        return normalized

    @model_validator(mode="after")
    def reject_insecure_production_defaults(self) -> "Settings":
        if self.AIRMA_WORKER_ENVIRONMENT != self.APP_ENV:
            raise ValueError("AIRMA_WORKER_ENVIRONMENT must match APP_ENV")
        pool_values = {
            "DB_POOL_SIZE": self.DB_POOL_SIZE,
            "DB_MAX_OVERFLOW": self.DB_MAX_OVERFLOW,
            "DB_POOL_TIMEOUT_SECONDS": self.DB_POOL_TIMEOUT_SECONDS,
            "DB_POOL_RECYCLE_SECONDS": self.DB_POOL_RECYCLE_SECONDS,
        }
        invalid_pool = [
            name
            for name, value in pool_values.items()
            if value < (0 if name == "DB_MAX_OVERFLOW" else 1)
        ]
        if invalid_pool:
            raise ValueError(f"invalid database pool settings: {', '.join(invalid_pool)}")
        oversized_pool = [
            name
            for name, value, maximum in (
                ("DB_POOL_SIZE", self.DB_POOL_SIZE, 100),
                ("DB_MAX_OVERFLOW", self.DB_MAX_OVERFLOW, 100),
                ("DB_POOL_TIMEOUT_SECONDS", self.DB_POOL_TIMEOUT_SECONDS, 300),
                ("DB_POOL_RECYCLE_SECONDS", self.DB_POOL_RECYCLE_SECONDS, 86400),
            )
            if value > maximum
        ]
        if oversized_pool:
            raise ValueError(f"database pool settings exceed safe limits: {', '.join(oversized_pool)}")
        if self.AIRMA_WORKER_HEARTBEAT_SECONDS <= 0 or self.AIRMA_JOB_HEARTBEAT_SECONDS <= 0:
            raise ValueError("worker heartbeat intervals must be positive")
        if self.AIRMA_WORKER_LEASE_SECONDS <= self.AIRMA_WORKER_HEARTBEAT_SECONDS * 2:
            raise ValueError("AIRMA_WORKER_LEASE_SECONDS must exceed twice the heartbeat interval")
        if self.AIRMA_EVENT_LOOP_WATCHDOG_SECONDS <= self.AIRMA_WORKER_HEARTBEAT_SECONDS * 2:
            raise ValueError("AIRMA_EVENT_LOOP_WATCHDOG_SECONDS must exceed twice the heartbeat interval")
        if self.APP_ENV != "production":
            return self
        insecure: list[str] = []
        if len(self.JWT_SECRET) < 32 or "change-me" in self.JWT_SECRET.lower():
            insecure.append("JWT_SECRET")
        if "change-me" in self.DATABASE_URL.lower():
            insecure.append("DATABASE_URL")
        if len(self.DEFAULT_ADMIN_PASSWORD) < 12 or "change-me" in self.DEFAULT_ADMIN_PASSWORD.lower():
            insecure.append("DEFAULT_ADMIN_PASSWORD")
        if not self.CORS_ALLOWED_ORIGINS or "*" in self.CORS_ALLOWED_ORIGINS:
            insecure.append("CORS_ALLOWED_ORIGINS")
        if not self.TRUSTED_HOSTS or "*" in self.TRUSTED_HOSTS:
            insecure.append("TRUSTED_HOSTS")
        if self.IMAP_FETCH_ENABLED:
            if not self.IMAP_HOST or self.IMAP_HOST.endswith("example.com"):
                insecure.append("IMAP_HOST")
            if not self.IMAP_USER or self.IMAP_USER.endswith("example.com"):
                insecure.append("IMAP_USER")
            if not self.IMAP_PASSWORD:
                insecure.append("IMAP_PASSWORD")
            if self.IMAP_ARCHIVE_TO_OSS and (
                not self.OSS_ACCESS_KEY or not self.OSS_SECRET_KEY
            ):
                insecure.append("OSS_CREDENTIALS")
        if self.AUTO_SEND_ENABLED or self.RMA_AUTO_SEND_ENABLED or self.SMTP_ASYNC_ENABLED:
            if not self.SMTP_HOST or self.SMTP_HOST.endswith("example.com"):
                insecure.append("SMTP_HOST")
            if not self.SMTP_USER or self.SMTP_USER.endswith("example.com"):
                insecure.append("SMTP_USER")
            if not self.SMTP_PASSWORD:
                insecure.append("SMTP_PASSWORD")
        if self.RELAY_SQLSERVER_ENABLED:
            relay_values = {
                "RELAY_SQLSERVER_HOST": self.RELAY_SQLSERVER_HOST,
                "RELAY_SQLSERVER_DATABASE": self.RELAY_SQLSERVER_DATABASE,
                "RELAY_SQLSERVER_USER": self.RELAY_SQLSERVER_USER,
                "RELAY_SQLSERVER_PASSWORD": self.RELAY_SQLSERVER_PASSWORD,
            }
            insecure.extend(name for name, value in relay_values.items() if not value)
        if insecure:
            raise ValueError(f"insecure production settings: {', '.join(sorted(set(insecure)))}")
        return self

    @property
    def database_name(self) -> str:
        """Canonical database name; DATABASE_URL is the only runtime source."""
        return str(make_url(self.DATABASE_URL).database or "")

    model_config = SettingsConfigDict(env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore")


settings = Settings()
