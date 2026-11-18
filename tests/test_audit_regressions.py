"""Regression tests for issues found in the pre-submission audit."""

from __future__ import annotations

import json
import os
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
from mcp_guard.installer.desktop_config import restore_config, wrap_servers
from mcp_guard.jsonrpc import batch_contains_tools_call, cancelled_request_id, extract_tool_call
from mcp_guard.settings import Settings

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def daemon(tmp_path: Path):
    port = _free_port()
    settings = Settings(daemon_host="127.0.0.1", daemon_port=port,
                        approval_timeout_seconds=10, fail_closed=True)
    app = create_app(settings=settings, audit=AuditLog(path=tmp_path / "audit.jsonl"))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{base}/health", timeout=0.2).status_code == 200:
                break
        except Exception:
            time.sleep(0.05)
    yield {"base": base, "port": port}
    server.should_exit = True
    t.join(timeout=5)


def _proxy(port: int, child: list[str], extra_env: dict[str, str] | None = None):
    env = {**os.environ, "MCP_GUARD_DAEMON_PORT": str(port), "MCP_GUARD_DAEMON_HOST": "127.0.0.1",
           "PYTHONPATH": SRC, **(extra_env or {})}
    return subprocess.Popen(
        [sys.executable, "-m", "mcp_guard.proxy.stdio_proxy", "--server-name", "demo", "--", *child],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)


def _pending(base: str, n: int = 1) -> list[dict]:
    for _ in range(100):
        p = httpx.get(f"{base}/v1/approvals/pending").json()
        if len(p) >= n:
            return p
        time.sleep(0.05)
    return p


def test_jsonrpc_helpers():
    assert batch_contains_tools_call([{"jsonrpc": "2.0", "id": 1, "method": "tools/call"}])
    assert not batch_contains_tools_call({"jsonrpc": "2.0", "id": 1, "method": "tools/call"})
    assert cancelled_request_id(
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 7}}) == 7
    assert extract_tool_call({"id": 1, "method": "tools/call", "params": ["x"]})[0] == "unknown"


def test_batched_tools_call_requires_approval(daemon):
    proc = _proxy(daemon["port"], [sys.executable, "-m", "mcp_guard.demo_server.stdio_server"])
    batch = [{"jsonrpc": "2.0", "id": 5, "method": "tools/call",
              "params": {"name": "danger_delete", "arguments": {"path": "/b"}}}]
    proc.stdin.write((json.dumps(batch) + "\n").encode())
    proc.stdin.flush()
    pending = _pending(daemon["base"])
    assert [p["tool_name"] for p in pending] == ["danger_delete"]
    httpx.post(f"{daemon['base']}/v1/approvals/{pending[0]['id']}/decide", json={"decision": "block"})
    line = json.loads(proc.stdout.readline())
    assert line["error"]["code"] == -32042
    proc.stdin.close()
    proc.wait(10)


def test_cancelled_call_is_never_forwarded(daemon):
    child = [sys.executable, "-u", "-c", "import sys\nfor l in sys.stdin: sys.stderr.write('GOT '+l)"]
    proc = _proxy(daemon["port"], child)
    w = lambda m: (proc.stdin.write((json.dumps(m) + "\n").encode()), proc.stdin.flush())  # noqa: E731
    w({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "danger_delete"}})
    assert len(_pending(daemon["base"])) == 1
    w({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 9}})
    time.sleep(0.5)
    assert httpx.get(f"{daemon['base']}/v1/approvals/pending").json() == []
    proc.stdin.close()
    proc.wait(10)
    assert "GOT {" not in proc.stderr.read().decode()


def test_non_ascii_arguments_survive_ansi_codepage(daemon):
    """Windows pipes default to cp1252; the proxy must read UTF-8 bytes."""
    proc = _proxy(daemon["port"], [sys.executable, "-m", "mcp_guard.demo_server.stdio_server"],
                  {"PYTHONIOENCODING": "cp1252"})
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "echo", "arguments": {"message": "héllo नमस्ते"}}}
    proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
    proc.stdin.flush()
    pending = _pending(daemon["base"])
    assert pending[0]["arguments"] == {"message": "héllo नमस्ते"}
    httpx.post(f"{daemon['base']}/v1/approvals/{pending[0]['id']}/decide", json={"decision": "allow"})
    out = json.loads(proc.stdout.readline())
    assert out["result"]["content"][0]["text"] == "echo: héllo नमस्ते"
    proc.stdin.close()
    proc.wait(10)


def test_daemon_rejects_foreign_host_header(daemon):
    r = httpx.get(f"{daemon['base']}/v1/approvals/pending", headers={"Host": "evil.example"})
    assert r.status_code == 400


def test_install_twice_then_restore_gives_original(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "fs": {"command": "npx", "args": ["-y", "server-filesystem"]},
        "remote": {"type": "http", "url": "https://example.com/mcp"},
    }}), encoding="utf-8")
    wrap_servers(cfg)  # remote entry is skipped, not fatal
    time.sleep(1.1)
    wrap_servers(cfg)
    restore_config(cfg)
    assert json.loads(cfg.read_text())["mcpServers"]["fs"]["command"] == "npx"


def test_installer_accepts_utf8_bom(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_bytes(b'\xef\xbb\xbf{"mcpServers": {}}')
    wrap_servers(cfg, dry_run=True)
