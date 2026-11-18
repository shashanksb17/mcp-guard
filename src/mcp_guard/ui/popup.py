"""
Always-on-top approval UI.

Uses Tkinter when available (macOS / Windows / most Linux desktops).
Falls back to a terminal prompt when Tkinter is missing (headless CI, some
minimal Linux images). Multiple pending requests are shown as stacked
panels so concurrent MCP calls can be decided independently.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from typing import Any, Callable

import httpx

from mcp_guard.models import Decision
from mcp_guard.settings import get_settings

logger = logging.getLogger(__name__)


def _format_arguments(arguments: dict[str, Any]) -> str:
    try:
        return json.dumps(arguments, indent=2, ensure_ascii=False)
    except TypeError:
        return str(arguments)


class ApprovalUIController:
    """Polls the daemon and dispatches decisions."""

    def __init__(
        self,
        base_url: str,
        *,
        poll_interval: float = 0.5,
        decide_fn: Callable[[str, Decision], None] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.poll_interval = poll_interval
        self._decide_fn = decide_fn
        self._stop = threading.Event()

    def list_pending(self) -> list[dict[str, Any]]:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{self.base_url}/v1/approvals/pending")
            resp.raise_for_status()
            return resp.json()

    def decide(self, request_id: str, decision: Decision) -> None:
        if self._decide_fn is not None:
            self._decide_fn(request_id, decision)
            return
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(
                f"{self.base_url}/v1/approvals/{request_id}/decide",
                json={"decision": decision.value, "decided_by": "ui"},
            )
            resp.raise_for_status()

    def stop(self) -> None:
        self._stop.set()


def _tkinter_available() -> bool:
    try:
        import tkinter  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def run_tk_ui(controller: ApprovalUIController) -> int:
    import tkinter as tk
    from tkinter import scrolledtext, ttk

    root = tk.Tk()
    root.title("MCP Guard — Approval Required")
    root.attributes("-topmost", True)
    try:
        # Windows: force foreground affinity when supported.
        root.wm_attributes("-topmost", True)
    except tk.TclError:
        pass
    root.geometry("640x520")
    root.minsize(480, 360)

    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    header = ttk.Label(
        root,
        text="MCP Guard",
        font=("Segoe UI", 16, "bold") if sys.platform == "win32" else ("Helvetica", 16, "bold"),
    )
    header.pack(anchor="w", padx=16, pady=(16, 4))
    sub = ttk.Label(
        root,
        text="Pending MCP tool calls — Allow or Block each request.",
    )
    sub.pack(anchor="w", padx=16, pady=(0, 8))

    canvas = tk.Canvas(root, highlightthickness=0)
    scrollbar = ttk.Scrollbar(root, orient="vertical", command=canvas.yview)
    frame = ttk.Frame(canvas)
    frame.bind(
        "<Configure>",
        lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
    )
    canvas.create_window((0, 0), window=frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side="left", fill="both", expand=True, padx=(16, 0), pady=8)
    scrollbar.pack(side="right", fill="y", padx=(0, 8), pady=8)

    panels: dict[str, ttk.LabelFrame] = {}

    def clear_stale(active_ids: set[str]) -> None:
        for rid in list(panels):
            if rid not in active_ids:
                panels[rid].destroy()
                del panels[rid]

    def render(pending: list[dict[str, Any]]) -> None:
        active = {p["id"] for p in pending}
        clear_stale(active)
        for item in pending:
            rid = item["id"]
            if rid in panels:
                continue
            box = ttk.LabelFrame(
                frame,
                text=f"{item.get('server_name')} · {item.get('tool_name')}",
                padding=10,
            )
            box.pack(fill="x", padx=4, pady=6)

            meta = ttk.Label(
                box,
                text=(
                    f"Transport: {item.get('transport')}    "
                    f"Expires: {item.get('expires_at')}"
                ),
            )
            meta.pack(anchor="w")

            args_view = scrolledtext.ScrolledText(box, height=8, wrap="word")
            args_view.insert("1.0", _format_arguments(item.get("arguments") or {}))
            args_view.configure(state="disabled")
            args_view.pack(fill="x", pady=6)

            btns = ttk.Frame(box)
            btns.pack(anchor="e")

            def make_handler(request_id: str, decision: Decision):
                def _handler() -> None:
                    try:
                        controller.decide(request_id, decision)
                    except Exception as exc:  # noqa: BLE001
                        logger.error("decide failed: %s", exc)

                return _handler

            ttk.Button(
                btns, text="Block", command=make_handler(rid, Decision.BLOCK)
            ).pack(side="right", padx=4)
            ttk.Button(
                btns, text="Allow", command=make_handler(rid, Decision.ALLOW)
            ).pack(side="right", padx=4)

            panels[rid] = box

        if not pending:
            if "empty" not in panels:
                empty = ttk.LabelFrame(frame, text="Idle", padding=10)
                empty.pack(fill="x", padx=4, pady=6)
                ttk.Label(
                    empty,
                    text="No pending MCP requests. Waiting for Claude…",
                ).pack(anchor="w")
                panels["empty"] = empty
        elif "empty" in panels:
            panels["empty"].destroy()
            del panels["empty"]

    def poll() -> None:
        if controller._stop.is_set():
            return
        try:
            pending = controller.list_pending()
            render(pending)
        except Exception as exc:  # noqa: BLE001
            logger.debug("poll error: %s", exc)
        root.after(int(controller.poll_interval * 1000), poll)

    def on_close() -> None:
        controller.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    # Re-assert topmost periodically so the dialog stays above Claude Desktop.
    def keep_topmost() -> None:
        if controller._stop.is_set():
            return
        try:
            root.attributes("-topmost", True)
            root.lift()
        except tk.TclError:
            pass
        root.after(2000, keep_topmost)

    poll()
    keep_topmost()
    root.mainloop()
    return 0


def run_terminal_ui(controller: ApprovalUIController) -> int:
    print("MCP Guard terminal UI (Tkinter unavailable).", flush=True)
    print("Commands: a <id> = allow, b <id> = block, r = refresh, q = quit", flush=True)
    known: dict[str, dict[str, Any]] = {}

    def refresh() -> None:
        nonlocal known
        try:
            pending = controller.list_pending()
        except Exception as exc:  # noqa: BLE001
            print(f"[error] cannot reach daemon: {exc}", flush=True)
            return
        known = {p["id"]: p for p in pending}
        if not pending:
            print("[idle] no pending requests", flush=True)
            return
        for p in pending:
            print(
                f"- {p['id'][:8]}…  {p.get('server_name')}/{p.get('tool_name')}\n"
                f"  args: {_format_arguments(p.get('arguments') or {})}",
                flush=True,
            )

    refresh()
    while not controller._stop.is_set():
        try:
            line = input("> ").strip()
        except EOFError:
            break
        if not line:
            refresh()
            continue
        if line in {"q", "quit", "exit"}:
            break
        if line in {"r", "refresh"}:
            refresh()
            continue
        parts = line.split()
        if len(parts) == 2 and parts[0] in {"a", "b", "allow", "block"}:
            action, token = parts
            match = next((rid for rid in known if rid.startswith(token)), None)
            if match is None and token in known:
                match = token
            if match is None:
                print("unknown id; run r to refresh", flush=True)
                continue
            decision = Decision.ALLOW if action.startswith("a") else Decision.BLOCK
            try:
                controller.decide(match, decision)
                print(f"decided {decision.value} for {match}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"decide failed: {exc}", flush=True)
            refresh()
            continue
        print("unknown command", flush=True)
    return 0


def run_ui(*, prefer_terminal: bool = False) -> int:
    settings = get_settings()
    controller = ApprovalUIController(settings.daemon_base_url)
    if prefer_terminal or not _tkinter_available():
        return run_terminal_ui(controller)
    return run_tk_ui(controller)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(prog="mcp-guard ui")
    parser.add_argument(
        "--terminal",
        action="store_true",
        help="Force terminal UI even if Tkinter is available",
    )
    args = parser.parse_args(argv)
    return run_ui(prefer_terminal=args.terminal)


if __name__ == "__main__":
    raise SystemExit(main())
