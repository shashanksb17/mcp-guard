"""HTTP reverse-proxy interception tests."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from mcp_guard.daemon.audit import AuditLog
from mcp_guard.daemon.server import create_app
from mcp_guard.demo_server.http_server import create_demo_http_app
from mcp_guard.proxy.http_proxy import create_http_proxy_app
from mcp_guard.settings import Settings


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def stack(tmp_path: Path):
    daemon_port = _free_port()
    upstream_port = _free_port()
    settings = Settings(
        daemon_host="127.0.0.1",
        daemon_port=daemon_port,
        approval_timeout_seconds=10,
        fail_closed=True,
    )
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    daemon_app = create_app(settings=settings, audit=audit)
    upstream_app = create_demo_http_app()

    d_cfg = uvicorn.Config(daemon_app, host="127.0.0.1", port=daemon_port, log_level="warning")
    u_cfg = uvicorn.Config(upstream_app, host="127.0.0.1", port=upstream_port, log_level="warning")
    d_server = uvicorn.Server(d_cfg)
    u_server = uvicorn.Server(u_cfg)
    threads = [
        threading.Thread(target=d_server.run, daemon=True),
        threading.Thread(target=u_server.run, daemon=True),
    ]
    for t in threads:
        t.start()

    for url in (
        f"http://127.0.0.1:{daemon_port}/health",
        f"http://127.0.0.1:{upstream_port}/health",
    ):
        for _ in range(50):
            try:
                if httpx.get(url, timeout=0.2).status_code == 200:
                    break
            except Exception:
                time.sleep(0.05)
        else:
            pytest.fail(f"service not up: {url}")

    from mcp_guard.client import ApprovalClient

    client = ApprovalClient(settings)
    proxy_app = create_http_proxy_app(
        f"http://127.0.0.1:{upstream_port}",
        server_name="demo-http",
        settings=settings,
        client=client,
    )

    yield {
        "daemon": f"http://127.0.0.1:{daemon_port}",
        "upstream": f"http://127.0.0.1:{upstream_port}",
        "proxy_app": proxy_app,
        "settings": settings,
        "servers": (d_server, u_server),
        "threads": threads,
    }

    d_server.should_exit = True
    u_server.should_exit = True
    for t in threads:
        t.join(timeout=5)


def test_http_proxy_blocks_and_allows(stack):
    daemon = stack["daemon"]
    proxy_app = stack["proxy_app"]
    stop = threading.Event()

    def auto_decide() -> None:
        with httpx.Client(timeout=5.0) as c:
            while not stop.is_set():
                try:
                    pending = c.get(f"{daemon}/v1/approvals/pending").json()
                except httpx.HTTPError:
                    time.sleep(0.05)
                    continue
                for item in pending:
                    decision = "allow" if item["tool_name"] == "echo" else "block"
                    try:
                        c.post(
                            f"{daemon}/v1/approvals/{item['id']}/decide",
                            json={"decision": decision},
                        )
                    except httpx.HTTPError:
                        pass
                time.sleep(0.05)

    t = threading.Thread(target=auto_decide, daemon=True)
    t.start()

    try:
        with TestClient(proxy_app) as client:
            # tools/list passes through without approval
            listed = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            assert listed.status_code == 200
            assert "tools" in listed.json()["result"]

            allowed = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "echo", "arguments": {"message": "web"}},
                },
            )
            assert allowed.status_code == 200
            body = allowed.json()
            assert "result" in body
            assert "web" in body["result"]["content"][0]["text"]

            blocked = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "danger_delete",
                        "arguments": {"path": "/etc/passwd"},
                    },
                },
            )
            assert blocked.status_code == 200
            err = blocked.json()
            assert err["error"]["code"] == -32042
    finally:
        stop.set()
        t.join(timeout=2)
