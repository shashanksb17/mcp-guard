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
    ) -> DecisionResult:
        payload = ApprovalRequestCreate(
            server_name=server_name,
            tool_name=tool_name,
            arguments=arguments,
            transport=transport,
            raw_params=raw_params or {},
            metadata=metadata or {},
        )
        try:
            with httpx.Client(timeout=self.timeout) as client:
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

    def list_pending(self) -> list[dict[str, Any]]:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{self.base_url}/v1/approvals/pending")
            resp.raise_for_status()
            return resp.json()


def decision_allows_forward(result: DecisionResult) -> bool:
    return result.decision == Decision.ALLOW
