"""Screen / window capture: DXGI Desktop Duplication (dxcam) -> mss (GDI) -> PrintWindow.

All calls must come from the automation worker thread (D3D/COM objects are
created there and reused).
Overlay windows set WDA_EXCLUDEFROMCAPTURE, so neither DXGI nor GDI sees them.
"""

from __future__ import annotations

import ctypes
import io
import logging
import time

import numpy as np
from PIL import Image

from . import winapi as w
from .monitors import CaptureFrame, Monitor

log = logging.getLogger("desktop-mcp.capture")

# dxcam retries a lost duplication FOREVER inside grab() (every ~5 s while the
# PC is locked: 2026-09-28 it blocked the automation thread for 49 min and
# every other tool timed out). We give up after this long instead.
DX_RECOVERY_GIVE_UP_S = 8.0


class ScreenUnavailable(RuntimeError):
    """Windows is not letting us see the screen (locked / UAC / Ctrl+Alt+Del)."""


def check_screen_available() -> None:
    desk = w.input_desktop()
    if desk is None or desk.lower() != "default":
        raise ScreenUnavailable(
            f"the screen is locked or a secure Windows prompt is showing (input desktop: {desk or 'inaccessible'}): "
            "Windows does not allow capture or input. Nothing to retry until the user unlocks the PC; "
            "the server recovers by itself afterwards.")


def _bound_dx_recovery(cam) -> None:
    """Make dxcam's endless recovery loop raise ScreenUnavailable after DX_RECOVERY_GIVE_UP_S."""
    rec = getattr(cam, "_display_recovery", None)
    wait = getattr(rec, "_wait", None)
    if wait is None or getattr(wait, "_dmcp_bounded", False):
        return
    orig_next, orig_reset = wait.next_delay_seconds, wait.reset
    state = {"t0": None}

    def next_delay_seconds():
        now = time.monotonic()
        if state["t0"] is None:
            state["t0"] = now
        elif now - state["t0"] > DX_RECOVERY_GIVE_UP_S:
            state["t0"] = None
            raise ScreenUnavailable(f"screen duplication lost for more than {DX_RECOVERY_GIVE_UP_S:.0f}s "
                                    "(PC locked, UAC prompt or display change still in progress)")
        return min(orig_next(), 1.0)

    def reset():
        state["t0"] = None
        return orig_reset()

    wait.next_delay_seconds, wait.reset, wait._dmcp_bounded = next_delay_seconds, reset, True


