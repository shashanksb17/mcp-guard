"""Platform-aware paths for config, audit log, and Claude Desktop."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from mcp_guard.constants import APP_DIR_NAME, AUDIT_LOG_NAME, RUNTIME_STATE_NAME


def app_data_dir() -> Path:
    """Return the per-user data directory for MCP Guard."""
    override = os.environ.get("MCP_GUARD_HOME")
    if override:
        path = Path(override).expanduser()
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    elif sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        path = Path(base) / APP_DIR_NAME
    else:
        # Linux / other — Claude Desktop is not officially supported here,
        # but the daemon and proxies still run for development and CI.
        xdg = os.environ.get("XDG_DATA_HOME")
        path = Path(xdg) / APP_DIR_NAME if xdg else Path.home() / ".local" / "share" / APP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def audit_log_path() -> Path:
    return app_data_dir() / AUDIT_LOG_NAME


def daemon_pid_path() -> Path:
    return app_data_dir() / RUNTIME_STATE_NAME


def claude_desktop_config_path() -> Path:
    """Return the expected Claude Desktop MCP config path for this OS."""
    override = os.environ.get("MCP_GUARD_CLAUDE_CONFIG")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    if sys.platform == "win32":
        # Standard Roaming path. MSIX installs may use a LocalCache path;
        # the installer documents that fallback.
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Claude" / "claude_desktop_config.json"
    # Community / beta Linux builds.
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
