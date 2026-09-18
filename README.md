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

## License

MIT
