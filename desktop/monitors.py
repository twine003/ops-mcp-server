"""Monitor enumeration and coordinate conversions.

Coordinate systems (the process is per-monitor DPI aware v2, so everything
below is in PHYSICAL pixels):

  virtual  - Windows virtual desktop: origin = top-left of the primary
             monitor; other monitors may have negative x/y.
  monitor  - relative to one monitor's top-left.
  client   - relative to a window's client area (Win32 messages use this).
  image    - pixel in a returned screenshot: the captured region, scaled.

The pure functions (no Win32 calls) are unit-tested in tests/test_coords.py.
"""

from __future__ import annotations

import ctypes
import math
from dataclasses import dataclass, asdict

from . import winapi as w


@dataclass(frozen=True)
class Monitor:
    index: int              # Windows display number (\\.\DISPLAYn -> n), as in Settings
    device: str             # \\.\DISPLAY1
    name: str               # friendly adapter/monitor string
    rect: tuple             # (left, top, right, bottom) in virtual coords
    work: tuple             # work area (without the taskbar)
    dpi: int
    primary: bool
    hmonitor: int = 0

    @property
    def scale(self) -> float:
        return self.dpi / 96.0

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]

    def contains(self, x: int, y: int) -> bool:
        return self.rect[0] <= x < self.rect[2] and self.rect[1] <= y < self.rect[3]

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("hmonitor")
        d["scale_percent"] = round(self.scale * 100)
        d["size"] = (self.width, self.height)
        return d


# ---------------------------------------------------------------- pure math
@dataclass(frozen=True)
class CaptureFrame:
    """Where a screenshot came from and how it was scaled.

    image (ix, iy) -> virtual (origin_x + ix / scale, origin_y + iy / scale)
    """
    origin_x: int
    origin_y: int
    width: int              # captured width in physical px (before scaling)
    height: int
    scale: float            # image px per physical px (0.5 = half size)

    def image_to_virtual(self, ix: float, iy: float) -> tuple[int, int]:
        x = self.origin_x + ix / self.scale
        y = self.origin_y + iy / self.scale
        # clamp inside the captured region: a click on the last image pixel
        # must never land one pixel outside it because of rounding.
        x = min(max(round(x), self.origin_x), self.origin_x + self.width - 1)
        y = min(max(round(y), self.origin_y), self.origin_y + self.height - 1)
        return int(x), int(y)

    def virtual_to_image(self, x: float, y: float) -> tuple[int, int]:
        return (round((x - self.origin_x) * self.scale), round((y - self.origin_y) * self.scale))

    @property
    def image_size(self) -> tuple[int, int]:
        return (max(1, round(self.width * self.scale)), max(1, round(self.height * self.scale)))


def virtual_bounds(monitors: list[Monitor]) -> tuple:
    return (min(m.rect[0] for m in monitors), min(m.rect[1] for m in monitors),
            max(m.rect[2] for m in monitors), max(m.rect[3] for m in monitors))


def monitor_at(monitors: list[Monitor], x: int, y: int) -> Monitor | None:
    for m in monitors:
        if m.contains(x, y):
            return m
    return None


def nearest_monitor(monitors: list[Monitor], x: int, y: int) -> Monitor:
    m = monitor_at(monitors, x, y)
    if m:
        return m

    def dist(mon):
        cx = min(max(x, mon.rect[0]), mon.rect[2] - 1)
        cy = min(max(y, mon.rect[1]), mon.rect[3] - 1)
        return (cx - x) ** 2 + (cy - y) ** 2
    return min(monitors, key=dist)