class Capturer:
    def __init__(self, cfg: dict):
        self.cfg = cfg.get("capture", {})
        self.backend_pref = self.cfg.get("backend", "auto")
        self._cams: dict[str, object] = {}     # device name -> DXCamera
        self._dx_ok = self.backend_pref in ("auto", "dxgi")
        self._mss = None
        self._last: dict[str, np.ndarray] = {}  # diff baseline per target key
        self.last_backend = ""

    # ------------------------------------------------------------ lifecycle
    def reset(self) -> None:
        """Monitors changed: drop all duplication objects, recreate lazily."""
        for cam in self._cams.values():
            try:
                cam.release()
            except Exception:
                pass
        self._cams.clear()
        # dxcam's factory only forgets an instance once nothing references it
        import gc
        gc.collect()
        if self._mss is not None:
            try:
                self._mss.close()
            except Exception:
                pass
            self._mss = None
        self._dx_ok = self.backend_pref in ("auto", "dxgi")

    def _camera(self, device: str):
        if device in self._cams:
            return self._cams[device]
        import dxcam
        # dxcam indexes outputs per adapter and keeps ONE instance per
        # (adapter, output): scan them all once and keep every camera, keyed
        # by device name. Releasing a non-matching one here would make the
        # next dxcam.create() hand back that dead instance.
        for dev_idx in range(4):
            for out_idx in range(8):
                try:
                    cam = dxcam.create(device_idx=dev_idx, output_idx=out_idx, output_color="BGRA")
                except Exception:
                    break
                if cam is None:
                    break
                _bound_dx_recovery(cam)
                self._cams.setdefault(getattr(cam._output, "devicename", f"{dev_idx}:{out_idx}"), cam)
        if device in self._cams:
            return self._cams[device]
        raise RuntimeError(f"No DXGI output for {device}")

    # ------------------------------------------------------------ primitives
    def _grab_monitor_dx(self, mon: Monitor) -> np.ndarray:
        check_screen_available()
        cam = self._camera(mon.device)
        for _ in range(8):
            frame = cam.grab(new_frame_only=False)
            if frame is not None:
                return np.asarray(frame)
            time.sleep(0.01)
        raise RuntimeError("DXGI returned no frame")

    def _grab_rect_mss(self, rect: tuple) -> np.ndarray:
        import mss
        if self._mss is None:
            self._mss = mss.mss()
        l, t, r, b = rect
        shot = self._mss.grab({"left": l, "top": t, "width": r - l, "height": b - t})
        return np.asarray(shot)  # BGRA

    def grab_rect(self, rect: tuple, monitors: list[Monitor]) -> np.ndarray:
        """BGRA array for any virtual-desktop rectangle (may span monitors)."""
        check_screen_available()
        l, t, r, b = rect
        if self._dx_ok:
            try:
                canvas = np.zeros((b - t, r - l, 4), dtype=np.uint8)
                touched = False
                for m in monitors:
                    ix0, iy0 = max(l, m.rect[0]), max(t, m.rect[1])
                    ix1, iy1 = min(r, m.rect[2]), min(b, m.rect[3])
                    if ix0 >= ix1 or iy0 >= iy1:
                        continue
                    frame = self._grab_monitor_dx(m)
                    fh, fw = frame.shape[:2]
                    if (fw, fh) != (m.width, m.height):
                        raise RuntimeError(f"DXGI frame {fw}x{fh} != monitor {m.width}x{m.height}")
                    canvas[iy0 - t:iy1 - t, ix0 - l:ix1 - l] = \
                        frame[iy0 - m.rect[1]:iy1 - m.rect[1], ix0 - m.rect[0]:ix1 - m.rect[0]]
                    touched = True
                if touched:
                    self.last_backend = "dxgi"
                    return canvas
            except ScreenUnavailable:
                raise  # GDI would return a black/stale image: report it instead
            except Exception as e:
                log.warning("DXGI capture failed (%s) - falling back to mss/GDI", e)
                if self.backend_pref == "dxgi":
                    raise
                self.reset()
                self._dx_ok = False  # stays on GDI until the next display change
        self.last_backend = "gdi"
        return self._grab_rect_mss(rect)

    def grab_window_printwindow(self, hwnd: int) -> tuple[np.ndarray, tuple]:
        """PrintWindow(PW_RENDERFULLCONTENT): works even when the window is covered."""
        rect = w.RECT()
        w.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width <= 0 or height <= 0:
            raise RuntimeError("window has no area (minimized?)")
        hdc_screen = w.user32.GetDC(None)
        memdc = w.gdi32.CreateCompatibleDC(hdc_screen)
        bmi = w.BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(w.BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = width, -height
        bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32
        bits = ctypes.c_void_p()
        hbmp = w.gdi32.CreateDIBSection(memdc, ctypes.byref(bmi), w.DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
        old = w.gdi32.SelectObject(memdc, hbmp)
        try:
            ok = w.user32.PrintWindow(hwnd, memdc, w.PW_RENDERFULLCONTENT)
            if not ok:
                raise RuntimeError("PrintWindow failed")
            buf = (ctypes.c_ubyte * (width * height * 4)).from_address(bits.value)
            arr = np.frombuffer(buf, dtype=np.uint8).reshape(height, width, 4).copy()
        finally:
            w.gdi32.SelectObject(memdc, old)
            w.gdi32.DeleteObject(hbmp)
            w.gdi32.DeleteDC(memdc)
            w.user32.ReleaseDC(None, hdc_screen)
        arr[..., 3] = 255
        # Crop GetWindowRect's invisible resize borders down to the DWM frame.
        fl, ft, fr, fb = w.window_rect(hwnd)
        cx0, cy0 = max(0, fl - rect.left), max(0, ft - rect.top)
        cx1, cy1 = min(width, fr - rect.left), min(height, fb - rect.top)
        if cx1 > cx0 and cy1 > cy0:
            arr = arr[cy0:cy1, cx0:cx1]
            return arr, (rect.left + cx0, rect.top + cy0, rect.left + cx1, rect.top + cy1)
        return arr, (rect.left, rect.top, rect.right, rect.bottom)

    # ------------------------------------------------------------ post-processing
    @staticmethod
    def to_image(bgra: np.ndarray) -> Image.Image:
        h, wd = bgra.shape[:2]
        return Image.frombuffer("RGBA", (wd, h), np.ascontiguousarray(bgra).tobytes(), "raw", "BGRA", 0, 1).convert("RGB")

    @staticmethod
    def scale_image(img: Image.Image, scale: float) -> Image.Image:
        if abs(scale - 1.0) < 1e-6:
            return img
        size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
        return img.resize(size, Image.Resampling.BILINEAR if scale < 1 else Image.Resampling.NEAREST)

    @staticmethod
    def encode(img: Image.Image, fmt: str, quality: int) -> tuple[bytes, str]:
        buf = io.BytesIO()
        fmt = (fmt or "jpeg").lower()
        if fmt in ("jpg", "jpeg"):
            img.save(buf, "JPEG", quality=int(quality), optimize=False)
            return buf.getvalue(), "jpeg"
        img.save(buf, "PNG", compress_level=1)
        return buf.getvalue(), "png"

    def diff(self, key: str, img: Image.Image) -> dict:
        """Compare with the previous capture of the same target.

        Returns {'changed': bool, 'bbox': (l,t,r,b) in image px or None, 'changed_ratio'}.
        """
        cur = np.asarray(img.convert("L"), dtype=np.int16)
        prev = self._last.get(key)
        self._last[key] = cur
        if prev is None or prev.shape != cur.shape:
            return {"changed": True, "bbox": None, "changed_ratio": 1.0, "baseline": "none"}
        mask = np.abs(cur - prev) > int(self.cfg.get("diff_threshold", 12))
        n = int(mask.sum())
        if n == 0:
            return {"changed": False, "bbox": None, "changed_ratio": 0.0}
        ys, xs = np.nonzero(mask)
        return {"changed": True, "bbox": (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1),
                "changed_ratio": round(n / mask.size, 5)}


def frame_for(rect: tuple, scale: float) -> CaptureFrame:
    return CaptureFrame(origin_x=rect[0], origin_y=rect[1], width=rect[2] - rect[0],
                        height=rect[3] - rect[1], scale=scale)
