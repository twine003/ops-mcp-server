"""Linux stand-in for desktop/winapi.py.

Exposes the names the platform-neutral modules use (desktop_server.py,
overlay.py, callout.py, inbox.py, capture.py, monitors.py), implemented on
X11 with EWMH. A "hwnd" here is the X id of a top-level CLIENT window (the one
listed in _NET_CLIENT_LIST), never the window manager's frame.

Coordinates: X has no DPI virtualisation, so everything is in physical pixels
of the X screen, origin at its top-left, exactly like the Win32 side.
"""

from __future__ import annotations

import os
import threading
import time

from . import xconn as x

# ---------------------------------------------------------------- constants used by shared code
WM_APP = 0x8000
WM_CHAR = 0x0102
BM_CLICK = 0x00F5
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WDA_EXCLUDEFROMCAPTURE = 0x11
SW_HIDE, SW_SHOWMAXIMIZED, SW_SHOWNOACTIVATE, SW_MINIMIZE, SW_RESTORE = 0, 3, 4, 6, 9
GA_ROOT = 2
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10
INJECT_MAGIC = 0x434C4D43
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 1, 2, 4, 8, 0x4000


def WNDENUMPROC(fn):  # noqa: N802 - same name as the ctypes prototype on Windows
    return fn


WNDPROC = WNDENUMPROC
HOOKPROC = WNDENUMPROC


class GUITHREADINFO:  # only referenced on code paths that are skipped on Linux
    cbSize = 0
    hwndFocus = 0


def set_dpi_awareness() -> str:
    return "x11 (physical pixels, no DPI virtualisation)"


# ---------------------------------------------------------------- window properties
def _wm_state(h) -> int | None:
    """ICCCM WM_STATE: 0 withdrawn, 1 normal, 3 iconic. None = not a managed client."""
    v = x.prop(h, "WM_STATE")
    return None if v is None else x.first_int(v, 0)


def net_wm_state(h) -> set[str]:
    out = set()
    for a in x.prop_ints(h, "_NET_WM_STATE"):
        try:
            out.add(x.d().get_atom_name(a))
        except Exception:  # noqa: BLE001
            pass
    return out


def window_type(h) -> set[str]:
    out = set()
    for a in x.prop_ints(h, "_NET_WM_WINDOW_TYPE"):
        try:
            out.add(x.d().get_atom_name(a))
        except Exception:  # noqa: BLE001
            pass
    return out


def window_text(h) -> str:
    return x.prop_str(h, "_NET_WM_NAME") or x.prop_str(h, "WM_NAME")


def class_name(h) -> str:
    v = x.prop_str(h, "WM_CLASS")
    parts = [p for p in v.split("\x00") if p]
    return parts[-1] if parts else ""


def window_pid(h) -> int:
    if not h:
        return 0
    return x.first_int(x.prop(h, "_NET_WM_PID"), 0)


def window_thread(h) -> int:
    return 0


