# Demo script (record on your Mac or Windows laptop)

Record a 3–5 minute screencast showing **Allow** and **Block**. You need
**Claude Desktop** for the best Desktop path. Claude Web requires a public
HTTPS tunnel (Cloudflare Tunnel or ngrok).

---

## 0. One-time setup (before you hit record)

```bash
git clone <your-repo-url> mcp-guard
cd mcp-guard

# Create the venv:
#   macOS / Linux:  python3 -m venv .venv
#   Windows:        python -m venv .venv
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# After the venv is active, use `python` on every OS (it points at the venv).
python -m pip install -e ".[dev]"
python -m pytest -q                # should be green
```

Confirm Claude Desktop is installed and that you can edit config via
**Claude → Settings → Developer → Edit Config**.

---

## 1. Terminal layout (suggested)

Open **three** terminal panes **plus** Claude Desktop:

| Pane | Command |
|------|---------|
| A | `mcp-guard daemon` |
| B | `mcp-guard ui` |
| C | kept free for install / status |

Keep the approval window visible (not behind Claude).

---

## 2. Desktop demo — Allow + Block (primary)

### 2.1 Install the demo server into Claude Desktop

```bash
# Optional: start from a known config. If you already have servers, install
# will wrap all of them. To wrap only the demo, add it first.

# Uses sys.executable so Claude launches the same interpreter as this venv
# (works on macOS, Windows, and Linux).
python - <<'PY'
import json, sys
from mcp_guard.paths import claude_desktop_config_path
path = claude_desktop_config_path()
path.parent.mkdir(parents=True, exist_ok=True)
data = {"mcpServers": {}}
if path.exists():
    data = json.loads(path.read_text())
    data.setdefault("mcpServers", {})
data["mcpServers"]["mcp-guard-demo"] = {
    "command": sys.executable,
    "args": ["-m", "mcp_guard.demo_server.stdio_server"],
}
path.write_text(json.dumps(data, indent=2) + "\n")
print("wrote", path)
PY

mcp-guard install --only mcp-guard-demo
mcp-guard status
```

**Windows PowerShell** (the `<<'PY'` heredoc above is bash-only). Pipe a
here-string into the venv's Python instead:

```powershell
@'
import json, sys
from mcp_guard.paths import claude_desktop_config_path
path = claude_desktop_config_path()
path.parent.mkdir(parents=True, exist_ok=True)
data = {"mcpServers": {}}
if path.exists():
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    data.setdefault("mcpServers", {})
data["mcpServers"]["mcp-guard-demo"] = {
    "command": sys.executable,
    "args": ["-m", "mcp_guard.demo_server.stdio_server"],
}
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
print("wrote", path)
'@ | python -

mcp-guard install --only mcp-guard-demo
mcp-guard status
```

> Decide within ~50 s while recording: Claude's MCP client gives up on a
> request after about 60 s, independently of `MCP_GUARD_TIMEOUT`. If Claude
> cancels first, MCP Guard withdraws the popup and never runs the call.

**Fully quit Claude Desktop** (macOS: Cmd+Q; Windows: tray → Quit) and reopen it.

### 2.2 Record — Allow path

1. Show panes A/B running (daemon + UI).
2. In Claude Desktop, start a new chat and enable the **mcp-guard-demo** connector/tools.
3. Prompt: `Use the echo tool with message "hello from MCP Guard".`
4. When Claude tries to call the tool, the **MCP Guard** window should jump
   to the front showing server `mcp-guard-demo`, tool `echo`, and arguments.
5. Click **Allow**.
6. Show Claude receiving the echoed result.

### 2.3 Record — Block path

1. Prompt: `Use the danger_delete tool on path "/tmp/demo-secret.txt".`
2. Popup appears with tool `danger_delete`.
3. Click **Block**.
4. Show Claude surfacing an error / denial (JSON-RPC error from the proxy).
5. Optionally open the audit log:

```bash
# macOS
tail -n 5 "$HOME/Library/Application Support/mcp-guard/audit.jsonl"
# Windows (PowerShell)
Get-Content "$env:APPDATA\mcp-guard\audit.jsonl" -Tail 5
```

### 2.4 Narrate concurrency (optional but strong)

In one prompt ask Claude to call `echo` twice with different messages (or run
two chats). Show two pending panels and Allow one / Block the other.

---

## 3. Claude Web demo (secondary, if time allows)

Claude Web talks to MCP from **Anthropic’s cloud**, so your proxy must be on a
public `https://…:443` URL.

```bash
# Terminal D — upstream demo HTTP MCP (serves POST /mcp)
mcp-guard demo-http-server --port 8765

# Terminal E — local reverse proxy
# Upstream is the *origin* only. The proxy appends the request path (/mcp),
# so do not pass .../mcp here or you will get /mcp/mcp upstream.
mcp-guard http-proxy --server-name demo-http --upstream http://127.0.0.1:8765/

# Terminal F — publish :8742 (default proxy port) on HTTPS 443
# Cloudflare quick tunnel example:
cloudflared tunnel --url http://127.0.0.1:8742
# or: ngrok http 8742
```

1. Copy the `https://…` URL from the tunnel.
2. In Claude Web: **Customize → Connectors → Add custom connector**.
3. Paste `https://<tunnel>/mcp` (Claude will POST to `/mcp` on the proxy,
   which forwards to `http://127.0.0.1:8765/mcp`). Recommended: start the
   proxy with `MCP_GUARD_HTTP_TOKEN=<random>` and paste
   `https://<tunnel>/<random>/mcp` instead, so nobody else who finds the
   tunnel URL can reach it.
4. Auth: “No sign in” for the local demo.
5. In a chat, enable the connector and repeat the Allow / Block prompts from §2.
6. Narrate: “Anthropic’s servers hit my tunnel; the local proxy holds
   `tools/call` until I approve.”

---

## 4. Cleanup after recording

```bash
mcp-guard uninstall --only mcp-guard-demo
# or fully restore the pre-install backup:
mcp-guard restore
```

Fully quit and restart Claude Desktop again.

---

## 5. Talking points while presenting

- Desktop interception is a **stdio JSON-RPC proxy** pointed to by
  `claude_desktop_config.json` — not an OS hook.
- Web interception is a **Streamable HTTP reverse proxy** that you must place
  on the connector URL Anthropic calls.
- One daemon owns the queue, audit log, and timeouts; proxies are fail-closed.
- Block returns a deliberate JSON-RPC error (`-32042`) and never forwards.

## 6. If something doesn’t pop up

1. `curl -s http://127.0.0.1:8741/health` — daemon up?
2. `mcp-guard status` — demo server wrapped?
3. Did you **fully** restart Claude Desktop after install?
4. UI hidden? `mcp-guard ui --terminal` as a fallback and still show decisions.
