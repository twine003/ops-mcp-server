"""Overlay windows on X11: the Linux LayeredWindow and the overlay UI loop.

Each overlay piece (border strips, label, stop button, ghost cursor,
highlight, trail) is an override-redirect window with a 32-bit ARGB visual,
so a compositor (xfwm4, picom, mutter, KWin) blends it per pixel. The global
fade level is _NET_WM_WINDOW_OPACITY. Without a compositor the window is
shaped to its opaque pixels (no black boxes) and the fade becomes on/off.

Click-through windows get an EMPTY input shape: the pointer, real or XTEST,
goes to the app underneath.

Capture exclusion does not exist on X: the overlay is always in screenshots
and in what RustDesk streams (the Windows option remote_visible=true).
"""

from __future__ import annotations

import logging
import os
import queue
import time

import numpy as np
from PIL import Image
from Xlib import X, Xatom
from Xlib.ext import shape

from . import winapi as w
from . import xconn as xc

log = logging.getLogger("desktop-mcp.overlay")


def _argb_visual():
    dpy = xc.d()
    scr = dpy.screen()
    for depth in scr.allowed_depths:
        if depth.depth != 32:
            continue
        for v in depth.visuals:
            if v.visual_class == X.TrueColor:
                return 32, v.visual_id
    return scr.root_depth, scr.root_visual


