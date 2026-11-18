"""FastAPI approval daemon — single source of truth for pending MCP calls."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from mcp_guard import __version__
from mcp_guard.daemon.approval_queue import ApprovalQueue
from mcp_guard.daemon.audit import AuditLog
from mcp_guard.models import (
    ApprovalRequestCreate,
    Decision,
    DecisionRequest,
    DecisionResult,
    HealthResponse,
    PendingApproval,
)
from mcp_guard.paths import daemon_pid_path
from mcp_guard.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class DaemonState:
    def __init__(self, settings: Settings, audit: AuditLog | None = None) -> None:
        self.settings = settings
        self.audit = audit or AuditLog()
        self.queue = ApprovalQueue(
            timeout_seconds=settings.approval_timeout_seconds,
            fail_closed=settings.fail_closed,
        )
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def broadcast(self, event: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


def create_app(settings: Settings | None = None, audit: AuditLog | None = None) -> FastAPI:
    settings = settings or get_settings()
    state = DaemonState(settings, audit=audit)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.daemon = state
        pid_file = daemon_pid_path()
        try:
            pid_file.write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            logger.warning("could not write pid file at %s", pid_file)
        state.audit.record("daemon_start", details={"pid": os.getpid()})
        logger.info(
            "MCP Guard daemon listening on %s:%s (fail_closed=%s timeout=%ss)",
            settings.daemon_host,
            settings.daemon_port,
            settings.fail_closed,
            settings.approval_timeout_seconds,
        )
        try:
            yield
        finally:
            state.audit.record("daemon_stop")
            try:
                if pid_file.exists():
                    pid_file.unlink()
            except OSError:
                pass

    app = FastAPI(
        title="MCP Guard Daemon",
        version=__version__,
        lifespan=lifespan,
    )

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            pending=await state.queue.pending_count(),
            version=__version__,
            fail_closed=settings.fail_closed,
            approval_timeout_seconds=settings.approval_timeout_seconds,
        )

    @app.get("/v1/approvals/pending", response_model=list[PendingApproval])
    async def list_pending() -> list[PendingApproval]:
        return await state.queue.list_pending()

    @app.post("/v1/approvals", response_model=DecisionResult)
    async def submit_approval(body: ApprovalRequestCreate) -> DecisionResult:
        """Block until the user decides (or timeout). Used by stdio/HTTP proxies."""
        state.audit.record(
            "approval_requested",
            server_name=body.server_name,
            tool_name=body.tool_name,
            transport=body.transport,
            details={"arguments": body.arguments, "metadata": body.metadata},
        )
        state.broadcast({"type": "pending_changed"})
        result = await state.queue.submit_and_wait(body)
        state.audit.record(
            "approval_decided",
            request_id=result.id,
            server_name=result.server_name,
            tool_name=result.tool_name,
            transport=result.transport,
            decision=result.decision,
            details={
                "decided_by": result.decided_by,
                "timed_out": result.timed_out,
                "arguments": result.arguments,
            },
        )
        state.broadcast({"type": "pending_changed", "decision": result.model_dump(mode="json")})
        return result

    @app.post("/v1/approvals/{request_id}/decide", response_model=DecisionResult)
    async def decide(request_id: str, body: DecisionRequest) -> DecisionResult:
        try:
            result = await state.queue.decide(
                request_id, body.decision, decided_by=body.decided_by
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result

    @app.get("/v1/audit")
    async def get_audit(limit: int = 100) -> list[dict[str, Any]]:
        events = state.audit.read_all()
        if limit > 0:
            return events[-limit:]
        return events

    @app.get("/v1/events")
    async def event_stream(request: Request) -> JSONResponse:
        """
        Lightweight long-poll for UI refresh.

        Returns when pending approvals change or after ~2s. Keeps the UI
        dependency-free (no WebSocket client required).
        """
        q = state.subscribe()
        try:
            try:
                event = await asyncio.wait_for(q.get(), timeout=2.0)
            except asyncio.TimeoutError:
                event = {"type": "heartbeat"}
            if await request.is_disconnected():
                return JSONResponse({"type": "disconnected"})
            pending = await state.queue.list_pending()
            return JSONResponse(
                {
                    "event": event,
                    "pending": [p.model_dump(mode="json") for p in pending],
                }
            )
        finally:
            state.unsubscribe(q)

    return app


def run_daemon(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.daemon_host,
        port=settings.daemon_port,
        log_level="info",
    )


# Convenience for tests / type checkers
def is_allowed(result: DecisionResult) -> bool:
    return result.decision == Decision.ALLOW
