"""DeviceHub: live device connections and the calls in flight to them.

The hub is transport-agnostic: a connection is anything with ``send_json`` and
``close(code, reason)`` (the FastAPI WebSocket in production, a fake in tests).

Guarantees:
- one live connection per device (a new one replaces the old: CLOSE_REPLACED);
- every call has a gateway-generated UUID and a deadline; a result that arrives
  late, twice, or for an unknown id is dropped;
- callers may pass an idempotency ``request_id``: repeating it within 10 min
  returns the first result instead of running the tool again;
- a revoked device is disconnected within ``watch_interval`` seconds, and its
  token is refused from then on.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import protocol as P
from .events import AuditLog, EventBus
from .policy import DEFAULT_POLICY, ApprovalStore, args_digest, level_for, stricter
from .registry import DeviceRegistry

log = logging.getLogger("remote_bridge.hub")

IDEMPOTENCY_TTL = 600.0


class Transport(Protocol):
    async def send_json(self, data: Any) -> None: ...
    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


class CallError(Exception):
    """A call that did not run. ``status`` maps to an HTTP status on the internal API."""

    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.payload = {"status": code, "error": message, **extra}


@dataclass
class DeviceConnection:
    device_id: str
    transport: Transport
    session_id: str = field(default_factory=P.new_id)
    hostname: str = ""
    agent_version: str = ""
    tools: list[str] = field(default_factory=list)
    device_policy: dict[str, str] = field(default_factory=dict)
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.monotonic)
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    closed: bool = False


class DeviceHub:
    def __init__(
        self,
        registry: DeviceRegistry,
        *,
        events: EventBus | None = None,
        audit: AuditLog | None = None,
        approvals: ApprovalStore | None = None,
        heartbeat_interval: float = 20.0,
        idle_timeout: float = 60.0,
        watch_interval: float = 5.0,
    ):
        self.registry = registry
        self.events = events or EventBus()
        self.audit = audit or AuditLog(None)
        self.approvals = approvals or ApprovalStore()
        self.heartbeat_interval = heartbeat_interval
        self.idle_timeout = idle_timeout
        self.watch_interval = watch_interval
        self.connections: dict[str, DeviceConnection] = {}
        self._idem: dict[str, tuple[float, Any]] = {}
        self._last_seen_offline: dict[str, float] = {}

    # ------------------------------------------------------------ connections
    async def attach(self, conn: DeviceConnection, hello: dict) -> None:
        conn.hostname = hello.get("hostname", "")
        conn.agent_version = hello.get("agent_version", "")
        conn.tools = list(hello.get("tools", []))
        conn.device_policy = dict(hello.get("policy", {}))
        old = self.connections.get(conn.device_id)
        self.connections[conn.device_id] = conn
        if old is not None and old is not conn:
            await self._drop(old, P.CLOSE_REPLACED, "replaced by a newer connection")
        await conn.transport.send_json({
            "type": "welcome",
            "session_id": conn.session_id,
            "heartbeat_interval": self.heartbeat_interval,
            "server_time": time.time(),
        })
        self.audit.write("device_connected", device_id=conn.device_id, session_id=conn.session_id,
                         hostname=conn.hostname, agent_version=conn.agent_version,
                         tools=len(conn.tools))
        log.info("device %s connected (session %s, %d tools)",
                 conn.device_id, conn.session_id[:8], len(conn.tools))

    def detach(self, conn: DeviceConnection, reason: str = "disconnected") -> None:
        conn.closed = True
        for fut in conn.pending.values():
            if not fut.done():
                fut.set_exception(CallError(503, "device_disconnected",
                                            f"device disconnected: {reason}"))
        conn.pending.clear()
        if self.connections.get(conn.device_id) is conn:
            self.connections.pop(conn.device_id, None)
            self._last_seen_offline[conn.device_id] = time.time()
            self.audit.write("device_disconnected", device_id=conn.device_id,
                             session_id=conn.session_id, reason=reason)
            log.info("device %s disconnected: %s", conn.device_id, reason)

    async def _drop(self, conn: DeviceConnection, code: int, reason: str) -> None:
        self.detach(conn, reason)
        try:
            await conn.transport.send_json({"type": "bye", "reason": reason})
        except Exception:
            pass
        try:
            await conn.transport.close(code, reason)
        except Exception:
            pass

    async def on_frame(self, conn: DeviceConnection, frame: dict) -> None:
        """Handle one validated frame from a device (after hello)."""
        conn.last_seen = time.monotonic()
        kind = frame["type"]
        if kind == "ping":
            await conn.transport.send_json({"type": "pong", "ts": frame["ts"]})
        elif kind == "result":
            fut = conn.pending.pop(frame["id"], None)
            if fut is None or fut.done():
                log.info("device %s: dropping late/duplicate result %s", conn.device_id, frame["id"][:8])
                return
            fut.set_result(frame)
        elif kind == "hello":
            raise P.ProtocolError("hello sent twice")

    async def watchdog(self) -> None:
        """Disconnect revoked and silent devices. Runs for the life of the gateway."""
        while True:
            await asyncio.sleep(self.watch_interval)
            try:
                self.registry.changed()
                now = time.monotonic()
                for conn in list(self.connections.values()):
                    if not self.registry.is_active(conn.device_id):
                        self.audit.write("device_revoked_kick", device_id=conn.device_id)
                        await self._drop(conn, P.CLOSE_REVOKED, "credential revoked")
                    elif now - conn.last_seen > self.idle_timeout:
                        await self._drop(conn, P.CLOSE_IDLE, "no heartbeat")
                self._gc_idempotency()
            except Exception:  # pragma: no cover - never let the watchdog die
                log.exception("watchdog iteration failed")

    # ------------------------------------------------------------ queries
    def effective_policy(self, device_id: str) -> dict[str, str]:
        """tool -> level, the stricter of server and device policy, for offered tools."""
        server = self.registry.server_policy(device_id) or DEFAULT_POLICY
        conn = self.connections.get(device_id)
        if conn is None:
            return {}
        out = {}
        for tool in conn.tools:
            device_level = conn.device_policy.get(tool, "deny")
            out[tool] = stricter(level_for(server, tool), device_level)
        return out

    def snapshot(self) -> list[dict]:
        devices = []
        for device_id, info in self.registry.list().items():
            conn = self.connections.get(device_id)
            devices.append({
                "device_id": device_id,
                "label": info.get("label", ""),
                "revoked": bool(info.get("revoked_at")),
                "connected": conn is not None,
                "hostname": conn.hostname if conn else None,
                "agent_version": conn.agent_version if conn else None,
                "connected_since": conn.connected_at if conn else None,
                "seconds_since_heartbeat": round(time.monotonic() - conn.last_seen, 1) if conn else None,
                "last_disconnect": self._last_seen_offline.get(device_id),
                "tools": self.effective_policy(device_id) if conn else {},
            })
        return devices

    # ------------------------------------------------------------ calls
    def _gc_idempotency(self) -> None:
        now = time.monotonic()
        for key, (ts, _) in list(self._idem.items()):
            if now - ts > IDEMPOTENCY_TTL:
                self._idem.pop(key, None)

    async def call(
        self,
        device_id: str,
        tool: str,
        arguments: dict | None = None,
        *,
        requested_by: str,
        timeout: float = 30.0,
        approval_id: str | None = None,
        request_id: str | None = None,
    ) -> dict:
        arguments = arguments or {}
        P.validate_device_id(device_id)
        P.validate_tool_name(tool)
        if len(repr(arguments)) > P.MAX_ARGUMENTS_BYTES:
            raise CallError(413, "arguments_too_large", "arguments too large")

        if request_id:
            key = f"{device_id}:{request_id}"
            hit = self._idem.get(key)
            if hit is not None:
                cached = hit[1]
                if isinstance(cached, asyncio.Future):
                    return await asyncio.shield(cached)
                return {**cached, "idempotent_replay": True}
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._idem[key] = (time.monotonic(), fut)
            try:
                result = await self._call(device_id, tool, arguments, requested_by=requested_by,
                                          timeout=timeout, approval_id=approval_id)
            except CallError as e:
                self._idem.pop(key, None)       # failures are not cached: the caller may retry
                if not fut.done():
                    fut.set_exception(e)
                    fut.exception()             # mark retrieved
                raise
            self._idem[key] = (time.monotonic(), result)
            if not fut.done():
                fut.set_result(result)
            return result
        return await self._call(device_id, tool, arguments, requested_by=requested_by,
                                timeout=timeout, approval_id=approval_id)

    async def _call(self, device_id, tool, arguments, *, requested_by, timeout, approval_id) -> dict:
        digest = args_digest(arguments)
        base_audit = dict(device_id=device_id, tool=tool, args_sha256=digest[:16],
                          arg_keys=sorted(arguments), requested_by=requested_by)

        if not self.registry.is_active(device_id):
            self.audit.write("call_refused", reason="unknown_or_revoked", **base_audit)
            raise CallError(404, "unknown_device", f"device {device_id!r} is not enrolled or is revoked")
        conn = self.connections.get(device_id)
        if conn is None:
            self.audit.write("call_refused", reason="offline", **base_audit)
            raise CallError(503, "device_offline", f"device {device_id!r} is not connected")
        if tool not in conn.tools:
            self.audit.write("call_refused", reason="not_offered", **base_audit)
            raise CallError(403, "tool_not_offered", f"{tool} is not offered by the device")

        level = self.effective_policy(device_id).get(tool, "deny")
        approval = None
        if level == "deny":
            self.audit.write("call_refused", reason="policy_deny", **base_audit)
            raise CallError(403, "denied_by_policy", f"{tool} is denied by policy")
        if level == "confirm":
            if not approval_id:
                pending = self.approvals.request(device_id, tool, arguments, requested_by)
                self.audit.write("approval_requested", approval_id=pending.id, **base_audit)
                await self.events.publish("approval_requested", pending.public())
                raise CallError(202, "approval_required",
                                f"{tool} needs a one-time approval from the owner",
                                approval_id=pending.id, expires_in=int(self.approvals.ttl))
            approval = self.approvals.consume(approval_id, device_id, tool, arguments)
            if approval is None:
                self.audit.write("call_refused", reason="bad_approval", approval_id=approval_id, **base_audit)
                raise CallError(403, "approval_invalid",
                                "approval missing, expired, already used, or for another call")

        call_id = P.new_id()
        deadline_ms = int(min(max(timeout, 1.0), 300.0) * 1000)
        frame = {"type": "call", "id": call_id, "tool": tool, "arguments": arguments,
                 "deadline_ms": deadline_ms}
        if approval is not None:
            frame["approval"] = {"approval_id": approval.id, "decided_by": approval.decided_by}

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        conn.pending[call_id] = fut
        started = time.monotonic()
        try:
            await conn.transport.send_json(frame)
            reply = await asyncio.wait_for(asyncio.shield(fut), timeout=timeout + 2.0)
        except asyncio.TimeoutError:
            conn.pending.pop(call_id, None)
            self.audit.write("call_timeout", call_id=call_id, level=level, **base_audit)
            raise CallError(504, "timeout", f"{tool} did not answer within {timeout:.0f}s",
                            call_id=call_id)
        except CallError:
            self.audit.write("call_failed", call_id=call_id, reason="disconnected", **base_audit)
            raise
        except Exception as e:
            conn.pending.pop(call_id, None)
            self.audit.write("call_failed", call_id=call_id, reason=type(e).__name__, **base_audit)
            raise CallError(502, "transport_error", f"could not reach device: {e}", call_id=call_id)

        elapsed = int((time.monotonic() - started) * 1000)
        result = {
            "call_id": call_id,
            "device_id": device_id,
            "tool": tool,
            "ok": reply["ok"],
            "content": reply.get("content", []),
            "error": reply.get("error"),
            "error_type": reply.get("error_type"),
            "duration_ms": reply.get("duration_ms"),
            "roundtrip_ms": elapsed,
        }
        self.audit.write("call_done", call_id=call_id, level=level, ok=reply["ok"],
                         error_type=reply.get("error_type"), roundtrip_ms=elapsed,
                         content_items=len(result["content"]),
                         approval_id=approval.id if approval else None, **base_audit)
        return result

    async def decide_approval(self, approval_id: str, approve: bool, decided_by: str) -> dict | None:
        """Approve/deny a pending call. On approval the gateway runs it right away
        and keeps the result on the approval, so the requester just polls it."""
        a = self.approvals.decide(approval_id, approve, decided_by)
        if a is None:
            return None
        self.audit.write("approval_decided", approval_id=approval_id, approved=approve,
                         decided_by=decided_by, status=a.status)
        if a.status == "approved":
            try:
                a.result = await self.call(a.device_id, a.tool, a.arguments,
                                           requested_by=a.requested_by, approval_id=a.id)
            except CallError as e:
                a.result = {"ok": False, **e.payload}
            await self.events.publish("approval_executed", {"approval_id": a.id, "ok": a.result.get("ok")})
        return a.public()
