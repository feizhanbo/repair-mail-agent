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
    IMAP_MARK_SEEN_AFTER_SUCCESS: bool = False
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
    MAIL_PRECHECK_IRRELEVANT_MIN_CONFIDENCE: float = 0.85

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

    AUTO_SEND_ENABLED: bool = False
    REPLY_SEND_MODE: str = "human_review"
    AUTO_SEND_MIN_CONFIDENCE: float = 0.85
    SMTP_RECIPIENT_WHITELIST: list[str] = ["rmatest2@accotest.com"]
    MAX_FOLLOW_UP: int = 3
    CONFIDENCE_THRESHOLD: float = 0.7
    RUNTIME_CONFIG_PATH: str = str(BACKEND_DIR / "config" / "runtime_config.json")

    DEFAULT_ADMIN_USERNAME: str = "admin"
    DEFAULT_ADMIN_PASSWORD: str = "change-me-admin"
    DEFAULT_ADMIN_REAL_NAME: str = "System Administrator"
    DEFAULT_ADMIN_EMAIL: str = "admin@example.com"

    model_config = SettingsConfigDict(env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore")


settings = Settings()
