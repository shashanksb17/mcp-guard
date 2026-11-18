"""Synchronous HTTP client used by stdio/HTTP proxies to reach the daemon."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from mcp_guard.models import ApprovalRequestCreate, Decision, DecisionResult
from mcp_guard.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class DaemonUnavailable(RuntimeError):
    """Raised when the approval daemon cannot be reached."""


class ApprovalClient:
    def __init__(self, settings: Settings | None = None, timeout: float | None = None) -> None:
        self.settings = settings or get_settings()
        # Client timeout must exceed approval timeout so the daemon can return TIMEOUT.
        self.timeout = timeout if timeout is not None else (
            self.settings.approval_timeout_seconds + 10
        )

    @property
    def base_url(self) -> str:
        return self.settings.daemon_base_url

    def health(self) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=2.0) as client:
                resp = client.get(f"{self.base_url}/health")
                resp.raise_for_status()
                return resp.json()
        except (httpx.HTTPError, OSError) as exc:
            raise DaemonUnavailable(str(exc)) from exc

    def request_approval(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        transport: str,
        raw_params: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        approval_id: str | None = None,
    ) -> DecisionResult:
        payload = ApprovalRequestCreate(
            id=approval_id,
            server_name=server_name,
            tool_name=tool_name,
            arguments=arguments,
            transport=transport,
            raw_params=raw_params or {},
            metadata=metadata or {},
        )
        try:
            # The *daemon* owns the approval timeout. A proxy launched by Claude
            # Desktop usually doesn't inherit MCP_GUARD_TIMEOUT, so never let the
            # client give up first (that would be misreported as daemon-down).
            read_timeout = self.timeout
            try:
                read_timeout = max(read_timeout, float(self.health()["approval_timeout_seconds"]) + 10)
            except (DaemonUnavailable, KeyError, TypeError, ValueError):
                pass
            timeout = httpx.Timeout(5.0, read=read_timeout)
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(
                    f"{self.base_url}/v1/approvals",
                    json=payload.model_dump(mode="json"),
                )
                resp.raise_for_status()
                return DecisionResult.model_validate(resp.json())
        except (httpx.HTTPError, OSError) as exc:
            raise DaemonUnavailable(str(exc)) from exc

    def decide(self, request_id: str, decision: Decision, decided_by: str = "ui") -> DecisionResult:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(
                f"{self.base_url}/v1/approvals/{request_id}/decide",
                json={"decision": decision.value, "decided_by": decided_by},
            )
            resp.raise_for_status()
            return DecisionResult.model_validate(resp.json())

    def cancel(self, approval_id: str) -> None:
        """Best-effort: withdraw a pending approval (popup disappears)."""
        try:
            with httpx.Client(timeout=2.0) as client:
                client.post(f"{self.base_url}/v1/approvals/{approval_id}/cancel")
        except (httpx.HTTPError, OSError):
            pass

    def list_pending(self) -> list[dict[str, Any]]:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{self.base_url}/v1/approvals/pending")
            resp.raise_for_status()
            return resp.json()


def decision_allows_forward(result: DecisionResult) -> bool:
    return result.decision == Decision.ALLOW
