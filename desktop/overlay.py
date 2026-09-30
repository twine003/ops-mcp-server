"""On-screen overlay: active-monitor border, ghost cursor, highlight, stop button.

Every overlay element is its own small layered top-level window, owned by a
dedicated UI thread with a Win32 message loop:

  border   4 strips per monitor (top/bottom/left/right) - cheap to fade/pulse,
           and one set per monitor handles mixed DPI + negative coordinates.
  label    "Claude - <window> - <state>" pill in the active monitor's corner.
  ghost    orange pointer + "Claude" tag, animated between targets.
  highlight outline around the target control's bounding box.
  trail    optional dots at the last N targets.
  stop     "Detener Claude" button (the only clickable overlay window).

All windows: WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
(+ WS_EX_TRANSPARENT = click-through for all but the stop button), and
SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) so screenshots never see
them. Nothing redraws while idle: a timer runs only during a fade, the
ghost glide, a click pulse, or the soft "working" pulse (25 fps, alpha-only).
"""

from __future__ import annotations

import ctypes
import logging
import math
import queue
import threading
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import winapi as w
from .monitors import Monitor, border_width_physical, enumerate_monitors, interpolate

log = logging.getLogger("desktop-mcp.overlay")

CLASS_NAME = "DesktopMcpOverlay"
WM_COMMAND_QUEUE = w.WM_APP + 10
T_ANIM, T_LINGER, T_IDLE, T_REBUILD, T_HILITE = 1, 2, 3, 4, 5


