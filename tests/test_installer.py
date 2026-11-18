import json
from pathlib import Path

from mcp_guard.installer.desktop_config import (
    backup_config,
    restore_config,
    status,
    unwrap_servers,
    wrap_servers,
)


def test_wrap_unwrap_and_backup(tmp_path: Path):
    config = tmp_path / "claude_desktop_config.json"
    original = {
        "mcpServers": {
            "demo": {
                "command": "python",
                "args": ["-m", "mcp_guard.demo_server.stdio_server"],
                "env": {"TOKEN": "secret"},
            }
        }
    }
    config.write_text(json.dumps(original, indent=2), encoding="utf-8")

    wrapped = wrap_servers(config)
    entry = wrapped["mcpServers"]["demo"]
    assert "mcp_guard.proxy.stdio_proxy" in entry["args"]
    assert entry["env"]["MCP_GUARD_WRAPPED"] == "1"
    assert entry["env"]["TOKEN"] == "secret"
    assert "--server-name" in entry["args"]
    assert "demo" in entry["args"]

    st = status(config)
    assert st["servers"]["demo"]["wrapped"] is True

    # Latest backup should exist and match pre-wrap content conceptually
    backup = config.with_name(config.name + ".mcp-guard.bak")
    assert backup.exists()

    unwrapped = unwrap_servers(config)
    demo = unwrapped["mcpServers"]["demo"]
    assert demo["command"] == "python"
    assert demo["args"] == ["-m", "mcp_guard.demo_server.stdio_server"]
    assert demo["env"]["TOKEN"] == "secret"
    assert "MCP_GUARD_WRAPPED" not in demo["env"]


def test_restore(tmp_path: Path):
    config = tmp_path / "claude_desktop_config.json"
    config.write_text('{"mcpServers":{"a":{"command":"x"}}}', encoding="utf-8")
    backup_config(config)
    config.write_text('{"mcpServers":{}}', encoding="utf-8")
    restore_config(config)
    data = json.loads(config.read_text(encoding="utf-8"))
    assert "a" in data["mcpServers"]
