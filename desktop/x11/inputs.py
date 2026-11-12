"""Keyboard / mouse input on X11 through the XTEST extension (the SendInput of X).

Same API as desktop/inputs.py. The Win32 "message" mechanisms (WM_CHAR,
WM_LBUTTONDOWN to an HWND) have no X equivalent - X toolkits ignore synthetic
events from XSendEvent - so post_* raise and desktop_server.py only uses
XTEST and AT-SPI on Linux.
"""

from __future__ import annotations

import logging
import threading
import time

from Xlib import X, XK
from Xlib.ext import xtest

from . import winapi as w
from . import xconn as xc
from ..monitors import densify_path
from ..safety import StoppedError, StopState

log = logging.getLogger("desktop-mcp.input")

# ---------------------------------------------------------------- key names -> keysym names
KEYSYM_NAMES = {
    "backspace": "BackSpace", "tab": "Tab", "enter": "Return", "return": "Return", "shift": "Shift_L",
    "ctrl": "Control_L", "control": "Control_L", "alt": "Alt_L", "pause": "Pause", "capslock": "Caps_Lock",
    "esc": "Escape", "escape": "Escape", "space": "space", "pageup": "Prior", "pgup": "Prior",
    "pagedown": "Next", "pgdn": "Next", "end": "End", "home": "Home", "left": "Left", "up": "Up",
    "right": "Right", "down": "Down", "printscreen": "Print", "insert": "Insert", "ins": "Insert",
    "delete": "Delete", "del": "Delete", "win": "Super_L", "lwin": "Super_L", "rwin": "Super_R",
    "super": "Super_L", "apps": "Menu", "menu": "Menu", "numlock": "Num_Lock", "scrolllock": "Scroll_Lock",
    "plus": "plus", "minus": "minus", "comma": "comma", "period": "period",
}
KEYSYM_NAMES.update({f"f{i}": f"F{i}" for i in range(1, 25)})
MODS = {"ctrl": "Control_L", "control": "Control_L", "shift": "Shift_L", "alt": "Alt_L",
        "win": "Super_L", "super": "Super_L"}


def char_keysym(ch: str) -> int:
    code = ord(ch)
    if ch == "\n":
        return XK.string_to_keysym("Return")
    if ch == "\t":
        return XK.string_to_keysym("Tab")
    if 0x20 <= code <= 0x7E or 0xA0 <= code <= 0xFF:
        return code                       # Latin-1 keysyms equal the code point
    return 0x01000000 | code              # Unicode keysym


def key_keysym(name: str) -> int:
    n = name.strip()
    low = n.lower()
    if low in KEYSYM_NAMES:
        return XK.string_to_keysym(KEYSYM_NAMES[low])
    if len(n) == 1:
        return char_keysym(low)
    ks = XK.string_to_keysym(n)
    if ks:
        return ks
    raise ValueError(f"unknown key '{name}'")


key_vk = key_keysym


def parse_combo(combo: str) -> tuple[list[int], int]:
    parts = [p for p in combo.replace(" ", "").lower().split("+") if p]
    if not parts:
        raise ValueError("empty key combo")
    unknown = [p for p in parts[:-1] if p not in MODS]
    if unknown:
        raise ValueError(f"not modifiers: {unknown}")
    mods = [XK.string_to_keysym(MODS[p]) for p in parts[:-1]]
    return mods, key_keysym(parts[-1])


# ---------------------------------------------------------------- keyboard
_KB_LOCK = threading.Lock()


def _keycode_for(keysym: int) -> tuple[int, int]:
    """(keycode, shift-level) of a keysym in the current mapping, or (0, 0)."""
    dpy = xc.d()
    for kc, idx in dpy.keysym_to_keycodes(keysym):
        return kc, idx
    return 0, 0


def _spare_keycode() -> int:
    """A keycode with no keysym at all: used to type characters the layout lacks."""
    dpy = xc.d()
    lo, hi = dpy.display.info.min_keycode, dpy.display.info.max_keycode
    m = dpy.get_keyboard_mapping(lo, hi - lo + 1)
    for i in range(len(m) - 1, -1, -1):
        if not any(m[i]):
            return lo + i
    return 0


