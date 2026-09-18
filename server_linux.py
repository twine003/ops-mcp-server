"""
MCP Server for Ops MCP Remote Management - Linux Version
===========================================================

Linux port of server_new.py (the Windows version). Same FastMCP /
Streamable HTTP architecture and the same tool set, minus the
Windows-only tools (IIS, PowerShell-based ps/shell) which are replaced
with POSIX/systemd equivalents.

Usage:
    python3 server_linux.py [--port 8001] [--host 0.0.0.0]

Endpoints:
    /mcp    - Standard MCP Streamable HTTP endpoint
    /health - Health check
"""

import os
import sys
import json
import subprocess
import logging
from datetime import datetime
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
os.chdir(PROJECT_ROOT)

# Load environment variables
try:
    from dotenv import load_dotenv
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=True)
except ImportError:
    pass

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("mcp-server")


# =============================================================================
# Configuration
# =============================================================================

class Config:
    HOST = os.getenv("MCP_HOST", "0.0.0.0")
    PORT = int(os.getenv("MCP_PORT", "8001"))
    API_KEY = os.getenv("MCP_API_KEY", "")
    PROJECT_PATH = PROJECT_ROOT
    GIT_PATH = "git"
    # Name of the systemd unit that fronts the web app (nginx, gunicorn,
    # uwsgi, ...) - restarted by service_restart()/full_deploy(). Empty by
    # default so nothing gets restarted unless explicitly configured.
    WEB_SERVICE_NAME = os.getenv("WEB_SERVICE_NAME", "")


# =============================================================================
# Helper Functions
# =============================================================================

