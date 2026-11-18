"""Append-only JSONL audit log."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from mcp_guard.models import AuditEvent, Decision
from mcp_guard.paths import audit_log_path


class AuditLog:
    """Thread-safe append-only audit trail."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or audit_log_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, event: AuditEvent) -> None:
        line = event.model_dump_json() + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)

    def record(
        self,
        event: str,
        *,
        request_id: str | None = None,
        server_name: str | None = None,
        tool_name: str | None = None,
        transport: str | None = None,
        decision: Decision | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.write(
            AuditEvent(
                event=event,
                request_id=request_id,
                server_name=server_name,
                tool_name=tool_name,
                transport=transport,
                decision=decision,
                details=details or {},
            )
        )

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                events.append(json.loads(line))
        return events
