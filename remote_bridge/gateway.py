"""Alejandro cloud bridge: minimal authenticated heartbeat API.

This phase deliberately does NOT expose MCP tools or remote command execution.
Run: pip install fastapi uvicorn
     ALEJANDRO_BRIDGE_TOKEN=<random-secret> uvicorn remote_bridge.gateway:app --host 127.0.0.1 --port 8765
Put a TLS reverse proxy in front of the application before using remotely.
"""
import os
import secrets
import time

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Alejandro bridge gateway", docs_url=None, redoc_url=None)
_last_seen: dict[str, float] = {}


class Heartbeat(BaseModel):
    device_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    desktop_healthy: bool


def authorize(header: str | None) -> None:
    expected = os.environ.get("ALEJANDRO_BRIDGE_TOKEN", "")
    if len(expected) < 32:
        raise HTTPException(status_code=503, detail="Bridge not configured")
    if header is None or not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not secrets.compare_digest(header[7:], expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.post("/bridge/v1/heartbeat")
def heartbeat(body: Heartbeat, authorization: str | None = Header(default=None)):
    authorize(authorization)
    _last_seen[body.device_id] = time.monotonic()
    return {"ok": True, "poll_after_seconds": 15}


@app.get("/bridge/v1/status/{device_id}")
def status(device_id: str, authorization: str | None = Header(default=None)):
    authorize(authorization)
    last = _last_seen.get(device_id)
    return {"connected": last is not None and time.monotonic() - last < 60}


@app.get("/health")
def health():
    return {"status": "ok"}