def _fake(kind, detail) -> None:
    xtest.fake_input(xc.d(), kind, detail)


def _tap_keycode(kc: int, shift: bool = False, altgr: bool = False) -> None:
    dpy = xc.d()
    sh = dpy.keysym_to_keycode(XK.string_to_keysym("Shift_L")) if shift else 0
    ag = dpy.keysym_to_keycode(XK.string_to_keysym("ISO_Level3_Shift")) if altgr else 0
    for m in (sh, ag):
        if m:
            _fake(X.KeyPress, m)
    _fake(X.KeyPress, kc)
    _fake(X.KeyRelease, kc)
    for m in (ag, sh):
        if m:
            _fake(X.KeyRelease, m)
    dpy.sync()


def type_keysym(keysym: int) -> None:
    """Press+release the key that produces keysym, with Shift/AltGr as needed. A character
    the layout lacks is typed by mapping it onto a free keycode for the stroke (ñ, é, €...)."""
    with _KB_LOCK:
        kc, idx = _keycode_for(keysym)
        if kc and idx in (0, 1, 4, 5):
            _tap_keycode(kc, shift=idx in (1, 5), altgr=idx in (4, 5))
            return
        dpy = xc.d()
        spare = _spare_keycode()
        if not spare:
            raise RuntimeError(f"cannot type keysym 0x{keysym:x}: no free keycode to remap")
        dpy.change_keyboard_mapping(spare, [(keysym, keysym)])
        dpy.sync()
        time.sleep(0.02)  # let the client see the MappingNotify before the stroke
        _tap_keycode(spare)
        time.sleep(0.02)
        dpy.change_keyboard_mapping(spare, [(0, 0)])
        dpy.sync()


def sendinput_combo(combo: str) -> None:
    mods, ks = parse_combo(combo)
    dpy = xc.d()
    mod_codes = [dpy.keysym_to_keycode(m) for m in mods]
    kc, idx = _keycode_for(ks)
    if not kc:
        raise ValueError(f"key of '{combo}' is not on the keyboard layout")
    with _KB_LOCK:
        for m in mod_codes:
            _fake(X.KeyPress, m)
        _fake(X.KeyPress, kc)
        _fake(X.KeyRelease, kc)
        for m in reversed(mod_codes):
            _fake(X.KeyRelease, m)
        dpy.sync()


def sendinput_text(text: str, stop: StopState | None = None) -> None:
    for ch in text:
        if stop and stop.stopped:
            raise StoppedError(stop.reason)
        type_keysym(char_keysym(ch))
        time.sleep(0.004)


def release_all_modifiers() -> None:
    dpy = xc.d()
    for name in ("Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Super_L", "ISO_Level3_Shift"):
        kc = dpy.keysym_to_keycode(XK.string_to_keysym(name))
        if kc:
            _fake(X.KeyRelease, kc)
    dpy.sync()


# ---------------------------------------------------------------- Win32-only paths
def _no_messages(*_a, **_k):
    raise RuntimeError("mechanism 'message' does not exist on Linux/X11: use 'auto' (AT-SPI, then XTEST)")


post_key = post_click = post_wheel = _no_messages


def deepest_child_at(root_hwnd: int, x: int, y: int) -> int:
    return int(root_hwnd)


