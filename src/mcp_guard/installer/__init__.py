"""Claude Desktop config installer (backup / wrap / restore)."""

from mcp_guard.installer.desktop_config import (
    backup_config,
    restore_config,
    wrap_servers,
    unwrap_servers,
)

__all__ = ["backup_config", "restore_config", "wrap_servers", "unwrap_servers"]
