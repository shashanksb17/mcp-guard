"""
Minimal stdio MCP server for demos and tests.

Implements enough of the MCP lifecycle to exercise ``tools/list`` and
``tools/call`` without pulling in the full MCP Python SDK.
"""

from __future__ import annotations

import json
import sys
from typing import Any


TOOLS = [
    {
        "name": "echo",
        "description": "Echo a message back (safe demo tool).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Text to echo"},
            },
            "required": ["message"],
        },
    },
    {
        "name": "danger_delete",
        "description": "Pretend to delete a file (demo of a risky tool).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path that would be deleted"},
            },
            "required": ["path"],
        },
    },
]


def _respond(message: dict[str, Any]) -> None:
    # Binary + explicit UTF-8: on Windows text-mode stdout would use cp1252 and
    # translate "\n" to "\r\n".
    sys.stdout.buffer.write(
        (json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    )
    sys.stdout.buffer.flush()


def _handle(message: dict[str, Any]) -> None:
    method = message.get("method")
    req_id = message.get("id")

    if method == "initialize":
        _respond(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mcp-guard-demo", "version": "1.0.0"},
                },
            }
        )
        return

    if method == "notifications/initialized":
        return

    if method == "tools/list":
        _respond({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
        return

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "echo":
            text = f"echo: {arguments.get('message', '')}"
        elif name == "danger_delete":
            text = f"deleted (demo only): {arguments.get('path', '')}"
        else:
            _respond(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {name}"},
                }
            )
            return
        _respond(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": False,
                },
            }
        )
        return

    if method == "ping":
        _respond({"jsonrpc": "2.0", "id": req_id, "result": {}})
        return

    if req_id is not None:
        _respond(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        )


def main() -> int:
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            sys.stderr.write(f"invalid json: {exc}\n")
            sys.stderr.flush()
            continue
        if not isinstance(message, dict):
            # Batches are not supported by this tiny demo server.
            _respond({"jsonrpc": "2.0", "id": None,
                      "error": {"code": -32600, "message": "Invalid Request"}})
            continue
        _handle(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
