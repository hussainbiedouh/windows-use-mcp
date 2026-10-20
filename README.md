# windows-use-mcp

An [MCP server](https://modelcontextprotocol.io) that puts the
[Windows-Use](https://github.com/Jeomon/Windows-Use) GUI agent behind a set of
MCP tools, so any MCP client (OpenCode, Claude Desktop, Cursor, VS Code, …) can
drive the real Windows desktop.

Windows-Use reads the screen through the Windows UI Automation API and asks an
LLM what to click, type or scroll. This wrapper does the plumbing: it turns
the agent into MCP tools, keeps the protocol stream clean, and survives
providers that run out of credits mid-task.

> This is an **unofficial** companion project. The agent itself lives in
> [`Jeomon/Windows-Use`](https://github.com/Jeomon/Windows-Use) (MIT) and all
> credit for it goes there.

---

## ✨ What you get

| Tool | What it does |
| --- | --- |
| `windows_use_task` | Run a natural-language GUI task (click, type, scroll, launch apps, run PowerShell) and get back the agent's answer plus a transcript of every step |
| `windows_desktop_state` | Read-only list of open windows and the active window (fast, no LLM) |
| `windows_accessibility_tree` | Read-only UI Automation tree of the active window as text |
| `windows_screenshot` | PNG capture of all monitors |
| `windows_use_status` | Version info and the resolved LLM configuration (the API key is never printed) |

---

## 📦 Install

```powershell
uv sync          # or: pip install -e .
```

You also need an LLM. Any OpenAI-compatible endpoint works.

```powershell
$env:WINDOWS_USE_PROVIDER = "openai"
$env:WINDOWS_USE_MODEL    = "gpt-4o"
$env:WINDOWS_USE_API_KEY  = "sk-..."
$env:WINDOWS_USE_BASE_URL = "https://api.openai.com/v1"
```

If you have run the `windows-use` CLI before, the server picks up
`~/.windows-use/config.json` automatically and you do not need any of the
above.

### 🔁 Fallback chain

`WINDOWS_USE_FALLBACKS` takes a JSON array of extra provider configurations.
They are tried in order when the primary provider cannot serve a request —
quota exhausted, rate limited, network error:

```json
[
  {"name": "backup", "provider": "openai", "model": "gpt-4o-mini",
   "api_key": "sk-...", "base_url": "https://api.openai.com/v1"}
]
```

The server only moves on to the next entry when the run never managed a
single successful tool call, so a genuine agent failure is not retried
elsewhere.

---

## 🔌 Register it with your client

**OpenCode** — `~/.config/opencode/opencode.jsonc`:

```jsonc
{
  "mcp": {
    "windows-use": {
      "type": "local",
      "command": ["python", "-m", "windows_use_mcp"],
      "environment": {
        "WINDOWS_USE_PROVIDER": "openai",
        "WINDOWS_USE_MODEL": "gpt-4o",
        "WINDOWS_USE_API_KEY": "sk-..."
      }
    }
  }
}
```

**Claude Desktop** — `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "windows-use": {
      "command": "python",
      "args": ["-m", "windows_use_mcp"],
      "env": {
        "WINDOWS_USE_PROVIDER": "openai",
        "WINDOWS_USE_MODEL": "gpt-4o",
        "WINDOWS_USE_API_KEY": "sk-..."
      }
    }
  }
}
```

---

## 🛠️ Why this wrapper exists

`windows-use` is a great agent, but dropping it into an MCP server needs a
few things that the package does not do on its own:

- **A clean stdout.** The upstream UI Automation watchdog prints callback
  exceptions straight to stdout, which lands in the middle of the JSON-RPC
  stream and kills the connection. This server redirects `print()` to stderr.
  (Fixed upstream in
  [PR #9](https://github.com/Jeomon/Windows-Use/pull/9).)
- **A fast handshake.** Importing `windows_use` pulls in every LLM SDK the
  package supports — around 15 seconds. The import is lazy here, so the
  server answers `initialize` in under two seconds.
- **A stdio transport that clients can actually talk to.** The MCP Python SDK
  2.x "claims" fd 0/1 on Windows and rebinds the standard handles. That
  breaks clients that spawn the server over pipes (Bun based clients, for
  instance). This server uses a plain file descriptor transport instead.
- **One COM apartment.** UI Automation objects belong to the thread that
  created them, so every GUI call here runs on a single dedicated thread.
- **A provider fallback chain**, because a GUI task that dies halfway through
  because of a quota error is a bad afternoon.

---

## ⚙️ Environment variables

| Variable | Purpose |
| --- | --- |
| `WINDOWS_USE_PROVIDER` | Provider key, e.g. `openai`, `anthropic`, `google`, `ollama` |
| `WINDOWS_USE_MODEL` | Model id |
| `WINDOWS_USE_API_KEY` | API key for hosted providers |
| `WINDOWS_USE_BASE_URL` | Base URL for any OpenAI-compatible endpoint |
| `WINDOWS_USE_FALLBACKS` | JSON array of fallback provider configurations |
| `WINDOWS_USE_LOG_LEVEL` | Log level for the upstream package (default `WARNING`) |
| `ANONYMIZED_TELEMETRY` | Set to `false` to disable the upstream PostHog telemetry |

---

## 🔒 Security

Windows-Use has **no sandbox**. It moves the real mouse and keyboard of the
logged-in user, can run PowerShell and can modify files. Treat an MCP client
with this server installed as a remote control for that desktop session.

Run it on a machine you are willing to let an agent operate, or inside a VM
or Windows Sandbox. The upstream
[security policy](https://github.com/Jeomon/Windows-Use/blob/main/SECURITY.md)
is worth reading before you point it at anything important.

---

## 🧪 Development

```powershell
uv sync
uv run python -m windows_use_mcp
```

To develop against a local checkout of the agent:

```powershell
uv pip install -e ../Windows-Use
```

## 📄 License

MIT — see [LICENSE](LICENSE). The `windows-use` package it wraps is MIT
licensed by Jeomon George.