def _hex(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


def _premul_bgra(rgba: np.ndarray) -> bytes:
    a = rgba[..., 3:4].astype(np.uint16)
    rgb = (rgba[..., :3].astype(np.uint16) * a // 255).astype(np.uint8)
    out = np.empty(rgba.shape, dtype=np.uint8)
    out[..., 0], out[..., 1], out[..., 2] = rgb[..., 2], rgb[..., 1], rgb[..., 0]
    out[..., 3] = rgba[..., 3]
    return out.tobytes()


def _font(size: int, bold: bool = False):
    for name in (("segoeuib.ttf", "arialbd.ttf") if bold else ("segoeui.ttf", "arial.ttf")):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


# ---------------------------------------------------------------- layered window
class LayeredWindow:
    def __init__(self, owner: "Overlay", clickthrough: bool = True, kind: str = ""):
        ex = w.WS_EX_LAYERED | w.WS_EX_TOPMOST | w.WS_EX_TOOLWINDOW | w.WS_EX_NOACTIVATE
        if clickthrough:
            ex |= w.WS_EX_TRANSPARENT
        self.hwnd = w.user32.CreateWindowExW(ex, CLASS_NAME, "Claude overlay", w.WS_POPUP,
                                             0, 0, 1, 1, None, None, owner.hinst, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self.kind = kind
        self.excluded = bool(w.user32.SetWindowDisplayAffinity(self.hwnd, w.WDA_EXCLUDEFROMCAPTURE))
        owner.windows[int(self.hwnd)] = self
        self.memdc = None
        self.hbmp = None
        self.old = None
        self.size = (0, 0)
        self.pos = (0, 0)
        self.alpha = 0
        self.visible = False
        self._dirty = True

    def set_bitmap(self, bgra_premul: bytes, size: tuple[int, int]) -> None:
        self._free()
        wd, ht = size
        hdc_screen = w.user32.GetDC(None)
        self.memdc = w.gdi32.CreateCompatibleDC(hdc_screen)
        bmi = w.BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(w.BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = wd, -ht
        bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32
        bits = ctypes.c_void_p()
        self.hbmp = w.gdi32.CreateDIBSection(self.memdc, ctypes.byref(bmi), w.DIB_RGB_COLORS,
                                             ctypes.byref(bits), None, 0)
        ctypes.memmove(bits, bgra_premul, len(bgra_premul))
        self.old = w.gdi32.SelectObject(self.memdc, self.hbmp)
        w.user32.ReleaseDC(None, hdc_screen)
        self.size = size
        self._dirty = True

    def set_image(self, img: Image.Image) -> None:
        self.set_bitmap(_premul_bgra(np.asarray(img.convert("RGBA"))), img.size)

    def update(self, pos: tuple[int, int] | None = None, alpha: float | None = None) -> None:
        if self.memdc is None:
            return
        new_pos = self.pos if pos is None else (int(pos[0]), int(pos[1]))
        new_alpha = self.alpha if alpha is None else int(max(0, min(255, round(alpha * 255))))
        if not self._dirty and new_pos == self.pos and new_alpha == self.alpha:
            return  # nothing changed: no GPU/compositor work while idle
        self.pos, self.alpha, self._dirty = new_pos, new_alpha, False
        if self.alpha == 0 and not self.visible:
            return
        blend = w.BLENDFUNCTION(0, 0, self.alpha, w.AC_SRC_ALPHA)
        pt, sz, src = w.POINT(*self.pos), w.SIZE(*self.size), w.POINT(0, 0)
        w.user32.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(pt), ctypes.byref(sz), self.memdc,
                                     ctypes.byref(src), 0, ctypes.byref(blend), w.ULW_ALPHA)
        want = self.alpha > 0
        if want and not self.visible:
            w.user32.ShowWindow(self.hwnd, w.SW_SHOWNOACTIVATE)
            w.user32.SetWindowPos(self.hwnd, w.HWND_TOPMOST, 0, 0, 0, 0,
                                  w.SWP_NOMOVE | w.SWP_NOSIZE | w.SWP_NOACTIVATE)
            self.visible = True
        elif not want and self.visible:
            w.user32.ShowWindow(self.hwnd, w.SW_HIDE)
            self.visible = False

    def raise_top(self) -> None:
        if self.visible:
            w.user32.SetWindowPos(self.hwnd, w.HWND_TOPMOST, 0, 0, 0, 0,
                                  w.SWP_NOMOVE | w.SWP_NOSIZE | w.SWP_NOACTIVATE)

    def _free(self):
        if self.memdc:
            w.gdi32.SelectObject(self.memdc, self.old)
            w.gdi32.DeleteObject(self.hbmp)
            w.gdi32.DeleteDC(self.memdc)
        self.memdc = self.hbmp = self.old = None

    def destroy(self, owner: "Overlay") -> None:
        self._free()
        owner.windows.pop(int(self.hwnd), None)
        w.user32.DestroyWindow(self.hwnd)


# ---------------------------------------------------------------- image builders
def border_strips(mon: Monitor, color: tuple, bw: int, max_opacity: float) -> dict:
    """RGBA arrays for the 4 strips. Alpha fades from max_opacity at the edge
    to 0 inward; top/bottom strips also carry the corner gradients."""
    W, H = mon.width, mon.height
    bw = max(1, min(bw, H // 4, W // 4))
    d = np.arange(bw, dtype=np.float32)
    f = (1.0 - d / bw) ** 1.6 * max_opacity                 # edge -> inside

    def make(h_, w_, alpha):
        arr = np.zeros((h_, w_, 4), dtype=np.uint8)
        arr[..., 0], arr[..., 1], arr[..., 2] = color
        arr[..., 3] = (alpha * 255).clip(0, 255).astype(np.uint8)
        return arr

    colf = np.zeros(W, dtype=np.float32)
    colf[:bw] = f
    colf[W - bw:] = np.maximum(colf[W - bw:], f[::-1])
    top_a = np.maximum(f[:, None], colf[None, :])           # rows: distance from top edge
    bottom_a = top_a[::-1]
    rowf = f[None, :]                                        # cols: distance from left edge
    side_h = H - 2 * bw
    left_a = np.repeat(rowf, side_h, axis=0)
    right_a = left_a[:, ::-1]
    L, T = mon.rect[0], mon.rect[1]
    return {
        "top": (make(bw, W, top_a), (L, T)),
        "bottom": (make(bw, W, bottom_a), (L, T + H - bw)),
        "left": (make(side_h, bw, left_a), (L, T + bw)),
        "right": (make(side_h, bw, right_a), (L + W - bw, T + bw)),
    }


def pill(text: str, scale: float, accent: tuple, bold: bool = False, bg=(24, 24, 24, 215),
         fg=(255, 255, 255, 255), dot: bool = True) -> Image.Image:
    size = max(11, round(12 * scale))
    font = _font(size, bold)
    pad_x, pad_y = round(10 * scale), round(5 * scale)
    dot_d = round(8 * scale) if dot else 0
    tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    l, t, r, b = tmp.textbbox((0, 0), text, font=font)
    tw, th = r - l, b - t
    wd = tw + 2 * pad_x + (dot_d + round(6 * scale) if dot else 0)
    ht = max(th, dot_d) + 2 * pad_y
    img = Image.new("RGBA", (wd, ht), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    dr.rounded_rectangle((0, 0, wd - 1, ht - 1), radius=ht // 2, fill=bg, outline=(*accent, 255),
                         width=max(1, round(scale)))
    x = pad_x
    if dot:
        cy = ht // 2
        dr.ellipse((x, cy - dot_d // 2, x + dot_d, cy + dot_d // 2), fill=(*accent, 255))
        x += dot_d + round(6 * scale)
    dr.text((x - l, pad_y - t + (ht - 2 * pad_y - th) // 2), text, font=font, fill=fg)
    return img


GHOST_HOTSPOT = 30  # hotspot offset inside the ghost bitmap (room for the click ring)


def ghost_image(scale: float, color: tuple, label: str, ring: float = 0.0) -> Image.Image:
    s = 1.25 * scale
    arrow = [(0, 0), (0, 20), (5, 15.5), (8.8, 24), (12, 22.6), (8.3, 14.4), (15, 14.4)]
    hs = GHOST_HOTSPOT
    tag = pill(label, scale, color, bold=True, bg=(*color, 235), fg=(255, 255, 255, 255), dot=False)
    wd = int(hs + 16 * s + tag.width + 4)
    ht = int(hs + 26 * s + tag.height // 2 + 4)
    img = Image.new("RGBA", (max(wd, 2 * hs + 2), max(ht, 2 * hs + 2)), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    if ring > 0:  # click pulse: expanding ring that fades out
        r = 6 + 22 * ring
        a = int(230 * (1 - ring))
        dr.ellipse((hs - r, hs - r, hs + r, hs + r), outline=(*color, a), width=max(2, round(3 * scale)))
    pts = [(hs + x * s, hs + y * s) for x, y in arrow]
    dr.polygon(pts, fill=(*color, 255), outline=(255, 255, 255, 255), width=max(1, round(1.6 * scale)))
    img.alpha_composite(tag, (int(hs + 13 * s), int(hs + 18 * s)))
    return img


def highlight_image(size: tuple[int, int], color: tuple, scale: float) -> Image.Image:
    wd, ht = max(4, size[0]), max(4, size[1])
    img = Image.new("RGBA", (wd, ht), (*color, 28))
    dr = ImageDraw.Draw(img)
    lw = max(2, round(2.5 * scale))
    dr.rounded_rectangle((0, 0, wd - 1, ht - 1), radius=round(4 * scale), outline=(*color, 255), width=lw)
    return img


# ---------------------------------------------------------------- overlay
class Overlay:
    def __init__(self, cfg: dict, stop_state, on_monitors_changed=None):
        self.cfg = cfg
        self.stop_state = stop_state
        self.on_monitors_changed = on_monitors_changed
        self.q: "queue.Queue" = queue.Queue()
        self.windows: dict[int, LayeredWindow] = {}
        self.hinst = w.kernel32.GetModuleHandleW(None)
        self.ctrl_hwnd = None
        self.monitors: list[Monitor] = []
        self.strips: dict[str, dict] = {}         # device -> {side: LayeredWindow}
        self.level: dict[str, float] = {}         # device -> current border factor 0..1
        self.target: dict[str, float] = {}
        self.active: str | None = None            # device of the active monitor
        self.state = "idle"                       # working | steady | paused | idle
        self.label_text = ""
        self.label_win = self.stop_win = self.ghost_win = self.hilite_win = None
        self.trail: list[LayeredWindow] = []
        self.trail_pos: list[tuple] = []
        self.ghost_pos = None
        self.ghost_anim = None                    # (p0, p1, t0, dur, event)
        self.ghost_ring = None                    # (t0, dur)
        self.ghost_scale = None
        self.hilite_until = 0.0
        self.hilite_level = 0.0
        self._anim_interval = 0
        self._last_frame = 0.0
        self.ready = threading.Event()
        self.error = ""
        self.stop_btn_rect = None
        self._wndproc = w.WNDPROC(self._proc)
        self.thread = threading.Thread(target=self._run, name="overlay-ui", daemon=True)
        self.thread.start()
        self.ready.wait(10)

    # ============================================================ public (any thread)
    def post(self, cmd: str, wait: bool = False, timeout: float = 3.0, **kw):
        ev = threading.Event() if wait else None
        self.q.put((cmd, kw, ev))
        if self.ctrl_hwnd:
            w.user32.PostMessageW(self.ctrl_hwnd, WM_COMMAND_QUEUE, 0, 0)
        if ev:
            ev.wait(timeout)

    def activity(self, device: str | None, label: str, mode: str = "") -> None:
        self.post("activity", device=device, label=label, mode=mode)

    def ghost_move(self, x: int, y: int, wait: bool = True) -> None:
        dur = float(self.cfg.get("ghost", {}).get("anim_ms", 220)) / 1000
        self.post("ghost_move", wait=wait, timeout=dur + 1.5, x=x, y=y)

    def click_pulse(self) -> None:
        self.post("pulse")

    def highlight(self, rect: tuple | None) -> None:
        if rect:
            self.post("highlight", rect=rect)

    # ============================================================ UI thread
    def _run(self):
        try:
            wc = w.WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(wc)
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = self.hinst
            wc.lpszClassName = CLASS_NAME
            wc.hCursor = w.user32.LoadCursorW(None, ctypes.c_void_p(32512))
            w.user32.RegisterClassExW(ctypes.byref(wc))
            # hidden top-level window: receives WM_DISPLAYCHANGE broadcasts + our command pings
            self.ctrl_hwnd = w.user32.CreateWindowExW(w.WS_EX_TOOLWINDOW, CLASS_NAME, "Claude overlay control",
                                                      w.WS_POPUP, 0, 0, 0, 0, None, None, self.hinst, None)
            self._build()
        except Exception as e:  # noqa: BLE001
            self.error = f"overlay init failed: {e}"
            log.exception(self.error)
        self.ready.set()
        msg = w.MSG()
        while w.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            w.user32.TranslateMessage(ctypes.byref(msg))
            w.user32.DispatchMessageW(ctypes.byref(msg))

    def _proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_COMMAND_QUEUE:
                self._drain()
                return 0
            if msg == w.WM_TIMER:
                self._on_timer(int(wparam))
                return 0
            if msg in (w.WM_DISPLAYCHANGE, w.WM_DPICHANGED) or \
                    (msg == w.WM_SETTINGCHANGE and self.ctrl_hwnd and int(hwnd) == int(self.ctrl_hwnd)):
                # bursts of these arrive together: rebuild once, 400 ms later
                if self.ctrl_hwnd:
                    w.user32.SetTimer(self.ctrl_hwnd, T_REBUILD, 400, None)
                return 0
            if self.stop_win is not None and int(hwnd) == int(self.stop_win.hwnd):
                if msg == w.WM_MOUSEACTIVATE:
                    return w.MA_NOACTIVATE
                if msg == w.WM_SETCURSOR:
                    w.user32.SetCursor(w.user32.LoadCursorW(None, ctypes.c_void_p(32649)))  # IDC_HAND
                    return 1
                if msg == w.WM_LBUTTONUP:
                    # only a PHYSICAL click counts: our own SendInput clicks carry the magic tag
                    if (w.user32.GetMessageExtraInfo() & 0xFFFFFFFF) != w.INJECT_MAGIC:
                        if self.stop_state.stopped:
                            self.stop_state.resume("stop button")
                        else:
                            self.stop_state.stop("stop button")
                    return 0
        except Exception:  # never let an exception escape into Win32
            log.exception("overlay wndproc error")
        return w.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ------------------------------------------------------------ build / rebuild
    def _build(self):
        self.rebuilds = getattr(self, "rebuilds", -1) + 1
        bcfg = self.cfg.get("border", {})
        for dev, strips in self.strips.items():
            for win in strips.values():
                win.destroy(self)
        self.strips.clear()
        self.monitors = enumerate_monitors()
        color = _hex(bcfg.get("paused_color") if self.state == "paused" else bcfg.get("color", "#FF8C00"))
        for m in self.monitors:
            bw = border_width_physical(int(bcfg.get("width_px", 16)), m, bcfg.get("width_mode", "physical"))
            arrs = border_strips(m, color, bw, float(bcfg.get("max_opacity", 0.65)))
            wins = {}
            for side, (arr, pos) in arrs.items():
                lw = LayeredWindow(self, True, "border")
                lw.set_bitmap(_premul_bgra(arr), (arr.shape[1], arr.shape[0]))
                lw.pos = pos
                lw._dirty = True
                wins[side] = lw
            self.strips[m.device] = wins
            self.level.setdefault(m.device, 0.0)
            self.target.setdefault(m.device, 0.0)
        for dev in list(self.level):
            if dev not in self.strips:
                self.level.pop(dev, None)
                self.target.pop(dev, None)
        if self.active not in self.strips:
            self.active = None
        for win in (self.label_win, self.stop_win, self.hilite_win, self.ghost_win, *self.trail):
            if win is not None:
                win.destroy(self)
        self.label_win = LayeredWindow(self, True, "label")
        self.stop_win = LayeredWindow(self, False, "stop") if self.cfg.get("stop_button", {}).get("enabled", True) else None
        self.hilite_win = LayeredWindow(self, True, "highlight")
        self.ghost_win = LayeredWindow(self, True, "ghost")
        self.ghost_scale = None
        n_trail = int(self.cfg.get("ghost", {}).get("trail_length", 8)) if self.cfg.get("ghost", {}).get("trail") else 0
        self.trail = [LayeredWindow(self, True, "trail") for _ in range(n_trail)]
        self._render_label()
        self._apply_levels()
        if self.ghost_pos is not None:
            self._render_ghost(0.0)
        self._ensure_anim()

    def excluded_from_capture(self) -> bool:
        return all(win.excluded for win in self.windows.values())

    def _mon(self, device: str | None) -> Monitor | None:
        return next((m for m in self.monitors if m.device == device), None)

    def _mon_at(self, x, y) -> Monitor | None:
        return next((m for m in self.monitors if m.contains(x, y)), None)

    # ------------------------------------------------------------ commands
    def _drain(self):
        while True:
            try:
                cmd, kw, ev = self.q.get_nowait()
            except queue.Empty:
                return
            try:
                getattr(self, "_cmd_" + cmd)(ev=ev, **kw)
            except Exception:
                log.exception("overlay command %s failed", cmd)
                if ev:
                    ev.set()
            else:
                if ev and cmd != "ghost_move":
                    ev.set()

    def _cmd_activity(self, ev, device, label, mode=""):
        bcfg = self.cfg.get("border", {})
        if device and device in self.strips:
            if self.active and self.active != device:
                self.target[self.active] = 0.0          # fade out the previous monitor
            self.active = device
        self.label_text = label + (f"  ·  {mode}" if mode else "")
        if self.state != "paused":
            self.state = "working"
        if self.active:
            self.target[self.active] = 1.0
        w.user32.KillTimer(self.ctrl_hwnd, T_IDLE)
        w.user32.SetTimer(self.ctrl_hwnd, T_LINGER, int(float(bcfg.get("working_linger_s", 3)) * 1000), None)
        self._render_label()
        self._ensure_anim()

    def _cmd_paused(self, ev, paused: bool, reason: str = ""):
        self.state = "paused" if paused else "working"
        self._build()  # recolour strips
        if self.active:
            self.target[self.active] = 1.0
        if not paused:
            w.user32.SetTimer(self.ctrl_hwnd, T_LINGER, 1500, None)
        self._render_label(reason)
        self._ensure_anim()

    def _cmd_border(self, ev, action: str, device: str | None = None, color: str | None = None,
                    width: int | None = None):
        bcfg = self.cfg.setdefault("border", {})
        if color:
            bcfg["color"] = color
        if width:
            bcfg["width_px"] = int(width)
        if color or width:
            self._build()
        if action == "hide":
            for d in self.target:
                self.target[d] = 0.0
            self.state = "idle" if self.state != "paused" else self.state
        elif action in ("show", "monitor"):
            if device and device in self.strips:
                if self.active and self.active != device:
                    self.target[self.active] = 0.0
                self.active = device
            if self.active:
                self.target[self.active] = 1.0
                if self.state == "idle":
                    self.state = "steady"
        self._render_label()
        self._ensure_anim()

    def _cmd_ghost_move(self, ev, x, y):
        gcfg = self.cfg.get("ghost", {})
        if not gcfg.get("enabled", True):
            if ev:
                ev.set()
            return
        target = (int(x), int(y))
        if self.ghost_pos is None:
            m = self._mon(self.active) or (self.monitors[0] if self.monitors else None)
            self.ghost_pos = ((m.rect[0] + m.rect[2]) // 2, (m.rect[1] + m.rect[3]) // 2) if m else target
        dur = float(gcfg.get("anim_ms", 220)) / 1000
        if self.ghost_anim and self.ghost_anim[4]:
            self.ghost_anim[4].set()
        self.ghost_anim = (self.ghost_pos, target, time.perf_counter(), dur, ev)
        self._ensure_anim()

    def _cmd_ghost(self, ev, action: str, x=None, y=None):
        if action == "hide":
            self.ghost_win.update(alpha=0)
            for t in self.trail:
                t.update(alpha=0)
        elif action == "show":
            if self.ghost_pos is None:
                m = self._mon(self.active) or self.monitors[0]
                self.ghost_pos = ((m.rect[0] + m.rect[2]) // 2, (m.rect[1] + m.rect[3]) // 2)
            self._render_ghost(0.0)
        elif action == "move_to":
            self._cmd_ghost_move(None, x, y)

    def _cmd_pulse(self, ev):
        self.ghost_ring = (time.perf_counter(), 0.45)
        self._ensure_anim()

    def _cmd_highlight(self, ev, rect):
        l, t, r, b = [int(v) for v in rect]
        m = self._mon_at((l + r) // 2, (t + b) // 2)
        scale = m.scale if m else 1.0
        pad = max(3, round(3 * scale))
        img = highlight_image((r - l + 2 * pad, b - t + 2 * pad), _hex(self.cfg.get("ghost", {}).get("color", "#FF8C00")), scale)
        self.hilite_win.set_image(img)
        self.hilite_win.update(pos=(l - pad, t - pad), alpha=1.0)
        self.hilite_level = 1.0
        self.hilite_until = time.perf_counter() + float(self.cfg.get("ghost", {}).get("highlight_ms", 1400)) / 1000
        self._ensure_anim()

    def _cmd_reconfig(self, ev, cfg: dict):
        self.cfg = cfg
        self._build()

    # ------------------------------------------------------------ rendering
    def _render_label(self, reason: str = ""):
        m = self._mon(self.active)
        if m is None or self.label_win is None:
            return
        bcfg = self.cfg.get("border", {})
        accent = _hex(bcfg.get("paused_color") if self.state == "paused" else bcfg.get("color", "#FF8C00"))
        state_txt = {"working": "trabajando", "steady": "en espera", "paused": "PAUSADO",
                     "idle": "inactivo"}.get(self.state, self.state)
        if self.state == "paused" and (reason or self.stop_state.reason):
            state_txt += f" ({reason or self.stop_state.reason})"
        bw = border_width_physical(int(bcfg.get("width_px", 16)), m, bcfg.get("width_mode", "physical"))
        if bcfg.get("label", True):
            text = f"Claude  ·  {self.label_text or '—'}  ·  {state_txt}"
            if len(text) > 110:
                text = text[:107] + "…"
            img = pill(text, m.scale, accent)
            self.label_win.set_image(img)
            self.label_win._dirty = True
            self.label_win.pos = (m.rect[0] + bw + round(8 * m.scale), m.rect[1] + bw + round(6 * m.scale))
        if self.stop_win is not None:
            btxt = "▶  Reanudar Claude" if self.state == "paused" else "■  Detener Claude"
            img = pill(btxt, m.scale * 1.1, accent, bold=True, bg=(150, 30, 20, 235) if self.state != "paused" else (40, 110, 40, 235), dot=False)
            self.stop_win.set_image(img)
            x = m.rect[0] + (m.width - img.width) // 2
            y = m.rect[1] + bw + round(6 * m.scale)
            self.stop_win._dirty = True
            self.stop_win.pos = (x, y)
            self.stop_btn_rect = (x, y, x + img.width, y + img.height)
        self._apply_levels()

    def _render_ghost(self, ring: float):
        m = self._mon_at(*self.ghost_pos) or (self.monitors[0] if self.monitors else None)
        scale = m.scale if m else 1.0
        gcfg = self.cfg.get("ghost", {})
        color = _hex(gcfg.get("color", "#FF8C00"))
        if ring > 0 or self.ghost_scale != scale:
            self.ghost_win.set_image(ghost_image(scale, color, gcfg.get("label", "Claude"), ring))
            self.ghost_scale = scale if ring <= 0 else None
        self.ghost_win.update(pos=(self.ghost_pos[0] - GHOST_HOTSPOT, self.ghost_pos[1] - GHOST_HOTSPOT), alpha=1.0)

    def _render_trail(self):
        if not self.trail:
            return
        color = _hex(self.cfg.get("ghost", {}).get("color", "#FF8C00"))
        for i, win in enumerate(self.trail):
            if i >= len(self.trail_pos):
                win.update(alpha=0)
                continue
            x, y = self.trail_pos[-1 - i]
            if win.memdc is None:
                img = Image.new("RGBA", (12, 12), (0, 0, 0, 0))
                ImageDraw.Draw(img).ellipse((1, 1, 10, 10), fill=(*color, 255), outline=(255, 255, 255, 200))
                win.set_image(img)
            win.update(pos=(x - 6, y - 6), alpha=0.8 * (1 - i / max(1, len(self.trail))))

    def _apply_levels(self):
        bcfg = self.cfg.get("border", {})
        pulse = 1.0
        if self.state == "working":
            period = float(bcfg.get("pulse_period_ms", 1800)) / 1000
            lo = float(bcfg.get("pulse_min_factor", 0.55))
            ph = (time.perf_counter() % period) / period
            pulse = lo + (1 - lo) * (0.5 + 0.5 * math.cos(2 * math.pi * ph))
        visible = bcfg.get("enabled", True)
        for dev, strips in self.strips.items():
            lvl = self.level.get(dev, 0.0) * (pulse if dev == self.active else 1.0)
            for win in strips.values():
                win.update(alpha=lvl if visible else 0)
        act = self.level.get(self.active, 0.0) if self.active else 0.0
        if self.label_win is not None:
            self.label_win.update(alpha=act if self.cfg.get("border", {}).get("label", True) else 0)
        if self.stop_win is not None:
            self.stop_win.update(alpha=min(1.0, act * 1.2))

    # ------------------------------------------------------------ animation loop
    def _needs_fast(self) -> bool:
        return self.ghost_anim is not None or self.ghost_ring is not None or \
            (self.hilite_level > 0 and time.perf_counter() >= self.hilite_until - 0.01)

    def _animating(self) -> bool:
        fading = any(abs(self.level.get(d, 0) - self.target.get(d, 0)) > 1e-3 for d in self.level)
        return fading or self._needs_fast() or self.state == "working" or self.hilite_level > 0

    def _ensure_anim(self):
        if not self.ctrl_hwnd:
            return
        want = 0
        if self._animating():
            want = 16 if self._needs_fast() else 40
        if want != self._anim_interval:
            if want:
                w.user32.SetTimer(self.ctrl_hwnd, T_ANIM, want, None)
            else:
                w.user32.KillTimer(self.ctrl_hwnd, T_ANIM)
            self._anim_interval = want
        if not want:
            self._apply_levels()

    def _on_timer(self, tid: int):
        bcfg = self.cfg.get("border", {})
        if tid == T_REBUILD:
            w.user32.KillTimer(self.ctrl_hwnd, T_REBUILD)
            old = [(m.device, m.rect, m.dpi) for m in self.monitors]
            # Only rebuild on a REAL change. Our own freshly created windows get
            # WM_DPICHANGED when placed on a monitor with another scale; rebuilding
            # on that re-created them forever (found in test 7, 125 % on monitor 2).
            if old == [(m.device, m.rect, m.dpi) for m in enumerate_monitors()]:
                return
            self._build()
            if old != [(m.device, m.rect, m.dpi) for m in self.monitors]:
                log.info("Display configuration changed: %s", [m.to_dict() for m in self.monitors])
                if self.on_monitors_changed:
                    try:
                        self.on_monitors_changed()
                    except Exception:
                        log.exception("monitor change callback failed")
            return
        if tid == T_LINGER:
            w.user32.KillTimer(self.ctrl_hwnd, T_LINGER)
            if self.state == "working":
                self.state = "steady"
                self._render_label()
            w.user32.SetTimer(self.ctrl_hwnd, T_IDLE, int(float(bcfg.get("idle_hide_s", 20)) * 1000), None)
            self._ensure_anim()
            return
        if tid == T_IDLE:
            w.user32.KillTimer(self.ctrl_hwnd, T_IDLE)
            if self.state in ("steady", "working"):
                self.state = "idle"
                for d in self.target:
                    self.target[d] = 0.0
                self._fade_ghost_out = True
                self.ghost_win.update(alpha=0)
                for t in self.trail:
                    t.update(alpha=0)
            self._ensure_anim()
            return
        if tid != T_ANIM:
            return
        now = time.perf_counter()
        dt = now - self._last_frame if self._last_frame else 0.016
        self._last_frame = now
        fade = max(0.05, float(bcfg.get("fade_ms", 250)) / 1000)
        step = min(1.0, dt / fade)
        for d in self.level:
            cur, tgt = self.level[d], self.target.get(d, 0.0)
            self.level[d] = tgt if abs(tgt - cur) <= step else cur + step * (1 if tgt > cur else -1)
        # ghost glide
        if self.ghost_anim:
            p0, p1, t0, dur, ev = self.ghost_anim
            t = 1.0 if dur <= 0 else (now - t0) / dur
            self.ghost_pos = interpolate(p0, p1, t)
            self._render_ghost(0.0)
            if t >= 1.0:
                self.ghost_anim = None
                self.trail_pos.append(p1)
                self.trail_pos = self.trail_pos[-max(1, len(self.trail) or 1):]
                self._render_trail()
                if ev:
                    ev.set()
        if self.ghost_ring:
            t0, dur = self.ghost_ring
            t = (now - t0) / dur
            if t >= 1.0:
                self.ghost_ring = None
                self._render_ghost(0.0)
                self.ghost_scale = None
                self._render_ghost(0.0)
            else:
                self._render_ghost(t)
        if self.hilite_level > 0 and now >= self.hilite_until:
            self.hilite_level = max(0.0, self.hilite_level - dt / 0.3)
            self.hilite_win.update(alpha=self.hilite_level)
        self._apply_levels()
        if not self._animating():
            self._last_frame = 0.0
        self._ensure_anim()

    # ------------------------------------------------------------ info
    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "active_monitor": self.active,
            "label": self.label_text,
            "ghost_pos": self.ghost_pos,
            "windows": len(self.windows),
            "all_excluded_from_capture": self.excluded_from_capture(),
            "timer_ms": self._anim_interval,
            "stop_button_rect": self.stop_btn_rect,
            "error": self.error,
            "rebuilds": self.rebuilds,
            "border_geometry": {
                m.device: {"monitor_rect": m.rect, "dpi": m.dpi,
                           "strips": {side: (*win.pos, win.pos[0] + win.size[0], win.pos[1] + win.size[1])
                                      for side, win in self.strips.get(m.device, {}).items()}}
                for m in self.monitors},
        }
