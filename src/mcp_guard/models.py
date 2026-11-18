"""Pydantic models shared by daemon, proxies, and UI."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Decision(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    TIMEOUT = "timeout"
    DAEMON_DOWN = "daemon_down"


class ApprovalRequestCreate(BaseModel):
    """Payload a proxy sends when it needs a human decision."""

    server_name: str = Field(..., description="Logical MCP server name from config")
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    transport: str = Field(..., description="stdio | http")
    method: str = "tools/call"
    raw_params: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PendingApproval(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    server_name: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    transport: str
    method: str = "tools/call"
    raw_params: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"  # pending | decided


class DecisionRequest(BaseModel):
    decision: Decision
    decided_by: str = "ui"


class DecisionResult(BaseModel):
    id: str
    decision: Decision
    decided_at: datetime = Field(default_factory=utc_now)
    decided_by: str = "ui"
    server_name: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    transport: str
    timed_out: bool = False


class AuditEvent(BaseModel):
    ts: datetime = Field(default_factory=utc_now)
    event: str
    request_id: str | None = None
    server_name: str | None = None
    tool_name: str | None = None
    transport: str | None = None
    decision: Decision | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: str = "ok"
    pending: int = 0
    version: str
    fail_closed: bool = True
    approval_timeout_seconds: int
