"""Alejandro Windows Connector — the PC's side of the bridge.

Opens an OUTBOUND WebSocket over TLS to the gateway (no inbound port, nothing
published), authenticates as one enrolled device, and runs the tool calls the
gateway sends — but only those its LOCAL policy allows. Tools are:

    device.ping, device.status        built in
    desktop.<name>                    forwarded to the local desktop-mcp (127.0.0.1:8011)

Commands:

    python -m remote_bridge.windows_connector run
    python -m remote_bridge.windows_connector set-token        (reads the token from stdin)
    python -m remote_bridge.windows_connector status
    python -m remote_bridge.windows_connector policy

Files under %USERPROFILE%\\.alejandro-connector\\:
    config.json   gateway url, device id, desktop-mcp url
    policy.json   tool -> allow | confirm | deny   (anything missing = deny)
    token.bin     device token, DPAPI-encrypted for this Windows user
    state.json    live connection state (read by `status`)
    audit.jsonl   one line per call: tool, decision, outcome, duration (no arguments)
    connector.log
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import random
import socket
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import __version__
from . import protocol as P
from .events import AuditLog
from .policy import DEFAULT_POLICY, LEVELS

log = logging.getLogger("alejandro.connector")

MAX_RESULT_BYTES = 7 * 1024 * 1024
MAX_TEXT_CHARS = 64 * 1024
RESULT_CACHE = 256


def default_home() -> Path:
    """%USERPROFILE%\\.alejandro-connector (override: ALEJANDRO_CONNECTOR_HOME).

    Not %LOCALAPPDATA%: a process started from a packaged (MSIX) app — e.g. a
    terminal inside a desktop app — gets its AppData writes redirected to the
    package's private LocalCache, so the scheduled task (outside the package)
    would never see the token or config the installer wrote. The profile root
    is not virtualised.
    """
    override = os.environ.get("ALEJANDRO_CONNECTOR_HOME")
    return Path(override) if override else Path.home() / ".alejandro-connector"


@dataclass
class ConnectorConfig:
    gateway_url: str = ""
    device_id: str = ""
    desktop_url: str = "http://127.0.0.1:8011/mcp"
    max_backoff: float = 60.0
    auth_backoff: float = 300.0
    home: Path = field(default_factory=default_home)

    @classmethod
    def load(cls, home: Path | None = None) -> "ConnectorConfig":
        home = home or default_home()
        cfg = cls(home=home)
        path = home / "config.json"
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8-sig"))   # PowerShell 5.1 writes a BOM
            for key in ("gateway_url", "device_id", "desktop_url", "max_backoff", "auth_backoff"):
                if key in raw:
                    setattr(cfg, key, raw[key])
        return cfg


def load_policy(home: Path) -> dict[str, str]:
    """Local policy. Created from the defaults on first run so the owner can edit it."""
    path = home / "policy.json"
    if not path.exists():
        home.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULT_POLICY, indent=2, sort_keys=True), encoding="utf-8")
    raw = json.loads(path.read_text(encoding="utf-8-sig"))   # PowerShell 5.1 writes a BOM
    return {k: v for k, v in raw.items() if v in LEVELS}


class _Stop(Exception):
    pass


class WindowsConnector:
    def __init__(
        self,
        config: ConnectorConfig,
        token: str,
        *,
        desktop=None,
        policy: dict[str, str] | None = None,
        connect: Callable[..., Awaitable[Any]] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        if not token:
            raise RuntimeError("no device token; run `set-token` first")
        P.validate_device_id(config.device_id)
        if not config.gateway_url.startswith(("wss://", "ws://127.0.0.1", "ws://localhost")):
            raise RuntimeError("gateway_url must be wss:// (plain ws:// only to localhost, for tests)")
        self.cfg = config
        self._token = token
        self.desktop = desktop
        self.policy = policy if policy is not None else load_policy(config.home)
        self._connect = connect or _ws_connect
        self._clock = clock
        self.audit = AuditLog(config.home / "audit.jsonl")
        self._results: OrderedDict[str, dict] = OrderedDict()
        self._inflight: set[str] = set()
        self._sem = asyncio.Semaphore(2)
        self.state: dict[str, Any] = {"connected": False}
        self.connections = 0
        self._stop = asyncio.Event()

    # ------------------------------------------------------------ state
    def _set_state(self, **kw) -> None:
        self.state.update(kw, updated_at=self._clock())
        try:
            self.cfg.home.mkdir(parents=True, exist_ok=True)
            (self.cfg.home / "state.json").write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        except OSError:
            pass

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------ tools
    async def available_tools(self) -> list[str]:
        tools = ["device.ping", "device.status"]
        if self.desktop is not None:
            try:
                tools += [f"desktop.{name}" for name in await self.desktop.list_tools()]
            except Exception as e:
                log.warning("desktop-mcp unavailable, offering device.* only: %s", e)
        # Never advertise what the local policy denies.
        return [t for t in tools if self.policy.get(t, "deny") != "deny" and P.TOOL_RE.match(t)]

    async def _builtin(self, tool: str, arguments: dict) -> dict:
        if tool == "device.ping":
            return {"ok": True, "content": P.text_content(json.dumps({"pong": True, "ts": self._clock()}))}
        if tool == "device.status":
            desktop = await self.desktop.health() if self.desktop is not None else {"status": "absent"}
            info = {
                "device_id": self.cfg.device_id,
                "hostname": socket.gethostname(),
                "os": f"{platform.system()} {platform.release()} ({platform.version()})",
                "connector_version": __version__,
                "python": platform.python_version(),
                "connected_since": self.state.get("connected_since"),
                "desktop_mcp": desktop,
            }
            return {"ok": True, "content": P.text_content(json.dumps(info, ensure_ascii=False))}
        return {"ok": False, "error": f"unknown builtin {tool}", "error_type": "not_found", "content": []}

    async def execute(self, frame: dict) -> dict:
        """Run one validated call frame; returns a result frame. Policy is checked HERE."""
        call_id, tool, args = frame["id"], frame["tool"], frame.get("arguments") or {}
        level = self.policy.get(tool, "deny")
        started = time.monotonic()
        decision = level
        if level == "deny":
            out = {"ok": False, "error": f"{tool} is denied by the device policy",
                   "error_type": "refused", "content": []}
        elif level == "confirm" and not frame.get("approval"):
            decision = "confirm_missing"
            out = {"ok": False, "error": f"{tool} requires an owner approval",
                   "error_type": "refused", "content": []}
        else:
            timeout = frame["deadline_ms"] / 1000.0
            try:
                async with self._sem:
                    if tool.startswith("device."):
                        coro = self._builtin(tool, args)
                    elif tool.startswith("desktop.") and self.desktop is not None:
                        coro = self.desktop.call_tool(tool.split(".", 1)[1], args, timeout=timeout)
                    else:
                        raise LookupError(f"no handler for {tool}")
                    out = await asyncio.wait_for(coro, timeout=timeout)
            except asyncio.TimeoutError:
                out = {"ok": False, "error": "tool timed out on the device", "error_type": "timeout",
                       "content": []}
            except Exception as e:
                out = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}", "error_type": "error",
                       "content": []}
        duration = int((time.monotonic() - started) * 1000)
        result = {"type": "result", "id": call_id, "ok": bool(out.get("ok")),
                  "content": _bounded(out.get("content", [])), "duration_ms": duration}
        if out.get("error"):
            result["error"] = str(out["error"])
            result["error_type"] = str(out.get("error_type") or "error")
        self.audit.write("call", call_id=call_id, tool=tool, decision=decision, ok=result["ok"],
                         error_type=result.get("error_type"), duration_ms=duration,
                         approval_id=(frame.get("approval") or {}).get("approval_id"))
        return result

    async def _handle_call(self, ws, frame: dict) -> None:
        call_id = frame["id"]
        if call_id in self._results:            # duplicate: answer from cache, do not re-run
            await ws.send(json.dumps(self._results[call_id]))
            return
        if call_id in self._inflight:           # duplicate while running: the first answer will do
            return
        self._inflight.add(call_id)
        try:
            result = await self.execute(frame)
            self._results[call_id] = result
            while len(self._results) > RESULT_CACHE:
                self._results.popitem(last=False)
            await ws.send(json.dumps(result))
        finally:
            self._inflight.discard(call_id)

    # ------------------------------------------------------------ session
    async def session(self) -> None:
        """One connection, from handshake to close. Raises on any failure."""
        headers = {"Authorization": f"Bearer {self._token}", "X-Device-Id": self.cfg.device_id,
                   "User-Agent": f"alejandro-connector/{__version__}"}
        ws = await self._connect(self.cfg.gateway_url, headers)
        tasks: list[asyncio.Task] = []
        try:
            tools = await self.available_tools()
            await ws.send(json.dumps({
                "type": "hello", "protocol": P.PROTOCOL_VERSION, "device_id": self.cfg.device_id,
                "agent_version": __version__, "hostname": socket.gethostname()[:128], "tools": tools,
                "policy": {t: self.policy[t] for t in tools},
            }))
            welcome = P.parse_gateway_frame(json.loads(await asyncio.wait_for(ws.recv(), timeout=15)))
            if welcome["type"] != "welcome":
                raise P.ProtocolError("expected welcome")
            interval = float(welcome["heartbeat_interval"])
            self.connections += 1
            self._set_state(connected=True, connected_since=self._clock(), session_id=welcome.get("session_id"),
                            gateway=self.cfg.gateway_url, tools=tools, last_error=None)
            self.audit.write("connected", session_id=welcome.get("session_id"), tools=len(tools))
            log.info("connected to %s as %s (%d tools)", self.cfg.gateway_url, self.cfg.device_id, len(tools))

            last_pong = time.monotonic()

            async def heartbeat():
                nonlocal last_pong
                wall, mono = self._clock(), time.monotonic()
                while True:
                    await asyncio.sleep(interval)
                    # A wall-clock jump much bigger than the monotonic one means the PC slept:
                    # the socket is almost certainly dead even if the OS has not noticed yet.
                    now_wall, now_mono = self._clock(), time.monotonic()
                    if (now_wall - wall) - (now_mono - mono) > 30:
                        raise ConnectionError("system resumed from sleep")
                    wall, mono = now_wall, now_mono
                    if now_mono - last_pong > 3 * interval:
                        raise ConnectionError("no pong from gateway")
                    await ws.send(json.dumps({"type": "ping", "ts": now_wall}))

            async def reader():
                nonlocal last_pong
                while True:
                    raw = await ws.recv()
                    frame = P.parse_gateway_frame(json.loads(raw))
                    if frame["type"] == "pong":
                        last_pong = time.monotonic()
                    elif frame["type"] == "call":
                        last_pong = time.monotonic()
                        tasks.append(asyncio.create_task(self._handle_call(ws, frame)))
                        tasks[:] = [t for t in tasks if not t.done()]
                    elif frame["type"] == "bye":
                        raise ConnectionError(f"gateway said bye: {frame.get('reason')}")

            async def stopper():
                await self._stop.wait()
                raise _Stop()

            runners = [asyncio.create_task(c()) for c in (heartbeat, reader, stopper)]
            done, pending = await asyncio.wait(runners, return_when=asyncio.FIRST_EXCEPTION)
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc:
                    raise exc
        finally:
            for t in tasks:
                t.cancel()
            self._set_state(connected=False)
            try:
                await ws.close()
            except Exception:
                pass

    async def run_forever(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            started = time.monotonic()
            delay = None
            try:
                await self.session()
            except _Stop:
                break
            except Exception as e:
                code = _close_code(e)
                reason = f"{type(e).__name__}: {str(e)[:200]}"
                self._set_state(connected=False, last_error=reason, last_close_code=code)
                self.audit.write("disconnected", reason=reason, close_code=code)
                if code in (P.CLOSE_UNAUTHORIZED, P.CLOSE_REVOKED) or _is_http_403(e):
                    # Rejected credential: retrying fast would only trip the gateway limiter.
                    log.error("gateway refused this device (%s); retrying in %ds", reason, self.cfg.auth_backoff)
                    delay = self.cfg.auth_backoff
                else:
                    log.warning("connection lost: %s", reason)
            if time.monotonic() - started > 60:
                backoff = 1.0                   # it was a healthy session; reconnect quickly
            if delay is None:
                delay = min(backoff, self.cfg.max_backoff) * (0.5 + random.random())
                backoff = min(backoff * 2, self.cfg.max_backoff)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass


def _bounded(content: list[dict]) -> list[dict]:
    out, size = [], 0
    for item in content:
        if item.get("type") == "text":
            text = item.get("text", "")
            if len(text) > MAX_TEXT_CHARS:
                text = text[:MAX_TEXT_CHARS] + "\n…[truncated]"
            item = {"type": "text", "text": text}
        size += len(json.dumps(item))
        if size > MAX_RESULT_BYTES:
            out.append({"type": "text", "text": "[result truncated: too large]"})
            break
        out.append(item)
    return out


def _close_code(exc: BaseException) -> int | None:
    rcvd = getattr(exc, "rcvd", None)
    return getattr(rcvd, "code", None)


def _is_http_403(exc: BaseException) -> bool:
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None) in (401, 403)


async def _ws_connect(url: str, headers: dict):
    from websockets.asyncio.client import connect
    return await connect(url, additional_headers=headers, open_timeout=15, ping_interval=20,
                         ping_timeout=20, max_size=P.MAX_FRAME_BYTES, close_timeout=5)


# ---------------------------------------------------------------------------- CLI
def _setup_logging(home: Path, verbose: bool) -> None:
    from logging.handlers import RotatingFileHandler
    home.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [RotatingFileHandler(home / "connector.log", maxBytes=2_000_000,
                                                           backupCount=3, encoding="utf-8")]
    if sys.stderr is not None and verbose:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main(argv: list[str] | None = None) -> int:
    from .secret_store import load_token, save_token

    ap = argparse.ArgumentParser(prog="alejandro-connector")
    ap.add_argument("--home", type=Path, default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("set-token")
    sub.add_parser("status")
    sub.add_parser("policy")
    args = ap.parse_args(argv)
    cfg = ConnectorConfig.load(args.home)
    token_path = cfg.home / "token.bin"

    if args.cmd == "set-token":
        token = sys.stdin.readline().strip()
        if len(token) < 32:
            print("token looks wrong (too short)", file=sys.stderr)
            return 2
        save_token(token_path, token)
        print(f"token stored (DPAPI) in {token_path}")
        return 0
    if args.cmd == "status":
        path = cfg.home / "state.json"
        print(path.read_text(encoding="utf-8") if path.exists() else '{"connected": false, "note": "never ran"}')
        return 0
    if args.cmd == "policy":
        print(json.dumps(load_policy(cfg.home), indent=2, sort_keys=True))
        return 0

    _setup_logging(cfg.home, args.verbose)
    from .desktop_client import DesktopClient
    connector = WindowsConnector(cfg, load_token(token_path), desktop=DesktopClient(cfg.desktop_url))
    log.info("alejandro-connector %s starting (device %s)", __version__, cfg.device_id)
    try:
        asyncio.run(connector.run_forever())
    except KeyboardInterrupt:
        pass
    return 0


def _main_logging_crashes() -> int:
    """Under pythonw (the scheduled task) there is no stderr: a crash before the
    log is set up would vanish. Write it to crash.log instead."""
    try:
        return main()
    except SystemExit:
        raise
    except BaseException:
        import traceback
        try:
            home = default_home()
            home.mkdir(parents=True, exist_ok=True)
            with (home / "crash.log").open("a", encoding="utf-8") as f:
                f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n{traceback.format_exc()}\n")
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(_main_logging_crashes())
