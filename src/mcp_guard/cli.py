"""Command-line entrypoint: ``mcp-guard <subcommand>``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-guard",
        description="Human-in-the-loop approval gate for MCP tool calls",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("daemon", help="Run the background approval daemon")

    ui = sub.add_parser("ui", help="Run the always-on-top approval UI")
    ui.add_argument("--terminal", action="store_true", help="Force terminal UI")

    stdio = sub.add_parser(
        "stdio-proxy",
        help="Wrap a local stdio MCP server (used by Claude Desktop config)",
    )
    stdio.add_argument("--server-name", required=True)
    stdio.add_argument("upstream", nargs=argparse.REMAINDER)

    http = sub.add_parser(
        "http-proxy",
        help="Run an HTTP reverse proxy for Claude Web / remote connectors",
    )
    http.add_argument("--upstream", required=True)
    http.add_argument("--server-name", required=True)
    http.add_argument("--host", default=None)
    http.add_argument("--port", type=int, default=None)

    install = sub.add_parser(
        "install",
        help="Rewrite claude_desktop_config.json to wrap stdio servers",
    )
    install.add_argument("--config", type=Path, default=None)
    install.add_argument("--only", nargs="*", default=None)
    install.add_argument("--dry-run", action="store_true")

    uninstall = sub.add_parser(
        "uninstall",
        help="Remove MCP Guard wrappers from claude_desktop_config.json",
    )
    uninstall.add_argument("--config", type=Path, default=None)
    uninstall.add_argument("--only", nargs="*", default=None)
    uninstall.add_argument("--dry-run", action="store_true")

    restore = sub.add_parser(
        "restore",
        help="Restore claude_desktop_config.json from the latest backup",
    )
    restore.add_argument("--config", type=Path, default=None)
    restore.add_argument("--backup", type=Path, default=None)

    status = sub.add_parser("status", help="Show wrap status of Claude Desktop config")
    status.add_argument("--config", type=Path, default=None)

    demo = sub.add_parser("demo-server", help="Run the sample stdio MCP demo server")
    demo_http = sub.add_parser(
        "demo-http-server", help="Run the sample HTTP MCP demo server"
    )
    demo_http.add_argument("--host", default="127.0.0.1")
    demo_http.add_argument("--port", type=int, default=8765)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if args.command == "daemon":
        from mcp_guard.daemon.server import run_daemon

        run_daemon()
        return 0

    if args.command == "ui":
        from mcp_guard.ui.popup import run_ui

        return run_ui(prefer_terminal=args.terminal)

    if args.command == "stdio-proxy":
        from mcp_guard.proxy.stdio_proxy import run_stdio_proxy

        command = list(args.upstream)
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            parser.error("stdio-proxy requires an upstream command after --")
        return run_stdio_proxy(command, server_name=args.server_name)

    if args.command == "http-proxy":
        from mcp_guard.proxy.http_proxy import run_http_proxy
        from mcp_guard.settings import Settings, get_settings

        settings = get_settings()
        if args.host or args.port:
            settings = Settings(
                daemon_host=settings.daemon_host,
                daemon_port=settings.daemon_port,
                http_proxy_host=args.host or settings.http_proxy_host,
                http_proxy_port=args.port or settings.http_proxy_port,
                approval_timeout_seconds=settings.approval_timeout_seconds,
                fail_closed=settings.fail_closed,
                fail_open=settings.fail_open,
            )
        run_http_proxy(args.upstream, server_name=args.server_name, settings=settings)
        return 0

    if args.command == "install":
        from mcp_guard.installer.desktop_config import wrap_servers

        data = wrap_servers(args.config, only=args.only, dry_run=args.dry_run)
        print(json.dumps(data, indent=2))
        if args.dry_run:
            print("(dry-run: no files written)", file=sys.stderr)
        else:
            print("Installed. Fully quit and restart Claude Desktop.", file=sys.stderr)
        return 0

    if args.command == "uninstall":
        from mcp_guard.installer.desktop_config import unwrap_servers

        data = unwrap_servers(args.config, only=args.only, dry_run=args.dry_run)
        print(json.dumps(data, indent=2))
        return 0

    if args.command == "restore":
        from mcp_guard.installer.desktop_config import restore_config

        path = restore_config(args.config, backup=args.backup)
        print(f"Restored {path}")
        return 0

    if args.command == "status":
        from mcp_guard.installer.desktop_config import status as config_status

        print(json.dumps(config_status(args.config), indent=2))
        return 0

    if args.command == "demo-server":
        from mcp_guard.demo_server.stdio_server import main as demo_main

        return demo_main()

    if args.command == "demo-http-server":
        from mcp_guard.demo_server.http_server import main as demo_http_main

        return demo_http_main(["--host", args.host, "--port", str(args.port)])

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
