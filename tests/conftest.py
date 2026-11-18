"""Shared pytest fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mcp_guard.daemon.audit import AuditLog
from mcp_guard.daemon.server import create_app
from mcp_guard.settings import Settings


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "mcp-guard-home"
    home.mkdir()
    monkeypatch.setenv("MCP_GUARD_HOME", str(home))
    return home


@pytest.fixture
def settings(tmp_home: Path) -> Settings:
    return Settings(
        daemon_host="127.0.0.1",
        daemon_port=8741,
        http_proxy_host="127.0.0.1",
        http_proxy_port=8742,
        approval_timeout_seconds=5,
        fail_closed=True,
        fail_open=False,
    )


@pytest.fixture
def audit(tmp_home: Path) -> AuditLog:
    return AuditLog(path=tmp_home / "audit.jsonl")


@pytest.fixture
def app(settings: Settings, audit: AuditLog):
    return create_app(settings=settings, audit=audit)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client
