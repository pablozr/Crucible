"""Shared API response envelope and data containers."""

from __future__ import annotations

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel

from crucible_core.schemas.admissions import (
    EventDetail,
    EventResponse,
    TaskDetail,
    TaskSummary,
)
from crucible_core.schemas.system import (
    HealthResponse,
    OperationalStatusResponse,
)

DataT = TypeVar("DataT")


class ResponseEnvelope(BaseModel, Generic[DataT]):
    status: Literal["ok", "error"]
    message: str
    data: DataT


class ErrorData(BaseModel):
    code: str


class EventData(BaseModel):
    event: EventResponse


class EventDetailData(BaseModel):
    event: EventDetail


class TaskData(BaseModel):
    task: TaskDetail


class TasksData(BaseModel):
    tasks: list[TaskSummary]
    next_cursor: str | None = None


class HealthData(BaseModel):
    health: HealthResponse


class SystemData(BaseModel):
    system: OperationalStatusResponse


ErrorEnvelope = ResponseEnvelope[ErrorData]