def process_name(pid: int) -> str:
    if not pid:
        return ""
    try:
        return os.path.basename(os.readlink(f"/proc/{int(pid)}/exe"))
    except OSError:
        try:
            with open(f"/proc/{int(pid)}/comm", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""


def frame_extents(h) -> tuple[int, int, int, int]:
    v = x.prop_ints(h, "_NET_FRAME_EXTENTS")
    return tuple(v[:4]) if len(v) >= 4 else (0, 0, 0, 0)  # left, right, top, bottom


def client_rect(h) -> tuple[int, int, int, int]:
    """Client area in screen coords. python-xlib: dst.translate_coords(src, x, y) maps a point
    of src INTO dst - so it is called on the ROOT (inverted, every window lands off-screen)."""
    wo = x.win(h)
    g = wo.get_geometry()
    t = x.root().translate_coords(wo, 0, 0)
    return (int(t.x), int(t.y), int(t.x) + int(g.width), int(t.y) + int(g.height))


def window_rect(h) -> tuple[int, int, int, int]:
    """Like GetWindowRect: the client plus the window manager decorations."""
    if not h:
        return (0, 0, 0, 0)
    try:
        l, t, r, b = client_rect(h)
    except Exception:  # noqa: BLE001
        return (0, 0, 0, 0)
    el, er, et, eb = frame_extents(h)
    return (l - el, t - et, r + er, b + eb)


def is_cloaked(h) -> bool:
    return False


def is_window(h) -> bool:
    try:
        x.win(h).get_geometry()
        return True
    except Exception:  # noqa: BLE001
        return False


def is_iconic(h) -> bool:
    return _wm_state(h) == 3 or "_NET_WM_STATE_HIDDEN" in net_wm_state(h)


def is_zoomed(h) -> bool:
    s = net_wm_state(h)
    return "_NET_WM_STATE_MAXIMIZED_VERT" in s and "_NET_WM_STATE_MAXIMIZED_HORZ" in s


def toplevel_of(h) -> int:
    """Top-level client that contains window h (the ancestor carrying WM_STATE)."""
    if not h:
        return 0
    try:
        cur = int(h)
        r = int(x.root().id)
        best = cur
        for _ in range(16):
            if _wm_state(cur) is not None:
                return cur
            q = x.win(cur).query_tree()
            parent = int(q.parent.id) if q.parent else r
            if parent == r or parent == 0:
                return best
            best = cur = parent
        return best
    except Exception:  # noqa: BLE001
        return int(h)


def client_list_stacking() -> list[int]:
    """Managed top-level clients, BOTTOM first (EWMH order)."""
    v = x.prop_ints(x.root().id, "_NET_CLIENT_LIST_STACKING") or x.prop_ints(x.root().id, "_NET_CLIENT_LIST")
    return v


def active_window() -> int:
    return x.first_int(x.prop(x.root().id, "_NET_ACTIVE_WINDOW"), 0)


def cursor_pos() -> tuple[int, int]:
    p = x.root().query_pointer()
    return (int(p.root_x), int(p.root_y))


def pointer_mask() -> int:
    return int(x.root().query_pointer().mask)


def input_desktop() -> str:
    """'Default' when the X session is up and unlocked (same contract as Win32 OpenInputDesktop)."""
    try:
        x.d()
    except Exception:  # noqa: BLE001
        return ""
    if screen_locked():
        return "Winlogon"
    return "Default"


def screen_locked() -> bool:
    """A screen locker holds a full-screen override-redirect window: detect the usual ones by name."""
    try:
        for c in x.root().query_tree().children:
            if c.get_attributes().map_state != x.X.IsViewable:
                continue
            nm = (x.prop_str(c.id, "WM_NAME") + " " + x.prop_str(c.id, "WM_CLASS")).lower()
            if any(k in nm for k in ("xscreensaver", "light-locker", "xfce4-screensaver", "i3lock", "xsecurelock")):
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


# ---------------------------------------------------------------- EWMH actions
def _net_wm_state_change(h, action: int, *names: str) -> None:
    atoms = [x.atom(n) for n in names] + [0, 0]
    x.send_client_message(h, "_NET_WM_STATE", [action, atoms[0], atoms[1], 2, 0])


def activate(h) -> None:
    if is_iconic(h):
        x.win(h).map()
    # source indication 2 = pager/tool: window managers honour it without focus-stealing prevention
    x.send_client_message(h, "_NET_ACTIVE_WINDOW", [2, 0, 0, 0, 0])


def show_window(h, cmd: int) -> int:
    if cmd == SW_SHOWMAXIMIZED:
        if is_iconic(h):
            x.win(h).map()
        _net_wm_state_change(h, 1, "_NET_WM_STATE_MAXIMIZED_VERT", "_NET_WM_STATE_MAXIMIZED_HORZ")
    elif cmd == SW_RESTORE:
        if is_iconic(h):
            x.win(h).map()
            activate(h)
        _net_wm_state_change(h, 0, "_NET_WM_STATE_MAXIMIZED_VERT", "_NET_WM_STATE_MAXIMIZED_HORZ")
    elif cmd == SW_MINIMIZE:
        from Xlib.protocol import event
        ev = event.ClientMessage(window=x.win(h), client_type=x.atom("WM_CHANGE_STATE"), data=(32, [3, 0, 0, 0, 0]))
        x.root().send_event(ev, event_mask=x.X.SubstructureRedirectMask | x.X.SubstructureNotifyMask)
    elif cmd == SW_HIDE:
        x.win(h).unmap()
    else:
        x.win(h).map()
    x.d().flush()
    return 1


def move_resize(h, left: int, top: int, width: int, height: int) -> None:
    """Outer rect (decorations included) like SetWindowPos: the client gets the rest."""
    el, er, et, eb = frame_extents(h)
    cw, ch = max(1, int(width) - el - er), max(1, int(height) - et - eb)
    # _NET_MOVERESIZE_WINDOW: gravity 1 (NorthWest) + x,y,w,h flags, source 2. x/y = frame origin.
    flags = 1 | (1 << 8) | (1 << 9) | (1 << 10) | (1 << 11) | (2 << 12)
    x.send_client_message(h, "_NET_MOVERESIZE_WINDOW", [flags, int(left), int(top), cw, ch])


def set_title(h, text: str) -> None:
    try:
        wo = x.win(h)
        wo.change_property(x.atom("_NET_WM_NAME"), x.atom("UTF8_STRING"), 8, text.encode("utf-8"))
        wo.set_wm_name(text)
        wo.change_property(x.atom("_NET_WM_PID"), x.Xatom.CARDINAL, 32, [os.getpid()])
        x.d().flush()
    except Exception:  # noqa: BLE001
        pass


def raise_window(h) -> None:
    try:
        x.win(h).configure(stack_mode=x.X.Above)
        x.d().flush()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- timers (overlay loop)
_TIMERS: dict[tuple[int, int], tuple[float, float]] = {}
_TLOCK = threading.Lock()


def timers_due(now: float | None = None) -> list[tuple[int, int]]:
    now = time.monotonic() if now is None else now
    out = []
    with _TLOCK:
        for k, (interval, due) in list(_TIMERS.items()):
            if now >= due:
                out.append(k)
                _TIMERS[k] = (interval, now + interval)
    return out


def timers_next_delay(default: float = 0.05) -> float:
    with _TLOCK:
        if not _TIMERS:
            return default
        return max(0.0, min(due for _, due in _TIMERS.values()) - time.monotonic())


# ---------------------------------------------------------------- user32 / kernel32 look-alikes
class _User32:
    """The handful of user32 calls the shared modules make, on X11."""

    def IsIconic(self, h):
        return int(bool(h) and is_iconic(h))

    def IsZoomed(self, h):
        return int(bool(h) and is_zoomed(h))

    def IsWindow(self, h):
        return int(bool(h) and is_window(h))

    def IsWindowVisible(self, h):
        if not h:
            return 0
        st = _wm_state(h)
        if st is not None:
            return int(st in (1, 3))
        try:
            return int(x.win(h).get_attributes().map_state == x.X.IsViewable)
        except Exception:  # noqa: BLE001
            return 0

    def IsWindowEnabled(self, h):
        return 1

    def GetForegroundWindow(self):
        return active_window()

    def SetForegroundWindow(self, h):
        activate(h)
        return 1

    def ShowWindow(self, h, cmd):
        return show_window(h, cmd)

    def GetWindowLongW(self, h, idx):
        return 0

    def GetGUIThreadInfo(self, tid, ref):
        return 0

    def GetAsyncKeyState(self, vk):
        bit = {1: x.X.Button1Mask, 2: x.X.Button3Mask, 4: x.X.Button2Mask}.get(int(vk))
        return 0x8000 if bit and pointer_mask() & bit else 0

    def EnumWindows(self, cb, lparam):
        for h in reversed(client_list_stacking()):   # top of the z-order first, like Win32
            if cb(h, lparam) is False:
                break
        return 1

    def EnumChildWindows(self, parent, cb, lparam):
        return 1  # X toolkits draw their controls themselves: AT-SPI (x11/uia.py) is the way in

    def PostMessageW(self, *a):
        return 0

    def SetWindowDisplayAffinity(self, h, aff):
        return 0  # X has no capture exclusion: overlays are always in captures (like remote_visible)

    def GetMessageExtraInfo(self):
        return 0

    def GetAncestor(self, h, flag):
        return toplevel_of(h)

    def SetWindowTextW(self, h, text):
        set_title(h, text)
        return 1

    def SetWindowPos(self, h, after, xx, yy, cx, cy, flags):
        if not flags & SWP_NOMOVE or not flags & SWP_NOSIZE:
            move_resize(h, xx, yy, cx, cy)
        if after == HWND_TOPMOST:
            raise_window(h)
        return 1

    def SetCursorPos(self, xx, yy):
        x.root().warp_pointer(int(xx), int(yy))
        x.d().sync()
        return 1

    def SetTimer(self, h, tid, ms, fn):
        with _TLOCK:
            _TIMERS[(int(h or 0), int(tid))] = (max(0.001, ms / 1000.0), time.monotonic() + ms / 1000.0)
        return tid

    def KillTimer(self, h, tid):
        with _TLOCK:
            _TIMERS.pop((int(h or 0), int(tid)), None)
        return 1


class _Kernel32:
    def GetModuleHandleW(self, name):
        return 0

    def GetCurrentThreadId(self):
        return threading.get_ident()


user32 = _User32()
kernel32 = _Kernel32()


# ---------------------------------------------------------------- monitors
def work_area() -> tuple[int, int, int, int] | None:
    v = x.prop_ints(x.root().id, "_NET_WORKAREA")
    if len(v) >= 4:
        return (v[0], v[1], v[0] + v[2], v[1] + v[3])
    return None


def monitor_list() -> list[dict]:
    """[{index, device, name, rect, primary}] from RandR 1.5 monitors, or the whole screen."""
    dpy = x.d()
    out = []
    try:
        if dpy.has_extension("RANDR"):
            r = dpy.xrandr_get_monitors(x.root().id, is_active=True)
            for i, m in enumerate(r.monitors, start=1):
                try:
                    name = dpy.get_atom_name(m.name)
                except Exception:  # noqa: BLE001
                    name = f"monitor{i}"
                out.append({"index": i, "device": name, "name": name,
                            "rect": (int(m.x), int(m.y), int(m.x) + int(m.width_in_pixels),
                                     int(m.y) + int(m.height_in_pixels)),
                            "primary": bool(m.primary)})
    except Exception:  # noqa: BLE001
        out = []
    if not out:
        s = dpy.screen()
        out = [{"index": 1, "device": "screen0", "name": "X screen",
                "rect": (0, 0, int(s.width_in_pixels), int(s.height_in_pixels)), "primary": True}]
    if not any(m["primary"] for m in out):
        out[0]["primary"] = True
    return out
