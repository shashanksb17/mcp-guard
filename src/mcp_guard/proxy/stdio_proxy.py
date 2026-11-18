"""
Stdio MCP proxy for Claude Desktop.

Claude Desktop launches this process instead of the real MCP server. The proxy:

1. Spawns the real server as a child (command + args from config).
2. Relays newline-delimited JSON-RPC in both directions.
3. Holds every ``tools/call`` until the approval daemon returns Allow/Block.
4. On Block / timeout / daemon-down (fail-closed), returns a JSON-RPC error
   and never forwards the call to the child.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
from typing import Any, TextIO

from mcp_guard.client import ApprovalClient, DaemonUnavailable, decision_allows_forward
from mcp_guard.jsonrpc import (
    blocked_response,
    daemon_down_response,
    encode_message,
    extract_tool_call,
    is_tools_call,
    parse_message,
    timeout_response,
)
from mcp_guard.models import Decision
from mcp_guard.settings import get_settings

logger = logging.getLogger(__name__)

# Serialize writes to Claude (stdout) and to the child (stdin).
_stdout_lock = threading.Lock()
_stdin_lock = threading.Lock()


def _write_stdout(message: dict[str, Any]) -> None:
    with _stdout_lock:
        sys.stdout.buffer.write(encode_message(message))
        sys.stdout.buffer.flush()


def _write_raw_to(stream, data: bytes) -> None:
    with _stdin_lock:
        stream.write(data)
        stream.flush()


def _forward_stderr(proc: subprocess.Popen[bytes]) -> None:
    assert proc.stderr is not None
    for line in iter(proc.stderr.readline, b""):
        sys.stderr.buffer.write(line)
        sys.stderr.buffer.flush()


def _forward_child_stdout(proc: subprocess.Popen[bytes]) -> None:
    """Pass child JSON-RPC frames through to Claude Desktop unchanged."""
    assert proc.stdout is not None
    for raw in iter(proc.stdout.readline, b""):
        with _stdout_lock:
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()


def handle_tools_call(
    message: dict[str, Any],
    *,
    proc: subprocess.Popen[bytes],
    client: ApprovalClient,
    server_name: str,
    fail_closed: bool,
) -> None:
    """Hold one tools/call until the daemon returns a decision."""
    assert proc.stdin is not None
    tool_name, arguments = extract_tool_call(message)
    request_id = message.get("id")
    logger.info("intercept tools/call server=%s tool=%s id=%s", server_name, tool_name, request_id)

    try:
        result = client.request_approval(
            server_name=server_name,
            tool_name=tool_name,
            arguments=arguments,
            transport="stdio",
            raw_params=message.get("params") or {},
            metadata={"jsonrpc_id": request_id},
        )
    except DaemonUnavailable:
        logger.error("daemon unavailable; fail_closed=%s", fail_closed)
        if fail_closed:
            _write_stdout(daemon_down_response(request_id))
            return
        _write_raw_to(proc.stdin, encode_message(message))
        return

    if result.decision == Decision.TIMEOUT:
        _write_stdout(timeout_response(request_id))
        return

    if result.decision == Decision.BLOCK or not decision_allows_forward(result):
        _write_stdout(blocked_response(request_id))
        return

    _write_raw_to(proc.stdin, encode_message(message))


def handle_client_message(
    message: dict[str, Any],
    *,
    proc: subprocess.Popen[bytes],
    client: ApprovalClient,
    server_name: str,
    fail_closed: bool,
    workers: list[threading.Thread],
) -> None:
    """
    Inspect one client→server message.

    Non-tool traffic is forwarded immediately. ``tools/call`` runs in a worker
    thread so several approvals can be pending at once on a single stdio pipe.
    """
    assert proc.stdin is not None

    if not is_tools_call(message):
        _write_raw_to(proc.stdin, encode_message(message))
        return

    worker = threading.Thread(
        target=handle_tools_call,
        kwargs={
            "message": message,
            "proc": proc,
            "client": client,
            "server_name": server_name,
            "fail_closed": fail_closed,
        },
        daemon=True,
    )
    workers.append(worker)
    worker.start()


def run_stdio_proxy(
    command: list[str],
    *,
    server_name: str,
    env: dict[str, str] | None = None,
    client: ApprovalClient | None = None,
    stdin: TextIO | None = None,
) -> int:
    """
    Run the proxy until the child exits or stdin closes.

    ``command`` is the real MCP server argv (e.g. ``["python", "demo_server.py"]``).
    """
    if not command:
        raise ValueError("upstream command is required")

    settings = get_settings()
    client = client or ApprovalClient(settings)
    child_env = os.environ.copy()
    if env:
        child_env.update(env)

    logger.info("starting upstream: %s (server_name=%s)", command, server_name)
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_env,
    )

    t_out = threading.Thread(target=_forward_child_stdout, args=(proc,), daemon=True)
    t_err = threading.Thread(target=_forward_stderr, args=(proc,), daemon=True)
    t_out.start()
    t_err.start()

    workers: list[threading.Thread] = []
    input_stream = stdin or sys.stdin
    try:
        while True:
            line = input_stream.readline()
            if line == "" or line is None:
                break
            try:
                message = parse_message(line)
            except Exception:  # noqa: BLE001 — malformed frames go to upstream
                logger.warning("malformed JSON-RPC from client; forwarding raw")
                assert proc.stdin is not None
                raw = line if isinstance(line, str) else line.decode("utf-8", "replace")
                if not raw.endswith("\n"):
                    raw += "\n"
                _write_raw_to(proc.stdin, raw.encode("utf-8"))
                continue
            if message is None:
                continue
            handle_client_message(
                message,
                proc=proc,
                client=client,
                server_name=server_name,
                fail_closed=settings.fail_closed,
                workers=workers,
            )
    finally:
        for worker in workers:
            worker.join(timeout=settings.approval_timeout_seconds + 15)
        if proc.stdin and not proc.stdin.closed:
            try:
                proc.stdin.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    return proc.returncode or 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-guard stdio-proxy",
        description="Wrap a local stdio MCP server with human approval for tools/call",
    )
    parser.add_argument(
        "--server-name",
        required=True,
        help="Logical server name shown in the approval popup",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Upstream command after --  e.g. -- python -m mcp_guard.demo_server",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    parser = build_parser()
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("upstream command required after --")
    return run_stdio_proxy(command, server_name=args.server_name)


if __name__ == "__main__":
    raise SystemExit(main())
