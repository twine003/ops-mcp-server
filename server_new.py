"""
MCP Server for Ops MCP Remote Management - Streamable HTTP Version
===================================================================

This MCP server uses FastMCP with Streamable HTTP transport, which is the
recommended transport for production deployments and maximum compatibility
with MCP clients like Claude Code.

Usage:
    python server_new.py [--port 8001] [--host 0.0.0.0]

Endpoints:
    /mcp - Standard MCP Streamable HTTP endpoint
    /health - Health check
    /api/tools - List tools (simple API)
    /api/tool - Call tool (simple API)
"""

import os
import sys
import json
import subprocess
import logging
import secrets
from datetime import datetime
from pathlib import Path
from typing import Optional

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
    IIS_APP_POOL = os.getenv("IIS_APP_POOL", "")
    IIS_SITE_NAME = os.getenv("IIS_SITE_NAME", "")


# =============================================================================
# Helper Functions
# =============================================================================

# The server runs as a service in session 0: no one can ever answer a prompt.
# Git / Git Credential Manager must fail fast instead of waiting on an
# invisible login window (hung 33 git processes on 2026-09-23).
NONINTERACTIVE_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
}


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill proc AND its descendants (with shell=True, proc is only cmd.exe)."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        proc.kill()


def run_command(cmd: list[str], cwd: str = None, timeout: int = 60) -> dict:
    """Execute a command and return result.

    Non-interactive (stdin closed, git prompts disabled). On timeout the whole
    process tree is killed: subprocess.run(timeout=) only kills the direct
    child, and a surviving grandchild holding the pipes blocks forever.
    """
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd or str(Config.PROJECT_PATH),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            env={**os.environ, **NONINTERACTIVE_ENV},
            shell=True if sys.platform == "win32" else False,
            start_new_session=sys.platform != "win32",
        )
    except Exception as e:
        return {"success": False, "error": str(e)}
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        return {
            "success": False,
            "error": f"Command timed out after {timeout}s (process tree killed)",
            "stdout": stdout,
            "stderr": stderr,
        }
    return {
        "success": proc.returncode == 0,
        "stdout": stdout,
        "stderr": stderr,
        "returncode": proc.returncode
    }


