# Ops MCP Server

A self-hosted [MCP](https://modelcontextprotocol.io) server that gives an AI
assistant (Claude Code, or any MCP-compatible client) live diagnostic and
deployment access to a Windows or Linux machine over HTTP: shell/PowerShell
commands, file exploration, git and Django operations, process/environment
info, and IIS (Windows) or systemd (Linux) service control.

Two parallel implementations, same tool set otherwise:

| | Server | Installer |
|---|---|---|
| Windows | `server_new.py` | `install_service.ps1` (NSSM service, or a scheduled task if NSSM isn't installed) |
| Linux | `server_linux.py` | `install_linux.sh` (systemd service, auto-creates its own venv) |

Both installers are **self-healing** — safe to re-run any time on an
already-installed machine. Re-running one:
- kills a stuck process still holding the port,
- syntax-checks the server script before touching the service (catches a
  corrupted/interrupted deploy with a clear error instead of installing
  something that crash-loops),
- re-registers the service/task with current settings,
- picks up a changed API key without a reboot,
- and at the end prints the **current API key**, the **reachable LAN IPs**,
  and the **live tool list** (queried from the server itself, not a
  hardcoded doc) — so re-running with no arguments is also how you find out
  what's already configured.

A second, independent server lives in [`desktop/`](desktop/): a **desktop
"computer use" server for Windows** (screenshots, UI Automation, clicks and
typing on the interactive desktop). See
[Desktop computer-use server](#desktop-computer-use-server-windows) below.

## Quick start

### Windows

```powershell
# Copy this folder to a local disk first (e.g. C:\ops-mcp-server) - do not
# run it straight from a mapped network drive, see Troubleshooting below.
powershell -ExecutionPolicy Bypass -File install_service.ps1 -Port 8001 -ApiKey "a-long-random-key"
```

### Linux

```bash
sudo ./install_linux.sh --port 8001 --api-key "a-long-random-key"
```

Omit `-ApiKey`/`--api-key` and the installer will reuse whatever key is
already configured, or generate a new one and print it.

### Connect from Claude Code

Add to `.mcp.json`:

```json
{
  "mcpServers": {
    "ops-mcp-remote": {
      "type": "streamable-http",
      "url": "http://<machine-ip>:8001/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_API_KEY"
      }
    }
  }
}
```

## Available tools

**Git** — `git_status`, `git_pull`, `git_log`, `git_branch`

**Django** — `django_collectstatic`, `django_migrate`, `django_check`, `django_manage`

**Web service** — Windows: `iis_status`, `iis_restart`, `iis_stop`, `iis_start`
· Linux: `service_status`, `service_restart`, `service_stop`, `service_start`
(systemd unit name set via `WEB_SERVICE_NAME`)

**Deployment** — `pip_install`, `full_deploy` (git pull + pip install +
collectstatic + migrate + restart web service)

**File system** — `ls`, `cat`, `head`, `tail`, `find`, `grep`, `pwd`, `cd`, `file_info`

**System** — `server_info`, `env`, `ps`, `shell`

Django and git tools are optional conveniences — the file system and system
tools work standalone on any machine, with no Django project required.

## Security

**This server has no authentication unless you configure an API key** — do
that before exposing it beyond an isolated lab machine. `shell` is
unrestricted command execution; treat the API key like a root password.

- Both installers generate/print a key when none is configured. Pass one
  explicitly with `-ApiKey`/`--api-key`, or set it later:
  - Windows: `[Environment]::SetEnvironmentVariable("MCP_API_KEY", "...", "Machine")`, then re-run the installer so the service picks it up.
  - Linux: edit `MCP_API_KEY=` in `/etc/ops-mcp/mcp.env`, then `systemctl restart ops-mcp`.
- The installers open the firewall for the port you choose (`New-NetFirewallRule` / `ufw`/`firewalld`) — nothing else.
- `shell()`/`django_manage()` block an explicit list of destructive command
  patterns (`rm -rf /`, `shutdown`, `flush`, ...) as a safety net, **not** a
  security boundary — anyone with the API key can still run arbitrary code.
  Scope network access accordingly (VPN/LAN-only, not the open internet).

## Configuration

Environment variables:

| Variable | Default | Description |
|---|---|---|
| `MCP_HOST` | `0.0.0.0` | Host to bind to |
| `MCP_PORT` | `8001` | Port to listen on |
| `MCP_API_KEY` | *(empty)* | Bearer token for authentication |
| `IIS_APP_POOL` | *(empty)* | Windows only — required for `iis_*` tools |
| `IIS_SITE_NAME` | *(empty)* | Windows only — required for `iis_*` tools |
| `WEB_SERVICE_NAME` | *(empty)* | Linux only — systemd unit for `service_*` tools |

## Troubleshooting

**"Impossible de charger... l'execution de scripts est desactivee" /
"running scripts is disabled on this system"** (PSSecurityException) when
running `install_service.ps1`

This is Windows' default PowerShell execution policy (`Restricted`), not a
problem with the installer:

```powershell
powershell -ExecutionPolicy Bypass -File install_service.ps1
```

**Service installs but never starts, right after installing from a mapped
drive or `\\server\share` path**

The Windows service (or scheduled task) runs as `SYSTEM`, which cannot see
drives mapped in *your* logged-in session (`N:\`, `W:\`, ...) or shares that
need your user credentials. Copy this folder to a local path first (e.g.
`C:\ops-mcp-server`) and run the installer from there. (Linux has no
equivalent problem — `systemd` services can read whatever's mounted at the
OS level, e.g. NFS/CIFS in `/etc/fstab`.)

**"Python est introuvable..." / pip not recognized, even though `python`
seemingly "runs"**

That's Windows' App Execution Alias stub in `WindowsApps`, not a real
Python — it only opens the Microsoft Store. `install_service.ps1` looks for
a real interpreter on disk (ignoring `PATH`) and silently installs one from
python.org if none is found; just re-run it. This only bites you if you run
`start_server.bat` directly without having run the installer first, or if
the machine has no internet access for the download.

**Server won't start**

- Check if the port is already in use: `netstat -an | findstr 8001` (Windows) / `ss -ltnp | grep 8001` (Linux)
- Check the firewall isn't blocking it
- Windows: `nssm status OpsMCP` or `Get-ScheduledTaskInfo -TaskName OpsMCP`
- Linux: `systemctl status ops-mcp` and `journalctl -u ops-mcp -n 50 --no-pager`

**IIS/systemd commands fail**

- Windows: run the MCP server as Administrator, or grant the account IIS management rights
- Linux: the systemd unit runs as `root` by default (edit `install_linux.sh`'s generated unit if you want a lower-privilege user)

**Git commands fail**

- Ensure `git` is on `PATH`, or set the full path in `Config.GIT_PATH`

## Desktop computer-use server (Windows)

`desktop/desktop_server.py` is a separate MCP server that gives an AI
assistant control of **this PC's interactive desktop**: multi-monitor
screenshots (with change detection), the UI Automation / Win32 control tree,
reading values, clicking, typing, dragging, scrolling and moving windows,
designed so you can **watch it work without your mouse and its actions
fighting each other**.

It is not part of `server_new.py` because that one runs as a service
(session 0), which has no access to the interactive desktop. The desktop
server runs as a **scheduled task at logon, inside your own session**.

### Install (normal PowerShell, no admin needed)

```powershell
powershell -ExecutionPolicy Bypass -File install_service.ps1 -Desktop
```

The installer (self-healing, safe to re-run):
- creates/repairs `desktop\.venv` (needs Python >= 3.11 already installed;
  nothing is installed system-wide) and installs `requirements-desktop.txt`;
- compiles the server and runs its unit tests before touching anything;
- generates (or keeps) an API key in your **user** environment variable
  `DESKTOP_MCP_API_KEY`;
- registers the scheduled task `Desktop-MCP` (at logon, your user, no stored
  password), starts it and checks `/health`;
- prints the key, the port, the live tool list and the command to register it
  in Claude Code (it does not run it):

```powershell
claude mcp add-json --scope project desktop-mcp '{"type":"http","url":"http://127.0.0.1:8011/mcp","headersHelper":"powershell -NoProfile -ExecutionPolicy Bypass -File C:/path/to/ops-mcp-server/desktop/mcp_headers.ps1"}'
```

`headersHelper` runs `desktop/mcp_headers.ps1` on every connection: it reads
the key from the user registry, so the key is never written into `.mcp.json`
and a Claude session started before the variable existed still gets it
(a `${DESKTOP_MCP_API_KEY}` header would expand to empty, giving HTTP 401).

Options: `-DesktopPort 8011` (default), `-DesktopListenLan` (listen on the
LAN: needs an elevated shell for the firewall rule, and the server refuses to
listen outside 127.0.0.1 without an API key), `-DesktopRemove` (unregister).
By default it only listens on **127.0.0.1**.

### One cursor, three mechanisms

Windows has a single cursor per session, so every action uses the first
mechanism that works and **reports which one** (`mechanism`) and whether the
real cursor moved (`cursor_moved`):

1. **UI Automation** (Invoke, Value, SelectionItem, Toggle, ExpandCollapse,
   Text, LegacyIAccessible): no cursor movement, no focus stealing;
2. **Win32 messages** to the window (BM_CLICK, WM_SETTEXT, EM_REPLACESEL,
   WM_CHAR, WM_KEYDOWN/UP, CB_SETCURSEL, client-coordinate mouse messages):
   no cursor movement;
3. **SendInput** (drawing, dragging, apps that ignore 1 and 2): moves the
   real cursor, which is saved and restored immediately.

Your mouse is never blocked by default. Optionally
(`set_option(freeze_user_mouse=true)`, or `input.freeze_user_mouse_during_gestures`
in the config) your physical mouse is frozen only during a SendInput gesture.

### How to stop it

- The on-screen **Stop** button (top centre of the active monitor; only a
  physical click counts, and it never appears in screenshots);
- a message to the assistant in the chat;
- the hotkey **Ctrl+Alt+Shift+F12** (`stop_hotkey`);
- the `pause()` / `resume()` / `status()` tools.

Moving your mouse or typing does **not** stop or pause the assistant.

### What you see

An orange "Claude" ghost cursor animates to each target before acting, and a
soft border marks the monitor where it is working (grey while paused). All
overlay windows are click-through, never take focus and are excluded from
every screen capture (`WDA_EXCLUDEFROMCAPTURE`). Colours and sizes: the
`screen_border` tool or the `border` / `ghost` sections of the config.

### Safety and configuration

- **Password fields are never typed into or read.**
- `deny_processes` (password managers, UAC, lock screen...) and
  `deny_window_title_regex` are never controlled, not even for reading;
  add your own (e.g. a browser window with a banking session).
- `allow_write_processes`: `"*"` = any app not denied; list exe names to make
  everything else read-only.
- Every action is appended to an audit log
  (`%LOCALAPPDATA%\desktop-mcp\audit.jsonl`).
- Defaults are in `desktop/config.json`; put your own overrides in
  `%LOCALAPPDATA%\desktop-mcp\config.json` (same keys, merged). No secrets in
  either file.

## License

MIT
