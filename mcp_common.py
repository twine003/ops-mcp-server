"""
Shared helpers for the Ops and Desktop MCP servers
===========================================

Same patterns as server_new.py (env loading, logging, Bearer-token auth,
non-interactive command execution with process-tree kill on timeout),
extracted so new servers (desktop/desktop_server.py) reuse them instead of
copying them.

server_new.py and server_linux.py are deliberately NOT rewired to import
this module yet: both are deployed on production machines and keep their
own inline copy. Moving them over is a mechanical follow-up.
"""

import logging
import os
import subprocess
import sys
from pathlib import Path


def load_env(project_root: Path) -> None:
    """Load <project_root>/.env if python-dotenv is installed."""
    try:
        from dotenv import load_dotenv
        env_file = Path(project_root) / ".env"
        if env_file.exists():
            load_dotenv(env_file, override=True)
    except ImportError:
        pass


def setup_logging(name: str, log_file: Path | None = None) -> logging.Logger:
    """Same format as server_new.py; optionally also to a file.

    Under pythonw.exe (no console, scheduled task) sys.stdout/stderr are None:
    they are redirected to the log file so print() and tracebacks don't crash.
    """
    handlers: list[logging.Handler] = []
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        if sys.stdout is None or sys.stderr is None:
            stream = open(log_file, "a", encoding="utf-8", buffering=1)
            sys.stdout = sys.stdout or stream
            sys.stderr = sys.stderr or stream
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=handlers,
    )
    return logging.getLogger(name)


def make_auth_provider(api_key: str, logger: logging.Logger):
    """Bearer-token auth for FastMCP, or None if no key / class unavailable.

    fastmcp 4.x renamed/moved the class (bearer.StaticBearerAuthProvider ->
    jwt.StaticTokenVerifier, same tokens={...} signature): try both.
    """
    if not api_key:
        logger.warning("No API key configured - server running without authentication")
        return None
    provider_cls = None
    try:
        from fastmcp.server.auth.providers.jwt import StaticTokenVerifier as provider_cls
    except ImportError:
        try:
            from fastmcp.server.auth.providers.bearer import StaticBearerAuthProvider as provider_cls
        except ImportError:
            provider_cls = None
    if provider_cls is None:
        logger.warning("fastmcp auth provider not available - server running without authentication")
        return None
    logger.info("Bearer token authentication enabled")
    return provider_cls(tokens={api_key: {"client_id": "claude-code", "scopes": ["admin"]}})


# Nobody can answer a prompt from an MCP tool: git / Git Credential Manager
# must fail fast instead of waiting on an invisible login window.
NONINTERACTIVE_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
}


def kill_process_tree(proc: subprocess.Popen) -> None:
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


def run_command(cmd: list[str], cwd: str | None = None, timeout: int = 60) -> dict:
    """Execute a command non-interactively; kill the whole tree on timeout."""
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
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
        kill_process_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        return {"success": False,
                "error": f"Command timed out after {timeout}s (process tree killed)",
                "stdout": stdout, "stderr": stderr}
    return {"success": proc.returncode == 0, "stdout": stdout,
            "stderr": stderr, "returncode": proc.returncode}


def start_self_watchdog(port: int, logger: logging.Logger, host: str = "127.0.0.1") -> None:
    """Exit(3) after 3 consecutive failed GET /health (live process, dead listener).

    Same guard as server_new.py (2026-07-08 WinError 64 incident): any HTTP
    answer counts as alive; the external launcher restarts us on exit.
    """
    import threading
    import time
    import urllib.error
    import urllib.request

    url = f"http://{host}:{port}/health"

    def _watch():
        consecutive = 0
        time.sleep(30)
        while True:
            alive = False
            try:
                with urllib.request.urlopen(url, timeout=10):
                    alive = True
            except urllib.error.HTTPError:
                alive = True
            except Exception as e:
                logger.warning("Self-watchdog: /health unreachable (%d/3): %s", consecutive + 1, e)
            consecutive = 0 if alive else consecutive + 1
            if consecutive >= 3:
                logger.critical("Self-watchdog: HTTP listener dead, exiting with code 3 for restart.")
                os._exit(3)
            time.sleep(60)

    threading.Thread(target=_watch, daemon=True, name="self-watchdog").start()