def safe_path(path: str) -> Path:
    """Validate and resolve path within allowed directories."""
    resolved = Path(path).resolve()
    allowed_roots = [Config.PROJECT_PATH.resolve(), Path("C:/").resolve()]

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
    # IIS Management Tools
    # ==========================================================================

    def _iis_restart() -> str:
        if not Config.IIS_APP_POOL:
            return "Error: IIS_APP_POOL not configured."
        cmd = ["appcmd", "recycle", "apppool", f"/apppool.name:{Config.IIS_APP_POOL}"]
        result = run_command(cmd)
        if result["success"]:
            return f"App pool '{Config.IIS_APP_POOL}' recycled successfully"
        ps_cmd = ["powershell", "-Command", f"Restart-WebAppPool -Name '{Config.IIS_APP_POOL}'"]
        result = run_command(ps_cmd)
        if result["success"]:
            return f"App pool '{Config.IIS_APP_POOL}' recycled via PowerShell"
        return f"Failed to recycle app pool: {result.get('stderr') or result.get('error')}"

    @mcp.tool()
    def iis_restart() -> str:
        """Recycle the IIS application pool to restart the website."""
        return _iis_restart()

    @mcp.tool()
    def iis_stop() -> str:
        """Stop the IIS website."""
        if not Config.IIS_SITE_NAME:
            return "Error: IIS_SITE_NAME not configured."
        cmd = ["powershell", "-Command", f"Stop-WebSite -Name '{Config.IIS_SITE_NAME}'"]
        result = run_command(cmd)
        if result["success"]:
            return f"Site '{Config.IIS_SITE_NAME}' stopped"
        return f"Failed: {result.get('stderr') or result.get('error')}"

    @mcp.tool()
    def iis_start() -> str:
        """Start the IIS website."""
        if not Config.IIS_SITE_NAME:
            return "Error: IIS_SITE_NAME not configured."
        cmd = ["powershell", "-Command", f"Start-WebSite -Name '{Config.IIS_SITE_NAME}'"]
        result = run_command(cmd)
        if result["success"]:
            return f"Site '{Config.IIS_SITE_NAME}' started"
        return f"Failed: {result.get('stderr') or result.get('error')}"

    @mcp.tool()
    def iis_status() -> str:
        """Get status of all IIS websites and app pools."""
        results = []

        cmd = ["powershell", "-Command", "Get-Website | Select-Object Name, State, PhysicalPath | Format-Table -AutoSize"]
        result = run_command(cmd)
        if result["success"]:
            results.append("=== IIS Websites ===")
            results.append(result["stdout"])

        cmd = ["powershell", "-Command", "Get-WebAppPoolState -Name * | Format-Table -AutoSize"]
        result = run_command(cmd)
        if result["success"]:
            results.append("=== App Pools ===")
            results.append(result["stdout"])

        return "\n".join(results) if results else "Could not get IIS status"

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
        except:
            pass
        return json.dumps(info, indent=2)

    @mcp.tool()
    def full_deploy() -> str:
        """Full deployment: git pull + pip install + collectstatic + migrate + restart IIS."""
        results = []
        results.append("=== Git Pull ===")
        results.append(_git_pull())
        results.append("\n=== Install Requirements ===")
        results.append(_pip_install())
        results.append("\n=== Collectstatic ===")
        results.append(_django_collectstatic())
        results.append("\n=== Migrate ===")
        results.append(_django_migrate())
        results.append("\n=== Restart IIS ===")
        results.append(_iis_restart())
        return '\n'.join(results)

    # ==========================================================================
    # File System Tools
    # ==========================================================================

    @mcp.tool()
    def ls(path: str = ".", pattern: str = "*") -> str:
        """List directory contents (like dir/ls command)."""
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
                except:
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
                except:
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

        info.append("\n=== Python Environment ===")
        info.append(f"Python Version: {sys.version}")
        info.append(f"Python Executable: {sys.executable}")

        info.append("\n=== Project Paths ===")
        info.append(f"Project Root: {Config.PROJECT_PATH}")
        info.append(f"Current Directory: {os.getcwd()}")

        info.append("\n=== Disk Usage ===")
        for drive in ['C:', 'D:', 'E:']:
            try:
                usage = shutil.disk_usage(drive)
                total_gb = usage.total / (1024**3)
                free_gb = usage.free / (1024**3)
                info.append(f"{drive} Total: {total_gb:.1f}GB, Free: {free_gb:.1f}GB")
            except:
                pass

        return "\n".join(info)

    @mcp.tool()
    def ps(filter_name: str = "") -> str:
        """List running processes (optionally filtered by name)."""
        cmd = ["powershell", "-Command",
               "Get-Process | Select-Object Id, ProcessName, CPU, WorkingSet64 | Format-Table -AutoSize"]
        if filter_name:
            cmd = ["powershell", "-Command",
                   f"Get-Process -Name '*{filter_name}*' -ErrorAction SilentlyContinue | Select-Object Id, ProcessName, CPU, WorkingSet64 | Format-Table -AutoSize"]

        result = run_command(cmd, timeout=30)
        if result["success"]:
            return f"Running processes{' (filtered: ' + filter_name + ')' if filter_name else ''}:\n{result['stdout']}"
        return f"Error: {result.get('stderr') or result.get('error')}"

    @mcp.tool()
    def shell(command: str) -> str:
        """Execute a shell command (with safety restrictions)."""
        blocked = ['format', 'del /s', 'rmdir /s', 'rm -rf', 'shutdown', 'restart-computer',
                   'stop-computer', 'remove-item -recurse', 'clear-content']

        cmd_lower = command.lower()
        for blocked_cmd in blocked:
            if blocked_cmd in cmd_lower:
                return f"Error: Command contains blocked operation: {blocked_cmd}"

        result = run_command(["cmd", "/c", command], timeout=60)

        output = []
        if result.get("error"):
            output.append(f"ERROR: {result['error']}")
        if result.get("stdout"):
            output.append(result["stdout"])
        if result.get("stderr"):
            output.append(f"STDERR:\n{result['stderr']}")

        status = "Success" if result["success"] else f"Failed (code {result.get('returncode', '?')})"
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
    """Vigilante interno contra el fallo 'proceso vivo, listener muerto'.

    El 2026-07-08 un corte de red provocó `OSError [WinError 64]` en el bucle
    accept de asyncio (proactor de Windows): el proceso siguió corriendo pero
    dejó de aceptar conexiones TCP nuevas — invisible para un supervisor de
    procesos porque el proceso nunca termina.

    Este hilo hace un GET a /health por loopback cada 60s. Cualquier respuesta
    HTTP (incluso 401/500) cuenta como vivo: lo que se detecta es el socket
    muerto, no errores de aplicación. Tras 3 fallos de conexión consecutivos
    sale con os._exit(3) para que el lanzador (start_server.bat / NSSM) lo
    reinicie limpio.
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
        time.sleep(30)  # gracia de arranque del servidor
        while True:
            alive = False
            try:
                with urllib.request.urlopen(url, timeout=10):
                    alive = True
            except urllib.error.HTTPError:
                alive = True  # respondió HTTP -> el listener está vivo
            except Exception as e:
                logger.warning(
                    "Self-watchdog: /health inalcanzable (%d/%d): %s",
                    consecutive + 1, max_failures, e,
                )
            consecutive = 0 if alive else consecutive + 1
            if consecutive >= max_failures:
                logger.critical(
                    "Self-watchdog: listener HTTP muerto tras %d chequeos. "
                    "Saliendo con código 3 para reinicio externo.",
                    max_failures,
                )
                os._exit(3)
            time.sleep(interval_s)

    threading.Thread(target=_watch, daemon=True, name="self-watchdog").start()
    logger.info("Self-watchdog activo: GET %s cada %ss (3 fallos => reinicio)", url, interval_s)


# =============================================================================
# Main Entry Point
# =============================================================================

def main():
    """Run the MCP server with Streamable HTTP transport."""
    import argparse

    parser = argparse.ArgumentParser(description="Ops MCP Server (FastMCP)")
    parser.add_argument("--port", type=int, default=Config.PORT, help="Port to listen on")
    parser.add_argument("--host", default=Config.HOST, help="Host to bind to")
    args = parser.parse_args()

    if not FASTMCP_AVAILABLE:
        print("ERROR: FastMCP not installed.")
        print("Install with: pip install fastmcp")
        sys.exit(1)

    Config.PORT = args.port
    Config.HOST = args.host

    auth_status = "ENABLED (Bearer Token)" if Config.API_KEY else "DISABLED (no API_KEY)"

    print(f"""
================================================================
          Ops MCP Server (FastMCP v2.0)
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
