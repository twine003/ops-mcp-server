"""Keyboard/mouse input: Win32 messages (no cursor) and SendInput (real cursor).

InputGuard owns a dedicated thread with its own message loop for:
  - the stop hotkey (RegisterHotKey), always active;
  - OPTIONAL (off by default, input.freeze_user_mouse_during_gestures or the
    per-call freeze_user_mouse): a low-level mouse hook installed only while a
    SendInput gesture runs, swallowing the user's PHYSICAL mouse events so
    their movement can't corrupt a stroke. Our own events carry
    dwExtraInfo=INJECT_MAGIC and pass. Off, the user keeps full control and
    Claude simply shares the single system cursor for the 1-3 s of a gesture.
The hook runs on its own thread (not the overlay/render thread) because
Windows silently drops a low-level hook whose callback is slow.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
import time

from . import winapi as w
from .monitors import densify_path, to_absolute_input
from .safety import StoppedError, StopState

log = logging.getLogger("desktop-mcp.input")

# ---------------------------------------------------------------- key names
VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "shift": 0x10, "ctrl": 0x11,
    "control": 0x11, "alt": 0x12, "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22, "end": 0x23,
    "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "printscreen": 0x2C,
    "insert": 0x2D, "ins": 0x2D, "delete": 0x2E, "del": 0x2E, "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C,
    "apps": 0x5D, "menu": 0x5D, "numlock": 0x90, "scrolllock": 0x91,
    "plus": 0xBB, "minus": 0xBD, "comma": 0xBC, "period": 0xBE,
}
VK.update({f"f{i}": 0x6F + i for i in range(1, 25)})
EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D, 0x90}
MODS = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B}
HOTKEY_MODS = {"ctrl": w.MOD_CONTROL, "control": w.MOD_CONTROL, "shift": w.MOD_SHIFT,
               "alt": w.MOD_ALT, "win": w.MOD_WIN}


def key_vk(name: str) -> int:
    n = name.strip().lower()
    if n in VK:
        return VK[n]
    if len(n) == 1:
        r = w.user32.VkKeyScanW(n)
        if r == -1:
            raise ValueError(f"no virtual key for '{name}'")
        return r & 0xFF
    raise ValueError(f"unknown key '{name}'")


def parse_combo(combo: str) -> tuple[list[int], int]:
    parts = [p for p in combo.replace(" ", "").lower().split("+") if p]
    if not parts:
        raise ValueError("empty key combo")
    mods = [MODS[p] for p in parts[:-1] if p in MODS]
    unknown = [p for p in parts[:-1] if p not in MODS]
    if unknown:
        raise ValueError(f"not modifiers: {unknown}")
    return mods, key_vk(parts[-1])


def _kbd(vk: int, up: bool) -> w.INPUT:
    inp = w.INPUT(type=w.INPUT_KEYBOARD)
    flags = w.KEYEVENTF_KEYUP if up else 0
    if vk in EXTENDED:
        flags |= w.KEYEVENTF_EXTENDEDKEY
    inp.ki = w.KEYBDINPUT(wVk=vk, wScan=w.user32.MapVirtualKeyW(vk, 0), dwFlags=flags, time=0,
                          dwExtraInfo=w.INJECT_MAGIC)
    return inp


def _send(inputs: list) -> int:
    arr = (w.INPUT * len(inputs))(*inputs)
    return w.user32.SendInput(len(inputs), arr, ctypes.sizeof(w.INPUT))


def sendinput_combo(combo: str) -> None:
    mods, vk = parse_combo(combo)
    seq = [_kbd(m, False) for m in mods] + [_kbd(vk, False), _kbd(vk, True)] + \
          [_kbd(m, True) for m in reversed(mods)]
    _send(seq)


def sendinput_text(text: str, stop: StopState | None = None) -> None:
    for ch in text:
        if stop and stop.stopped:
            raise StoppedError(stop.reason)
        if ch == "\n":
            _send([_kbd(0x0D, False), _kbd(0x0D, True)])
        else:
            code = ord(ch)
            down = w.INPUT(type=w.INPUT_KEYBOARD)
            down.ki = w.KEYBDINPUT(wVk=0, wScan=code, dwFlags=w.KEYEVENTF_UNICODE, time=0, dwExtraInfo=w.INJECT_MAGIC)
            up = w.INPUT(type=w.INPUT_KEYBOARD)
            up.ki = w.KEYBDINPUT(wVk=0, wScan=code, dwFlags=w.KEYEVENTF_UNICODE | w.KEYEVENTF_KEYUP, time=0,
                                 dwExtraInfo=w.INJECT_MAGIC)
            _send([down, up])
        time.sleep(0.004)


def release_all_modifiers() -> None:
    _send([_kbd(v, True) for v in (0x10, 0x11, 0x12, 0x5B)])


def post_key(hwnd: int, combo: str) -> None:
    """WM_KEYDOWN/WM_KEYUP straight to a window (no focus needed; no modifiers)."""
    mods, vk = parse_combo(combo)
    if mods:
        raise ValueError("modifier combos need SendInput (target must be focused)")
    scan = w.user32.MapVirtualKeyW(vk, 0)
    ext = (1 << 24) if vk in EXTENDED else 0
    w.user32.PostMessageW(hwnd, w.WM_KEYDOWN, vk, 1 | (scan << 16) | ext)
    w.user32.PostMessageW(hwnd, w.WM_KEYUP, vk, 1 | (scan << 16) | ext | (1 << 30) | (1 << 31))


# ---------------------------------------------------------------- messages (mouse, no cursor)
_BTN_MSG = {"left": (w.WM_LBUTTONDOWN, w.WM_LBUTTONUP, w.MK_LBUTTON),
            "right": (w.WM_RBUTTONDOWN, w.WM_RBUTTONUP, w.MK_RBUTTON),
            "middle": (w.WM_MBUTTONDOWN, w.WM_MBUTTONUP, w.MK_MBUTTON)}


def deepest_child_at(root_hwnd: int, x: int, y: int) -> int:
    """Deepest visible child window under a virtual point (like WindowFromPoint,
    but restricted to one top-level window and ignoring overlaps)."""
    h = root_hwnd
    for _ in range(32):
        pt = w.POINT(x, y)
        w.user32.ScreenToClient(h, ctypes.byref(pt))
        c = w.user32.ChildWindowFromPointEx(h, pt, 0x1 | 0x4)  # SKIPINVISIBLE | SKIPTRANSPARENT
        if not c or int(c) == int(h):
            return int(h)
        h = int(c)
    return int(h)


def post_click(hwnd: int, x: int, y: int, button: str = "left", double: bool = False) -> None:
    down, up, mk = _BTN_MSG[button]
    pt = w.POINT(x, y)
    w.user32.ScreenToClient(hwnd, ctypes.byref(pt))
    lp = w.make_lparam(pt.x, pt.y)
    w.user32.PostMessageW(hwnd, w.WM_MOUSEMOVE, 0, lp)
    w.user32.PostMessageW(hwnd, down, mk, lp)
    w.user32.PostMessageW(hwnd, up, 0, lp)
    if double:
        w.user32.PostMessageW(hwnd, w.WM_LBUTTONDBLCLK if button == "left" else down, mk, lp)
        w.user32.PostMessageW(hwnd, up, 0, lp)


def post_wheel(hwnd: int, x: int, y: int, dy: int, dx: int = 0) -> None:
    # WM_MOUSEWHEEL takes SCREEN coords; positive delta = away from user (up)
    lp = w.make_lparam(x, y)
    if dy:
        w.user32.PostMessageW(hwnd, w.WM_MOUSEWHEEL, ((-dy * 120) & 0xFFFF) << 16, lp)
    if dx:
        w.user32.PostMessageW(hwnd, w.WM_MOUSEHWHEEL, ((dx * 120) & 0xFFFF) << 16, lp)


# ---------------------------------------------------------------- guard thread
class InputGuard:
    def __init__(self, stop: StopState, hotkey: str):
        self.stop = stop
        self.hotkey = hotkey
        self.hotkey_registered = False
        self.hotkey_error = ""
        self._tid = 0
        self._ready = threading.Event()
        self._ack = queue.Queue()
        self._hook = None
        self._frozen = False
        self.swallowed = 0
        self._proc = w.HOOKPROC(self._mouse_proc)  # keep a reference: GC'd callback = crash
        self._thread = threading.Thread(target=self._run, name="input-guard", daemon=True)
        self._thread.start()
        self._ready.wait(5)

    WM_FREEZE = w.WM_APP + 1
    WM_UNFREEZE = w.WM_APP + 2

    def _mouse_proc(self, code, wparam, lparam):
        if code == 0 and self._frozen:
            info = ctypes.cast(lparam, ctypes.POINTER(w.MSLLHOOKSTRUCT)).contents
            if info.dwExtraInfo != w.INJECT_MAGIC:
                self.swallowed += 1
                return 1  # swallow the user's physical event
        return w.user32.CallNextHookEx(None, code, wparam, lparam)

    def _register_hotkey(self):
        parts = [p for p in self.hotkey.lower().replace(" ", "").split("+") if p]
        mods = 0
        for p in parts[:-1]:
            mods |= HOTKEY_MODS.get(p, 0)
        try:
            vk = key_vk(parts[-1])
        except ValueError as e:
            self.hotkey_error = str(e)
            return
        if w.user32.RegisterHotKey(None, 1, mods | w.MOD_NOREPEAT, vk):
            self.hotkey_registered = True
        else:
            self.hotkey_error = f"RegisterHotKey failed (err {ctypes.get_last_error()}): combo taken by another app?"
            log.error(self.hotkey_error)

    def _run(self):
        self._tid = w.kernel32.GetCurrentThreadId()
        msg = w.MSG()
        w.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # create the queue
        self._register_hotkey()
        self._ready.set()
        while w.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == w.WM_HOTKEY:
                log.warning("Stop hotkey pressed")
                self.stop.stop("hotkey")
            elif msg.message == self.WM_FREEZE:
                if self._hook is None:
                    self._hook = w.user32.SetWindowsHookExW(w.WH_MOUSE_LL, self._proc,
                                                            w.kernel32.GetModuleHandleW(None), 0)
                self._frozen = bool(self._hook)
                self._ack.put(self._frozen)
            elif msg.message == self.WM_UNFREEZE:
                self._frozen = False
                if self._hook:
                    w.user32.UnhookWindowsHookEx(self._hook)
                    self._hook = None
                self._ack.put(True)
            else:
                w.user32.TranslateMessage(ctypes.byref(msg))
                w.user32.DispatchMessageW(ctypes.byref(msg))

    def _call(self, message) -> bool:
        while not self._ack.empty():
            self._ack.get_nowait()
        w.user32.PostThreadMessageW(self._tid, message, 0, 0)
        try:
            return self._ack.get(timeout=2)
        except queue.Empty:
            return False

    def freeze(self) -> bool:
        return self._call(self.WM_FREEZE)

    def unfreeze(self) -> None:
        self._call(self.WM_UNFREEZE)


# ---------------------------------------------------------------- SendInput mouse
_BTN_FLAGS = {"left": (w.MOUSEEVENTF_LEFTDOWN, w.MOUSEEVENTF_LEFTUP),
              "right": (w.MOUSEEVENTF_RIGHTDOWN, w.MOUSEEVENTF_RIGHTUP),
              "middle": (w.MOUSEEVENTF_MIDDLEDOWN, w.MOUSEEVENTF_MIDDLEUP)}


def _mouse(flags: int, nx: int = 0, ny: int = 0, data: int = 0) -> w.INPUT:
    inp = w.INPUT(type=w.INPUT_MOUSE)
    inp.mi = w.MOUSEINPUT(dx=nx, dy=ny, mouseData=data & 0xFFFFFFFF, dwFlags=flags, time=0,
                          dwExtraInfo=w.INJECT_MAGIC)
    return inp


def _vbounds():
    x, y = w.user32.GetSystemMetrics(w.SM_XVIRTUALSCREEN), w.user32.GetSystemMetrics(w.SM_YVIRTUALSCREEN)
    return (x, y, x + w.user32.GetSystemMetrics(w.SM_CXVIRTUALSCREEN), y + w.user32.GetSystemMetrics(w.SM_CYVIRTUALSCREEN))


def _move_abs(x: int, y: int, vb) -> None:
    nx, ny = to_absolute_input(x, y, vb)
    _send([_mouse(w.MOUSEEVENTF_MOVE | w.MOUSEEVENTF_ABSOLUTE | w.MOUSEEVENTF_VIRTUALDESK, nx, ny)])


def _physical_buttons_down() -> bool:
    return any(w.user32.GetAsyncKeyState(v) & 0x8000 for v in (0x01, 0x02, 0x04))


class Gesture:
    """Real-cursor action: save cursor -> freeze user's mouse -> act -> restore.

    Usage:  with Gesture(guard, stop, cfg) as g:  g.move(x, y); g.down(); ...
    On stop (button/hotkey) any held button is released and the cursor restored.
    """

    def __init__(self, guard: InputGuard, stop: StopState, cfg: dict):
        self.guard, self.stop = guard, stop
        icfg = cfg.get("input", {})
        self.freeze_enabled = bool(icfg.get("freeze_user_mouse_during_gestures", False))
        self.step = float(icfg.get("move_step_px", 6))
        self.interval = float(icfg.get("move_interval_ms", 4)) / 1000.0
        self.saved = (0, 0)
        self.held: set[str] = set()
        self.frozen = False
        self.vb = _vbounds()
        self.report = {"cursor_moved": True, "cursor_restored": False, "user_mouse_frozen": False,
                       "user_events_swallowed": 0}

    def __enter__(self):
        if self.stop.stopped:
            raise StoppedError(self.stop.reason)
        # A user already holding a physical button would mix with ours: wait a bit.
        t0 = time.time()
        while _physical_buttons_down() and time.time() - t0 < 3:
            time.sleep(0.02)
        self.saved = w.cursor_pos()
        start_swallowed = self.guard.swallowed
        self._swallow_base = start_swallowed
        if self.freeze_enabled:
            self.frozen = self.guard.freeze()
            self.report["user_mouse_frozen"] = self.frozen
        return self

    def _check(self):
        if self.stop.stopped:
            raise StoppedError(self.stop.reason)

    def move(self, x: int, y: int) -> None:
        self._check()
        _move_abs(x, y, self.vb)

    def glide(self, points: list[tuple]) -> None:
        for p in densify_path(points, self.step):
            self.move(*p)
            time.sleep(self.interval)

    def down(self, button: str = "left") -> None:
        self._check()
        _send([_mouse(_BTN_FLAGS[button][0])])
        self.held.add(button)

    def up(self, button: str = "left") -> None:
        _send([_mouse(_BTN_FLAGS[button][1])])
        self.held.discard(button)

    def wheel(self, dy: int = 0, dx: int = 0) -> None:
        self._check()
        if dy:
            _send([_mouse(w.MOUSEEVENTF_WHEEL, data=-dy * 120)])
        if dx:
            _send([_mouse(w.MOUSEEVENTF_HWHEEL, data=dx * 120)])

    def __exit__(self, exc_type, exc, tb):
        for b in list(self.held):          # never leave a button "grabbed"
            try:
                self.up(b)
            except Exception:
                pass
        if exc_type is StoppedError:
            release_all_modifiers()
        time.sleep(0.02)
        w.user32.SetCursorPos(*self.saved)
        self.report["cursor_restored"] = w.cursor_pos() == tuple(self.saved)
        if self.frozen:
            self.guard.unfreeze()
        self.report["user_events_swallowed"] = self.guard.swallowed - self._swallow_base
        return False
