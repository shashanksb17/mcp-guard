"""
Safely rewrite Claude Desktop's ``claude_desktop_config.json``.

Each stdio server entry is rewritten so ``command`` launches
``python -m mcp_guard.proxy.stdio_proxy --server-name <name> -- <original…>``.

A timestamped backup is written before every destructive change. ``restore``
copies the newest backup back into place (or a path you pass explicitly).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp_guard.constants import CONFIG_BACKUP_SUFFIX
from mcp_guard.paths import claude_desktop_config_path

MARKER_ENV = "MCP_GUARD_WRAPPED"
ORIGINAL_COMMAND_KEY = "_mcp_guard_original"


def _python_executable() -> str:
    return sys.executable


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"mcpServers": {}}
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"config root must be an object: {path}")
    data.setdefault("mcpServers", {})
    if not isinstance(data["mcpServers"], dict):
        raise ValueError("mcpServers must be an object")
    return data


def _dump(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    tmp.replace(path)


def backup_config(path: Path | None = None) -> Path:
    """Copy the current config to a timestamped ``.mcp-guard.bak`` file."""
    path = path or claude_desktop_config_path()
    if not path.exists():
        # Create an empty stub so first-time users still get a restore target.
        _dump(path, {"mcpServers": {}})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}{CONFIG_BACKUP_SUFFIX}.{stamp}")
    shutil.copy2(path, backup)
    # Also maintain a "latest" pointer for easy restore.
    latest = path.with_name(f"{path.name}{CONFIG_BACKUP_SUFFIX}")
    shutil.copy2(path, latest)
    return backup


def restore_config(
    path: Path | None = None,
    *,
    backup: Path | None = None,
) -> Path:
    """Restore config from ``backup`` or the latest ``.mcp-guard.bak`` file."""
    path = path or claude_desktop_config_path()
    if backup is None:
        backup = path.with_name(f"{path.name}{CONFIG_BACKUP_SUFFIX}")
    if not backup.exists():
        raise FileNotFoundError(f"no backup found at {backup}")
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup, path)
    return path


def _is_wrapped(entry: dict[str, Any]) -> bool:
    env = entry.get("env") or {}
    if env.get(MARKER_ENV) == "1":
        return True
    args = entry.get("args") or []
    return any("mcp_guard.proxy.stdio_proxy" in str(a) for a in args)


def _wrap_entry(name: str, entry: dict[str, Any]) -> dict[str, Any]:
    if _is_wrapped(entry):
        return entry

    original_command = entry.get("command")
    if not original_command:
        raise ValueError(f"server '{name}' has no command (HTTP entries are not wrapable here)")

    original_args = list(entry.get("args") or [])
    original_env = dict(entry.get("env") or {})

    wrapped = deepcopy(entry)
    wrapped[ORIGINAL_COMMAND_KEY] = {
        "command": original_command,
        "args": original_args,
        "env": original_env,
    }
    wrapped["command"] = _python_executable()
    wrapped["args"] = [
        "-m",
        "mcp_guard.proxy.stdio_proxy",
        "--server-name",
        name,
        "--",
        original_command,
        *original_args,
    ]
    env = dict(original_env)
    env[MARKER_ENV] = "1"
    wrapped["env"] = env
    return wrapped


def _unwrap_entry(entry: dict[str, Any]) -> dict[str, Any]:
    if ORIGINAL_COMMAND_KEY in entry:
        original = entry[ORIGINAL_COMMAND_KEY]
        restored = {k: v for k, v in entry.items() if k != ORIGINAL_COMMAND_KEY}
        restored["command"] = original["command"]
        restored["args"] = original.get("args") or []
        env = dict(original.get("env") or {})
        env.pop(MARKER_ENV, None)
        if env:
            restored["env"] = env
        elif "env" in restored:
            # Drop marker-only env.
            existing = dict(restored.get("env") or {})
            existing.pop(MARKER_ENV, None)
            if existing:
                restored["env"] = existing
            else:
                restored.pop("env", None)
        return restored

    if not _is_wrapped(entry):
        return entry

    # Best-effort unwrap when metadata was stripped: peel args after "--".
    args = list(entry.get("args") or [])
    if "--" in args:
        idx = args.index("--")
        upstream = args[idx + 1 :]
        if upstream:
            restored = {k: v for k, v in entry.items() if k != ORIGINAL_COMMAND_KEY}
            restored["command"] = upstream[0]
            restored["args"] = upstream[1:]
            env = dict(restored.get("env") or {})
            env.pop(MARKER_ENV, None)
            if env:
                restored["env"] = env
            else:
                restored.pop("env", None)
            return restored
    return entry


def wrap_servers(
    path: Path | None = None,
    *,
    only: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Rewrite stdio servers to go through the MCP Guard proxy.

    Returns the new config object. Writes to disk unless ``dry_run``.
    """
    path = path or claude_desktop_config_path()
    data = _load(path)
    servers = data["mcpServers"]
    names = list(servers.keys()) if only is None else only
    for name in names:
        if name not in servers:
            raise KeyError(f"server not found in config: {name}")
        servers[name] = _wrap_entry(name, servers[name])
    if not dry_run:
        backup_config(path)
        _dump(path, data)
    return data


def unwrap_servers(
    path: Path | None = None,
    *,
    only: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    path = path or claude_desktop_config_path()
    data = _load(path)
    servers = data["mcpServers"]
    names = list(servers.keys()) if only is None else only
    for name in names:
        if name not in servers:
            raise KeyError(f"server not found in config: {name}")
        servers[name] = _unwrap_entry(servers[name])
    if not dry_run:
        backup_config(path)
        _dump(path, data)
    return data


def status(path: Path | None = None) -> dict[str, Any]:
    path = path or claude_desktop_config_path()
    data = _load(path)
    servers = data.get("mcpServers") or {}
    return {
        "path": str(path),
        "exists": path.exists(),
        "servers": {
            name: {
                "wrapped": _is_wrapped(entry if isinstance(entry, dict) else {}),
                "command": (entry or {}).get("command") if isinstance(entry, dict) else None,
            }
            for name, entry in servers.items()
        },
    }
