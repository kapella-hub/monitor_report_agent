import os
from dataclasses import dataclass


def getenv(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None:
        return default
    return value


@dataclass
class Settings:
    database_backend: str = getenv("DATABASE_BACKEND", "sqlite")
    database_path: str = getenv("DATABASE_PATH", "./monitor.db")
    database_url: str | None = getenv("DATABASE_URL")
    scheduler_tick_seconds: int = int(getenv("SCHEDULER_TICK_SECONDS", "1"))
    command_timeout_seconds: int = int(getenv("COMMAND_TIMEOUT_SECONDS", "60"))
    max_command_workers: int = int(getenv("MAX_COMMAND_WORKERS", "4"))

    smtp_host: str | None = getenv("SMTP_HOST")
    smtp_port: int = int(getenv("SMTP_PORT", "587"))
    smtp_username: str | None = getenv("SMTP_USERNAME")
    smtp_password: str | None = getenv("SMTP_PASSWORD")
    smtp_from: str | None = getenv("SMTP_FROM")
    smtp_use_tls: bool = getenv("SMTP_USE_TLS", "true").lower() == "true"

    sms_provider: str | None = getenv("SMS_PROVIDER")
    sms_api_key: str | None = getenv("SMS_API_KEY")

    max_run_history_per_monitor: int = int(getenv("MAX_RUN_HISTORY_PER_MONITOR", "200"))


settings = Settings()
