"""End-to-end stdio proxy tests against the demo MCP server."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from mcp_guard.daemon.audit import AuditLog
from mcp_guard.daemon.server import create_app
from mcp_guard.jsonrpc import encode_message, parse_message
from mcp_guard.settings import Settings


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_daemon(tmp_path: Path):
    port = _free_port()
    settings = Settings(
        daemon_host="127.0.0.1",
        daemon_port=port,
        approval_timeout_seconds=10,
        fail_closed=True,
    )
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    app = create_app(settings=settings, audit=audit)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            r = httpx.get(f"{base}/health", timeout=0.2)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.05)
    else:
        pytest.fail("daemon failed to start")

    yield {"base": base, "port": port, "settings": settings, "server": server}
    server.should_exit = True
    thread.join(timeout=5)


def _auto_decide(base: str, decision: str, tool_filter: str | None = None) -> threading.Event:
    stop = threading.Event()

    def loop() -> None:
        with httpx.Client(timeout=5.0) as client:
            while not stop.is_set():
                try:
                    pending = client.get(f"{base}/v1/approvals/pending").json()
                except Exception:
                    time.sleep(0.05)
                    continue
                for item in pending:
                    if tool_filter and item.get("tool_name") != tool_filter:
                        continue
                    client.post(
                        f"{base}/v1/approvals/{item['id']}/decide",
                        json={"decision": decision, "decided_by": "test"},
                    )
                time.sleep(0.05)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return stop


def test_stdio_proxy_allow_and_block(live_daemon, monkeypatch, tmp_path):
    base = live_daemon["base"]
    port = live_daemon["port"]
    monkeypatch.setenv("MCP_GUARD_DAEMON_PORT", str(port))
    monkeypatch.setenv("MCP_GUARD_DAEMON_HOST", "127.0.0.1")
    monkeypatch.setenv("MCP_GUARD_TIMEOUT", "10")
    monkeypatch.setenv("MCP_GUARD_FAIL_CLOSED", "1")

    stop_allow = _auto_decide(base, "allow", tool_filter="echo")
    # Separate blocker for danger_delete
    stop_block = _auto_decide(base, "block", tool_filter="danger_delete")

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mcp_guard.proxy.stdio_proxy",
            "--server-name",
            "demo",
            "--",
            sys.executable,
            "-m",
            "mcp_guard.demo_server.stdio_server",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**dict(**{k: v for k, v in __import__("os").environ.items()}),
             "MCP_GUARD_DAEMON_PORT": str(port),
             "MCP_GUARD_DAEMON_HOST": "127.0.0.1",
             "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
             },
    )

    def send(msg: dict) -> dict | None:
        assert proc.stdin and proc.stdout
        proc.stdin.write(encode_message(msg))
        proc.stdin.flush()
        # Read until we get a response with matching id (skip unrelated)
        deadline = time.time() + 10
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                return None
            parsed = parse_message(line)
            if parsed and parsed.get("id") == msg.get("id"):
                return parsed
        return None

    try:
        init = send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            }
        )
        assert init and "result" in init

        allowed = send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"message": "hello"}},
            }
        )
        assert allowed is not None
        assert "result" in allowed
        assert "hello" in allowed["result"]["content"][0]["text"]

        blocked = send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "danger_delete",
                    "arguments": {"path": "/tmp/x"},
                },
            }
        )
        assert blocked is not None
        assert "error" in blocked
        assert blocked["error"]["code"] == -32042
    finally:
        stop_allow.set()
        stop_block.set()
        if proc.stdin:
            proc.stdin.close()
        proc.kill()
        proc.wait(timeout=5)


def test_stdio_proxy_fail_closed_when_daemon_down(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_GUARD_DAEMON_PORT", "1")  # nothing listening
    monkeypatch.setenv("MCP_GUARD_DAEMON_HOST", "127.0.0.1")
    monkeypatch.setenv("MCP_GUARD_FAIL_CLOSED", "1")
    monkeypatch.setenv("MCP_GUARD_TIMEOUT", "2")

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mcp_guard.proxy.stdio_proxy",
            "--server-name",
            "demo",
            "--",
            sys.executable,
            "-m",
            "mcp_guard.demo_server.stdio_server",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **{k: v for k, v in __import__("os").environ.items()},
            "MCP_GUARD_DAEMON_PORT": "1",
            "MCP_GUARD_DAEMON_HOST": "127.0.0.1",
            "MCP_GUARD_FAIL_CLOSED": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        },
    )
    assert proc.stdin and proc.stdout
    try:
        # initialize still works (pass-through, no approval)
        proc.stdin.write(
            encode_message(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"},
                    },
                }
            )
        )
        proc.stdin.flush()
        line = proc.stdout.readline()
        assert parse_message(line)["id"] == 1

        proc.stdin.write(
            encode_message(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "echo", "arguments": {"message": "x"}},
                }
            )
        )
        proc.stdin.flush()
        line = proc.stdout.readline()
        msg = parse_message(line)
        assert msg["id"] == 2
        assert msg["error"]["code"] == -32043
    finally:
        proc.kill()
        proc.wait(timeout=5)
