"""Shared fixtures: a real gateway (both apps, real uvicorn, real WebSockets) on
ephemeral ports in a background thread, plus a fake desktop-mcp and a test PKI
that stands in for Amazon's Alexa signing certificate."""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import uvicorn

from remote_bridge.alexa import AlexaVerifier
from remote_bridge.gateway import Gateway, Settings
from remote_bridge.windows_connector import ConnectorConfig, WindowsConnector

MAXBOT_TOKEN = "m" * 40
AGENT_TOKEN = "a" * 40
SKILL_ID = "amzn1.ask.skill.test-0000"
ALEXA_USER = "amzn1.ask.account.OWNER"
CERT_URL = "https://s3.amazonaws.com/echo.api/echo-api-cert-test.pem"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------ test PKI
class FakePKI:
    def __init__(self, san: str = "echo-api.amazon.com", expired: bool = False):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

        now = datetime.now(timezone.utc)
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Root CA")])
        ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
        ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
              .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=30))
              .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
              .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False,
                                           key_encipherment=False, data_encipherment=False,
                                           key_agreement=False, key_cert_sign=True, crl_sign=True,
                                           encipher_only=False, decipher_only=False), critical=True)
              .add_extension(ca_ski, critical=False)
              .sign(ca_key, hashes.SHA256()))
        self.leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        nb, na = (now - timedelta(days=10), now - timedelta(days=1)) if expired else \
                 (now - timedelta(days=1), now + timedelta(days=10))
        leaf = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, san)]))
                .issuer_name(ca_name).public_key(self.leaf_key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(nb).not_valid_after(na)
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(san)]), critical=False)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski),
                               critical=False)
                .sign(ca_key, hashes.SHA256()))
        self.ca_pem = ca.public_bytes(serialization.Encoding.PEM)
        self.chain_pem = leaf.public_bytes(serialization.Encoding.PEM)

    def sign(self, body: bytes) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        return base64.b64encode(self.leaf_key.sign(body, padding.PKCS1v15(), hashes.SHA256())).decode()

    def verifier(self) -> AlexaVerifier:
        async def fetch(url):
            return self.chain_pem
        return AlexaVerifier(trust_store_pem=self.ca_pem, fetch=fetch)


@pytest.fixture(scope="session")
def pki():
    return FakePKI()


def alexa_request(rtype="IntentRequest", intent=None, slots=None, *, user=ALEXA_USER,
                  skill=SKILL_ID, ts=None) -> dict:
    ts = ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    request = {"type": rtype, "requestId": "amzn1.echo-api.request.1", "timestamp": ts, "locale": "es-US"}
    if intent:
        request["intent"] = {"name": intent,
                             "slots": {k: {"name": k, "value": v} for k, v in (slots or {}).items()}}
    return {
        "version": "1.0",
        "session": {"new": True, "sessionId": "s1", "application": {"applicationId": skill},
                    "user": {"userId": user}},
        "context": {"System": {"application": {"applicationId": skill}, "user": {"userId": user},
                               "apiEndpoint": "https://api.amazonalexa.invalid", "apiAccessToken": "x"}},
        "request": request,
    }


def signed(pki: FakePKI, payload: dict) -> tuple[dict, bytes]:
    body = json.dumps(payload).encode()
    return {"SignatureCertChainUrl": CERT_URL, "Signature-256": pki.sign(body),
            "Content-Type": "application/json"}, body


# ------------------------------------------------------------------ live gateway
class LiveGateway:
    def __init__(self, tmp: Path, pki: FakePKI, **overrides):
        settings = Settings(
            data_dir=tmp / "gw",
            maxbot_token=MAXBOT_TOKEN,
            agent_token=AGENT_TOKEN,
            alexa_skill_id=SKILL_ID,
            alexa_allowed_user_ids={ALEXA_USER},
            alexa_budget_seconds=overrides.pop("budget", 1.5),
            alexa_progressive=False,
            heartbeat_interval=overrides.pop("heartbeat_interval", 0.5),
            idle_timeout=overrides.pop("idle_timeout", 3.0),
            watch_interval=0.2,
        )
        self.gw = Gateway(settings, verifier=pki.verifier())
        self.public_port, self.internal_port = free_port(), free_port()
        self.loop = asyncio.new_event_loop()
        self._servers = []
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        asyncio.set_event_loop(self.loop)

        async def main():
            for app, port in ((self.gw.public, self.public_port), (self.gw.internal, self.internal_port)):
                s = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                                  ws_ping_interval=None))
                s.install_signal_handlers = lambda: None
                self._servers.append(s)
            self._watch = asyncio.create_task(self.gw.hub.watchdog())
            await asyncio.gather(*(s.serve() for s in self._servers))

        self.loop.run_until_complete(main())

    def start(self):
        self._thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            if len(self._servers) == 2 and all(s.started for s in self._servers):
                return self
            time.sleep(0.05)
        raise RuntimeError("gateway did not start")

    def stop(self):
        self.loop.call_soon_threadsafe(self._watch.cancel)
        for s in self._servers:
            s.should_exit = True
        self._thread.join(timeout=10)

    @property
    def ws_url(self):
        return f"ws://127.0.0.1:{self.public_port}/bridge/v1/ws"

    @property
    def internal(self):
        return f"http://127.0.0.1:{self.internal_port}"

    @property
    def public(self):
        return f"http://127.0.0.1:{self.public_port}"

    def enroll(self, device_id="pc-test", tools=None) -> str:
        from remote_bridge.policy import DEFAULT_POLICY
        return self.gw.registry.add(device_id, label="PC de prueba",
                                    tools=dict(DEFAULT_POLICY) if tools is None else tools)


@pytest.fixture
def gateway(tmp_path, pki):
    g = LiveGateway(tmp_path, pki).start()
    yield g
    g.stop()


# ------------------------------------------------------------------ fake desktop-mcp
class FakeDesktop:
    def __init__(self, tools=("status", "list_monitors", "screenshot", "click"), delay: float = 0.0):
        self.tools = list(tools)
        self.delay = delay
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self):
        return self.tools

    async def health(self):
        return {"status": "ok"}

    async def call_tool(self, name, arguments, timeout):
        self.calls.append((name, arguments))
        if self.delay:
            await asyncio.sleep(self.delay)
        if name == "status":
            return {"ok": True, "content": [{"type": "text", "text": json.dumps({"ok": True, "paused": False})}]}
        if name == "screenshot":
            return {"ok": True, "content": [{"type": "image", "mimeType": "image/jpeg", "data": "AAAA"},
                                            {"type": "text", "text": '{"capture_id": 1}'}]}
        if name == "list_monitors":
            raise RuntimeError("monitor enumeration failed")
        return {"ok": True, "content": [{"type": "text", "text": "done"}]}


def make_connector(gateway: LiveGateway, token: str, tmp: Path, *, device_id="pc-test",
                   desktop=None, policy=None) -> WindowsConnector:
    from remote_bridge.policy import DEFAULT_POLICY
    cfg = ConnectorConfig(gateway_url=gateway.ws_url, device_id=device_id, home=tmp / f"home-{device_id}",
                          max_backoff=0.5, auth_backoff=0.5)
    return WindowsConnector(cfg, token, desktop=desktop or FakeDesktop(),
                            policy=dict(DEFAULT_POLICY) if policy is None else policy)


async def wait_until(pred, timeout=5.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(step)
    return False
