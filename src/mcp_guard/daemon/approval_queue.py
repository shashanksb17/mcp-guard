"""In-memory approval queue with concurrent waiters and timeouts."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Callable

from mcp_guard.models import (
    ApprovalRequestCreate,
    Decision,
    DecisionResult,
    PendingApproval,
    utc_now,
)

logger = logging.getLogger(__name__)


class ApprovalQueue:
    """
    Owns pending MCP tool-call approvals.

    Proxies await ``submit_and_wait``. The UI (or tests) call ``decide``.
    Multiple requests can be pending at once; each waiter is independent.
    """

    def __init__(
        self,
        *,
        timeout_seconds: int,
        fail_closed: bool = True,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.fail_closed = fail_closed
        self._on_change = on_change
        self._pending: dict[str, PendingApproval] = {}
        self._futures: dict[str, asyncio.Future[DecisionResult]] = {}
        self._lock = asyncio.Lock()

    def _notify(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:  # noqa: BLE001 — UI callbacks must not break the queue
                logger.exception("approval queue on_change callback failed")

    async def submit_and_wait(self, create: ApprovalRequestCreate) -> DecisionResult:
        expires_at = utc_now() + timedelta(seconds=self.timeout_seconds)
        extra = {"id": create.id} if create.id else {}
        pending = PendingApproval(
            **extra,
            expires_at=expires_at,
            server_name=create.server_name,
            tool_name=create.tool_name,
            arguments=create.arguments,
            transport=create.transport,
            method=create.method,
            raw_params=create.raw_params,
            metadata=create.metadata,
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[DecisionResult] = loop.create_future()

        async with self._lock:
            if pending.id in self._pending:
                raise ValueError(f"duplicate approval id: {pending.id}")
            self._pending[pending.id] = pending
            self._futures[pending.id] = future

        self._notify()
        logger.info(
            "approval pending id=%s server=%s tool=%s",
            pending.id,
            pending.server_name,
            pending.tool_name,
        )

        try:
            return await asyncio.wait_for(future, timeout=self.timeout_seconds)
        except asyncio.CancelledError:
            # Caller went away (daemon shutdown / handler cancelled): never leave
            # a ghost popup behind.
            async with self._lock:
                self._pending.pop(pending.id, None)
                self._futures.pop(pending.id, None)
            self._notify()
            raise
        except asyncio.TimeoutError:
            decision = Decision.BLOCK if self.fail_closed else Decision.ALLOW
            result = DecisionResult(
                id=pending.id,
                decision=Decision.TIMEOUT if self.fail_closed else Decision.ALLOW,
                decided_by="timeout",
                server_name=pending.server_name,
                tool_name=pending.tool_name,
                arguments=pending.arguments,
                transport=pending.transport,
                timed_out=True,
            )
            # If fail-open on timeout, decision is ALLOW; still mark timed_out.
            if not self.fail_closed:
                result = result.model_copy(update={"decision": Decision.ALLOW})
            else:
                # Expose TIMEOUT distinctly while still meaning "do not forward".
                result = result.model_copy(update={"decision": Decision.TIMEOUT})
            await self._finalize(pending.id, result, from_timeout=True)
            # Silence unused local in fail-open path clarity
            _ = decision
            return result

    async def decide(
        self,
        request_id: str,
        decision: Decision,
        *,
        decided_by: str = "ui",
    ) -> DecisionResult:
        if decision not in {Decision.ALLOW, Decision.BLOCK}:
            raise ValueError("decision must be allow or block")

        async with self._lock:
            pending = self._pending.get(request_id)
            future = self._futures.get(request_id)
            if pending is None or future is None or future.done():
                raise KeyError(f"unknown or already decided request: {request_id}")
            result = DecisionResult(
                id=pending.id,
                decision=decision,
                decided_by=decided_by,
                server_name=pending.server_name,
                tool_name=pending.tool_name,
                arguments=pending.arguments,
                transport=pending.transport,
            )

        await self._finalize(request_id, result, from_timeout=False)
        return result

    async def cancel(self, request_id: str, *, reason: str = "cancelled") -> DecisionResult:
        """Resolve a pending approval as BLOCK because the client cancelled it."""
        return await self.decide(request_id, Decision.BLOCK, decided_by=reason)

    async def _finalize(
        self,
        request_id: str,
        result: DecisionResult,
        *,
        from_timeout: bool,
    ) -> None:
        async with self._lock:
            future = self._futures.pop(request_id, None)
            pending = self._pending.pop(request_id, None)
            if pending is not None:
                pending.status = "decided"

            if future is not None and not future.done():
                future.set_result(result)
            elif from_timeout and future is not None and future.done():
                # Already decided by UI racing the timeout — keep first decision.
                pass

        self._notify()
        logger.info(
            "approval decided id=%s decision=%s by=%s timed_out=%s",
            result.id,
            result.decision.value,
            result.decided_by,
            result.timed_out,
        )

    async def list_pending(self) -> list[PendingApproval]:
        async with self._lock:
            # Drop expired entries that somehow linger (timeout waiter should clear).
            now = utc_now()
            items = [p for p in self._pending.values() if p.expires_at >= now]
            return sorted(items, key=lambda p: p.created_at)

    async def pending_count(self) -> int:
        return len(await self.list_pending())

    async def get(self, request_id: str) -> PendingApproval | None:
        async with self._lock:
            return self._pending.get(request_id)
