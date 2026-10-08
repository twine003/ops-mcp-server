"""Device registry: who may connect, with which token, and what the server lets it run.

Stored as JSON (default ``/var/lib/alejandro-gateway/devices.json``). Tokens are
never stored, only their SHA-256: a leaked registry does not let anyone connect.

    {
      "devices": {
        "pc-casa": {
          "token_sha256": "…",
          "created_at": "2026-10-08T…Z",
          "revoked_at": null,
          "label": "PC de casa",
          "tools": {"device.status": "allow", "desktop.screenshot": "confirm"}
        }
      }
    }

``tools`` is the SERVER-side policy for that device. The device keeps its own
policy file too, and a call only runs if both sides allow it (see policy.py).

The file is re-read when its mtime changes, so ``admin.py revoke`` takes effect
on a running gateway without a restart: the hub polls ``changed()`` and drops
connections whose device is no longer valid.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from .protocol import validate_device_id

TOKEN_BYTES = 32


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DeviceRegistry:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict = {"devices": {}}
        self._mtime: float | None = None
        self.reload()

    # ------- persistence -------
    def reload(self) -> None:
        with self._lock:
            if not self.path.exists():
                self._data, self._mtime = {"devices": {}}, None
                return
            self._data = json.loads(self.path.read_text(encoding="utf-8")) or {"devices": {}}
            self._data.setdefault("devices", {})
            self._mtime = self.path.stat().st_mtime

    def changed(self) -> bool:
        """True (and reloads) if the file changed on disk since the last read."""
        mtime = self.path.stat().st_mtime if self.path.exists() else None
        if mtime != self._mtime:
            self.reload()
            return True
        return False

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".devices.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, sort_keys=True)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        self._mtime = self.path.stat().st_mtime

    # ------- queries -------
    def get(self, device_id: str) -> dict | None:
        with self._lock:
            entry = self._data["devices"].get(device_id)
            return dict(entry) if entry else None

    def list(self) -> dict[str, dict]:
        with self._lock:
            return {k: {kk: vv for kk, vv in v.items() if kk != "token_sha256"}
                    for k, v in self._data["devices"].items()}

    def authenticate(self, device_id: str, token: str | None) -> bool:
        """Constant-time check; False for unknown, revoked or wrong token."""
        if not token:
            return False
        entry = self.get(device_id)
        expected = entry.get("token_sha256", "") if entry else "0" * 64
        ok = hmac.compare_digest(hash_token(token), expected)
        return bool(entry) and ok and not entry.get("revoked_at")

    def is_active(self, device_id: str) -> bool:
        entry = self.get(device_id)
        return bool(entry) and not entry.get("revoked_at")

    def server_policy(self, device_id: str) -> dict[str, str]:
        entry = self.get(device_id) or {}
        return dict(entry.get("tools") or {})

    # ------- mutations (admin CLI) -------
    def add(self, device_id: str, label: str = "", tools: dict[str, str] | None = None) -> str:
        """Create (or re-key) a device. Returns the plaintext token ONCE."""
        validate_device_id(device_id)
        token = secrets.token_urlsafe(TOKEN_BYTES)
        with self._lock:
            prev = self._data["devices"].get(device_id, {})
            self._data["devices"][device_id] = {
                "token_sha256": hash_token(token),
                "created_at": _now(),
                "revoked_at": None,
                "label": label or prev.get("label", ""),
                "tools": tools if tools is not None else prev.get("tools", {}),
            }
            self._save()
        return token

    def revoke(self, device_id: str) -> bool:
        with self._lock:
            entry = self._data["devices"].get(device_id)
            if not entry:
                return False
            entry["revoked_at"] = _now()
            self._save()
        return True

    def set_tools(self, device_id: str, tools: dict[str, str]) -> None:
        with self._lock:
            entry = self._data["devices"][device_id]
            entry["tools"] = tools
            self._save()
