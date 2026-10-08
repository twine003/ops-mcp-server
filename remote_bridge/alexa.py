"""Alexa Custom Skill endpoint: request verification + voice turns.

Verification follows "Host a Custom Skill as a Web Service" (Amazon docs):
  1. SignatureCertChainUrl: https, host s3.amazonaws.com, path under /echo.api/,
     port 443 if present (checked on the normalised path).
  2. The certificate chain chains to a trusted root, is in its validity window,
     and the leaf has echo-api.amazon.com in its SANs.
  3. Signature-256 = RSA/SHA-256 over the raw body, verified with the leaf key.
  4. request.timestamp within 150 s of now.
Then this gateway adds its own authorisation:
  5. the skill id (applicationId) must be ours;
  6. the Amazon user id must be on the allow-list.

Why the voice flow looks the way it does
----------------------------------------
Alexa waits ~8 s for a response, and progressive responses do NOT extend that
window. A MaxBot turn (CLI start + model + tools) often takes longer. So:

  - the query is published to MaxBot and we wait up to ``budget`` seconds;
  - if the answer is ready, Alexa speaks it and keeps the session open;
  - if not, Alexa says it is still working and keeps the session open; the user
    says "continúa" (or "sí") and we wait another budget for the same answer;
  - if the user walks away, MaxBot is told the answer was not heard and sends it
    to Telegram instead, so nothing is lost.

A traditional Custom Skill is request/response: there is no continuous two-way
audio, no barge-in into our audio stream, and no way to speak unprompted later.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import posixpath
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from .events import AuditLog, EventBus

log = logging.getLogger("remote_bridge.alexa")

TIMESTAMP_TOLERANCE = 150
ECHO_SAN = "echo-api.amazon.com"
MAX_SPEECH_CHARS = 1500           # Alexa allows 8000; long monologues are bad UX


class AlexaVerificationError(Exception):
    pass


# --------------------------------------------------------------------- verify
def check_cert_url(url: str) -> None:
    p = urlparse(url)
    if (p.scheme or "").lower() != "https":
        raise AlexaVerificationError("cert url: scheme must be https")
    if (p.hostname or "").lower() != "s3.amazonaws.com":
        raise AlexaVerificationError("cert url: host must be s3.amazonaws.com")
    path = posixpath.normpath(p.path or "")
    if not path.startswith("/echo.api/"):
        raise AlexaVerificationError("cert url: path must start with /echo.api/")
    if p.port not in (None, 443):
        raise AlexaVerificationError("cert url: port must be 443")


class AlexaVerifier:
    """Verifies Alexa request signatures. Certificates are cached per URL."""

    def __init__(
        self,
        *,
        trust_store_pem: bytes | None = None,
        fetch: Callable[[str], Awaitable[bytes]] | None = None,
        clock: Callable[[], float] = time.time,
        skip_cert_url_check: bool = False,
    ):
        self._trust_pem = trust_store_pem
        self._fetch = fetch or _http_fetch
        self._clock = clock
        self._skip_url_check = skip_cert_url_check   # tests only
        self._cache: dict[str, tuple[float, Any]] = {}

    def _store(self):
        from cryptography import x509
        from cryptography.x509.verification import Store
        pem = self._trust_pem
        if pem is None:
            import certifi
            with open(certifi.where(), "rb") as f:
                pem = f.read()
        return Store(x509.load_pem_x509_certificates(pem))

    async def _leaf_for(self, url: str):
        from cryptography import x509
        from cryptography.x509.verification import PolicyBuilder
        now = self._clock()
        hit = self._cache.get(url)
        if hit and hit[0] > now:
            return hit[1]
        pem = await self._fetch(url)
        certs = x509.load_pem_x509_certificates(pem)
        if not certs:
            raise AlexaVerificationError("empty certificate chain")
        leaf, intermediates = certs[0], certs[1:]
        verifier = (
            PolicyBuilder()
            .store(self._store())
            .time(datetime.fromtimestamp(now, tz=timezone.utc))
            .build_server_verifier(x509.DNSName(ECHO_SAN))
        )
        try:
            verifier.verify(leaf, intermediates)   # chain, validity, SAN, EKU
        except Exception as e:
            raise AlexaVerificationError(f"certificate rejected: {e}") from e
        expires = leaf.not_valid_after_utc.timestamp()
        self._cache[url] = (min(expires, now + 3600), leaf)
        return leaf

    async def verify(self, headers: dict[str, str], body: bytes, request_json: dict) -> None:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        lower = {k.lower(): v for k, v in headers.items()}
        url = lower.get("signaturecertchainurl")
        sig = lower.get("signature-256")
        if not url or not sig:
            raise AlexaVerificationError("missing signature headers")
        if not self._skip_url_check:
            check_cert_url(url)
        leaf = await self._leaf_for(url)
        try:
            leaf.public_key().verify(base64.b64decode(sig), body, padding.PKCS1v15(), hashes.SHA256())
        except Exception as e:
            raise AlexaVerificationError("bad signature") from e

        ts = (request_json.get("request") or {}).get("timestamp")
        try:
            sent = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
        except ValueError as e:
            raise AlexaVerificationError("bad timestamp") from e
        if abs(self._clock() - sent) > TIMESTAMP_TOLERANCE:
            raise AlexaVerificationError("timestamp outside tolerance")


async def _http_fetch(url: str) -> bytes:
    import httpx
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
        r = await client.get(url)
        r.raise_for_status()
        if len(r.content) > 64 * 1024:
            raise AlexaVerificationError("certificate chain too large")
        return r.content


# --------------------------------------------------------------------- speech
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL = re.compile(r"https?://\S+")
_CODE = re.compile(r"```.*?```", re.S)


def to_speech(text: str, limit: int = MAX_SPEECH_CHARS) -> tuple[str, bool]:
    """Markdown/chat text -> something a speaker can say. Returns (speech, truncated)."""
    t = _CODE.sub(" (te dejé el código por escrito) ", text or "")
    t = _MD_LINK.sub(r"\1", t)
    t = _URL.sub("el enlace", t)
    t = re.sub(r"[`*_#>|~]+", "", t)
    t = re.sub(r"^\s*[-•]\s+", "", t, flags=re.M)
    t = re.sub(r"\s+", " ", t).strip()
    t = t.replace("&", " y ").replace("<", " ").replace(">", " ")
    if len(t) <= limit:
        return t, False
    cut = t.rfind(". ", 0, limit)
    cut = cut + 1 if cut > limit // 2 else limit
    return t[:cut].rstrip() + " El resto te lo mando por Telegram.", True


def speak(text: str, *, end: bool, reprompt: str | None = None) -> dict:
    resp: dict[str, Any] = {
        "outputSpeech": {"type": "PlainText", "text": text},
        "shouldEndSession": end,
    }
    if reprompt and not end:
        resp["reprompt"] = {"outputSpeech": {"type": "PlainText", "text": reprompt}}
    return {"version": "1.0", "response": resp}


# --------------------------------------------------------------------- turns
@dataclass
class VoiceJob:
    id: str
    user_id: str
    text: str
    created: float = field(default_factory=time.monotonic)
    future: asyncio.Future | None = None
    waiters: int = 0
    answer: str | None = None
    error: bool = False
    timings: dict = field(default_factory=dict)


@dataclass
class AlexaSettings:
    skill_id: str
    allowed_user_ids: set[str]
    budget_seconds: float = 6.0
    progressive: bool = True
    enroll: bool = False          # answer unknown users with their id in the log
    assistant_name: str = "Alejandro"


class AlexaSkill:
    def __init__(
        self,
        settings: AlexaSettings,
        verifier: AlexaVerifier,
        events: EventBus,
        *,
        audit: AuditLog | None = None,
        device_summary: Callable[[], str] | None = None,
        progressive_sender: Callable[[dict, str], Awaitable[None]] | None = None,
    ):
        self.s = settings
        self.verifier = verifier
        self.events = events
        self.audit = audit or AuditLog(None)
        self.device_summary = device_summary or (lambda: "No tengo dispositivos registrados.")
        self.progressive_sender = progressive_sender or send_progressive
        self.jobs: dict[str, VoiceJob] = {}
        self.pending_by_user: dict[str, str] = {}
        self.metrics: list[dict] = []

    # ---- entry point
    async def handle(self, headers: dict[str, str], body: bytes) -> tuple[int, dict]:
        t0 = time.monotonic()
        import json
        try:
            req = json.loads(body)
        except ValueError:
            return 400, {"error": "bad json"}
        try:
            await self.verifier.verify(headers, body, req)
        except AlexaVerificationError as e:
            self.audit.write("alexa_rejected", reason=str(e))
            log.warning("alexa request rejected: %s", e)
            return 400, {"error": "verification failed"}
        t_verified = time.monotonic()

        system = (req.get("context") or {}).get("System") or {}
        app_id = ((system.get("application") or {}).get("applicationId")
                  or ((req.get("session") or {}).get("application") or {}).get("applicationId"))
        if app_id != self.s.skill_id:
            self.audit.write("alexa_rejected", reason="wrong skill id")
            return 400, {"error": "wrong skill"}
        user_id = ((system.get("user") or {}).get("userId")
                   or ((req.get("session") or {}).get("user") or {}).get("userId") or "")
        request = req.get("request") or {}
        rtype = request.get("type", "")

        if user_id not in self.s.allowed_user_ids:
            self.audit.write("alexa_unauthorized_user", user_id=user_id if self.s.enroll else user_id[-12:])
            if rtype == "SessionEndedRequest":
                return 200, {"version": "1.0", "response": {}}
            return 200, speak("Esta cuenta de Alexa no está autorizada para hablar con "
                              f"{self.s.assistant_name}.", end=True)

        try:
            response, meta = await self._route(req, request, rtype, user_id, system)
        except Exception:
            log.exception("alexa handler failed")
            response, meta = speak("Tuve un problema procesando eso. Inténtalo de nuevo.", end=True), {}

        total = time.monotonic() - t0
        metric = {
            "ts": time.time(),
            "request_type": rtype,
            "intent": (request.get("intent") or {}).get("name"),
            "verify_ms": int((t_verified - t0) * 1000),
            "total_ms": int(total * 1000),
            **meta,
        }
        self.metrics = (self.metrics + [metric])[-200:]
        self.audit.write("alexa_turn", **metric)
        return 200, response

    async def _route(self, req, request, rtype, user_id, system) -> tuple[dict, dict]:
        name = self.s.assistant_name
        if rtype == "LaunchRequest":
            return speak(f"Hola, soy {name}. ¿En qué te ayudo?", end=False,
                         reprompt="Dime qué necesitas."), {}
        if rtype == "SessionEndedRequest":
            return {"version": "1.0", "response": {}}, {}
        if rtype != "IntentRequest":
            return speak("No entendí esa solicitud.", end=True), {}

        intent = request.get("intent") or {}
        iname = intent.get("name", "")
        if iname in ("AMAZON.StopIntent", "AMAZON.CancelIntent", "AMAZON.NoIntent"):
            return speak("Hasta luego.", end=True), {}
        if iname == "AMAZON.HelpIntent":
            return speak(f"Puedes pedirme cualquier cosa, por ejemplo: pregúntale a {name} "
                         "qué tengo pendiente. Si tardo, di continúa.", end=False,
                         reprompt="¿Qué necesitas?"), {}
        if iname == "EstadoPCIntent":
            return speak(self.device_summary(), end=False, reprompt="¿Algo más?"), {"fast_path": True}
        if iname in ("ContinuarIntent", "AMAZON.YesIntent"):
            job_id = self.pending_by_user.get(user_id)
            if not job_id or job_id not in self.jobs:
                return speak("No tengo ninguna respuesta pendiente. ¿Qué necesitas?", end=False,
                             reprompt="Dime qué necesitas."), {}
            return await self._wait_and_speak(self.jobs[job_id], user_id, req, system, continued=True)
        if iname in ("ConsultaIntent", "AMAZON.FallbackIntent"):
            slot = ((intent.get("slots") or {}).get("consulta") or {}).get("value")
            if not slot:
                return speak("¿Qué quieres preguntarme?", end=False, reprompt="Dime tu pregunta."), {}
            if not self.events.consumer_alive():
                return speak(f"{name} no está disponible en este momento. "
                             "Inténtalo más tarde o escríbeme por Telegram.", end=True), {"maxbot": "down"}
            job = VoiceJob(id=str(uuid.uuid4()), user_id=user_id, text=slot)
            job.future = asyncio.get_running_loop().create_future()
            self.jobs[job.id] = job
            self.pending_by_user[user_id] = job.id
            self._gc_jobs()
            job.timings["published_at"] = time.monotonic()
            await self.events.publish("alexa_turn", {"job_id": job.id, "text": slot,
                                                     "user": user_id[-12:]})
            if self.s.progressive:
                asyncio.create_task(self._progressive(req, system))
            return await self._wait_and_speak(job, user_id, req, system, continued=False)
        return speak("Eso todavía no lo sé hacer.", end=False, reprompt="¿Algo más?"), {}

    async def _progressive(self, req, system) -> None:
        try:
            await self.progressive_sender({"req": req, "system": system}, "Déjame pensarlo.")
        except Exception as e:
            log.info("progressive response failed: %s", e)

    async def _wait_and_speak(self, job: VoiceJob, user_id, req, system, *, continued: bool):
        assert job.future is not None
        job.waiters += 1
        try:
            await asyncio.wait_for(asyncio.shield(job.future), timeout=self.s.budget_seconds)
        except asyncio.TimeoutError:
            pass
        finally:
            job.waiters -= 1
        meta = {"job_id": job.id[:8], "continued": continued}
        if job.future.done():
            self.pending_by_user.pop(user_id, None)
            speech, truncated = to_speech(job.answer or "No obtuve respuesta.")
            meta.update(job.timings, answered=True, truncated=truncated)
            meta.pop("published_at", None)
            return speak(speech, end=False, reprompt="¿Algo más?"), meta
        meta["answered"] = False
        return speak("Sigo trabajando en eso. Di continúa en unos segundos para escuchar la respuesta.",
                     end=False, reprompt="Di continúa para escuchar la respuesta."), meta

    def _gc_jobs(self) -> None:
        now = time.monotonic()
        for jid, job in list(self.jobs.items()):
            if now - job.created > 1800:
                self.jobs.pop(jid, None)
                if self.pending_by_user.get(job.user_id) == jid:
                    self.pending_by_user.pop(job.user_id, None)

    # ---- called by MaxBot through the internal API
    def deliver(self, job_id: str, text: str, *, error: bool = False,
                runner_ms: int | None = None) -> dict | None:
        """Store MaxBot's answer. Returns whether someone was listening for it."""
        job = self.jobs.get(job_id)
        if job is None or job.future is None:
            return None
        if job.future.done():
            return {"delivered_by_voice": False, "duplicate": True}
        job.answer, job.error = text, error
        published = job.timings.get("published_at", job.created)
        job.timings["answer_ms_since_publish"] = int((time.monotonic() - published) * 1000)
        if runner_ms is not None:
            job.timings["runner_ms"] = runner_ms
        listening = job.waiters > 0
        job.future.set_result(text)
        _, truncated = to_speech(text)
        self.audit.write("alexa_answer", job_id=job_id[:8], listening=listening,
                         truncated=truncated, **{k: v for k, v in job.timings.items()
                                                 if k != "published_at"})
        return {"delivered_by_voice": listening, "truncated": truncated}


async def send_progressive(ctx: dict, text: str) -> None:
    """Alexa Progressive Response API (VoicePlayer.Speak). Best effort."""
    import httpx
    system = ctx["system"]
    req = ctx["req"]
    endpoint = system.get("apiEndpoint")
    token = system.get("apiAccessToken")
    request_id = (req.get("request") or {}).get("requestId")
    if not (endpoint and token and request_id):
        return
    payload = {"header": {"requestId": request_id},
               "directive": {"type": "VoicePlayer.Speak", "speech": text}}
    async with httpx.AsyncClient(timeout=2.0) as client:
        await client.post(f"{endpoint}/v1/directives", json=payload,
                          headers={"Authorization": f"Bearer {token}"})
