"""Minimal async client for the local desktop-mcp server (FastMCP, streamable HTTP).

desktop-mcp runs stateless on http://127.0.0.1:8011/mcp and answers each POST on
its own, as an SSE stream with one ``data:`` line (or plain JSON). Every POST
needs ``Accept: application/json, text/event-stream`` and the Bearer key.

The key is read the same way desktop-mcp itself reads it: the process env first,
then the user's environment in the registry (HKCU\\Environment).
"""

from __future__ import annotations

import itertools
import json
import os
import sys
from typing import Any

import httpx

PROTOCOL_VERSION = "2025-06-18"


def read_desktop_key() -> str:
    key = os.environ.get("DESKTOP_MCP_API_KEY", "")
    if key or sys.platform != "win32":
        return key
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            value, _ = winreg.QueryValueEx(k, "DESKTOP_MCP_API_KEY")
            return str(value)
    except OSError:
        return ""


class DesktopError(Exception):
    pass


def _parse_body(resp: httpx.Response) -> dict:
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise DesktopError("empty event stream")
    return resp.json()


class DesktopClient:
    def __init__(self, url: str = "http://127.0.0.1:8011/mcp", api_key: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.url = url
        self.api_key = api_key if api_key is not None else read_desktop_key()
        self._ids = itertools.count(1)
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=3.0), transport=transport)

    @property
    def health_url(self) -> str:
        return self.url.rsplit("/", 1)[0] + "/health"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _rpc(self, method: str, params: dict | None = None, timeout: float = 60.0) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or {}}
        resp = await self._client.post(self.url, json=payload, headers=headers, timeout=timeout)
        if resp.status_code == 401:
            raise DesktopError("desktop-mcp rejected the key (401)")
        if resp.status_code >= 400:
            raise DesktopError(f"desktop-mcp HTTP {resp.status_code}")
        msg = _parse_body(resp)
        if "error" in msg:
            raise DesktopError(str(msg["error"].get("message", msg["error"])))
        return msg.get("result") or {}

    async def health(self) -> dict:
        try:
            r = await self._client.get(self.health_url, timeout=3.0)
            return r.json() if r.status_code == 200 else {"status": f"http {r.status_code}"}
        except Exception as e:
            return {"status": "down", "error": type(e).__name__}

    async def initialize(self) -> dict:
        return await self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "alejandro-connector", "version": "0.2.0"},
        }, timeout=10.0)

    async def list_tools(self) -> list[str]:
        result = await self._rpc("tools/list", timeout=10.0)
        return [t["name"] for t in result.get("tools", []) if isinstance(t, dict) and "name" in t]

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout: float) -> dict:
        """Returns {ok, content, error?, error_type?} in bridge format."""
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
        content = []
        for item in result.get("content", []):
            if item.get("type") == "text":
                content.append({"type": "text", "text": item.get("text", "")})
            elif item.get("type") == "image":
                content.append({"type": "image", "mimeType": item.get("mimeType", "image/png"),
                                "data": item.get("data", "")})
        ok = not result.get("isError", False)
        error = error_type = None
        # desktop-mcp reports refusals as {"ok": false, "error_type": ..., "error": ...} in text.
        for item in content:
            if item["type"] != "text":
                continue
            try:
                parsed = json.loads(item["text"])
            except (ValueError, TypeError):
                continue
            if isinstance(parsed, dict) and parsed.get("ok") is False:
                ok = False
                error = str(parsed.get("error", ""))[:500]
                error_type = str(parsed.get("error_type", "error"))[:40]
            break
        out = {"ok": ok, "content": content}
        if error is not None:
            out["error"], out["error_type"] = error, error_type
        return out
