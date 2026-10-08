"""Device bridge: authentication, connection lifecycle, MCP calls and policy.

Every test here runs the real gateway (uvicorn + WebSockets) and the real
connector; only desktop-mcp is faked.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from remote_bridge import protocol as P
from remote_bridge.policy import DEFAULT_POLICY, ApprovalStore

from .conftest import AGENT_TOKEN, MAXBOT_TOKEN, FakeDesktop, make_connector, wait_until

AG = {"Authorization": f"Bearer {AGENT_TOKEN}"}
MB = {"Authorization": f"Bearer {MAXBOT_TOKEN}"}


def run(coro):
    return asyncio.run(coro)


async def connected(gateway, connector, device="pc-test"):
    task = asyncio.create_task(connector.run_forever())
    ok = await wait_until(lambda: device in gateway.gw.hub.connections)
    assert ok, f"connector did not connect: {connector.state}"
    return task


async def shutdown(connector, task):
    connector.stop()
    await asyncio.wait_for(task, timeout=5)


# ------------------------------------------------------------------ authentication
def test_valid_credentials_connect_and_report_status(gateway, tmp_path):
    token = gateway.enroll()

    async def scenario():
        c = make_connector(gateway, token, tmp_path)
        task = await connected(gateway, c)
        async with httpx.AsyncClient(base_url=gateway.internal, headers=AG) as http:
            devices = (await http.get("/internal/v1/devices")).json()["devices"]
            assert devices[0]["connected"] is True
            # click is offered by desktop-mcp but denied locally -> never advertised
            assert "desktop.click" not in devices[0]["tools"]
            assert devices[0]["tools"]["device.status"] == "allow"
            r = await http.post("/internal/v1/devices/pc-test/call", json={"tool": "device.status"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["ok"] is True
            info = json.loads(body["content"][0]["text"])
            assert info["device_id"] == "pc-test" and info["desktop_mcp"]["status"] == "ok"
        assert c.state["connected"] is True
        await shutdown(c, task)
        assert await wait_until(lambda: "pc-test" not in gateway.gw.hub.connections)

    run(scenario())


def test_invalid_token_is_rejected(gateway, tmp_path):
    gateway.enroll()

    async def scenario():
        c = make_connector(gateway, "x" * 43, tmp_path)
        task = asyncio.create_task(c.run_forever())
        assert await wait_until(lambda: c.state.get("last_error"), timeout=5)
        assert "pc-test" not in gateway.gw.hub.connections
        await shutdown(c, task)

    run(scenario())
    events = [e["event"] for e in gateway.gw.audit.tail(20)]
    assert "device_auth_failed" in events


def test_unknown_device_is_rejected(gateway, tmp_path):
    token = gateway.enroll("pc-test")

    async def scenario():
        c = make_connector(gateway, token, tmp_path, device_id="pc-other")
        task = asyncio.create_task(c.run_forever())
        assert await wait_until(lambda: c.state.get("last_error"), timeout=5)
        assert not gateway.gw.hub.connections
        await shutdown(c, task)

    run(scenario())


def test_revocation_kicks_live_connection_and_blocks_reconnect(gateway, tmp_path):
    token = gateway.enroll()

    async def scenario():
        c = make_connector(gateway, token, tmp_path)
        task = await connected(gateway, c)
        first = c.connections
        gateway.gw.registry.revoke("pc-test")       # what `admin.py revoke` does
        assert await wait_until(lambda: "pc-test" not in gateway.gw.hub.connections, timeout=5)
        await asyncio.sleep(1.5)                    # several reconnect attempts
        assert "pc-test" not in gateway.gw.hub.connections
        assert c.connections == first
        await shutdown(c, task)

    run(scenario())
    events = [e["event"] for e in gateway.gw.audit.tail(50)]
    assert "device_revoked_kick" in events and "device_auth_failed" in events


def test_rotated_token_invalidates_old_one(gateway, tmp_path):
    old = gateway.enroll()
    gateway.gw.registry.add("pc-test", tools=dict(DEFAULT_POLICY))   # rotate
    assert not gateway.gw.registry.authenticate("pc-test", old)


def test_internal_api_requires_credentials_and_roles(gateway):
    with httpx.Client(base_url=gateway.internal) as http:
        assert http.get("/internal/v1/devices").status_code == 401
        assert http.get("/internal/v1/devices", headers={"Authorization": "Bearer nope"}).status_code == 401
        # the agent credential may not read events, approve, or answer Alexa
        assert http.get("/internal/v1/events?wait=0", headers=AG).status_code == 403
        assert http.post("/internal/v1/approvals/abc/decision", json={"approve": True},
                         headers=AG).status_code == 403
        assert http.get("/internal/v1/events?wait=0", headers=MB).status_code == 200


def test_internal_api_is_not_served_publicly(gateway):
    with httpx.Client(base_url=gateway.public) as http:
        assert http.get("/internal/v1/devices", headers=MB).status_code == 404
        assert http.get("/health").json()["status"] == "ok"


# ------------------------------------------------------------------ connection lifecycle
def test_reconnects_after_gateway_drops_the_socket(gateway, tmp_path):
    token = gateway.enroll()

    async def scenario():
        c = make_connector(gateway, token, tmp_path)
        task = await connected(gateway, c)
        conn = gateway.gw.hub.connections["pc-test"]
        fut = asyncio.run_coroutine_threadsafe(
            gateway.gw.hub._drop(conn, P.CLOSE_IDLE, "test drop"), gateway.loop)
        fut.result(5)
        assert await wait_until(lambda: c.connections >= 2, timeout=8), c.state
        assert await wait_until(lambda: "pc-test" in gateway.gw.hub.connections)
        await shutdown(c, task)

    run(scenario())


def test_silent_device_is_disconnected_by_watchdog(gateway, tmp_path):
    token = gateway.enroll()

    async def scenario():
        c = make_connector(gateway, token, tmp_path)
        task = await connected(gateway, c)
        conn = gateway.gw.hub.connections["pc-test"]
        conn.last_seen -= 100                       # as if no heartbeat for 100 s
        assert await wait_until(lambda: gateway.gw.hub.connections.get("pc-test") is not conn, timeout=5)
        await shutdown(c, task)

    run(scenario())
    assert any(e.get("reason") == "no heartbeat" for e in gateway.gw.audit.tail(50))


def test_new_connection_replaces_old_one(gateway, tmp_path):
    token = gateway.enroll()

    async def scenario():
        a = make_connector(gateway, token, tmp_path)
        ta = await connected(gateway, a)
        first = gateway.gw.hub.connections["pc-test"]
        b = make_connector(gateway, token, tmp_path / "b")
        tb = asyncio.create_task(b.run_forever())
        assert await wait_until(lambda: gateway.gw.hub.connections.get("pc-test") not in (None, first))
        await shutdown(b, tb)
        await shutdown(a, ta)

    run(scenario())


def test_call_timeout(gateway, tmp_path):
    token = gateway.enroll()

    async def scenario():
        c = make_connector(gateway, token, tmp_path, desktop=FakeDesktop(delay=5))
        task = await connected(gateway, c)
        async with httpx.AsyncClient(base_url=gateway.internal, headers=AG, timeout=15) as http:
            r = await http.post("/internal/v1/devices/pc-test/call",
                                json={"tool": "desktop.status", "timeout": 1})
            # the device enforces the deadline first and answers with a timeout result
            assert r.status_code == 200
            assert r.json()["ok"] is False and r.json()["error_type"] == "timeout"
        await shutdown(c, task)

    run(scenario())


def test_offline_device(gateway):
    gateway.enroll()
    with httpx.Client(base_url=gateway.internal, headers=AG) as http:
        r = http.post("/internal/v1/devices/pc-test/call", json={"tool": "device.ping"})
        assert r.status_code == 503 and r.json()["status"] == "device_offline"
        r = http.post("/internal/v1/devices/nope/call", json={"tool": "device.ping"})
        assert r.status_code == 404


def test_duplicate_call_frames_run_the_tool_once(tmp_path):
    from remote_bridge.windows_connector import ConnectorConfig, WindowsConnector

    class FakeWS:
        def __init__(self):
            self.sent = []

        async def send(self, data):
            self.sent.append(json.loads(data))

    async def scenario():
        desktop = FakeDesktop(delay=0.2)
        c = WindowsConnector(ConnectorConfig(gateway_url="ws://127.0.0.1:1/x", device_id="pc-test", home=tmp_path), "t" * 40,
                             desktop=desktop, policy=dict(DEFAULT_POLICY))
        ws = FakeWS()
        frame = {"type": "call", "id": P.new_id(), "tool": "desktop.status", "arguments": {},
                 "deadline_ms": 5000}
        await asyncio.gather(c._handle_call(ws, frame), c._handle_call(ws, frame))  # in-flight duplicate
        await c._handle_call(ws, frame)                                             # late duplicate
        assert len(desktop.calls) == 1
        assert len(ws.sent) == 2 and ws.sent[0] == ws.sent[1]

    run(scenario())


def test_idempotent_request_id(gateway, tmp_path):
    token = gateway.enroll()

    async def scenario():
        desktop = FakeDesktop()
        c = make_connector(gateway, token, tmp_path, desktop=desktop)
        task = await connected(gateway, c)
        async with httpx.AsyncClient(base_url=gateway.internal, headers=AG) as http:
            body = {"tool": "desktop.status", "request_id": "req-1"}
            r1 = (await http.post("/internal/v1/devices/pc-test/call", json=body)).json()
            r2 = (await http.post("/internal/v1/devices/pc-test/call", json=body)).json()
        assert len(desktop.calls) == 1
        assert r1["call_id"] == r2["call_id"] and r2["idempotent_replay"] is True
        await shutdown(c, task)

    run(scenario())


def test_malformed_frames_close_the_connection(gateway):
    token = gateway.enroll()

    async def scenario():
        from websockets.asyncio.client import connect
        headers = {"Authorization": f"Bearer {token}", "X-Device-Id": "pc-test"}
        async with connect(gateway.ws_url, additional_headers=headers) as ws:
            await ws.send(json.dumps({"type": "hello", "protocol": 99, "device_id": "pc-test", "tools": []}))
            with pytest.raises(Exception):
                await asyncio.wait_for(ws.recv(), timeout=5)
            assert ws.close_code == P.CLOSE_PROTOCOL

    run(scenario())


# ------------------------------------------------------------------ MCP tools and policy
def test_allowed_tool_and_execution_error(gateway, tmp_path):
    token = gateway.enroll()

    async def scenario():
        c = make_connector(gateway, token, tmp_path)
        task = await connected(gateway, c)
        async with httpx.AsyncClient(base_url=gateway.internal, headers=AG) as http:
            ok = (await http.post("/internal/v1/devices/pc-test/call", json={"tool": "desktop.status"})).json()
            assert ok["ok"] is True and json.loads(ok["content"][0]["text"])["paused"] is False
            bad = (await http.post("/internal/v1/devices/pc-test/call",
                                   json={"tool": "desktop.list_monitors"})).json()
            assert bad["ok"] is False and "monitor enumeration failed" in bad["error"]
        await shutdown(c, task)

    run(scenario())


def test_blocked_tools(gateway, tmp_path):
    # server allows click, device denies it: the device wins (it is never even offered)
    token = gateway.enroll(tools={**DEFAULT_POLICY, "desktop.click": "allow"})

    async def scenario():
        c = make_connector(gateway, token, tmp_path)
        task = await connected(gateway, c)
        async with httpx.AsyncClient(base_url=gateway.internal, headers=AG) as http:
            r = await http.post("/internal/v1/devices/pc-test/call",
                                json={"tool": "desktop.click", "arguments": {"x": 1, "y": 1}})
            assert r.status_code == 403 and r.json()["status"] == "tool_not_offered"
            r = await http.post("/internal/v1/devices/pc-test/call", json={"tool": "desktop.rm_rf"})
            assert r.status_code == 403
            r = await http.post("/internal/v1/devices/pc-test/call", json={"tool": "Bad Name"})
            assert r.status_code == 422
        await shutdown(c, task)

    run(scenario())


def test_server_deny_overrides_device_allow(gateway, tmp_path):
    token = gateway.enroll(tools={**DEFAULT_POLICY, "desktop.status": "deny"})

    async def scenario():
        c = make_connector(gateway, token, tmp_path)
        task = await connected(gateway, c)
        async with httpx.AsyncClient(base_url=gateway.internal, headers=AG) as http:
            r = await http.post("/internal/v1/devices/pc-test/call", json={"tool": "desktop.status"})
            assert r.status_code == 403 and r.json()["status"] == "denied_by_policy"
        await shutdown(c, task)

    run(scenario())


def test_device_refuses_confirm_tool_without_approval(tmp_path):
    """Even a compromised gateway cannot skip the approval: the device checks it."""
    from remote_bridge.windows_connector import ConnectorConfig, WindowsConnector

    async def scenario():
        desktop = FakeDesktop()
        c = WindowsConnector(ConnectorConfig(gateway_url="ws://127.0.0.1:1/x", device_id="pc-test", home=tmp_path), "t" * 40,
                             desktop=desktop, policy=dict(DEFAULT_POLICY))
        frame = {"type": "call", "id": P.new_id(), "tool": "desktop.screenshot", "arguments": {},
                 "deadline_ms": 5000}
        out = await c.execute(frame)
        assert out["ok"] is False and out["error_type"] == "refused" and not desktop.calls
        out = await c.execute({**frame, "id": P.new_id(), "tool": "desktop.click"})
        assert out["ok"] is False and not desktop.calls

    run(scenario())


def test_confirmation_flow_is_single_use_and_bound_to_arguments(gateway, tmp_path):
    token = gateway.enroll()

    async def scenario():
        desktop = FakeDesktop()
        c = make_connector(gateway, token, tmp_path, desktop=desktop)
        task = await connected(gateway, c)
        async with httpx.AsyncClient(base_url=gateway.internal, timeout=10) as http:
            args = {"scale": 0.5}
            r = await http.post("/internal/v1/devices/pc-test/call", headers=AG,
                                json={"tool": "desktop.screenshot", "arguments": args})
            assert r.status_code == 202 and r.json()["status"] == "approval_required"
            approval_id = r.json()["approval_id"]
            assert not desktop.calls

            # MaxBot sees the request on its event stream
            ev = (await http.get("/internal/v1/events?wait=0", headers=MB)).json()["events"]
            assert any(e["type"] == "approval_requested" and e["approval_id"] == approval_id for e in ev)

            # the agent cannot approve its own request
            r = await http.post(f"/internal/v1/approvals/{approval_id}/decision", headers=AG,
                                json={"approve": True})
            assert r.status_code == 403

            # a different call cannot use the approval id
            r = await http.post("/internal/v1/devices/pc-test/call", headers=AG,
                                json={"tool": "desktop.screenshot", "arguments": {"scale": 1},
                                      "approval_id": approval_id})
            assert r.status_code == 403 and r.json()["status"] == "approval_invalid"

            # owner approves -> the gateway runs it once and keeps the result
            r = await http.post(f"/internal/v1/approvals/{approval_id}/decision", headers=MB,
                                json={"approve": True, "decided_by": "telegram:owner"})
            assert r.status_code == 200 and r.json()["status"] == "executed"
            st = (await http.get(f"/internal/v1/approvals/{approval_id}", headers=AG)).json()
            assert st["result"]["ok"] is True and st["result"]["content"][0]["type"] == "image"
            assert len(desktop.calls) == 1

            # replaying the approval does nothing
            r = await http.post("/internal/v1/devices/pc-test/call", headers=AG,
                                json={"tool": "desktop.screenshot", "arguments": args,
                                      "approval_id": approval_id})
            assert r.status_code == 403
            assert len(desktop.calls) == 1
        await shutdown(c, task)

    run(scenario())


def test_denied_approval_never_runs(gateway, tmp_path):
    token = gateway.enroll()

    async def scenario():
        desktop = FakeDesktop()
        c = make_connector(gateway, token, tmp_path, desktop=desktop)
        task = await connected(gateway, c)
        async with httpx.AsyncClient(base_url=gateway.internal) as http:
            r = await http.post("/internal/v1/devices/pc-test/call", headers=AG,
                                json={"tool": "desktop.screenshot"})
            aid = r.json()["approval_id"]
            r = await http.post(f"/internal/v1/approvals/{aid}/decision", headers=MB, json={"approve": False})
            assert r.json()["status"] == "denied"
            r = await http.post("/internal/v1/devices/pc-test/call", headers=AG,
                                json={"tool": "desktop.screenshot", "approval_id": aid})
            assert r.status_code == 403 and not desktop.calls
        await shutdown(c, task)

    run(scenario())


def test_approvals_expire():
    store = ApprovalStore(ttl_seconds=0.0)
    a = store.request("pc", "desktop.screenshot", {}, "agent")
    import time
    time.sleep(0.01)
    assert store.decide(a.id, True, "owner").status == "expired"
    assert store.consume(a.id, "pc", "desktop.screenshot", {}) is None


def test_audit_log_never_contains_arguments(gateway, tmp_path):
    token = gateway.enroll()

    async def scenario():
        c = make_connector(gateway, token, tmp_path)
        task = await connected(gateway, c)
        async with httpx.AsyncClient(base_url=gateway.internal, headers=AG) as http:
            await http.post("/internal/v1/devices/pc-test/call",
                            json={"tool": "desktop.status", "arguments": {"secret_value": "hunter2"}})
        await shutdown(c, task)

    run(scenario())
    gw_audit = (gateway.gw.settings.data_dir / "audit.jsonl").read_text(encoding="utf-8")
    dev_audit = (tmp_path / "home-pc-test" / "audit.jsonl").read_text(encoding="utf-8")
    assert "hunter2" not in gw_audit and "hunter2" not in dev_audit
    assert token not in gw_audit and token not in dev_audit
    assert "secret_value" in gw_audit        # key names are kept, values are not
