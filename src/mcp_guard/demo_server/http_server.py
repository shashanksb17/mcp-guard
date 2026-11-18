"""Minimal Streamable-HTTP-ish MCP demo server (POST JSON-RPC to /mcp)."""

from __future__ import annotations

import argparse
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from mcp_guard.demo_server.stdio_server import TOOLS


def create_demo_http_app() -> FastAPI:
    app = FastAPI(title="MCP Guard Demo HTTP Server")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/mcp")
    async def mcp_endpoint(request: Request) -> JSONResponse:
        message = await request.json()
        response = _handle(message)
        if response is None:
            return JSONResponse(status_code=202, content=None)
        return JSONResponse(response)

    return app


def _handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    req_id = message.get("id")

    if method == "notifications/initialized" or (
        isinstance(method, str) and method.startswith("notifications/") and req_id is None
    ):
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mcp-guard-demo-http", "version": "1.0.0"},
            },
        }

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "echo":
            text = f"echo: {arguments.get('message', '')}"
        elif name == "danger_delete":
            text = f"deleted (demo only): {arguments.get('path', '')}"
        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {name}"},
            }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if req_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp-guard demo-http")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    uvicorn.run(create_demo_http_app(), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
