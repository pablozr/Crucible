from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    event_type: str = Field(min_length=1)
    occurred_at: datetime
    payload_version: int = Field(ge=1)
    adapter: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    agent_session_id: str = Field(min_length=1)
    input_id: str = Field(min_length=1)
    execution_id: str | None = Field(default=None, min_length=1)
    project_id: UUID
    git_root: Path
    workspace_path: Path
    payload: dict[str, Any]


class EventResponse(BaseModel):
    event_id: str
    status: Literal["accepted", "rejected", "processing"]
    outcome: str
    input_id: str | None = None
    task_id: str | None = None
    dispatch_authorized: bool


class EventDetail(EventResponse):
    payload_hash: str
    failure_code: str | None = None


class TaskSummary(BaseModel):
    id: str
    status: str
    started_at: str
    worktree: str
    project_id: str
    branch: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None


class BaselineFile(BaseModel):
    path: str
    status: str
    sha256: str | None = None
    size: int | None = None
    is_binary: bool | None = None
    content: str | None = None


class TaskDetail(TaskSummary):
    baseline_head: str | None = None
    baseline_status: str | None = None
    baseline_index_manifest: str | None = None
    input_ids: list[str]
    baseline_files: list[BaselineFile]


class TaskList(BaseModel):
    tasks: list[TaskSummary]
    next_cursor: str | None = None
