"""Daemon API tests including concurrent approvals."""

from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from mcp_guard.models import Decision


def test_health(client: TestClient):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["fail_closed"] is True


def test_allow_and_block(client: TestClient):
    results: dict[str, dict] = {}

    def submit(key: str, tool: str) -> None:
        r = client.post(
            "/v1/approvals",
            json={
                "server_name": "demo",
                "tool_name": tool,
                "arguments": {"x": key},
                "transport": "stdio",
            },
        )
        results[key] = r.json()

    t1 = threading.Thread(target=submit, args=("a", "echo"))
    t1.start()
    # Wait until pending appears
    for _ in range(50):
        pending = client.get("/v1/approvals/pending").json()
        if pending:
            break
        time.sleep(0.05)
    assert pending
    rid = pending[0]["id"]
    decide = client.post(
        f"/v1/approvals/{rid}/decide",
        json={"decision": "allow", "decided_by": "test"},
    )
    assert decide.status_code == 200
    t1.join(timeout=5)
    assert results["a"]["decision"] == "allow"

    t2 = threading.Thread(target=submit, args=("b", "danger_delete"))
    t2.start()
    for _ in range(50):
        pending = client.get("/v1/approvals/pending").json()
        if pending:
            break
        time.sleep(0.05)
    rid = pending[0]["id"]
    client.post(
        f"/v1/approvals/{rid}/decide",
        json={"decision": "block", "decided_by": "test"},
    )
    t2.join(timeout=5)
    assert results["b"]["decision"] == "block"


def test_concurrent_pending_requests(client: TestClient):
    """Multiple simultaneous MCP requests stay pending until individually decided."""
    n = 5
    results: dict[int, dict] = {}
    barrier = threading.Barrier(n + 1)

    def worker(i: int) -> None:
        barrier.wait(timeout=5)
        r = client.post(
            "/v1/approvals",
            json={
                "server_name": "demo",
                "tool_name": f"tool_{i}",
                "arguments": {"i": i},
                "transport": "stdio",
            },
        )
        results[i] = r.json()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    barrier.wait(timeout=5)

    pending_ids: list[str] = []
    for _ in range(100):
        pending = client.get("/v1/approvals/pending").json()
        if len(pending) == n:
            pending_ids = [p["id"] for p in pending]
            break
        time.sleep(0.05)
    assert len(pending_ids) == n

    # Decide half allow, half block
    for idx, rid in enumerate(pending_ids):
        decision = "allow" if idx % 2 == 0 else "block"
        resp = client.post(
            f"/v1/approvals/{rid}/decide",
            json={"decision": decision, "decided_by": "test"},
        )
        assert resp.status_code == 200

    for t in threads:
        t.join(timeout=5)
    assert len(results) == n
    allows = sum(1 for r in results.values() if r["decision"] == "allow")
    blocks = sum(1 for r in results.values() if r["decision"] == "block")
    assert allows + blocks == n
    assert allows >= 2 and blocks >= 2


def test_timeout_fail_closed(settings, audit):
    from mcp_guard.daemon.server import create_app

    settings = settings.__class__(
        daemon_host=settings.daemon_host,
        daemon_port=settings.daemon_port,
        http_proxy_host=settings.http_proxy_host,
        http_proxy_port=settings.http_proxy_port,
        approval_timeout_seconds=1,
        fail_closed=True,
        fail_open=False,
    )
    app = create_app(settings=settings, audit=audit)
    with TestClient(app) as client:
        start = time.time()
        resp = client.post(
            "/v1/approvals",
            json={
                "server_name": "demo",
                "tool_name": "echo",
                "arguments": {},
                "transport": "stdio",
            },
        )
        elapsed = time.time() - start
        assert resp.status_code == 200
        body = resp.json()
        assert body["decision"] == Decision.TIMEOUT.value
        assert body["timed_out"] is True
        assert elapsed >= 0.9


def test_audit_log_written(client: TestClient, audit):
    def submit() -> None:
        client.post(
            "/v1/approvals",
            json={
                "server_name": "demo",
                "tool_name": "echo",
                "arguments": {"message": "x"},
                "transport": "http",
            },
        )

    t = threading.Thread(target=submit)
    t.start()
    for _ in range(50):
        pending = client.get("/v1/approvals/pending").json()
        if pending:
            break
        time.sleep(0.05)
    client.post(
        f"/v1/approvals/{pending[0]['id']}/decide",
        json={"decision": "allow"},
    )
    t.join(timeout=5)
    events = audit.read_all()
    names = [e["event"] for e in events]
    assert "approval_requested" in names
    assert "approval_decided" in names
