from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator

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
    model_config = ConfigDict(from_attributes=True)


class TargetUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    connection_config: Optional[dict[str, Any]] = None


# Log sources
class LogSourceCreate(BaseModel):
    target_id: str
    name: str
    mode: Mode
    config: dict[str, Any]
    cursor_state: Optional[dict[str, Any]] = None


class LogSource(LogSourceCreate):
    id: str = Field(default_factory=generate_id)
    model_config = ConfigDict(from_attributes=True)


class LogSourceUpdate(BaseModel):
    target_id: Optional[str] = None
    name: Optional[str] = None
    mode: Optional[Mode] = None
    config: Optional[dict[str, Any]] = None
    cursor_state: Optional[dict[str, Any]] = None


# Monitors
class WindowConfig(BaseModel):
    max_lines: int | None = Field(default=500, ge=1)
    max_chars: int | None = Field(default=8000, ge=1)


class MonitorInput(BaseModel):
    label: str
    mode: str = Field(pattern=r"^command$")
    command: str
    timeout_seconds: int | None = Field(default=None, gt=0)
    workdir: str | None = None
    env: dict[str, str] | None = None

    @field_validator("label")
    @classmethod
    def label_must_not_be_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("label must not be empty")
        return cleaned

    @field_validator("command")
    @classmethod
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
    inputs: list[MonitorInput] = Field(default_factory=list)
    window_config: WindowConfig | None = None
    notification_config: NotificationConfig | None = None
    llm_provider: Optional[str] = None
    llm_provider_metadata: Optional[dict] = None
    enabled: bool = True

    @field_validator("llm_provider")
    @classmethod
    def validate_llm_provider(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        normalized = value.lower()
        if normalized not in SUPPORTED_LLM_PROVIDERS:
            raise ValueError(f"llm_provider must be one of {sorted(SUPPORTED_LLM_PROVIDERS)}")
        return normalized

    @model_validator(mode="after")
    def ensure_unique_labels(self) -> "PromptMonitorCreate":
        inputs = self.inputs or []
        labels = [item.label.lower() for item in inputs]
        if len(labels) != len(set(labels)):
            raise ValueError("monitor inputs must have unique labels")
        if not inputs and not self.log_source_id:
            raise ValueError("either inputs or log_source_id must be provided")
        return self


class PromptMonitorUpdate(BaseModel):
    name: Optional[str] = None
    target_id: Optional[str] = None
    log_source_id: Optional[str] = None
    interval_seconds: Optional[int] = Field(default=None, gt=0)
    prompt: Optional[str] = None
    inputs: Optional[list[MonitorInput]] = Field(default=None)
    window_config: Optional[WindowConfig] = None
    notification_config: Optional[NotificationConfig] = None
    llm_provider: Optional[str] = None
    llm_provider_metadata: Optional[dict] = None
    enabled: Optional[bool] = None

    @field_validator("llm_provider")
    @classmethod
    def validate_llm_provider(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        normalized = value.lower()
        if normalized not in SUPPORTED_LLM_PROVIDERS:
            raise ValueError(f"llm_provider must be one of {sorted(SUPPORTED_LLM_PROVIDERS)}")
        return normalized

    @model_validator(mode="after")
    def ensure_unique_labels(self) -> "PromptMonitorUpdate":
        inputs = self.inputs
        if inputs is not None:
            labels = [item.label.lower() for item in inputs]
            if len(labels) != len(set(labels)):
                raise ValueError("monitor inputs must have unique labels")
            if inputs == [] and not self.log_source_id:
                raise ValueError("either inputs or log_source_id must be provided")
        return self


class PromptMonitor(PromptMonitorCreate):
    id: str = Field(default_factory=generate_id)
    last_run_at: str | None = None
    model_config = ConfigDict(from_attributes=True)


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

    model_config = ConfigDict(from_attributes=True)


class MonitorStatus(BaseModel):
    monitor_id: str
    enabled: bool
    interval_seconds: int
    last_run_at: Optional[str]
    next_run_at: Optional[str]
    due_in_seconds: Optional[float]
    scheduler_enabled: bool
    latest_run: Optional[MonitorRun] = None


class MonitorCollection(BaseModel):
    logs_text: str
    success_count: int
    total_inputs: int
    log_source_id: Optional[str] = None
    log_source_name: Optional[str] = None
