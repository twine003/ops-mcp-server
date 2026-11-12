"""X11 connection helpers (python-xlib).

python-xlib Display objects are NOT thread safe: every thread gets its own
connection (thread-local). The automation thread, the overlay thread, the
input guard and the Tk callout thread never share one.
"""

from __future__ import annotations

import os
import threading

from Xlib import X, Xatom, display as xdisplay
from Xlib.error import BadWindow, XError

_local = threading.local()


def d() -> xdisplay.Display:
    """This thread's X connection (DISPLAY, default :0)."""
    dpy = getattr(_local, "dpy", None)
    if dpy is None:
        dpy = xdisplay.Display(os.environ.get("DISPLAY") or ":0")
        _local.dpy = dpy
        _local.atoms = {}
    return dpy


def root():
    return d().screen().root


def atom(name: str) -> int:
    cache = getattr(_local, "atoms", None)
    if cache is None:
        d()
        cache = _local.atoms
    a = cache.get(name)
    if a is None:
        a = cache[name] = d().intern_atom(name)
    return a


def win(wid: int):
    return d().create_resource_object("window", int(wid))


def prop(wid, name: str, ptype=X.AnyPropertyType):
    """Property value (list/bytes) or None. Never raises on a dead window."""
    try:
        r = win(wid).get_full_property(atom(name), ptype)
    except (BadWindow, XError, Exception):  # noqa: BLE001
        return None
    return None if r is None else r.value


def prop_ints(wid, name: str) -> list[int]:
    v = prop(wid, name)
    if v is None:
        return []
    try:
        return [int(x) for x in v]
    except TypeError:
        return []


def first_int(v, default: int = 0) -> int:
    """python-xlib returns array.array / tuples: int() on them fails (bug #2 of the first port)."""
    if v is None:
        return default
    try:
        return int(v[0]) if len(v) else default
    except TypeError:
        try:
            return int(v)
        except (TypeError, ValueError):
            return default


def prop_str(wid, name: str) -> str:
    v = prop(wid, name)
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace").rstrip("\x00")
    if isinstance(v, str):
        return v
    try:
        return bytes(v).decode("utf-8", "replace").rstrip("\x00")
    except Exception:  # noqa: BLE001
        return ""


def send_client_message(wid: int, type_name: str, data: list[int], mask=None) -> None:
    from Xlib.protocol import event
    data = (list(data) + [0, 0, 0, 0, 0])[:5]
    ev = event.ClientMessage(window=win(wid), client_type=atom(type_name), data=(32, data))
    root().send_event(ev, event_mask=mask or (X.SubstructureRedirectMask | X.SubstructureNotifyMask))
    d().flush()


def has_compositor() -> bool:
    try:
        sel = d().get_selection_owner(atom(f"_NET_WM_CM_S{d().get_default_screen()}"))
        return bool(sel) and int(getattr(sel, "id", sel) or 0) != 0
    except Exception:  # noqa: BLE001
        return False


__all__ = ["d", "root", "atom", "win", "prop", "prop_ints", "prop_str", "first_int",
           "send_client_message", "has_compositor", "X", "Xatom", "BadWindow", "XError"]
