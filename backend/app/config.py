from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    APP_ENV: str = "dev"
    APP_NAME: str = "repair-mail-agent"
    LOG_LEVEL: str = "INFO"

    DATABASE_URL: str = "mysql+asyncmy://root:change-me-root@127.0.0.1:13307/repair_system_dev"
    REDIS_URL: str = "redis://redis:6379/0"

    JWT_SECRET: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 480

    IMAP_HOST: str = "imap.example.com"
    IMAP_PORT: int = 993
    IMAP_USER: str = "repair@example.com"
    IMAP_PASSWORD: str = ""

    SMTP_HOST: str = "smtp.example.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = "repair@example.com"
    SMTP_PASSWORD: str = ""

    OSS_ENDPOINT: str = "https://oss-cn-hangzhou.aliyuncs.com"
    OSS_BUCKET: str = "repair-mail-agent"
    OSS_ACCESS_KEY: str = ""
    OSS_SECRET_KEY: str = ""

    AI_PROVIDER: str = "openai"
    AI_API_KEY: str = ""
    AI_MODEL: str = "gpt-4o-mini"
    AI_BASE_URL: str = "https://api.openai.com/v1"

    AUTO_SEND_ENABLED: bool = False
    MAX_FOLLOW_UP: int = 3
    CONFIDENCE_THRESHOLD: float = 0.7

    DEFAULT_ADMIN_USERNAME: str = "admin"
    DEFAULT_ADMIN_PASSWORD: str = "change-me-admin"
    DEFAULT_ADMIN_REAL_NAME: str = "System Administrator"
    DEFAULT_ADMIN_EMAIL: str = "admin@example.com"

    model_config = SettingsConfigDict(env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore")


settings = Settings()
