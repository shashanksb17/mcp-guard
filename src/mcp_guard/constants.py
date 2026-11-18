"""Shared defaults and protocol constants."""

from __future__ import annotations

# Local daemon binds only to loopback. Proxies talk to it over HTTP.
DEFAULT_DAEMON_HOST = "127.0.0.1"
DEFAULT_DAEMON_PORT = 8741

# HTTP reverse proxy for Claude Web / remote connectors (also loopback;
# expose publicly via a tunnel such as cloudflared or ngrok).
DEFAULT_HTTP_PROXY_HOST = "127.0.0.1"
DEFAULT_HTTP_PROXY_PORT = 8742

# How long a tools/call waits for a human decision before the timeout policy applies.
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 60

# Fail-closed: if the daemon is unreachable, or the user does not respond in time,
# the request is blocked. Override with MCP_GUARD_FAIL_OPEN=1 only for debugging.
DEFAULT_FAIL_CLOSED = True

# JSON-RPC / MCP error used when a tool call is denied.
BLOCKED_ERROR_CODE = -32042
BLOCKED_ERROR_MESSAGE = "MCP Guard: tool call blocked by user"

DAEMON_UNAVAILABLE_ERROR_CODE = -32043
DAEMON_UNAVAILABLE_ERROR_MESSAGE = (
    "MCP Guard: approval daemon unavailable (fail-closed)"
)

TIMEOUT_ERROR_CODE = -32044
TIMEOUT_ERROR_MESSAGE = "MCP Guard: approval timed out (fail-closed)"

# Methods that may execute side effects and therefore require approval.
INTERCEPTED_METHODS = frozenset({"tools/call"})

# Claude Desktop config locations (platform-specific helpers live in installer/).
CONFIG_BACKUP_SUFFIX = ".mcp-guard.bak"

APP_DIR_NAME = "mcp-guard"
AUDIT_LOG_NAME = "audit.jsonl"
RUNTIME_STATE_NAME = "daemon.pid"
