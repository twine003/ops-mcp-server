"""Small building blocks shared by the hub and the Alexa handler.

- ``EventBus``: ordered events that MaxBot long-polls from the internal API
  (approval requests, Alexa turns). Polling instead of pushing keeps MaxBot a
  pure HTTP client: it needs no listening port and no new dependency.
- ``AuditLog``: append-only JSONL. Arguments are never written in clear, only
  their digest and key names; results only as size and outcome.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class EventBus:
    def __init__(self, keep: int = 500):
        self._events: deque[dict] = deque(maxlen=keep)
        self._seq = 0
        self._cond: asyncio.Condition | None = None
        self.last_poll: float = 0.0

    def _condition(self) -> asyncio.Condition:
        # Created lazily so the bus can be built outside a running loop.
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    @property
    def seq(self) -> int:
        return self._seq

    async def publish(self, kind: str, payload: dict[str, Any]) -> dict:
        cond = self._condition()
        async with cond:
            self._seq += 1
            event = {"seq": self._seq, "type": kind, "ts": time.time(), **payload}
            self._events.append(event)
            cond.notify_all()
        return event

    async def wait(self, after: int, timeout: float) -> list[dict]:
        """Events with seq > after; waits up to `timeout` s if there are none yet."""
        self.last_poll = time.monotonic()
        cond = self._condition()
        async with cond:
            if after > self._seq:          # consumer from before a gateway restart
                after = 0
            if not any(e["seq"] > after for e in self._events):
                try:
                    await asyncio.wait_for(
                        cond.wait_for(lambda: any(e["seq"] > after for e in self._events)),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    pass
            self.last_poll = time.monotonic()
            return [e for e in self._events if e["seq"] > after]

    def consumer_alive(self, within: float = 90.0) -> bool:
        return self.last_poll > 0 and (time.monotonic() - self.last_poll) < within


class AuditLog:
    def __init__(self, path: Path | str | None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields: Any) -> None:
        if not self.path:
            return
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def tail(self, n: int = 50) -> list[dict]:
        if not self.path or not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