def monitor_for_rect(monitors: list[Monitor], rect: tuple) -> Monitor:
    """Monitor with the largest overlap with rect (like MonitorFromRect)."""
    best, best_area = None, -1
    for m in monitors:
        ox = max(0, min(rect[2], m.rect[2]) - max(rect[0], m.rect[0]))
        oy = max(0, min(rect[3], m.rect[3]) - max(rect[1], m.rect[1]))
        if ox * oy > best_area:
            best, best_area = m, ox * oy
    if best_area <= 0:
        return nearest_monitor(monitors, (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
    return best


def virtual_to_client(x: int, y: int, client_origin: tuple[int, int]) -> tuple[int, int]:
    return (x - client_origin[0], y - client_origin[1])


def client_to_virtual(cx: int, cy: int, client_origin: tuple[int, int]) -> tuple[int, int]:
    return (cx + client_origin[0], cy + client_origin[1])


def to_absolute_input(x: int, y: int, vbounds: tuple) -> tuple[int, int]:
    """Virtual px -> 0..65535 normalized coords for SendInput(ABSOLUTE|VIRTUALDESK).

    Windows maps normalized n to pixel floor(n * (W) / 65536) (roughly), so we
    aim at the pixel centre to survive rounding on every monitor.
    """
    vl, vt, vr, vb = vbounds
    vw, vh = vr - vl, vb - vt
    nx = int(((x - vl) + 0.5) * 65536 / vw)
    ny = int(((y - vt) + 0.5) * 65536 / vh)
    return (min(max(nx, 0), 65535), min(max(ny, 0), 65535))


def border_width_physical(width_px: int, monitor: Monitor, mode: str) -> int:
    """Border width on a given monitor.

    mode 'physical': same number of physical pixels everywhere (the spec:
    looks identical in px on every monitor regardless of scaling).
    mode 'logical': scaled with the monitor's DPI (same apparent size in
    Windows' logical units).
    """
    if mode == "logical":
        return max(1, round(width_px * monitor.scale))
    return max(1, int(width_px))


def interpolate(p0: tuple, p1: tuple, t: float) -> tuple[int, int]:
    """Ease-in-out cubic between two virtual points, t in [0,1]."""
    t = min(max(t, 0.0), 1.0)
    e = 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2
    return (round(p0[0] + (p1[0] - p0[0]) * e), round(p0[1] + (p1[1] - p0[1]) * e))


def densify_path(points: list[tuple], max_step: float = 6.0) -> list[tuple[int, int]]:
    """Insert intermediate points so no segment is longer than max_step px.

    Apps that sample WM_MOUSEMOVE draw straight chords between samples; a
    dense path keeps a curve looking like a curve.
    """
    if not points:
        return []
    out = [tuple(map(int, points[0]))]
    for a, b in zip(points, points[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        n = max(1, math.ceil((dx * dx + dy * dy) ** 0.5 / max_step))
        for i in range(1, n + 1):
            out.append((round(a[0] + dx * i / n), round(a[1] + dy * i / n)))
    return out


# ---------------------------------------------------------------- Win32
def enumerate_monitors() -> list[Monitor]:
    found: list[Monitor] = []

    @w.MONITORENUMPROC
    def _cb(hmon, hdc, lprect, lparam):
        info = w.MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        w.user32.GetMonitorInfoW(hmon, ctypes.byref(info))
        dx, dy = w.wt.UINT(), w.wt.UINT()
        dpi = 96
        if w.shcore.GetDpiForMonitor(hmon, w.MDT_EFFECTIVE_DPI, ctypes.byref(dx), ctypes.byref(dy)) == 0:
            dpi = int(dx.value)
        device = info.szDevice
        dd = w.DISPLAY_DEVICEW()
        dd.cb = ctypes.sizeof(dd)
        friendly = device
        if w.user32.EnumDisplayDevicesW(device, 0, ctypes.byref(dd), 0):
            friendly = dd.DeviceString or device
        try:
            idx = int("".join(ch for ch in device.rsplit("DISPLAY", 1)[-1] if ch.isdigit()))
        except ValueError:
            idx = len(found) + 1
        found.append(Monitor(index=idx, device=device, name=friendly, rect=info.rcMonitor.as_tuple(),
                             work=info.rcWork.as_tuple(), dpi=dpi,
                             primary=bool(info.dwFlags & w.MONITORINFOF_PRIMARY),
                             hmonitor=int(hmon or 0)))
        return True

    w.user32.EnumDisplayMonitors(None, None, _cb, 0)
    # Duplicate display numbers are possible in odd driver setups: keep them unique.
    seen = set()
    fixed = []
    for m in sorted(found, key=lambda m: (m.index, m.rect[0])):
        idx = m.index
        while idx in seen:
            idx += 100
        seen.add(idx)
        fixed.append(m if idx == m.index else Monitor(**{**asdict(m), "index": idx}))
    return fixed


def resolve_monitor(monitors: list[Monitor], spec, active: Monitor | None = None) -> Monitor:
    """spec: int index, 'active', 'primary', a device (\\\\.\\DISPLAY2) or a name substring."""
    if spec is None or spec == "" or spec == "active":
        if active is not None:
            for m in monitors:
                if m.device == active.device:
                    return m
        spec = "primary"
    if spec == "primary":
        return next((m for m in monitors if m.primary), monitors[0])
    if isinstance(spec, int) or (isinstance(spec, str) and spec.isdigit()):
        i = int(spec)
        for m in monitors:
            if m.index == i:
                return m
        raise ValueError(f"No monitor with index {i}. Available: {[m.index for m in monitors]}")
    s = str(spec).lower()
    for m in monitors:
        if m.device.lower() == s or s in m.name.lower() or s in m.device.lower():
            return m
    raise ValueError(f"Unknown monitor '{spec}'")
