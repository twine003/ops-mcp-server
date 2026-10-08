"""Wire protocol between the gateway and a device connector (JSON over WebSocket).

Kept dependency-free on purpose: the Windows connector imports it from the
desktop-mcp venv, the gateway from its own venv.

Frames (all JSON objects with a ``type``):

    device -> gateway
        hello   {protocol, device_id, agent_version, hostname, tools: [str],
                 policy: {tool: "allow"|"confirm"|"deny"}}
        ping    {ts}
        result  {id, ok, content: [...], error?, error_type?, duration_ms}

    gateway -> device
        welcome {session_id, heartbeat_interval, server_time}
        pong    {ts}
        call    {id, tool, arguments, deadline_ms, approval?}
        bye     {reason}

``call.id`` is a UUID4 chosen by the gateway; the device uses it to drop
duplicates (it answers a repeated id from its result cache instead of running
the tool twice).
"""

from __future__ import annotations

import re
import uuid
from typing import Any

PROTOCOL_VERSION = 1

# Close codes (4000-4999 are application-defined in RFC 6455).
CLOSE_UNAUTHORIZED = 4401
CLOSE_REVOKED = 4403
CLOSE_PROTOCOL = 4400
CLOSE_REPLACED = 4409
CLOSE_IDLE = 4408

DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
TOOL_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")

MAX_FRAME_BYTES = 8 * 1024 * 1024        # a 1080p JPEG screenshot fits comfortably
MAX_ARGUMENTS_BYTES = 16 * 1024
MAX_TOOLS = 200


class ProtocolError(ValueError):
    """A frame that does not follow the protocol. The connection is closed."""


def new_id() -> str:
    return str(uuid.uuid4())


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ProtocolError(msg)


def _is_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def validate_device_id(value: Any) -> str:
    _require(isinstance(value, str) and bool(DEVICE_ID_RE.match(value)), "invalid device_id")
    return value


def validate_tool_name(value: Any) -> str:
    _require(isinstance(value, str) and bool(TOOL_RE.match(value)), "invalid tool name")
    return value


def parse_device_frame(frame: Any) -> dict:
    """Validate a frame sent by a device. Returns it unchanged or raises."""
    _require(isinstance(frame, dict), "frame must be an object")
    kind = frame.get("type")
    if kind == "hello":
        _require(frame.get("protocol") == PROTOCOL_VERSION, "unsupported protocol version")
        validate_device_id(frame.get("device_id"))
        tools = frame.get("tools")
        _require(isinstance(tools, list) and len(tools) <= MAX_TOOLS, "tools must be a list")
        for t in tools:
            validate_tool_name(t)
        policy = frame.get("policy", {})
        _require(isinstance(policy, dict) and len(policy) <= MAX_TOOLS, "policy must be an object")
        for t, level in policy.items():
            validate_tool_name(t)
            _require(level in ("allow", "confirm", "deny"), "invalid policy level")
        for key in ("agent_version", "hostname"):
            _require(isinstance(frame.get(key, ""), str) and len(frame.get(key, "")) <= 128,
                     f"invalid {key}")
        return frame
    if kind == "ping":
        _require(isinstance(frame.get("ts"), (int, float)), "ping.ts must be a number")
        return frame
    if kind == "result":
        _require(_is_uuid(frame.get("id")), "result.id must be a uuid")
        _require(isinstance(frame.get("ok"), bool), "result.ok must be a bool")
        content = frame.get("content", [])
        _require(isinstance(content, list), "result.content must be a list")
        for item in content:
            _require(isinstance(item, dict) and item.get("type") in ("text", "image"),
                     "content items must be text or image")
        if "error" in frame:
            _require(isinstance(frame["error"], str), "result.error must be a string")
        return frame
    raise ProtocolError(f"unknown frame type: {kind!r}")


def parse_gateway_frame(frame: Any) -> dict:
    """Validate a frame sent by the gateway (used by the connector)."""
    _require(isinstance(frame, dict), "frame must be an object")
    kind = frame.get("type")
    if kind == "welcome":
        _require(isinstance(frame.get("heartbeat_interval"), (int, float)), "bad welcome")
        return frame
    if kind == "pong":
        return frame
    if kind == "bye":
        return frame
    if kind == "call":
        _require(_is_uuid(frame.get("id")), "call.id must be a uuid")
        validate_tool_name(frame.get("tool"))
        args = frame.get("arguments", {})
        _require(isinstance(args, dict), "call.arguments must be an object")
        _require(isinstance(frame.get("deadline_ms"), int) and 0 < frame["deadline_ms"] <= 300_000,
                 "call.deadline_ms out of range")
        approval = frame.get("approval")
        _require(approval is None or isinstance(approval, dict), "call.approval must be an object")
        return frame
    raise ProtocolError(f"unknown frame type: {kind!r}")


def text_content(text: str) -> list[dict]:
    return [{"type": "text", "text": text}]