def _mask_rects(alpha: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Opaque pixels as horizontal runs (x, y, w, h), merged row-wise when identical."""
    rects = []
    on = alpha > 24
    prev = None
    for y in range(on.shape[0]):
        row = on[y]
        if not row.any():
            prev = None
            continue
        d = np.diff(np.concatenate(([0], row.view(np.int8), [0])))
        starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
        runs = tuple(zip(starts.tolist(), ends.tolist()))
        if prev is not None and prev[0] == runs:
            for i, (s, e) in enumerate(runs):
                rx, ry, rw, rh = rects[prev[1] + i]
                rects[prev[1] + i] = (rx, ry, rw, rh + 1)
            continue
        base = len(rects)
        for s, e in runs:
            rects.append((s, y, e - s, 1))
        prev = (runs, base)
    return rects


class LayeredWindow:
    """Same interface as overlay.LayeredWindow (Win32 UpdateLayeredWindow)."""

    def __init__(self, owner, clickthrough: bool = True, kind: str = ""):
        dpy = xc.d()
        scr = dpy.screen()
        self.depth, visual = _argb_visual()
        self.composited = xc.has_compositor()
        cmap = scr.root.create_colormap(visual, X.AllocNone) if self.depth == 32 else scr.default_colormap
        self.win = scr.root.create_window(
            0, 0, 1, 1, 0, self.depth, X.InputOutput, visual,
            background_pixel=0, border_pixel=0, colormap=cmap, override_redirect=True,
            event_mask=X.ButtonPressMask | X.ButtonReleaseMask | X.ExposureMask)
        self.win.set_wm_name("Claude overlay")
        self.win.set_wm_class("desktop-mcp", "DesktopMcpOverlay")
        self.win.change_property(xc.atom("_NET_WM_PID"), Xatom.CARDINAL, 32, [os.getpid()])
        self.win.change_property(xc.atom("_NET_WM_WINDOW_TYPE"), Xatom.ATOM, 32,
                                 [xc.atom("_NET_WM_WINDOW_TYPE_NOTIFICATION")])
        if clickthrough:
            self.win.shape_rectangles(shape.SO.Set, shape.SK.Input, X.Unsorted, 0, 0, [])
        self.gc = self.win.create_gc(foreground=0, background=0)
        self.hwnd = int(self.win.id)
        self.kind = kind
        self.excluded = False
        owner.windows[self.hwnd] = self
        self.memdc = None        # "has a bitmap" (name kept from the Win32 class)
        self.size = (0, 0)
        self.pos = (0, 0)
        self.alpha = 0
        self.visible = False
        self._dirty = True
        self._data = b""
        self._data_id = 0
        self._shape = None
        self._applied = None
        self._pixels_dirty = False

    def set_bitmap(self, bgra_premul: bytes, size: tuple[int, int]) -> None:
        wd, ht = size
        self._data = bgra_premul
        self._data_id += 1
        self.size = (int(wd), int(ht))
        self.memdc = True
        self._dirty = True
        self._pixels_dirty = True
        if not self.composited or self.depth != 32:
            a = np.frombuffer(bgra_premul, dtype=np.uint8).reshape(ht, wd, 4)[..., 3]
            self._shape = _mask_rects(a)
        else:
            self._shape = None

    def set_image(self, img: Image.Image) -> None:
        from ..overlay import _premul_bgra
        self.set_bitmap(_premul_bgra(np.asarray(img.convert("RGBA"))), img.size)

    def _paint(self) -> None:
        wd, ht = self.size
        if wd <= 0 or ht <= 0:
            return
        if self.depth == 32:
            data = self._data
        else:  # 24-bit fallback: drop alpha (the shape hides the transparent pixels)
            data = self._data
        row = wd * 4
        step = max(1, 200_000 // max(1, row))       # stay far below the X request size limit
        for y in range(0, ht, step):
            h = min(step, ht - y)
            self.win.put_image(self.gc, 0, y, wd, h, X.ZPixmap, self.depth, 0, data[y * row:(y + h) * row])
        self._pixels_dirty = False

    def update(self, pos: tuple[int, int] | None = None, alpha: float | None = None) -> None:
        if self.memdc is None:
            return
        new_pos = self.pos if pos is None else (int(pos[0]), int(pos[1]))
        new_alpha = self.alpha if alpha is None else int(max(0, min(255, round(alpha * 255))))
        if not self._dirty and new_pos == self.pos and new_alpha == self.alpha:
            return
        self.pos, self.alpha, self._dirty = new_pos, new_alpha, False
        if self.alpha == 0 and not self.visible:
            return
        # the size/shape must reach the X window even if the bitmap changed while hidden
        geometry_changed = self._applied != (self.size, id(self._shape), self._data_id)
        try:
            if geometry_changed:
                self._applied = (self.size, id(self._shape), self._data_id)
                self._pixels_dirty = True
                self.win.configure(x=self.pos[0], y=self.pos[1], width=max(1, self.size[0]),
                                   height=max(1, self.size[1]))
                if self._shape is not None:
                    self.win.shape_rectangles(shape.SO.Set, shape.SK.Bounding, X.Unsorted, 0, 0, self._shape)
            else:
                self.win.configure(x=self.pos[0], y=self.pos[1])
            if self.composited:
                self.win.change_property(xc.atom("_NET_WM_WINDOW_OPACITY"), Xatom.CARDINAL, 32,
                                         [int(self.alpha / 255 * 0xFFFFFFFF)])
            want = self.alpha > (0 if self.composited else 40)
            if want and not self.visible:
                self.win.map()
                self.win.configure(stack_mode=X.Above)
                self.visible = True
                self._pixels_dirty = True
            elif not want and self.visible:
                self.win.unmap()
                self.visible = False
            if self.visible and (geometry_changed or self._pixels_dirty):
                self._paint()
            xc.d().flush()
        except Exception:  # noqa: BLE001
            log.exception("overlay window update failed (%s)", self.kind)

    def repaint(self) -> None:
        if self.visible:
            self._paint()

    def raise_top(self) -> None:
        if self.visible:
            self.win.configure(stack_mode=X.Above)

    def destroy(self, owner) -> None:
        owner.windows.pop(self.hwnd, None)
        try:
            self.win.destroy()
            xc.d().flush()
        except Exception:  # noqa: BLE001
            pass


def _handle(ov, item) -> None:
    """One queued overlay command (same as Overlay._drain does for each item)."""
    cmd, kw, ev = item
    try:
        getattr(ov, "_cmd_" + cmd)(ev=ev, **kw)
    except Exception:  # noqa: BLE001
        log.exception("overlay command %s failed", cmd)
        if ev:
            ev.set()
    else:
        if ev and cmd != "ghost_move":
            ev.set()


def run_loop(ov) -> None:
    """Overlay UI thread on X11: commands, timers (winapi SetTimer table), X events
    (stop button clicks, Expose), and a cheap display-change poll."""
    from ..overlay import T_REBUILD
    try:
        xc.d()
        ov.ctrl_hwnd = 1                                   # truthy: the Win32 code tests it
        ov._build()
    except Exception as e:  # noqa: BLE001
        ov.error = f"overlay init failed: {e}"
        log.exception(ov.error)
    ov.ready.set()
    dpy = xc.d()
    last_check = time.monotonic()
    while True:
        try:
            while dpy.pending_events():
                ev = dpy.next_event()
                if ev.type == X.Expose:
                    lw = ov.windows.get(int(ev.window.id))
                    if lw is not None and ev.count == 0:
                        lw.repaint()
                elif ev.type == X.ButtonRelease and ev.detail == 1:
                    if ov.stop_win is not None and int(ev.window.id) == ov.stop_win.hwnd:
                        if ov.stop_state.stopped:
                            ov.stop_state.resume("stop button")
                        else:
                            ov.stop_state.stop("stop button")
            ov._drain()
            for _h, tid in w.timers_due():
                ov._on_timer(tid)
            now = time.monotonic()
            if now - last_check > 3:
                last_check = now
                ov._on_timer(T_REBUILD)   # compares the monitors itself: rebuilds only on a real change
            dpy.flush()
            try:
                item = ov.q.get(timeout=min(0.05, w.timers_next_delay(0.05)))
            except queue.Empty:
                continue
            _handle(ov, item)
        except Exception:  # noqa: BLE001
            log.exception("overlay loop error")
            time.sleep(0.2)
