"""Tool policy and one-time approvals.

Every tool has a level, decided on BOTH ends of the bridge:

    allow    runs when asked
    confirm  runs only with a fresh human approval for that exact call
    deny     never runs (also the default for anything not listed)

The effective level is the stricter of the gateway's (per device, in the
registry) and the device's own (its local policy file). The device side is the
one that matters most: it lives on the Windows PC, under the owner's control, and
nothing the server says can widen it.

An approval is bound to (device, tool, sha256 of the arguments), is single use
and expires (default 120 s). Approving "take a screenshot" once does not
authorise the next screenshot, nor a screenshot with other arguments.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

LEVELS = ("allow", "confirm", "deny")
_RANK = {"allow": 0, "confirm": 1, "deny": 2}

# Initial, deliberately small surface: status and read-only queries run freely;
# anything that reveals screen content needs a per-call approval; everything
# that acts on the desktop is denied until a specific policy exists for it.
DEFAULT_POLICY: dict[str, str] = {
    "device.ping": "allow",
    "device.status": "allow",
    "desktop.status": "allow",
    "desktop.list_monitors": "allow",
    "desktop.list_windows": "confirm",
    "desktop.screenshot": "confirm",
    "desktop.ui_tree": "confirm",
    "desktop.find_element": "confirm",
    "desktop.read_value": "confirm",
    "desktop.audit_tail": "confirm",
}


def level_for(policy: dict[str, str], tool: str) -> str:
    level = policy.get(tool, "deny")
    return level if level in LEVELS else "deny"


def stricter(a: str, b: str) -> str:
    return a if _RANK[a] >= _RANK[b] else b


def args_digest(arguments: dict[str, Any]) -> str:
    canonical = json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class Approval:
    id: str
    device_id: str
    tool: str
    arguments: dict
    digest: str
    requested_by: str
    created: float
    expires: float
    status: str = "pending"           # pending | approved | denied | expired | executed
    decided_by: str | None = None
    result: dict | None = None
    meta: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {
            "approval_id": self.id,
            "device_id": self.device_id,
            "tool": self.tool,
            "arguments": self.arguments,
            "status": self.status,
            "requested_by": self.requested_by,
            "decided_by": self.decided_by,
            "expires_in": max(0, int(self.expires - time.monotonic())),
            "result": self.result,
        }


class ApprovalStore:
    """In-memory, single-use approvals. Losing them on restart is the safe failure."""

    def __init__(self, ttl_seconds: float = 120.0, keep_seconds: float = 900.0):
        self.ttl = ttl_seconds
        self.keep = keep_seconds
        self._items: dict[str, Approval] = {}

    def _gc(self) -> None:
        now = time.monotonic()
        for a in list(self._items.values()):
            if a.status == "pending" and now > a.expires:
                a.status = "expired"
            if now > a.expires + self.keep:
                self._items.pop(a.id, None)

    def request(self, device_id: str, tool: str, arguments: dict, requested_by: str) -> Approval:
        self._gc()
        now = time.monotonic()
        approval = Approval(
            id=secrets.token_hex(6),
            device_id=device_id,
            tool=tool,
            arguments=arguments,
            digest=args_digest(arguments),
            requested_by=requested_by,
            created=now,
            expires=now + self.ttl,
        )
        self._items[approval.id] = approval
        return approval

    def get(self, approval_id: str) -> Approval | None:
        self._gc()
        return self._items.get(approval_id)

    def decide(self, approval_id: str, approve: bool, decided_by: str) -> Approval | None:
        a = self.get(approval_id)
        if a is None or a.status != "pending":
            return a
        a.status = "approved" if approve else "denied"
        a.decided_by = decided_by
        return a

    def consume(self, approval_id: str, device_id: str, tool: str, arguments: dict) -> Approval | None:
        """Return the approval if it matches this exact call and was approved; marks it used."""
        a = self.get(approval_id)
        if (a is None or a.status != "approved" or a.device_id != device_id
                or a.tool != tool or a.digest != args_digest(arguments)):
            return None
        a.status = "executed"
        return a
