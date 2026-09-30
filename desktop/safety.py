"""Configuration, allow/deny policy, stop state and the audit log."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.path.expandvars(r"%LOCALAPPDATA%\desktop-mcp"))


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    cfg = json.loads((PACKAGE_DIR / "config.json").read_text(encoding="utf-8"))
    user_file = DATA_DIR / "config.json"
    if user_file.exists():
        cfg = _merge(cfg, json.loads(user_file.read_text(encoding="utf-8")))
    return cfg


class PolicyError(PermissionError):
    """Raised when an action is refused by the allow/deny policy."""


class Policy:
    def __init__(self, cfg: dict):
        self.reload(cfg)

    def reload(self, cfg: dict) -> None:
        self.allow = {p.lower() for p in cfg.get("allow_write_processes", [])}
        self.deny = {p.lower() for p in cfg.get("deny_processes", [])}
        self.deny_title = [re.compile(r) for r in cfg.get("deny_window_title_regex", [])]

    def is_denied(self, process: str, title: str = "") -> bool:
        if process and process.lower() in self.deny:
            return True
        return any(r.search(title or "") for r in self.deny_title)

    def check_read(self, process: str, title: str = "") -> None:
        if self.is_denied(process, title):
            raise PolicyError(f"'{process}' / '{title}' is in the deny list: not even read access.")

    def write_allowed(self, process: str, title: str = "") -> bool:
        """'*' in allow_write_processes = every process not in the deny list."""
        if not process or self.is_denied(process, title):
            return False
        return "*" in self.allow or process.lower() in self.allow

    def check_write(self, process: str, title: str = "") -> None:
        self.check_read(process, title)
        if not self.write_allowed(process, title):
            raise PolicyError(
                f"Write refused: process '{process or '?'}' is not in allow_write_processes "
                f"({sorted(self.allow)}). Read-only mode for everything else.")

    def describe(self) -> dict:
        return {"write_allowed_in": sorted(self.allow), "denied": sorted(self.deny),
                "denied_title_regex": [r.pattern for r in self.deny_title]}


class StopState:
    """Global stop flag, set by the on-screen button, the hotkey or pause().

    Every long action polls `stopped` between steps; the gesture code also
    releases any pressed button/key when it sees it.
    """

    def __init__(self):
        self._event = threading.Event()
        self.reason = ""
        self.since = 0.0
        self._listeners = []

    @property
    def stopped(self) -> bool:
        return self._event.is_set()

    def stop(self, reason: str) -> None:
        if not self._event.is_set():
            self.reason, self.since = reason, time.time()
            self._event.set()
            for cb in list(self._listeners):
                try:
                    cb(True, reason)
                except Exception:
                    pass

    def resume(self, reason: str = "resume") -> None:
        if self._event.is_set():
            self._event.clear()
            self.reason, self.since = "", time.time()
            for cb in list(self._listeners):
                try:
                    cb(False, reason)
                except Exception:
                    pass

    def on_change(self, cb) -> None:
        self._listeners.append(cb)


class StoppedError(RuntimeError):
    """The user pressed stop (button / hotkey) or pause() was called."""


class AuditLog:
    """Append-only JSONL. One line per action, flushed immediately."""

    def __init__(self, cfg: dict):
        a = cfg.get("audit", {})
        self.path = Path(os.path.expandvars(a.get("path", str(DATA_DIR / "audit.jsonl"))))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.thumbs = bool(a.get("thumbnails", False))
        self.thumb_dir = Path(os.path.expandvars(a.get("thumbnail_dir", str(DATA_DIR / "audit_thumbs"))))
        self.thumb_max = int(a.get("thumbnail_max_px", 320))
        self._lock = threading.Lock()

    def write(self, record: dict) -> None:
        rec = {"ts": datetime.now().isoformat(timespec="milliseconds"), **record}
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self._lock:
            # "a" = O_APPEND: never rewrites earlier lines.
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def save_thumb(self, image, tag: str) -> str | None:
        if not self.thumbs or image is None:
            return None
        self.thumb_dir.mkdir(parents=True, exist_ok=True)
        im = image.copy()
        im.thumbnail((self.thumb_max, self.thumb_max))
        name = f"{datetime.now():%Y%m%d_%H%M%S_%f}_{tag}.jpg"
        im.convert("RGB").save(self.thumb_dir / name, quality=60)
        return str(self.thumb_dir / name)

    def tail(self, n: int = 20) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        return [json.loads(l) for l in lines if l.strip()]
