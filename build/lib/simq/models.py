"""API request/response schemas (pydantic v2)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Optional

from pydantic import BaseModel, Field


class JobCreate(BaseModel):
    type: str
    params: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    max_attempts: int = Field(default=3, ge=1, le=10)
    timeout_s: Optional[int] = Field(default=None, ge=1, le=24 * 3600)


class JobOut(BaseModel):
    id: uuid.UUID
    type: str
    params: dict[str, Any]
    state: str
    priority: int
    attempts: int
    max_attempts: int
    timeout_s: int
    cancel_requested: bool
    next_run_at: dt.datetime
    lease_expires_at: Optional[dt.datetime]
    worker_id: Optional[str]
    progress: Optional[dict[str, Any]]
    result: Optional[Any]
    error: Optional[str]
    created_at: dt.datetime
    started_at: Optional[dt.datetime]
    finished_at: Optional[dt.datetime]
    deduplicated: Optional[bool] = None


class EventOut(BaseModel):
    at: dt.datetime
    event: str
    worker_id: Optional[str]
    detail: Optional[dict[str, Any]]


class JobDetailOut(JobOut):
    events: list[EventOut] = Field(default_factory=list)


class StatsOut(BaseModel):
    by_state: dict[str, int]
    oldest_queued_age_s: Optional[float]


class WorkloadOut(BaseModel):
    name: str
    description: str
    example_params: dict[str, Any]
