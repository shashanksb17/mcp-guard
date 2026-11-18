"""Minimal JSON-RPC 2.0 helpers for MCP message framing."""

from __future__ import annotations

import json
from typing import Any

from mcp_guard.constants import (
    BLOCKED_ERROR_CODE,
    BLOCKED_ERROR_MESSAGE,
    DAEMON_UNAVAILABLE_ERROR_CODE,
    DAEMON_UNAVAILABLE_ERROR_MESSAGE,
    TIMEOUT_ERROR_CODE,
    TIMEOUT_ERROR_MESSAGE,
)


def parse_message(line: str | bytes) -> dict[str, Any] | None:
    """Parse one newline-delimited JSON-RPC message. Returns None if empty."""
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    text = line.strip()
    if not text:
        return None
    return json.loads(text)


def encode_message(message: dict[str, Any]) -> bytes:
    """Encode a JSON-RPC object as a single newline-terminated UTF-8 frame."""
    return (json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def is_request(message: Any) -> bool:
    return isinstance(message, dict) and "method" in message and "id" in message


def is_notification(message: Any) -> bool:
    return isinstance(message, dict) and "method" in message and "id" not in message


def is_tools_call(message: Any) -> bool:
    return is_request(message) and message.get("method") == "tools/call"


def batch_contains_tools_call(message: Any) -> bool:
    """True if ``message`` is a JSON-RPC batch (list) with any tools/call in it."""
    return isinstance(message, list) and any(is_tools_call(m) for m in message)


def cancelled_request_id(message: Any) -> Any:
    """Return params.requestId for a notifications/cancelled message, else None."""
    if is_notification(message) and message.get("method") == "notifications/cancelled":
        params = message.get("params") or {}
        if isinstance(params, dict):
            return params.get("requestId")
    return None


def extract_tool_call(message: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    params = message.get("params") or {}
    if not isinstance(params, dict):
        # Malformed params must still go through approval, never crash the proxy.
        return "unknown", {"_raw_params": params}
    name = params.get("name") or "unknown"
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        arguments = {"_raw": arguments}
    return str(name), arguments


def error_response(
    request_id: Any,
    code: int,
    message: str,
    data: Any | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def blocked_response(request_id: Any, reason: str | None = None) -> dict[str, Any]:
    return error_response(
        request_id,
        BLOCKED_ERROR_CODE,
        BLOCKED_ERROR_MESSAGE if not reason else f"{BLOCKED_ERROR_MESSAGE}: {reason}",
    )


def daemon_down_response(request_id: Any) -> dict[str, Any]:
    return error_response(
        request_id,
        DAEMON_UNAVAILABLE_ERROR_CODE,
        DAEMON_UNAVAILABLE_ERROR_MESSAGE,
    )


def timeout_response(request_id: Any) -> dict[str, Any]:
    return error_response(
        request_id,
        TIMEOUT_ERROR_CODE,
        TIMEOUT_ERROR_MESSAGE,
    )


def tool_error_result(request_id: Any, text: str) -> dict[str, Any]:
    """
    Alternative denial shape: MCP tool result with isError=true.

    Some clients surface this more clearly than a JSON-RPC error.
    MCP Guard defaults to a proper JSON-RPC error; this helper is available
    for experiments and tests.
    """
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [{"type": "text", "text": text}],
            "isError": True,
        },
    }