# ---------------------------------------------------------------- stop hotkey
class InputGuard:
    """Stop hotkey (XGrabKey on the root, every NumLock/CapsLock combination).

    The Win32 guard can also freeze the user's physical mouse during a gesture
    (low-level hook). X has no equivalent without grabbing the pointer, which
    would also block our own XTEST events: freeze() reports False."""

    def __init__(self, stop: StopState, hotkey: str):
        self.stop = stop
        self.hotkey = hotkey
        self.hotkey_registered = False
        self.hotkey_error = ""
        self.swallowed = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="input-guard", daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def _run(self):
        try:
            dpy = xc.d()
            root = dpy.screen().root
            parts = [p for p in self.hotkey.lower().replace(" ", "").split("+") if p]
            mask = 0
            for p in parts[:-1]:
                mask |= {"ctrl": X.ControlMask, "control": X.ControlMask, "shift": X.ShiftMask,
                         "alt": X.Mod1Mask, "win": X.Mod4Mask, "super": X.Mod4Mask}.get(p, 0)
            kc = dpy.keysym_to_keycode(key_keysym(parts[-1]))
            if not kc:
                raise ValueError(f"key '{parts[-1]}' not on the keyboard")
            for extra in (0, X.Mod2Mask, X.LockMask, X.Mod2Mask | X.LockMask):
                root.grab_key(kc, mask | extra, True, X.GrabModeAsync, X.GrabModeAsync)
            dpy.sync()
            self.hotkey_registered = True
        except Exception as e:  # noqa: BLE001
            self.hotkey_error = f"XGrabKey failed: {e}"
            log.error(self.hotkey_error)
            self._ready.set()
            return
        self._ready.set()
        while True:
            try:
                ev = dpy.next_event()
            except Exception:  # noqa: BLE001
                log.exception("input guard X connection lost")
                return
            if ev.type == X.KeyPress:
                log.warning("Stop hotkey pressed")
                self.stop.stop("hotkey")

    def freeze(self) -> bool:
        return False

    def unfreeze(self) -> None:
        return None


# ---------------------------------------------------------------- mouse
_BUTTONS = {"left": 1, "middle": 2, "right": 3}


def _move_abs(x: int, y: int) -> None:
    xtest.fake_input(xc.d(), X.MotionNotify, x=int(x), y=int(y))
    xc.d().sync()


def _physical_buttons_down() -> bool:
    return bool(w.pointer_mask() & (X.Button1Mask | X.Button2Mask | X.Button3Mask))


class Gesture:
    """Real-cursor action: save cursor -> act -> restore (same contract as on Windows).

    Usage:  with Gesture(guard, stop, cfg) as g:  g.move(x, y); g.down(); ...
    On stop (button/hotkey) any held button is released and the cursor restored.
    """

    def __init__(self, guard: InputGuard, stop: StopState, cfg: dict):
        self.guard, self.stop = guard, stop
        icfg = cfg.get("input", {})
        self.step = float(icfg.get("move_step_px", 6))
        self.interval = float(icfg.get("move_interval_ms", 4)) / 1000.0
        self.saved = (0, 0)
        self.held: set[str] = set()
        self.report = {"cursor_moved": True, "cursor_restored": False, "user_mouse_frozen": False,
                       "user_events_swallowed": 0}

    def __enter__(self):
        if self.stop.stopped:
            raise StoppedError(self.stop.reason)
        t0 = time.time()
        while _physical_buttons_down() and time.time() - t0 < 3:
            time.sleep(0.02)
        self.saved = w.cursor_pos()
        return self

    def _check(self):
        if self.stop.stopped:
            raise StoppedError(self.stop.reason)

    def move(self, x: int, y: int) -> None:
        self._check()
        _move_abs(x, y)

    def glide(self, points: list[tuple]) -> None:
        for p in densify_path(points, self.step):
            self.move(*p)
            time.sleep(self.interval)

    def down(self, button: str = "left") -> None:
        self._check()
        xtest.fake_input(xc.d(), X.ButtonPress, _BUTTONS[button])
        xc.d().sync()
        self.held.add(button)

    def up(self, button: str = "left") -> None:
        xtest.fake_input(xc.d(), X.ButtonRelease, _BUTTONS[button])
        xc.d().sync()
        self.held.discard(button)

    def wheel(self, dy: int = 0, dx: int = 0) -> None:
        self._check()
        for btn, n in ((5 if dy > 0 else 4, abs(dy)), (7 if dx > 0 else 6, abs(dx))):
            for _ in range(n):
                xtest.fake_input(xc.d(), X.ButtonPress, btn)
                xtest.fake_input(xc.d(), X.ButtonRelease, btn)
                xc.d().sync()
                time.sleep(0.01)

    def __exit__(self, exc_type, exc, tb):
        for b in list(self.held):          # never leave a button "grabbed"
            try:
                self.up(b)
            except Exception:  # noqa: BLE001
                pass
        if exc_type is StoppedError:
            release_all_modifiers()
        time.sleep(0.02)
        w.user32.SetCursorPos(*self.saved)
        self.report["cursor_restored"] = w.cursor_pos() == tuple(self.saved)
        return False
