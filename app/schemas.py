from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

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


class NotificationConfig(BaseModel):
    email_recipients: list[str] = Field(default_factory=list)
    sms_recipients: list[str] = Field(default_factory=list)
    notify_on: NotifyOn = "alert_only"  # alert_only | warn_and_alert | all


class PromptMonitorCreate(BaseModel):
    name: str
    log_source_id: str
    interval_seconds: int = Field(gt=0)
    prompt: str
    inputs: list[MonitorInput] = Field(default_factory=list, min_items=1)
    window_config: WindowConfig | None = None
    notification_config: NotificationConfig | None = None


class PromptMonitorUpdate(BaseModel):
    name: Optional[str] = None
    log_source_id: Optional[str] = None
    interval_seconds: Optional[int] = Field(default=None, gt=0)
    prompt: Optional[str] = None
    inputs: Optional[list[MonitorInput]] = Field(default=None, min_items=1)
    window_config: Optional[WindowConfig] = None
    notification_config: Optional[NotificationConfig] = None


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
    llm_raw_input: Optional[str] = None
    llm_raw_output: Optional[str] = None
    summary: Optional[str] = None
    details: Optional[str] = None
    error_message: Optional[str] = None

    class Config:
        orm_mode = True
