"""Runtime settings loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from mcp_guard.constants import (
    DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    DEFAULT_DAEMON_HOST,
    DEFAULT_DAEMON_PORT,
    DEFAULT_FAIL_CLOSED,
    DEFAULT_HTTP_PROXY_HOST,
    DEFAULT_HTTP_PROXY_PORT,
)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    daemon_host: str = DEFAULT_DAEMON_HOST
    daemon_port: int = DEFAULT_DAEMON_PORT
    http_proxy_host: str = DEFAULT_HTTP_PROXY_HOST
    http_proxy_port: int = DEFAULT_HTTP_PROXY_PORT
    approval_timeout_seconds: int = DEFAULT_APPROVAL_TIMEOUT_SECONDS
    fail_closed: bool = DEFAULT_FAIL_CLOSED
    # When True, timeout/daemon-down auto-allow (debug only).
    fail_open: bool = False

    @property
    def daemon_base_url(self) -> str:
        return f"http://{self.daemon_host}:{self.daemon_port}"

    @classmethod
    def from_env(cls) -> Settings:
        fail_closed = _env_bool("MCP_GUARD_FAIL_CLOSED", DEFAULT_FAIL_CLOSED)
        fail_open = _env_bool("MCP_GUARD_FAIL_OPEN", False)
        if fail_open:
            fail_closed = False
        return cls(
            daemon_host=os.environ.get("MCP_GUARD_DAEMON_HOST", DEFAULT_DAEMON_HOST),
            daemon_port=_env_int("MCP_GUARD_DAEMON_PORT", DEFAULT_DAEMON_PORT),
            http_proxy_host=os.environ.get(
                "MCP_GUARD_HTTP_PROXY_HOST", DEFAULT_HTTP_PROXY_HOST
            ),
            http_proxy_port=_env_int(
                "MCP_GUARD_HTTP_PROXY_PORT", DEFAULT_HTTP_PROXY_PORT
            ),
            approval_timeout_seconds=_env_int(
                "MCP_GUARD_TIMEOUT", DEFAULT_APPROVAL_TIMEOUT_SECONDS
            ),
            fail_closed=fail_closed,
            fail_open=fail_open,
        )


def get_settings() -> Settings:
    return Settings.from_env()