def run_command(cmd: list[str], cwd: str = None, timeout: int = 60) -> dict:
    """Execute a command (argv list, no shell) and return result."""
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd or str(Config.PROJECT_PATH),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Command timed out after {timeout}s"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def run_shell(command: str, timeout: int = 60) -> dict:
    """Execute a raw shell command string (pipes/redirects allowed) via bash."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            executable="/bin/bash",
            cwd=str(Config.PROJECT_PATH),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Command timed out after {timeout}s"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def safe_path(path: str) -> Path:
    """Validate and resolve path within allowed directories."""
    resolved = Path(path).resolve()
    allowed_roots = [Config.PROJECT_PATH.resolve(), Path("/")]

    for root in allowed_roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue

    return (Config.PROJECT_PATH / path).resolve()


# =============================================================================
# FastMCP Server Setup
# =============================================================================

# Newer fastmcp releases removed the stateless_http kwarg from FastMCP()
# and read it from this env var instead (must be set before FastMCP() runs).
os.environ.setdefault("FASTMCP_STATELESS_HTTP", "true")

try:
    from fastmcp import FastMCP
    FASTMCP_AVAILABLE = True
except ImportError:
    FASTMCP_AVAILABLE = False
    logger.warning("FastMCP not installed. Run: pip install fastmcp")

# fastmcp 4.x renamed/moved this class (from fastmcp.server.auth.providers.bearer
# .StaticBearerAuthProvider to fastmcp.server.auth.providers.jwt.StaticTokenVerifier,
# same tokens={...} signature). Try the current path first, fall back to the old
# one for older fastmcp installs. If neither import works, auth is silently
# disabled below (logged) rather than crashing the server.
StaticBearerAuthProvider = None
if FASTMCP_AVAILABLE:
    try:
        from fastmcp.server.auth.providers.jwt import StaticTokenVerifier as StaticBearerAuthProvider
    except ImportError:
        try:
            from fastmcp.server.auth.providers.bearer import StaticBearerAuthProvider
        except ImportError:
            StaticBearerAuthProvider = None

if FASTMCP_AVAILABLE:
    # Setup Bearer Token Authentication
    auth_provider = None
    if Config.API_KEY and StaticBearerAuthProvider:
        auth_provider = StaticBearerAuthProvider(
            tokens={
                Config.API_KEY: {
                    "client_id": "claude-code",
                    "scopes": ["admin"]
                }
            }
        )
        logger.info("Bearer token authentication enabled")
    else:
        logger.warning("No API_KEY configured or auth not available - server running without authentication")

    # Create FastMCP server (stateless HTTP set via FASTMCP_STATELESS_HTTP above)
    mcp = FastMCP("ops-mcp", auth=auth_provider)

    # ==========================================================================
    # Git Tools
    # ==========================================================================

    @mcp.tool()
    def git_status() -> str:
        """Get git status of the project (shows modified/untracked files)."""
        result = run_command([Config.GIT_PATH, "status", "--porcelain"])
        if result["success"]:
            status = result["stdout"].strip()
            if not status:
                return "Working directory clean - no changes"
            return f"Changes detected:\n{status}"
        return f"Error: {result.get('stderr') or result.get('error')}"

    def _git_pull() -> str:
        result = run_command([Config.GIT_PATH, "pull", "--ff-only"])
        if result["success"]:
            return f"Pull successful:\n{result['stdout']}"
        return f"Pull failed:\n{result.get('stderr') or result.get('error')}"

    @mcp.tool()
    def git_pull() -> str:
        """Pull latest changes from the remote repository (fast-forward only)."""
        return _git_pull()

    @mcp.tool()
    def git_log(count: int = 5) -> str:
        """Get recent git commit history."""
        result = run_command([Config.GIT_PATH, "log", "--oneline", f"-{min(count, 20)}"])
        if result["success"]:
            return f"Recent commits:\n{result['stdout']}"
        return f"Error: {result.get('stderr') or result.get('error')}"

    @mcp.tool()
    def git_branch() -> str:
        """Get current git branch name."""
        result = run_command([Config.GIT_PATH, "branch", "--show-current"])
        if result["success"]:
            return f"Current branch: {result['stdout'].strip()}"
        return f"Error: {result.get('stderr') or result.get('error')}"

    # ==========================================================================
    # Web Service Management (systemd) - Linux equivalent of the IIS tools
    # ==========================================================================

    def _service_restart() -> str:
        if not Config.WEB_SERVICE_NAME:
            return "Error: WEB_SERVICE_NAME not configured - nothing to restart."
        result = run_command(["systemctl", "restart", Config.WEB_SERVICE_NAME])
        if result["success"]:
            return f"Service '{Config.WEB_SERVICE_NAME}' restarted successfully"
        return f"Failed to restart '{Config.WEB_SERVICE_NAME}': {result.get('stderr') or result.get('error')}"

    @mcp.tool()
    def service_restart() -> str:
        """Restart the configured web service (systemctl restart $WEB_SERVICE_NAME)."""
        return _service_restart()

    @mcp.tool()
    def service_stop() -> str:
        """Stop the configured web service."""
        if not Config.WEB_SERVICE_NAME:
            return "Error: WEB_SERVICE_NAME not configured."
        result = run_command(["systemctl", "stop", Config.WEB_SERVICE_NAME])
        if result["success"]:
            return f"Service '{Config.WEB_SERVICE_NAME}' stopped"
        return f"Failed: {result.get('stderr') or result.get('error')}"

    @mcp.tool()
    def service_start() -> str:
        """Start the configured web service."""
        if not Config.WEB_SERVICE_NAME:
            return "Error: WEB_SERVICE_NAME not configured."
        result = run_command(["systemctl", "start", Config.WEB_SERVICE_NAME])
        if result["success"]:
            return f"Service '{Config.WEB_SERVICE_NAME}' started"
        return f"Failed: {result.get('stderr') or result.get('error')}"

    @mcp.tool()
    def service_status(name: str = "") -> str:
        """Get systemd status of the configured web service, or any unit name given."""
        unit = name or Config.WEB_SERVICE_NAME
        if not unit:
            return "Error: no service name given and WEB_SERVICE_NAME not configured."
        result = run_command(["systemctl", "status", unit, "--no-pager"])
        return result.get("stdout") or result.get("stderr") or result.get("error") or "(no output)"

    # ==========================================================================
    # Django Management Tools
    # ==========================================================================

    def _django_collectstatic() -> str:
        result = run_command([sys.executable, "manage.py", "collectstatic", "--noinput"])
        if result["success"]:
            return f"Collectstatic completed:\n{result['stdout'][-500:]}"
        return f"Failed: {result.get('stderr') or result.get('error')}"

    @mcp.tool()
    def django_collectstatic() -> str:
        """Run Django collectstatic to gather static files."""
        return _django_collectstatic()

    def _django_migrate() -> str:
        result = run_command([sys.executable, "manage.py", "migrate", "--noinput"])
        if result["success"]:
            return f"Migrations applied:\n{result['stdout']}"
        return f"Failed: {result.get('stderr') or result.get('error')}"

    @mcp.tool()
    def django_migrate() -> str:
        """Run Django database migrations."""
        return _django_migrate()

    @mcp.tool()
    def django_check() -> str:
        """Run Django system check for configuration issues."""
        result = run_command([sys.executable, "manage.py", "check"])
        if result["success"]:
            return f"System check passed:\n{result['stdout']}"
        return f"Issues found:\n{result.get('stderr') or result.get('stdout')}"

    @mcp.tool()
    def django_manage(command: str) -> str:
        """Run a Django management command (e.g., 'showmigrations', 'check')."""
        dangerous = ['flush', 'reset', 'drop', 'delete', 'remove']
        if any(d in command.lower() for d in dangerous):
            return f"Error: Potentially dangerous command blocked: {command}"

        result = run_command([sys.executable, "manage.py"] + command.split(), timeout=120)
        if result["success"]:
            return f"Django command: {command}\n{'=' * 60}\n{result['stdout']}"
        return f"Django command failed: {command}\n{result.get('stderr') or result.get('error')}"

    # ==========================================================================
    # Deployment Tools
    # ==========================================================================

    def _pip_install() -> str:
        result = run_command([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], timeout=300)
        if result["success"]:
            lines = result['stdout'].strip().split('\n')
            if len(lines) > 10:
                return f"Requirements installed. Last lines:\n" + '\n'.join(lines[-10:])
            return f"Requirements installed:\n{result['stdout']}"
        return f"Failed: {result.get('stderr') or result.get('error')}"

    @mcp.tool()
    def pip_install() -> str:
        """Install Python dependencies from requirements.txt."""
        return _pip_install()

    @mcp.tool()
    def server_info() -> str:
        """Get server information (Python version, disk space, etc.)."""
        import shutil
        info = {
            "timestamp": datetime.now().isoformat(),
            "project_path": str(Config.PROJECT_PATH),
            "python_version": sys.version,
            "platform": sys.platform,
        }
        try:
            usage = shutil.disk_usage(Config.PROJECT_PATH)
            info["disk_free_gb"] = round(usage.free / (1024**3), 2)
        except Exception:
            pass
        return json.dumps(info, indent=2)

    @mcp.tool()
    def full_deploy() -> str:
        """Full deployment: git pull + pip install + collectstatic + migrate + restart web service."""
        results = []
        results.append("=== Git Pull ===")
        results.append(_git_pull())
        results.append("\n=== Install Requirements ===")
        results.append(_pip_install())
        results.append("\n=== Collectstatic ===")
        results.append(_django_collectstatic())
        results.append("\n=== Migrate ===")
        results.append(_django_migrate())
        results.append("\n=== Restart Web Service ===")
        results.append(_service_restart())
        return '\n'.join(results)

    # ==========================================================================
    # File System Tools
    # ==========================================================================

    @mcp.tool()
    def ls(path: str = ".", pattern: str = "*") -> str:
        """List directory contents (like ls)."""
        try:
            target = safe_path(path)
            if not target.exists():
                return f"Error: Path does not exist: {target}"
            if not target.is_dir():
                return f"Error: Not a directory: {target}"

            items = []
            for item in sorted(target.glob(pattern)):
                try:
                    stat = item.stat()
                    size = stat.st_size
                    mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                    item_type = "DIR " if item.is_dir() else "FILE"
                    size_str = f"{size:>10}" if item.is_file() else "          "
                    items.append(f"{item_type} {size_str} {mtime} {item.name}")
                except Exception:
                    items.append(f"???? {'':>10} {'':16} {item.name} (access denied)")

            header = f"Directory: {target}\n{'=' * 60}\n"
            return header + ("\n".join(items) if items else "(empty)")
        except Exception as e:
            return f"Error: {str(e)}"

    @mcp.tool()
    def cat(path: str, lines: int = 0, offset: int = 0) -> str:
        """Read and display file contents."""
        try:
            target = safe_path(path)
            if not target.exists():
                return f"Error: File does not exist: {target}"
            if not target.is_file():
                return f"Error: Not a file: {target}"

            size = target.stat().st_size
            if size > 1024 * 1024:
                return f"Error: File too large ({size} bytes). Use lines/offset parameters."

            with open(target, 'r', encoding='utf-8', errors='replace') as f:
                if lines > 0:
                    all_lines = f.readlines()
                    selected = all_lines[offset:offset + lines]
                    content = ''.join(selected)
                    return f"File: {target} (lines {offset+1}-{offset+len(selected)} of {len(all_lines)})\n{'=' * 60}\n{content}"
                else:
                    content = f.read()
                    return f"File: {target} ({size} bytes)\n{'=' * 60}\n{content}"
        except Exception as e:
            return f"Error: {str(e)}"

    @mcp.tool()
    def head(path: str, lines: int = 20) -> str:
        """Show first N lines of a file."""
        try:
            target = safe_path(path)
            if not target.exists():
                return f"Error: File does not exist: {target}"
            with open(target, 'r', encoding='utf-8', errors='replace') as f:
                head_lines = [f.readline() for _ in range(lines)]
                head_lines = [l for l in head_lines if l]
            return f"First {len(head_lines)} lines of {target}:\n{'=' * 60}\n{''.join(head_lines)}"
        except Exception as e:
            return f"Error: {str(e)}"

    @mcp.tool()
    def tail(path: str, lines: int = 20) -> str:
        """Show last N lines of a file."""
        try:
            target = safe_path(path)
            if not target.exists():
                return f"Error: File does not exist: {target}"
            with open(target, 'r', encoding='utf-8', errors='replace') as f:
                all_lines = f.readlines()
                tail_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
            return f"Last {len(tail_lines)} lines of {target}:\n{'=' * 60}\n{''.join(tail_lines)}"
        except Exception as e:
            return f"Error: {str(e)}"

    @mcp.tool()
    def find(pattern: str, path: str = ".", max_results: int = 50) -> str:
        """Find files matching a pattern recursively."""
        try:
            target = safe_path(path)
            if not target.exists():
                return f"Error: Path does not exist: {target}"

            matches = []
            for item in target.rglob(pattern):
                matches.append(str(item.relative_to(target)))
                if len(matches) >= max_results:
                    break

            if not matches:
                return f"No files matching '{pattern}' found in {target}"

            result = f"Files matching '{pattern}' in {target}:\n{'=' * 60}\n" + "\n".join(matches)
            if len(matches) >= max_results:
                result += f"\n\n(truncated at {max_results} results)"
            return result
        except Exception as e:
            return f"Error: {str(e)}"

    @mcp.tool()
    def grep(pattern: str, path: str = ".", file_pattern: str = "*.py", max_results: int = 30) -> str:
        """Search for text pattern in files (regex supported)."""
        import re
        try:
            target = safe_path(path)
            if not target.exists():
                return f"Error: Path does not exist: {target}"

            regex = re.compile(pattern, re.IGNORECASE)
            matches = []
            files_searched = 0

            for file_path in target.rglob(file_pattern):
                if not file_path.is_file():
                    continue
                files_searched += 1
                try:
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        for line_num, line in enumerate(f, 1):
                            if regex.search(line):
                                rel_path = file_path.relative_to(target)
                                matches.append(f"{rel_path}:{line_num}: {line.strip()[:100]}")
                                if len(matches) >= max_results:
                                    break
                    if len(matches) >= max_results:
                        break
                except Exception:
                    continue

            if not matches:
                return f"No matches for '{pattern}' in {file_pattern} files ({files_searched} files searched)"

            result = f"Matches for '{pattern}' in {file_pattern} ({files_searched} files searched):\n{'=' * 60}\n" + "\n".join(matches)
            if len(matches) >= max_results:
                result += f"\n\n(truncated at {max_results} results)"
            return result
        except re.error as e:
            return f"Invalid regex pattern: {e}"
        except Exception as e:
            return f"Error: {str(e)}"

    @mcp.tool()
    def pwd() -> str:
        """Print current working directory."""
        return f"Current directory: {os.getcwd()}\nProject root: {Config.PROJECT_PATH}"

    @mcp.tool()
    def cd(path: str) -> str:
        """Change current working directory."""
        try:
            target = safe_path(path)
            if not target.exists():
                return f"Error: Directory does not exist: {target}"
            if not target.is_dir():
                return f"Error: Not a directory: {target}"
            os.chdir(target)
            return f"Changed to: {os.getcwd()}"
        except Exception as e:
            return f"Error: {str(e)}"

    @mcp.tool()
    def file_info(path: str) -> str:
        """Get detailed info about a file or directory (size, dates, etc.)."""
        try:
            target = safe_path(path)
            if not target.exists():
                return f"Error: Path does not exist: {target}"

            stat = target.stat()
            info = []
            info.append(f"Path: {target}")
            info.append(f"Type: {'Directory' if target.is_dir() else 'File'}")
            info.append(f"Size: {stat.st_size:,} bytes")
            info.append(f"Created: {datetime.fromtimestamp(stat.st_ctime)}")
            info.append(f"Modified: {datetime.fromtimestamp(stat.st_mtime)}")
            info.append(f"Mode: {oct(stat.st_mode)}")
            info.append(f"Owner UID/GID: {stat.st_uid}/{stat.st_gid}")

            if target.is_dir():
                files = sum(1 for _ in target.iterdir() if _.is_file())
                dirs = sum(1 for _ in target.iterdir() if _.is_dir())
                info.append(f"Contents: {files} files, {dirs} directories")

            return "\n".join(info)
        except Exception as e:
            return f"Error: {str(e)}"

    # ==========================================================================
    # System Tools
    # ==========================================================================

    @mcp.tool()
    def env() -> str:
        """Get detailed environment info (system, Python, disk, env vars)."""
        import platform
        import shutil

        info = []
        info.append("=== System Information ===")
        info.append(f"Hostname: {platform.node()}")
        info.append(f"Platform: {platform.platform()}")
        info.append(f"Architecture: {platform.machine()}")

        distro = run_command(["cat", "/etc/os-release"])
        if distro["success"]:
            for line in distro["stdout"].splitlines():
                if line.startswith("PRETTY_NAME="):
                    distro_name = line.split('=', 1)[1].strip('"')
                    info.append(f"Distro: {distro_name}")
                    break

        info.append("\n=== Python Environment ===")
        info.append(f"Python Version: {sys.version}")
        info.append(f"Python Executable: {sys.executable}")

        info.append("\n=== Project Paths ===")
        info.append(f"Project Root: {Config.PROJECT_PATH}")
        info.append(f"Current Directory: {os.getcwd()}")

        info.append("\n=== Disk Usage ===")
        try:
            usage = shutil.disk_usage("/")
            total_gb = usage.total / (1024**3)
            free_gb = usage.free / (1024**3)
            info.append(f"/ Total: {total_gb:.1f}GB, Free: {free_gb:.1f}GB")
        except Exception:
            pass

        return "\n".join(info)

    @mcp.tool()
    def ps(filter_name: str = "") -> str:
        """List running processes (optionally filtered by name)."""
        result = run_command(["ps", "aux"], timeout=30)
        if not result["success"]:
            return f"Error: {result.get('stderr') or result.get('error')}"
        lines = result["stdout"].splitlines()
        if filter_name:
            header = lines[0] if lines else ""
            filtered = [l for l in lines[1:] if filter_name.lower() in l.lower()]
            lines = [header] + filtered
        return f"Running processes{' (filtered: ' + filter_name + ')' if filter_name else ''}:\n" + "\n".join(lines)

    @mcp.tool()
    def shell(command: str) -> str:
        """Execute a shell command (with safety restrictions)."""
        blocked = [
            'rm -rf /', 'mkfs', 'dd if=', ':(){ :|:& };:', 'shutdown', 'reboot',
            'poweroff', 'halt', '> /dev/sd', '> /dev/nvme', 'chmod -r 000 /',
        ]

        cmd_lower = command.lower()
        for blocked_cmd in blocked:
            if blocked_cmd in cmd_lower:
                return f"Error: Command contains blocked operation: {blocked_cmd}"

        result = run_shell(command, timeout=60)

        output = []
        if result.get("stdout"):
            output.append(result["stdout"])
        if result.get("stderr"):
            output.append(f"STDERR:\n{result['stderr']}")

        status = "Success" if result.get("success") else f"Failed (code {result.get('returncode', '?')})"
        return f"Command: {command}\nStatus: {status}\n{'=' * 60}\n" + "\n".join(output)


# =============================================================================
# Custom Routes (Health Check)
# =============================================================================

if FASTMCP_AVAILABLE:
    from starlette.responses import JSONResponse

    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(request):
        """Health check endpoint."""
        return JSONResponse({
            "status": "healthy",
            "server": "ops-mcp",
            "version": "2.0.0",
            "transport": "streamable-http",
            "timestamp": datetime.now().isoformat()
        })

    @mcp.custom_route("/", methods=["GET"])
    async def root(request):
        """Root endpoint with server info."""
        return JSONResponse({
            "name": "Ops MCP Server",
            "version": "2.0.0",
            "transport": "streamable-http",
            "endpoints": {
                "mcp": "/mcp",
                "health": "/health"
            },
            "documentation": "Connect using any MCP-compatible client to /mcp endpoint"
        })


# =============================================================================
# Self-Watchdog
# =============================================================================

def _start_self_watchdog(port: int) -> None:
    """Internal safeguard against 'process alive, listener dead'.

    Ported from the Windows version, which hit an OSError in asyncio's
    accept loop after a network blip: the process stayed alive but stopped
    accepting new TCP connections - invisible to a process supervisor since
    the process never exits. This thread GETs /health over loopback every
    60s. Any HTTP response (even 401/500) counts as alive - what's detected
    is a dead socket, not application errors. After 3 consecutive connection
    failures it exits with os._exit(3) so systemd (Restart=always) relaunches
    it cleanly.
    """
    import threading
    import time
    import urllib.request
    import urllib.error

    interval_s = 60
    max_failures = 3
    url = f"http://127.0.0.1:{port}/health"

    def _watch():
        consecutive = 0
        time.sleep(30)  # startup grace period
        while True:
            alive = False
            try:
                with urllib.request.urlopen(url, timeout=10):
                    alive = True
            except urllib.error.HTTPError:
                alive = True  # got an HTTP response -> listener is alive
            except Exception as e:
                logger.warning(
                    "Self-watchdog: /health unreachable (%d/%d): %s",
                    consecutive + 1, max_failures, e,
                )
            consecutive = 0 if alive else consecutive + 1
            if consecutive >= max_failures:
                logger.critical(
                    "Self-watchdog: HTTP listener dead after %d checks. "
                    "Exiting with code 3 for external restart.",
                    max_failures,
                )
                os._exit(3)
            time.sleep(interval_s)

    threading.Thread(target=_watch, daemon=True, name="self-watchdog").start()
    logger.info("Self-watchdog active: GET %s every %ss (3 failures => restart)", url, interval_s)


# =============================================================================
# Main Entry Point
# =============================================================================

def main():
    """Run the MCP server with Streamable HTTP transport."""
    import argparse

    parser = argparse.ArgumentParser(description="Ops MCP Server (FastMCP, Linux)")
    parser.add_argument("--port", type=int, default=Config.PORT, help="Port to listen on")
    parser.add_argument("--host", default=Config.HOST, help="Host to bind to")
    args = parser.parse_args()

    if not FASTMCP_AVAILABLE:
        print("ERROR: FastMCP not installed.")
        print("Install with: pip3 install fastmcp")
        sys.exit(1)

    Config.PORT = args.port
    Config.HOST = args.host

    auth_status = "ENABLED (Bearer Token)" if Config.API_KEY else "DISABLED (no API_KEY)"

    print(f"""
================================================================
          Ops MCP Server (FastMCP, Linux)
================================================================
  Host: {Config.HOST}
  Port: {Config.PORT}
  Project: {Config.PROJECT_PATH}
  Auth: {auth_status}
----------------------------------------------------------------
  Endpoints:
    - MCP:    http://{Config.HOST}:{Config.PORT}/mcp
    - Health: http://{Config.HOST}:{Config.PORT}/health

  Configure in Claude Code settings.json or .mcp.json:
  {{
    "mcpServers": {{
      "ops-mcp-remote": {{
        "type": "streamable-http",
        "url": "http://<server-ip>:{Config.PORT}/mcp",
        "headers": {{
          "Authorization": "Bearer YOUR_API_KEY"
        }}
      }}
    }}
  }}
================================================================
    """)

    _start_self_watchdog(Config.PORT)

    # Run with Streamable HTTP transport
    mcp.run(
        transport="streamable-http",
        host=Config.HOST,
        port=Config.PORT
    )


if __name__ == "__main__":
    main()
