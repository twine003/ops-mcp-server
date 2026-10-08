"""Alejandro gateway: two FastAPI apps sharing one DeviceHub.

PUBLIC app  (behind the TLS reverse proxy, bound to the Docker bridge address)
    GET  /health
    WS   /bridge/v1/ws          device connectors (Authorization: Bearer <device token>,
                                X-Device-Id: <id>)
    POST /alexa/v1              Alexa Custom Skill endpoint (only if configured)

INTERNAL app  (127.0.0.1 only — never routed by the proxy)
    GET  /internal/v1/devices
    POST /internal/v1/devices/{device_id}/call
    GET  /internal/v1/approvals/{approval_id}
    POST /internal/v1/approvals/{approval_id}/decision      maxbot token only
    GET  /internal/v1/events?after=&wait=                   maxbot token only
    POST /internal/v1/alexa/{job_id}/answer                 maxbot token only
    GET  /internal/v1/alexa/metrics
    GET  /internal/v1/audit?n=                              maxbot token only

Two internal credentials, on purpose:
    BRIDGE_MAXBOT_TOKEN  held by the MaxBot plugin: events, approvals, Alexa answers
    BRIDGE_AGENT_TOKEN   handed to the model's tool CLI: list devices, call tools,
                         read an approval's status — but never approve one.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from . import __version__
from . import protocol as P
from .alexa import AlexaSettings, AlexaSkill, AlexaVerifier
from .events import AuditLog, EventBus
from .hub import CallError, DeviceConnection, DeviceHub
from .registry import DeviceRegistry

log = logging.getLogger("remote_bridge.gateway")


@dataclass
class Settings:
    data_dir: Path
    maxbot_token: str
    agent_token: str
    alexa_skill_id: str = ""
    alexa_allowed_user_ids: set[str] = field(default_factory=set)
    alexa_budget_seconds: float = 6.0
    alexa_progressive: bool = True
    alexa_enroll: bool = False
    trusted_proxies: list = field(default_factory=lambda: [ipaddress.ip_network("172.16.0.0/12"),
                                                           ipaddress.ip_network("127.0.0.0/8")])
    heartbeat_interval: float = 20.0
    idle_timeout: float = 60.0
    watch_interval: float = 5.0
    assistant_name: str = "Alejandro"

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.environ.get("BRIDGE_DATA_DIR", "/var/lib/alejandro-gateway"))
        users = {u.strip() for u in os.environ.get("ALEXA_ALLOWED_USER_IDS", "").split(",") if u.strip()}
        return cls(
            data_dir=data_dir,
            maxbot_token=os.environ.get("BRIDGE_MAXBOT_TOKEN", ""),
            agent_token=os.environ.get("BRIDGE_AGENT_TOKEN", ""),
            alexa_skill_id=os.environ.get("ALEXA_SKILL_ID", ""),
            alexa_allowed_user_ids=users,
            alexa_budget_seconds=float(os.environ.get("ALEXA_BUDGET_SECONDS", "6.0")),
            alexa_progressive=os.environ.get("ALEXA_PROGRESSIVE", "1") == "1",
            alexa_enroll=os.environ.get("ALEXA_ENROLL", "0") == "1",
            assistant_name=os.environ.get("ASSISTANT_NAME", "Alejandro"),
        )

    def validate(self) -> None:
        for name in ("maxbot_token", "agent_token"):
            if len(getattr(self, name)) < 32:
                raise RuntimeError(f"{name.upper()} must be set and at least 32 chars")
        if hmac.compare_digest(self.maxbot_token, self.agent_token):
            raise RuntimeError("BRIDGE_MAXBOT_TOKEN and BRIDGE_AGENT_TOKEN must differ")


class AuthFailureLimiter:
    """Refuse an IP after too many bad device credentials (per sliding window)."""

    def __init__(self, max_failures: int = 10, window: float = 300.0):
        self.max = max_failures
        self.window = window
        self._fails: dict[str, deque] = defaultdict(deque)

    def blocked(self, ip: str) -> bool:
        q = self._fails[ip]
        now = time.monotonic()
        while q and now - q[0] > self.window:
            q.popleft()
        return len(q) >= self.max

    def fail(self, ip: str) -> None:
        self._fails[ip].append(time.monotonic())


def _client_ip(scope_client, headers, trusted) -> str:
    peer = scope_client.host if scope_client else ""
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if any(peer_ip in net for net in trusted):
        fwd = headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return peer


class Gateway:
    def __init__(self, settings: Settings, *, verifier: AlexaVerifier | None = None):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.audit = AuditLog(settings.data_dir / "audit.jsonl")
        self.events = EventBus()
        self.registry = DeviceRegistry(settings.data_dir / "devices.json")
        self.hub = DeviceHub(self.registry, events=self.events, audit=self.audit,
                             heartbeat_interval=settings.heartbeat_interval,
                             idle_timeout=settings.idle_timeout,
                             watch_interval=settings.watch_interval)
        self.limiter = AuthFailureLimiter()
        self.alexa: AlexaSkill | None = None
        if settings.alexa_skill_id:
            self.alexa = AlexaSkill(
                AlexaSettings(
                    skill_id=settings.alexa_skill_id,
                    allowed_user_ids=settings.alexa_allowed_user_ids,
                    budget_seconds=settings.alexa_budget_seconds,
                    progressive=settings.alexa_progressive,
                    enroll=settings.alexa_enroll,
                    assistant_name=settings.assistant_name,
                ),
                verifier or AlexaVerifier(),
                self.events,
                audit=self.audit,
                device_summary=self.device_summary,
            )
        self.public = self._build_public()
        self.internal = self._build_internal()

    # ---------------------------------------------------------------- helpers
    def device_summary(self) -> str:
        devices = self.hub.snapshot()
        if not devices:
            return "No tengo ninguna computadora registrada."
        parts = []
        for d in devices:
            name = d["label"] or d["device_id"]
            if d["revoked"]:
                continue
            parts.append(f"{name} está conectada." if d["connected"] else f"{name} está desconectada.")
        return " ".join(parts) or "No tengo ninguna computadora activa."

    def _who(self, authorization: str | None) -> str:
        """'maxbot' | 'agent'; raises 401 otherwise."""
        token = (authorization or "")[7:] if (authorization or "").startswith("Bearer ") else ""
        if token and hmac.compare_digest(token, self.settings.maxbot_token):
            return "maxbot"
        if token and hmac.compare_digest(token, self.settings.agent_token):
            return "agent"
        raise HTTPException(status_code=401, detail="unauthorized")

    def _require_maxbot(self, authorization: str | None) -> None:
        if self._who(authorization) != "maxbot":
            raise HTTPException(status_code=403, detail="maxbot credential required")

    # ---------------------------------------------------------------- public
    def _build_public(self) -> FastAPI:
        app = FastAPI(title="Alejandro gateway", docs_url=None, redoc_url=None, openapi_url=None)

        @app.get("/health")
        async def health():
            return {"status": "ok", "version": __version__,
                    "devices_connected": len(self.hub.connections),
                    "maxbot_polling": self.events.consumer_alive(),
                    "alexa": self.alexa is not None}

        @app.websocket("/bridge/v1/ws")
        async def device_ws(ws: WebSocket):
            ip = _client_ip(ws.client, ws.headers, self.settings.trusted_proxies)
            if self.limiter.blocked(ip):
                await ws.close(code=P.CLOSE_UNAUTHORIZED)
                return
            device_id = ws.headers.get("x-device-id", "")
            auth = ws.headers.get("authorization", "")
            token = auth[7:] if auth.startswith("Bearer ") else None
            if not P.DEVICE_ID_RE.match(device_id or "") or not self.registry.authenticate(device_id, token):
                self.limiter.fail(ip)
                self.audit.write("device_auth_failed", device_id=device_id[:64], ip=ip)
                await ws.close(code=P.CLOSE_UNAUTHORIZED)
                return
            await ws.accept()
            conn = DeviceConnection(device_id=device_id, transport=ws)
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
                hello = P.parse_device_frame(json.loads(raw))
                if hello["type"] != "hello" or hello["device_id"] != device_id:
                    raise P.ProtocolError("first frame must be hello for the authenticated device")
                await self.hub.attach(conn, hello)
                while True:
                    raw = await ws.receive_text()
                    if len(raw) > P.MAX_FRAME_BYTES:
                        raise P.ProtocolError("frame too large")
                    await self.hub.on_frame(conn, P.parse_device_frame(json.loads(raw)))
            except WebSocketDisconnect:
                self.hub.detach(conn, "socket closed")
            except (P.ProtocolError, ValueError, asyncio.TimeoutError) as e:
                self.audit.write("device_protocol_error", device_id=device_id, error=str(e)[:200])
                self.hub.detach(conn, f"protocol error: {e}")
                try:
                    await ws.close(code=P.CLOSE_PROTOCOL)
                except Exception:
                    pass
            except Exception as e:  # pragma: no cover
                log.exception("device socket error")
                self.hub.detach(conn, f"error: {type(e).__name__}")

        if self.alexa is not None:
            @app.post("/alexa/v1")
            async def alexa_endpoint(request: Request):
                body = await request.body()
                if len(body) > 128 * 1024:
                    return JSONResponse({"error": "too large"}, status_code=413)
                status, payload = await self.alexa.handle(dict(request.headers), body)
                return JSONResponse(payload, status_code=status)

        return app

    # ---------------------------------------------------------------- internal
    def _build_internal(self) -> FastAPI:
        app = FastAPI(title="Alejandro gateway (internal)", docs_url=None, redoc_url=None, openapi_url=None)

        @app.middleware("http")
        async def loopback_only(request: Request, call_next):
            host = request.client.host if request.client else ""
            if host not in ("127.0.0.1", "::1", "testclient"):
                return JSONResponse({"error": "internal API is loopback-only"}, status_code=403)
            return await call_next(request)

        @app.get("/internal/v1/devices")
        async def devices(authorization: str | None = Header(default=None)):
            self._who(authorization)
            return {"devices": self.hub.snapshot()}

        @app.post("/internal/v1/devices/{device_id}/call")
        async def call(device_id: str, body: dict = Body(...),
                       authorization: str | None = Header(default=None)):
            who = self._who(authorization)
            tool = body.get("tool")
            arguments = body.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise HTTPException(status_code=422, detail="arguments must be an object")
            try:
                timeout = float(body.get("timeout", 30))
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail="bad timeout")
            requested_by = f"{who}:{str(body.get('requested_by') or 'unspecified')[:60]}"
            try:
                return await self.hub.call(
                    device_id, tool, arguments, requested_by=requested_by,
                    timeout=min(max(timeout, 1.0), 120.0),
                    approval_id=body.get("approval_id"),
                    request_id=body.get("request_id"),
                )
            except P.ProtocolError as e:
                raise HTTPException(status_code=422, detail=str(e))
            except CallError as e:
                return JSONResponse(e.payload, status_code=e.status)

        @app.get("/internal/v1/approvals/{approval_id}")
        async def approval_status(approval_id: str, authorization: str | None = Header(default=None)):
            self._who(authorization)
            a = self.hub.approvals.get(approval_id)
            if a is None:
                raise HTTPException(status_code=404, detail="unknown approval")
            return a.public()

        @app.post("/internal/v1/approvals/{approval_id}/decision")
        async def approval_decision(approval_id: str, body: dict = Body(...),
                                    authorization: str | None = Header(default=None)):
            self._require_maxbot(authorization)
            decided_by = str(body.get("decided_by") or "owner")[:60]
            result = await self.hub.decide_approval(approval_id, bool(body.get("approve")), decided_by)
            if result is None:
                raise HTTPException(status_code=404, detail="unknown approval")
            return result

        @app.get("/internal/v1/events")
        async def events(after: int = Query(0, ge=0), wait: float = Query(25.0, ge=0, le=55),
                         authorization: str | None = Header(default=None)):
            self._require_maxbot(authorization)
            items = await self.events.wait(after, wait)
            return {"events": items, "seq": self.events.seq}

        @app.post("/internal/v1/alexa/{job_id}/answer")
        async def alexa_answer(job_id: str, body: dict = Body(...),
                               authorization: str | None = Header(default=None)):
            self._require_maxbot(authorization)
            if self.alexa is None:
                raise HTTPException(status_code=404, detail="alexa disabled")
            text = str(body.get("text") or "")[:20000]
            runner_ms = body.get("runner_ms")
            out = self.alexa.deliver(job_id, text, error=bool(body.get("error")),
                                     runner_ms=int(runner_ms) if isinstance(runner_ms, (int, float)) else None)
            if out is None:
                raise HTTPException(status_code=404, detail="unknown or expired job")
            return out

        @app.get("/internal/v1/alexa/metrics")
        async def alexa_metrics(authorization: str | None = Header(default=None)):
            self._who(authorization)
            return {"metrics": self.alexa.metrics if self.alexa else []}

        @app.get("/internal/v1/audit")
        async def audit_tail(n: int = Query(50, ge=1, le=500),
                             authorization: str | None = Header(default=None)):
            self._require_maxbot(authorization)
            return {"entries": self.audit.tail(n)}

        return app
