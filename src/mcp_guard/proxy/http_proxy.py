"""
Streamable HTTP MCP reverse proxy for Claude Web / remote connectors.

Claude Web (and Claude Desktop remote connectors) reach MCP servers from
Anthropic's cloud over HTTPS. This proxy sits in front of an upstream MCP
HTTP endpoint, intercepts JSON-RPC ``tools/call`` bodies, and asks the
local approval daemon before forwarding.

Typical demo topology:

    Anthropic cloud  --HTTPS-->  public tunnel  -->  this proxy (localhost)
                                                       |
                                                       +--> approval daemon + popup
                                                       |
                                                       +--> upstream MCP HTTP server

Limitations (see ARCHITECTURE.md): Anthropic must be able to reach the
public URL on port 443; OAuth/header credentials must still be configured
on the Claude connector pointing at the *proxied* URL.
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import uuid
from typing import Any

import anyio
import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from mcp_guard.client import ApprovalClient, DaemonUnavailable, decision_allows_forward
from mcp_guard.constants import (
    BLOCKED_ERROR_CODE,
    BLOCKED_ERROR_MESSAGE,
    DAEMON_UNAVAILABLE_ERROR_CODE,
    DAEMON_UNAVAILABLE_ERROR_MESSAGE,
    TIMEOUT_ERROR_CODE,
    TIMEOUT_ERROR_MESSAGE,
)
from mcp_guard.jsonrpc import (
    batch_contains_tools_call,
    cancelled_request_id,
    extract_tool_call,
    is_request,
    is_tools_call,
)
from mcp_guard.models import Decision
from mcp_guard.settings import Settings, get_settings

logger = logging.getLogger(__name__)

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}
# httpx transparently decompresses upstream bodies, so never echo the upstream
# Content-Encoding (the client would try to gunzip plain JSON).
RESPONSE_DROP = HOP_BY_HOP | {"content-encoding"}

# (Mcp-Session-Id, repr(jsonrpc id)) -> daemon approval id, for cancellation.
_inflight: dict[tuple[str, str], str] = {}


def join_upstream_url(upstream_base: str, path: str) -> str:
    """
    Append the incoming request path to an upstream *origin*.

    ``upstream_base`` should be ``scheme://host[:port]/`` (for the demo:
    ``http://127.0.0.1:8765/``). The client path (e.g. ``mcp``) is appended so
    ``POST /mcp`` becomes ``http://127.0.0.1:8765/mcp``.

    Do **not** put the MCP endpoint path in ``upstream_base`` as well
    (e.g. ``http://127.0.0.1:8765/mcp``) — that would produce ``/mcp/mcp``.
    """
    base = upstream_base.rstrip("/")
    suffix = path.lstrip("/")
    if not suffix:
        return f"{base}/"
    return f"{base}/{suffix}"


def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def create_http_proxy_app(
    upstream_base: str,
    *,
    server_name: str,
    settings: Settings | None = None,
    client: ApprovalClient | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    client = client or ApprovalClient(settings)
    upstream_base = upstream_base.rstrip("/") + "/"
    # Optional shared secret for the public tunnel URL. When set, Claude's
    # connector URL must be https://<tunnel>/<token>/mcp (or send
    # "Authorization: Bearer <token>"), so strangers who find the tunnel can't
    # spam approval popups or probe the upstream.
    token = os.environ.get("MCP_GUARD_HTTP_TOKEN") or None

    app = FastAPI(title="MCP Guard HTTP Proxy", version="1.0.0")
    http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))

    @app.on_event("shutdown")
    async def _close_http() -> None:
        await http.aclose()

    # Registered *before* the catch-all route, otherwise it is shadowed.
    @app.get("/_mcp_guard/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "upstream": upstream_base,
            "server_name": server_name,
            "token_required": token is not None,
        }

    @app.api_route("/{path:path}", methods=["GET", "POST", "DELETE", "OPTIONS", "HEAD"])
    async def proxy(path: str, request: Request) -> Response:
        if token is not None:
            auth = request.headers.get("authorization", "")
            first, _, rest = path.partition("/")
            if hmac.compare_digest(first, token):
                path = rest
            elif not hmac.compare_digest(auth, f"Bearer {token}"):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        target = join_upstream_url(upstream_base, path)
        if request.url.query:
            target = f"{target}?{request.url.query}"

        body = await request.body()
        drop = HOP_BY_HOP | ({"authorization"} if token is not None else set())
        headers = {k: v for k, v in request.headers.items() if k.lower() not in drop}

        # Intercept tools/call on POST JSON bodies (Streamable HTTP).
        if request.method == "POST" and body:
            intercepted = await _maybe_intercept(
                body,
                client=client,
                server_name=server_name,
                fail_closed=settings.fail_closed,
                session_id=request.headers.get("mcp-session-id", ""),
            )
            if intercepted is not None:
                if intercepted == {}:
                    return Response(status_code=202)  # cancelled notification consumed
                return JSONResponse(intercepted)

        try:
            upstream_req = http.build_request(
                request.method, target, content=body, headers=headers
            )
            # stream=True: forward SSE events as they arrive (progress
            # notifications, long-lived GET streams) instead of buffering.
            upstream = await http.send(upstream_req, stream=True)
        except httpx.HTTPError as exc:
            logger.exception("upstream request failed: %s", exc)
            return JSONResponse(
                {"error": "upstream_unreachable", "detail": str(exc)},
                status_code=502,
            )

        resp_headers = {
            k: v
            for k, v in upstream.headers.items()
            if k.lower() not in RESPONSE_DROP
        }
        content_type = upstream.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return StreamingResponse(
                upstream.aiter_bytes(),
                status_code=upstream.status_code,
                headers=resp_headers,
                media_type=content_type,
                background=BackgroundTask(upstream.aclose),
            )
        try:
            content = await upstream.aread()
        finally:
            await upstream.aclose()
        return Response(
            content=content,
            status_code=upstream.status_code,
            headers=resp_headers,
            media_type=content_type or None,
        )

    return app


async def _maybe_intercept(
    body: bytes,
    *,
    client: ApprovalClient,
    server_name: str,
    fail_closed: bool,
    session_id: str = "",
) -> Any:
    """Return a JSON-RPC error response if blocked; None to forward.

    Returns ``{}`` when the message was a cancellation we consumed locally.
    """
    try:
        message = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    if batch_contains_tools_call(message):
        # Never let a batched tools/call slip past approval. MCP 2025-06-18
        # removed batching, so rejecting the whole batch is safe.
        logger.warning("rejecting JSON-RPC batch containing tools/call")
        return [
            _jsonrpc_error(m.get("id"), -32600, "MCP Guard: batched tools/call not supported")
            for m in message
            if is_request(m)
        ]

    cancel_id = cancelled_request_id(message)
    if cancel_id is not None:
        approval_id = _inflight.pop((session_id, repr(cancel_id)), None)
        if approval_id is not None:
            await anyio.to_thread.run_sync(lambda: client.cancel(approval_id))
            return {}
        return None

    if not is_tools_call(message):
        return None

    tool_name, arguments = extract_tool_call(message)
    request_id = message.get("id")
    logger.info(
        "http intercept tools/call server=%s tool=%s id=%s",
        server_name,
        tool_name,
        request_id,
    )

    approval_id = str(uuid.uuid4())
    key = (session_id, repr(request_id))
    _inflight[key] = approval_id
    raw_params = message.get("params")
    try:
        # httpx sync client in thread — daemon API is sync-friendly and
        # approval waits are long-running by design.
        result = await anyio.to_thread.run_sync(
            lambda: client.request_approval(
                server_name=server_name,
                tool_name=tool_name,
                arguments=arguments,
                transport="http",
                raw_params=raw_params if isinstance(raw_params, dict) else {},
                metadata={"jsonrpc_id": request_id, "mcp_session_id": session_id},
                approval_id=approval_id,
            )
        )
    except DaemonUnavailable:
        _inflight.pop(key, None)
        if fail_closed:
            return _jsonrpc_error(
                request_id,
                DAEMON_UNAVAILABLE_ERROR_CODE,
                DAEMON_UNAVAILABLE_ERROR_MESSAGE,
            )
        return None

    _inflight.pop(key, None)
    if result.decided_by == "cancelled":
        return _jsonrpc_error(request_id, BLOCKED_ERROR_CODE, "MCP Guard: request cancelled by client")
    if result.decision == Decision.TIMEOUT:
        return _jsonrpc_error(request_id, TIMEOUT_ERROR_CODE, TIMEOUT_ERROR_MESSAGE)
    if result.decision == Decision.BLOCK or not decision_allows_forward(result):
        return _jsonrpc_error(request_id, BLOCKED_ERROR_CODE, BLOCKED_ERROR_MESSAGE)
    return None


def run_http_proxy(
    upstream_url: str,
    *,
    server_name: str,
    settings: Settings | None = None,
) -> None:
    settings = settings or get_settings()
    app = create_http_proxy_app(
        upstream_url, server_name=server_name, settings=settings
    )
    uvicorn.run(
        app,
        host=settings.http_proxy_host,
        port=settings.http_proxy_port,
        log_level="info",
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(prog="mcp-guard http-proxy")
    parser.add_argument(
        "--upstream",
        required=True,
        help=(
            "Upstream origin to reverse-proxy (e.g. http://127.0.0.1:8765/). "
            "Request paths like /mcp are appended; do not include /mcp in this URL."
        ),
    )
    parser.add_argument("--server-name", required=True)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.host:
        settings = Settings(
            daemon_host=settings.daemon_host,
            daemon_port=settings.daemon_port,
            http_proxy_host=args.host,
            http_proxy_port=args.port or settings.http_proxy_port,
            approval_timeout_seconds=settings.approval_timeout_seconds,
            fail_closed=settings.fail_closed,
            fail_open=settings.fail_open,
        )
    elif args.port:
        settings = Settings(
            daemon_host=settings.daemon_host,
            daemon_port=settings.daemon_port,
            http_proxy_host=settings.http_proxy_host,
            http_proxy_port=args.port,
            approval_timeout_seconds=settings.approval_timeout_seconds,
            fail_closed=settings.fail_closed,
            fail_open=settings.fail_open,
        )

    run_http_proxy(args.upstream, server_name=args.server_name, settings=settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
