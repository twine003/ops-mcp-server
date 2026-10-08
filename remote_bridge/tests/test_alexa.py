"""Alexa endpoint: verification, authorisation, the voice turn flow and timings."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from remote_bridge.alexa import AlexaVerificationError, check_cert_url, to_speech

from .conftest import ALEXA_USER, MAXBOT_TOKEN, FakePKI, alexa_request, signed

MB = {"Authorization": f"Bearer {MAXBOT_TOKEN}"}


def say(resp: httpx.Response) -> str:
    assert resp.status_code == 200, resp.text
    return resp.json()["response"]["outputSpeech"]["text"]


class FakeMaxBot:
    """Long-polls the gateway like the MaxBot plugin and answers Alexa turns."""

    def __init__(self, gateway, answer="Tienes dos pendientes: **revisar** el ERP y llamar a Ana.",
                 delay=0.1):
        self.gateway, self.answer, self.delay = gateway, answer, delay
        self.results = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        seq = 0
        with httpx.Client(base_url=self.gateway.internal, headers=MB, timeout=10) as http:
            while not self._stop.is_set():
                data = http.get("/internal/v1/events", params={"after": seq, "wait": 0.3}).json()
                for ev in data["events"]:
                    seq = max(seq, ev["seq"])
                    if ev["type"] == "alexa_turn":
                        time.sleep(self.delay)
                        r = http.post(f"/internal/v1/alexa/{ev['job_id']}/answer",
                                      json={"text": self.answer, "runner_ms": int(self.delay * 1000)})
                        self.results.append(r.json())

    def __enter__(self):
        self._t.start()
        deadline = time.time() + 5
        while not self.gateway.gw.events.consumer_alive() and time.time() < deadline:
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(5)


def post(gateway, pki, payload, **kw):
    headers, body = signed(pki, payload)
    headers.update(kw.pop("headers", {}))
    return httpx.post(f"{gateway.public}/alexa/v1", content=body, headers=headers, timeout=15)


# ------------------------------------------------------------------ verification
@pytest.mark.parametrize("url", [
    "http://s3.amazonaws.com/echo.api/echo-api-cert.pem",
    "https://notamazon.com/echo.api/echo-api-cert.pem",
    "https://s3.amazonaws.com/EcHo.aPi/echo-api-cert.pem",
    "https://s3.amazonaws.com:563/echo.api/echo-api-cert.pem",
    "https://s3.amazonaws.com/invalid.path/echo-api-cert.pem",
    "https://s3.amazonaws.com/echo.api/../invalid.path/echo-api-cert.pem",
])
def test_cert_url_rejected(url):
    with pytest.raises(AlexaVerificationError):
        check_cert_url(url)


@pytest.mark.parametrize("url", [
    "https://s3.amazonaws.com/echo.api/echo-api-cert.pem",
    "https://s3.amazonaws.com:443/echo.api/echo-api-cert.pem",
    "https://s3.amazonaws.com/echo.api/../echo.api/echo-api-cert.pem",
    "HTTPS://s3.amazonaws.com/echo.api/echo-api-cert.pem",
    "https://S3.AMAZONAWS.COM/echo.api/echo-api-cert.pem",
])
def test_cert_url_accepted(url):
    check_cert_url(url)


def test_valid_signature_launch(gateway, pki):
    assert "Alejandro" in say(post(gateway, pki, alexa_request("LaunchRequest")))


def test_bad_signature_rejected(gateway, pki):
    headers, body = signed(pki, alexa_request("LaunchRequest"))
    tampered = body.replace(b"LaunchRequest", b"IntentRequest")
    r = httpx.post(f"{gateway.public}/alexa/v1", content=tampered, headers=headers)
    assert r.status_code == 400


def test_missing_headers_rejected(gateway):
    r = httpx.post(f"{gateway.public}/alexa/v1", json=alexa_request("LaunchRequest"))
    assert r.status_code == 400


def test_stale_timestamp_rejected(gateway, pki):
    old = (datetime.now(timezone.utc) - timedelta(seconds=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert post(gateway, pki, alexa_request("LaunchRequest", ts=old)).status_code == 400


def test_untrusted_or_wrong_certificates_rejected(pki):
    async def check(other: FakePKI, trust: bytes):
        from remote_bridge.alexa import AlexaVerifier

        async def fetch(url):
            return other.chain_pem
        v = AlexaVerifier(trust_store_pem=trust, fetch=fetch)
        payload = alexa_request("LaunchRequest")
        headers, body = signed(other, payload)
        with pytest.raises(AlexaVerificationError):
            await v.verify(headers, body, payload)

    asyncio.run(check(FakePKI(), pki.ca_pem))                         # chains to another root
    wrong_san = FakePKI(san="evil.example.com")
    asyncio.run(check(wrong_san, wrong_san.ca_pem))                   # trusted, wrong SAN
    expired = FakePKI(expired=True)
    asyncio.run(check(expired, expired.ca_pem))                       # trusted, expired


def test_wrong_skill_id_rejected(gateway, pki):
    assert post(gateway, pki, alexa_request("LaunchRequest", skill="amzn1.ask.skill.other")).status_code == 400


def test_unknown_amazon_user_refused(gateway, pki):
    text = say(post(gateway, pki, alexa_request("LaunchRequest", user="amzn1.ask.account.STRANGER")))
    assert "no está autorizada" in text
    assert gateway.gw.audit.tail(5)[-1]["event"] == "alexa_unauthorized_user"


# ------------------------------------------------------------------ voice turns
def test_query_answered_within_budget(gateway, pki):
    with FakeMaxBot(gateway) as bot:
        t0 = time.monotonic()
        r = post(gateway, pki, alexa_request(intent="ConsultaIntent", slots={"consulta": "qué tengo pendiente"}))
        elapsed = time.monotonic() - t0
        text = say(r)
        assert "revisar el ERP" in text and "**" not in text
        assert r.json()["response"]["shouldEndSession"] is False
        assert elapsed < 1.5
        deadline = time.time() + 5          # the fake bot's POST may still be in flight
        while not bot.results and time.time() < deadline:
            time.sleep(0.02)
        assert bot.results[0]["delivered_by_voice"] is True
    m = gateway.gw.alexa.metrics[-1]
    assert m["answered"] is True and m["runner_ms"] == 100 and "answer_ms_since_publish" in m


def test_slow_answer_then_continue(gateway, pki):
    with FakeMaxBot(gateway, delay=2.0) as bot:
        r = post(gateway, pki, alexa_request(intent="ConsultaIntent", slots={"consulta": "algo largo"}))
        assert "Sigo trabajando" in say(r)
        assert r.json()["response"]["shouldEndSession"] is False
        r2 = post(gateway, pki, alexa_request(intent="ContinuarIntent"))
        assert "revisar el ERP" in say(r2)
        deadline = time.time() + 5
        while not bot.results and time.time() < deadline:
            time.sleep(0.02)
        assert bot.results[0]["delivered_by_voice"] is True
        r3 = post(gateway, pki, alexa_request(intent="ContinuarIntent"))
        assert "ninguna respuesta pendiente" in say(r3)


def test_answer_nobody_heard_is_flagged_for_telegram(gateway, pki):
    with FakeMaxBot(gateway, delay=2.0) as bot:
        assert "Sigo trabajando" in say(post(gateway, pki, alexa_request(
            intent="ConsultaIntent", slots={"consulta": "algo largo"})))
        deadline = time.time() + 5
        while not bot.results and time.time() < deadline:
            time.sleep(0.05)
        assert bot.results[0]["delivered_by_voice"] is False


def test_maxbot_down(gateway, pki):
    text = say(post(gateway, pki, alexa_request(intent="ConsultaIntent", slots={"consulta": "hola"})))
    assert "no está disponible" in text


def test_device_status_fast_path(gateway, pki):
    gateway.enroll()
    text = say(post(gateway, pki, alexa_request(intent="EstadoPCIntent")))
    assert "PC de prueba está desconectada" in text
    assert gateway.gw.alexa.metrics[-1]["fast_path"] is True


def test_stop_and_session_end(gateway, pki):
    r = post(gateway, pki, alexa_request(intent="AMAZON.StopIntent"))
    assert r.json()["response"]["shouldEndSession"] is True
    r = post(gateway, pki, alexa_request("SessionEndedRequest"))
    assert r.status_code == 200 and r.json()["response"] == {}


def test_answer_endpoint_rejects_unknown_jobs(gateway):
    r = httpx.post(f"{gateway.internal}/internal/v1/alexa/nope/answer", json={"text": "x"}, headers=MB)
    assert r.status_code == 404


def test_speech_formatting():
    speech, cut = to_speech("# Título\n- uno con [enlace](https://x.y)\n- dos `code` https://a.b/c")
    assert speech == "Título uno con enlace dos code el enlace" and cut is False
    long = ("Una frase bastante larga. " * 200)
    speech, cut = to_speech(long)
    assert cut is True and len(speech) < 1600 and speech.endswith("Telegram.")
