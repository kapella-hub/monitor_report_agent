from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, root_validator, validator

from .llm_client import SUPPORTED_LLM_PROVIDERS

Mode = str
NotifyOn = str


def generate_id() -> str:
    return uuid4().hex


# Targets
class TargetCreate(BaseModel):
    name: str
    type: str
    connection_config: Optional[dict[str, Any]] = None


class Target(TargetCreate):
    id: str = Field(default_factory=generate_id)

    class Config:
        orm_mode = True


# Log sources
class LogSourceCreate(BaseModel):
    target_id: str
    name: str
    mode: Mode
    config: dict[str, Any]
    cursor_state: Optional[dict[str, Any]] = None


class LogSource(LogSourceCreate):
    id: str = Field(default_factory=generate_id)

    class Config:
        orm_mode = True


# Monitors
class WindowConfig(BaseModel):
    max_lines: int | None = Field(default=500, ge=1)
    max_chars: int | None = Field(default=8000, ge=1)


class MonitorInput(BaseModel):
    label: str
    mode: str = Field(regex=r"^command$")
    command: str
    timeout_seconds: int | None = Field(default=None, gt=0)
    workdir: str | None = None
    env: dict[str, str] | None = None

    @validator("label")
    def label_must_not_be_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("label must not be empty")
        return cleaned

    @validator("command")
    def command_must_not_be_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("command must not be empty")
        return cleaned


class NotificationConfig(BaseModel):
    email_recipients: list[str] = Field(default_factory=list)
    sms_recipients: list[str] = Field(default_factory=list)
    notify_on: NotifyOn = "alert_only"  # alert_only | warn_and_alert | all


class PromptMonitorCreate(BaseModel):
    name: str
    target_id: str
    log_source_id: Optional[str] = None
    interval_seconds: int = Field(gt=0)
    prompt: str
    inputs: list[MonitorInput] = Field(default_factory=list, min_items=1)
    window_config: WindowConfig | None = None
    notification_config: NotificationConfig | None = None
    llm_provider: Optional[str] = None
    llm_provider_metadata: Optional[dict] = None
    enabled: bool = True

    @validator("llm_provider")
    def validate_llm_provider(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        normalized = value.lower()
        if normalized not in SUPPORTED_LLM_PROVIDERS:
            raise ValueError(f"llm_provider must be one of {sorted(SUPPORTED_LLM_PROVIDERS)}")
        return normalized

    @root_validator
    def ensure_unique_labels(cls, values: dict) -> dict:
        inputs = values.get("inputs") or []
        labels = [item.label.lower() for item in inputs]
        if len(labels) != len(set(labels)):
            raise ValueError("monitor inputs must have unique labels")
        return values


class PromptMonitorUpdate(BaseModel):
    name: Optional[str] = None
    target_id: Optional[str] = None
    log_source_id: Optional[str] = None
    interval_seconds: Optional[int] = Field(default=None, gt=0)
    prompt: Optional[str] = None
    inputs: Optional[list[MonitorInput]] = Field(default=None, min_items=1)
    window_config: Optional[WindowConfig] = None
    notification_config: Optional[NotificationConfig] = None
    llm_provider: Optional[str] = None
    llm_provider_metadata: Optional[dict] = None
    enabled: Optional[bool] = None

    @validator("llm_provider")
    def validate_llm_provider(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        normalized = value.lower()
        if normalized not in SUPPORTED_LLM_PROVIDERS:
            raise ValueError(f"llm_provider must be one of {sorted(SUPPORTED_LLM_PROVIDERS)}")
        return normalized

    @root_validator
    def ensure_unique_labels(cls, values: dict) -> dict:
        inputs = values.get("inputs") or []
        labels = [item.label.lower() for item in inputs]
        if inputs and len(labels) != len(set(labels)):
            raise ValueError("monitor inputs must have unique labels")
        return values


class PromptMonitor(PromptMonitorCreate):
    id: str = Field(default_factory=generate_id)
    last_run_at: str | None = None

    class Config:
        orm_mode = True


# Runs
class MonitorRun(BaseModel):
    id: str = Field(default_factory=generate_id)
    monitor_id: str
    started_at: str
    finished_at: Optional[str] = None
    status: Optional[str] = None
    llm_provider: Optional[str] = None
    llm_provider_metadata: Optional[dict] = None
    llm_raw_input: Optional[str] = None
    llm_raw_output: Optional[str] = None
    summary: Optional[str] = None
    details: Optional[str] = None
    error_message: Optional[str] = None

    class Config:
        orm_mode = True
