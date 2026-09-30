"""
Desktop MCP Server - desktop "computer use" for Claude Code
================================================================

Runs INSIDE the interactive user session (scheduled task "at logon"), not as
a service: session 0 has no access to the desktop (capture, overlay and input
would all fail there).

Action mechanisms, in order of preference (every tool reports which one ran
and whether the real cursor moved):
  1. UI Automation patterns (Invoke/Value/Toggle/SelectionItem/ExpandCollapse/
     Text/LegacyIAccessible)             -> real cursor untouched, no focus change
  2. Win32 messages to the HWND (BM_CLICK, WM_SETTEXT, EM_REPLACESEL, WM_CHAR,
     WM_KEYDOWN/UP, CB_SETCURSEL, WM_LBUTTONDOWN/UP) -> cursor untouched
  3. SendInput (drawing, dragging, apps that ignore 1 and 2) -> the real cursor
     moves and is put back where it was. Optionally (off by default) the
     user's physical mouse is frozen for the gesture only.

The user's own mouse/keyboard activity never pauses Claude. The only stops:
the on-screen "Detener Claude" button, the stop hotkey (backup), pause(),
or a chat message to the assistant (effective between tool calls).

Usage:
    python desktop_server.py [--port 8011] [--host 127.0.0.1]
"""

from __future__ import annotations

# DPI awareness MUST be set before any window, DC or UIA object exists.
import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent
if __package__ in (None, ""):
    # launched as a script: make "desktop" importable as a package and reach
    # mcp_common.py one level up (both live in this project folder).
    sys.path.insert(0, str(_PKG_DIR.parent))
    __package__ = "desktop"

from desktop import winapi as w  # noqa: E402

DPI_MODE = w.set_dpi_awareness()

import asyncio  # noqa: E402
import concurrent.futures  # noqa: E402
import ctypes  # noqa: E402
import ipaddress  # noqa: E402
import os  # noqa: E402
import queue  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from collections import OrderedDict  # noqa: E402
from datetime import datetime  # noqa: E402

import mcp_common  # noqa: E402
from desktop import capture as cap  # noqa: E402
from desktop import inputs as inp  # noqa: E402
from desktop import uia  # noqa: E402
from desktop import windows as wins  # noqa: E402
from desktop.monitors import (CaptureFrame, Monitor, enumerate_monitors, monitor_at,  # noqa: E402
                              monitor_for_rect, nearest_monitor, resolve_monitor, virtual_bounds)
from desktop.overlay import Overlay  # noqa: E402
from desktop.recorder import Recorder  # noqa: E402
from desktop.safety import (DATA_DIR, AuditLog, Policy, PolicyError, StoppedError,  # noqa: E402
                            StopState, load_config)

VERSION = "1.0.0"
PROJECT_ROOT = _PKG_DIR.parent.parent
mcp_common.load_env(PROJECT_ROOT)
logger = mcp_common.setup_logging("desktop-mcp", DATA_DIR / "server.log")

CFG = load_config()
OWN_PID = os.getpid()


def _user_env(name: str) -> str:
    """Process env first, then the user's registry environment (HKCU "Environment" key):
    a task started before the installer set the variable would not inherit it."""
    v = os.getenv(name, "")
    if v:
        return v
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return str(winreg.QueryValueEx(k, name)[0])
    except OSError:
        return ""


class Config:
    HOST = os.getenv("DESKTOP_MCP_HOST", CFG.get("host", "127.0.0.1"))
    PORT = int(os.getenv("DESKTOP_MCP_PORT", CFG.get("port", 8011)))
    API_KEY = _user_env("DESKTOP_MCP_API_KEY")


# =============================================================================
# Runtime objects
# =============================================================================

STOP = StopState()
POLICY = Policy(CFG)
AUDIT = AuditLog(CFG)
GUARD = inp.InputGuard(STOP, CFG.get("stop_hotkey", "ctrl+alt+shift+f12"))


class Worker:
    """Single automation thread: UIA/COM objects are apartment-bound, D3D
    duplication objects too, and there is only one real cursor anyway -
    actions are serialized here."""

    def __init__(self):
        self.q: "queue.Queue" = queue.Queue()
        self.busy_since = 0.0
        self.current = ""
        self.capturer: cap.Capturer | None = None
        self.t = threading.Thread(target=self._run, name="automation", daemon=True)
        self.t.start()

    def _run(self):
        import comtypes
        comtypes.CoInitialize()
        self.capturer = cap.Capturer(CFG)
        while True:
            fn, args, kwargs, fut, name = self.q.get()
            if not fut.set_running_or_notify_cancel():
                continue
            self.busy_since, self.current = time.time(), name
            try:
                fut.set_result(fn(*args, **kwargs))
            except BaseException as e:  # noqa: BLE001
                fut.set_exception(e)
            finally:
                self.busy_since, self.current = 0.0, ""

    def submit(self, name, fn, *args, **kwargs) -> concurrent.futures.Future:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self.q.put((fn, args, kwargs, fut, name))
        return fut


WORKER = Worker()


def _on_monitors_changed():
    WORKER.submit("capture_reset", lambda: WORKER.capturer and WORKER.capturer.reset())


OVERLAY = Overlay(CFG, STOP, on_monitors_changed=_on_monitors_changed)
STOP.on_change(lambda paused, reason: OVERLAY.post("paused", paused=paused, reason=reason))

FRAMES: "OrderedDict[int, CaptureFrame]" = OrderedDict()
_frame_counter = [0]


def _remember_frame(frame: CaptureFrame) -> int:
    _frame_counter[0] += 1
    FRAMES[_frame_counter[0]] = frame
    while len(FRAMES) > 100:
        FRAMES.popitem(last=False)
    return _frame_counter[0]


# =============================================================================
# Helpers (run on the automation thread)
# =============================================================================

def _monitors() -> list[Monitor]:
    return enumerate_monitors()


def _active_monitor(mons: list[Monitor]) -> Monitor | None:
    if OVERLAY.active:
        return next((m for m in mons if m.device == OVERLAY.active), None)
    return None


def _mode_text(process: str) -> str:
    if POLICY.write_allowed(process):
        return f"escritura permitida en {process}"
    return "solo lectura"


def _target_from_window(hwnd: int, mons: list[Monitor]) -> dict:
    info = wins.window_info(hwnd, mons)
    mon = monitor_for_rect(mons, info["rect"])
    return {"hwnd": hwnd, "process": info["process"], "title": info["title"], "monitor": mon, "rect": info["rect"]}


def _target_from_point(x: int, y: int, mons: list[Monitor]) -> dict:
    h = wins.window_at(x, y)
    if not h:
        raise PolicyError(f"no window at ({x},{y})")
    root = wins.root_window(h)
    if w.window_pid(root) == OWN_PID:
        raise PolicyError(f"({x},{y}) is on Claude's own overlay (stop button) - refused")
    t = _target_from_window(root, mons)
    t["monitor"] = nearest_monitor(mons, x, y)
    t["child"] = h
    return t


def _announce(target: dict, point: tuple | None = None, rect: tuple | None = None, glide: bool = True) -> None:
    """Border to the target's monitor, ghost cursor glides to the point, highlight."""
    mon: Monitor = target.get("monitor")
    title = (target.get("title") or target.get("process") or "")[:60]
    OVERLAY.activity(mon.device if mon else None, title, _mode_text(target.get("process", "")))
    if rect:
        OVERLAY.highlight(rect)
    if point and glide:
        OVERLAY.ghost_move(int(point[0]), int(point[1]), wait=True)


def _require_running():
    if STOP.stopped:
        raise StoppedError(f"Claude is paused ({STOP.reason}). Resume with the on-screen button or resume().")
    cap.check_screen_available()  # locked PC: input would go nowhere


def _audit(tool: str, t0: float, target: dict | None = None, **extra):
    rec = {"tool": tool, "ms": round((time.perf_counter() - t0) * 1000, 1)}
    if target:
        mon = target.get("monitor")
        rec.update({"monitor": mon.index if mon else None, "window": target.get("title"),
                    "process": target.get("process"), "hwnd": target.get("hwnd")})
    rec.update(extra)
    AUDIT.write(rec)


def _center(rect) -> tuple[int, int]:
    return ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)


def _resolve_point(x, y, capture_id) -> tuple[int, int]:
    if x is None or y is None:
        raise ValueError("x and y are required (or pass element)")
    if capture_id is not None:
        frame = FRAMES.get(int(capture_id))
        if frame is None:
            raise ValueError(f"unknown capture_id {capture_id} (only the last 100 are kept)")
        return frame.image_to_virtual(float(x), float(y))
    return int(x), int(y)


def _element_target(element: str, mons: list[Monitor]):
    kind, elem, hwnd = uia.resolve(element)
    if kind == "uia":
        info = uia.live_info(elem)
        root = wins.root_window(info["hwnd"] or hwnd) if (info["hwnd"] or hwnd) else 0
        process = w.process_name(info["pid"]) if info["pid"] else ""
        title = w.window_text(root) if root else ""
        rect = info["rect"]
    else:
        info = {"rect": w.window_rect(hwnd), "password": uia._is_win32_password(hwnd)}
        root = wins.root_window(hwnd)
        process = w.process_name(w.window_pid(root))
        title = w.window_text(root)
        rect = info["rect"]
    mon = monitor_for_rect(mons, rect) if rect else (monitor_for_rect(mons, w.window_rect(root)) if root else None)
    target = {"hwnd": root, "process": process, "title": title, "monitor": mon, "rect": rect}
    return kind, elem, hwnd, info, target


def _err(e: Exception) -> dict:
    kind = ("refused" if isinstance(e, PolicyError) else "paused" if isinstance(e, StoppedError)
            else "screen_locked" if isinstance(e, cap.ScreenUnavailable)
            else "not_found" if isinstance(e, (uia.ElementNotFound, LookupError)) else "error")
    return {"ok": False, "error_type": kind, "error": str(e)}


def _job(name, fn, *args, **kwargs):
    """Wrap a job: uniform errors + audit of failures."""
    t0 = time.perf_counter()
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        logger.info("%s failed: %s", name, e)
        AUDIT.write({"tool": name, "ok": False, "error": str(e), "ms": round((time.perf_counter() - t0) * 1000, 1)})
        return _err(e)


async def _run(name, fn, *args, timeout: float = 120, **kwargs):
    fut = WORKER.submit(name, _job, name, fn, *args, **kwargs)
    try:
        return await asyncio.wait_for(asyncio.wrap_future(fut), timeout=timeout)
    except asyncio.TimeoutError:
        return {"ok": False, "error_type": "timeout",
                "error": f"{name} still running after {timeout}s (the automation thread is busy: "
                         f"{WORKER.current})"}


# =============================================================================
# Tool implementations
# =============================================================================

def _draw_claude_cursor(img, frame: CaptureFrame) -> dict | None:
    """Mark Claude's (overlay) cursor on a capture - the overlay itself is
    excluded from capture, so without this the image never shows it."""
    pos = OVERLAY.ghost_pos
    if pos is None:
        return None
    x, y = pos
    inside = frame.origin_x <= x < frame.origin_x + frame.width and frame.origin_y <= y < frame.origin_y + frame.height
    info = {"virtual": (int(x), int(y)), "in_image": inside}
    if not inside:
        return info
    from PIL import ImageDraw
    ix, iy = frame.virtual_to_image(x, y)
    info["image"] = (ix, iy)
    d = ImageDraw.Draw(img)
    r = max(7, round(14 * min(1.0, frame.scale * 1.6)))
    for col, wd in (((255, 255, 255), 5), ((255, 120, 0), 3)):
        d.line((ix - r, iy, ix + r, iy), fill=col, width=wd)
        d.line((ix, iy - r, ix, iy + r), fill=col, width=wd)
    d.ellipse((ix - r // 2, iy - r // 2, ix + r // 2, iy + r // 2), outline=(255, 120, 0), width=2)
    tx, ty = ix + r + 3, iy + 2
    d.rectangle((tx - 2, ty - 1, tx + 44, ty + 13), fill=(255, 120, 0))
    d.text((tx + 1, ty), "Claude", fill=(255, 255, 255))
    return info


def _screenshot(target, region, scale, fmt, quality, diff, window_mode, show_cursor=True):
    t0 = time.perf_counter()
    mons = _monitors()
    capr = WORKER.capturer
    ccfg = CFG.get("capture", {})
    scale = float(scale if scale is not None else ccfg.get("default_scale", 0.5))
    scale = min(max(scale, 0.05), 2.0)
    fmt = fmt or ccfg.get("default_format", "jpeg")
    quality = int(quality or ccfg.get("jpeg_quality", 70))
    tgt = {"monitor": None}
    desc = {}
    t_cap = time.perf_counter()
    if region:
        rect = tuple(int(v) for v in region)
        if rect[2] <= rect[0] or rect[3] <= rect[1]:
            raise ValueError("region must be [left, top, right, bottom] in virtual-desktop px")
        vb = virtual_bounds(mons)
        rect = (max(rect[0], vb[0]), max(rect[1], vb[1]), min(rect[2], vb[2]), min(rect[3], vb[3]))
        arr = capr.grab_rect(rect, mons)
        tgt["monitor"] = monitor_for_rect(mons, rect)
        desc = {"kind": "region"}
        backend = capr.last_backend
    elif isinstance(target, str) and target.lower().startswith("window:"):
        hwnd = wins.find_window(target.split(":", 1)[1], mons, OWN_PID)
        tgt = _target_from_window(hwnd, mons)
        POLICY.check_read(tgt["process"], tgt["title"])
        if w.user32.IsIconic(hwnd):
            raise RuntimeError("window is minimized - restore it (focus_window/move_window) first")
        arr = None
        backend = ""
        if window_mode in ("auto", "printwindow"):
            try:
                arr, rect = capr.grab_window_printwindow(hwnd)
                backend = "printwindow"
                if window_mode == "auto" and float(arr[..., :3].mean()) < 1.5:
                    arr = None  # all black: app doesn't support PrintWindow
            except Exception as e:  # noqa: BLE001
                if window_mode == "printwindow":
                    raise
                logger.info("PrintWindow failed (%s), using screen crop", e)
                arr = None
        if arr is None:
            vb = virtual_bounds(mons)
            r = tgt["rect"]
            rect = (max(r[0], vb[0]), max(r[1], vb[1]), min(r[2], vb[2]), min(r[3], vb[3]))
            arr = capr.grab_rect(rect, mons)
            backend = capr.last_backend + "+crop"
        desc = {"kind": "window", "hwnd": hwnd, "title": tgt["title"], "process": tgt["process"],
                "note": "printwindow = window content even if covered" if backend == "printwindow" else
                "screen crop = what is visible on screen at the window's rect"}
    elif target in ("all", "virtual", "desktop"):
        rect = virtual_bounds(mons)
        arr = capr.grab_rect(rect, mons)
        desc = {"kind": "virtual_desktop"}
        backend = capr.last_backend
    else:
        mon = resolve_monitor(mons, target, _active_monitor(mons))
        rect = mon.rect
        arr = capr.grab_rect(rect, mons)
        tgt["monitor"] = mon
        desc = {"kind": "monitor", "monitor": mon.index}
        backend = capr.last_backend
    capture_ms = (time.perf_counter() - t_cap) * 1000

    img = capr.to_image(arr)
    img = capr.scale_image(img, scale)
    frame = CaptureFrame(rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1], scale)
    meta = {"ok": True, **desc, "backend": backend, "virtual_rect": rect, "scale": scale,
            "image_size": img.size}
    key = f"{target}|{region}|{scale}|{window_mode}"
    if diff and diff != "none":
        d = capr.diff(key, img)
        meta["diff"] = d
        if not d["changed"]:
            meta.update({"capture_ms": round(capture_ms, 1), "changed": False})
            _audit("screenshot", t0, tgt if tgt.get("monitor") else None, changed=False)
            return meta
        if diff == "region" and d.get("bbox"):
            m = 8
            l, t, r, b = d["bbox"]
            l, t = max(0, l - m), max(0, t - m)
            r, b = min(img.width, r + m), min(img.height, b + m)
            img = img.crop((l, t, r, b))
            frame = CaptureFrame(rect[0] + round(l / scale), rect[1] + round(t / scale),
                                 round((r - l) / scale), round((b - t) / scale), scale)
            meta["cropped_to_change"] = (l, t, r, b)
            meta["virtual_rect"] = (frame.origin_x, frame.origin_y, frame.origin_x + frame.width,
                                    frame.origin_y + frame.height)
            meta["image_size"] = img.size
    else:
        capr._last.pop(key, None)
    if show_cursor:
        ci = _draw_claude_cursor(img, frame)
        if ci:
            meta["claude_cursor"] = ci
    meta["user_cursor_virtual"] = w.cursor_pos()
    t_enc = time.perf_counter()
    data, fmt_used = capr.encode(img, fmt, quality)
    meta["encode_ms"] = round((time.perf_counter() - t_enc) * 1000, 1)
    meta["capture_ms"] = round(capture_ms, 1)
    meta["bytes"] = len(data)
    cid = _remember_frame(frame)
    meta["capture_id"] = cid
    mon = tgt.get("monitor")
    if mon is not None:
        meta["monitor_info"] = {"index": mon.index, "dpi": mon.dpi, "scale_percent": round(mon.scale * 100),
                                "rect": mon.rect}
        OVERLAY.activity(mon.device, (tgt.get("title") or f"captura monitor {mon.index}")[:60],
                         _mode_text(tgt.get("process", "")))
    meta["coords"] = ("image (ix,iy) -> virtual (x,y): x = virtual_rect[0] + ix/scale, "
                      "y = virtual_rect[1] + iy/scale. Or pass capture_id to click/drag with image coords.")
    _audit("screenshot", t0, tgt if tgt.get("monitor") else None, kind=desc.get("kind"), backend=backend,
           capture_ms=meta["capture_ms"])
    return {"__image__": (data, fmt_used), "meta": meta}


def _list_windows(include_all):
    mons = _monitors()
    out = []
    for x in wins.list_windows(mons, OWN_PID, include_all):
        x["denied"] = POLICY.is_denied(x["process"], x["title"])
        x["write_allowed"] = POLICY.write_allowed(x["process"], x["title"])
        out.append(x)
    return {"ok": True, "windows": out}


def _launch_app(name, args, monitor, maximize):
    t0 = time.perf_counter()
    _require_running()
    exe = os.path.basename(name).lower()
    exe = exe if exe.endswith(".exe") else exe + ".exe"
    POLICY.check_write(exe)
    mons = _monitors()
    hwnd = wins.launch(name, args, mons, OWN_PID)
    time.sleep(0.4)
    final_rect = w.window_rect(hwnd)
    if monitor is not None and monitor != "":
        mon = resolve_monitor(mons, monitor, _active_monitor(mons))
        final_rect = wins.move_to_monitor(hwnd, mon, None, bool(maximize))
    elif maximize:
        w.user32.ShowWindow(hwnd, w.SW_SHOWMAXIMIZED)
    tgt = _target_from_window(hwnd, _monitors())
    _announce(tgt, _center(final_rect), None)
    _audit("launch_app", t0, tgt, app=name, mechanism="CreateProcess")
    return {"ok": True, "hwnd": hwnd, "title": tgt["title"], "process": tgt["process"],
            "monitor": tgt["monitor"].index, "rect": w.window_rect(hwnd), "mechanism": "CreateProcess",
            "cursor_moved": False}


def _focus_window(window):
    t0 = time.perf_counter()
    _require_running()
    mons = _monitors()
    hwnd = wins.find_window(window, mons, OWN_PID)
    tgt = _target_from_window(hwnd, mons)
    POLICY.check_write(tgt["process"], tgt["title"])
    _announce(tgt, _center(tgt["rect"]), None)
    res = wins.focus(hwnd)
    _audit("focus_window", t0, tgt, mechanism=res["method"], focused=res["focused"])
    return {"ok": True, "hwnd": hwnd, **res, "cursor_moved": False,
            "note": "focus_window takes the keyboard focus from whatever you were using"}


def _move_window(window, monitor, rect, maximize):
    t0 = time.perf_counter()
    _require_running()
    mons = _monitors()
    hwnd = wins.find_window(window, mons, OWN_PID)
    tgt = _target_from_window(hwnd, mons)
    POLICY.check_write(tgt["process"], tgt["title"])
    mon = resolve_monitor(mons, monitor, _active_monitor(mons))
    final = wins.move_to_monitor(hwnd, mon, tuple(rect) if rect else None, bool(maximize))
    tgt = _target_from_window(hwnd, _monitors())
    _announce(tgt, _center(final), final)
    _audit("move_window", t0, tgt, mechanism="SetWindowPos(SWP_NOACTIVATE)")
    return {"ok": True, "hwnd": hwnd, "monitor": tgt["monitor"].index, "rect": final,
            "mechanism": "SetWindowPos(SWP_NOACTIVATE)", "cursor_moved": False}


def _ui_tree(window, depth, flt, max_nodes, backend):
    t0 = time.perf_counter()
    mons = _monitors()
    hwnd = wins.find_window(window, mons, OWN_PID)
    tgt = _target_from_window(hwnd, mons)
    POLICY.check_read(tgt["process"], tgt["title"])
    nodes, used, uia_error = [], backend, None
    if backend in ("auto", "uia"):
        try:
            nodes = uia.uia_tree(hwnd, int(depth), flt, int(max_nodes))
            used = "uia"
        except Exception as e:  # noqa: BLE001
            if backend == "uia":
                raise
            uia_error = str(e)
            logger.info("UIA tree failed (%s) - Win32 fallback", e)
    if backend == "win32" or (backend == "auto" and len(nodes) <= 1):
        w32 = uia.win32_tree(hwnd, flt, int(max_nodes))
        if backend == "win32" or w32:
            nodes = nodes + w32 if backend == "auto" else w32
            used = "uia+win32" if backend == "auto" and used == "uia" else "win32"
    OVERLAY.activity(tgt["monitor"].device, tgt["title"][:60], _mode_text(tgt["process"]))
    _audit("ui_tree", t0, tgt, backend=used, nodes=len(nodes))
    return {"ok": True, "hwnd": hwnd, "title": tgt["title"], "process": tgt["process"], "backend": used,
            "count": len(nodes), "truncated": len(nodes) >= int(max_nodes), "nodes": nodes,
            **({"uia_error": uia_error} if uia_error else {}),
            "legend": "d=depth, pat=available patterns, aid=AutomationId, cls=ClassName, rect=virtual px"}


def _find_element(window, query, max_results):
    t0 = time.perf_counter()
    mons = _monitors()
    hwnd = wins.find_window(window, mons, OWN_PID)
    tgt = _target_from_window(hwnd, mons)
    POLICY.check_read(tgt["process"], tgt["title"])
    res = uia.find(hwnd, query, int(max_results))
    if res and res[0].get("rect"):
        OVERLAY.activity(tgt["monitor"].device, tgt["title"][:60], _mode_text(tgt["process"]))
        OVERLAY.highlight(res[0]["rect"])
    _audit("find_element", t0, tgt, query=query, found=len(res))
    return {"ok": True, "count": len(res), "elements": res}


def _read_value(element):
    t0 = time.perf_counter()
    mons = _monitors()
    kind, elem, hwnd, info, tgt = _element_target(element, mons)
    POLICY.check_read(tgt["process"], tgt["title"])
    if info.get("password"):
        raise PolicyError("password field: reading refused")
    _announce(tgt, _center(tgt["rect"]) if tgt["rect"] else None, tgt["rect"])
    res = uia.read_value(kind, elem, hwnd)
    _audit("read_value", t0, tgt, element=element, mechanism=res["via"], chars=len(res["value"] or ""))
    return {"ok": True, "element": element, "value": res["value"], "mechanism": res["via"], "cursor_moved": False}


def _set_value(element, text):
    t0 = time.perf_counter()
    _require_running()
    mons = _monitors()
    kind, elem, hwnd, info, tgt = _element_target(element, mons)
    POLICY.check_write(tgt["process"], tgt["title"])
    if info.get("password") or uia.is_password(kind, elem, hwnd):
        raise PolicyError("password field: Claude never types passwords or credentials")
    _announce(tgt, _center(tgt["rect"]) if tgt["rect"] else None, tgt["rect"])
    res = uia.set_value(kind, elem, hwnd, text)
    OVERLAY.click_pulse()
    _audit("set_value", t0, tgt, element=element, mechanism=res["via"], chars=len(text))
    return {"ok": True, "mechanism": res["via"], "cursor_moved": False}


def _invoke(element):
    t0 = time.perf_counter()
    _require_running()
    mons = _monitors()
    kind, elem, hwnd, info, tgt = _element_target(element, mons)
    POLICY.check_write(tgt["process"], tgt["title"])
    _announce(tgt, _center(tgt["rect"]) if tgt["rect"] else None, tgt["rect"])
    res = uia.invoke(kind, elem, hwnd)
    OVERLAY.click_pulse()
    _audit("invoke", t0, tgt, element=element, mechanism=res["via"])
    return {"ok": True, "mechanism": res["via"], "cursor_moved": False}


def _select(element, item):
    t0 = time.perf_counter()
    _require_running()
    mons = _monitors()
    kind, elem, hwnd, info, tgt = _element_target(element, mons)
    POLICY.check_write(tgt["process"], tgt["title"])
    _announce(tgt, _center(tgt["rect"]) if tgt["rect"] else None, tgt["rect"])
    res = uia.select(kind, elem, hwnd, item)
    OVERLAY.click_pulse()
    _audit("select", t0, tgt, element=element, item=item, mechanism=res["via"])
    return {"ok": True, "mechanism": res["via"], "cursor_moved": False}


CLICKABLE_TYPES = {"Button", "MenuItem", "TabItem", "ListItem", "CheckBox", "RadioButton", "Hyperlink",
                   "SplitButton", "TreeItem", "DataItem"}


_FREEZE_OVERRIDE: list = [None]   # per-call freeze_user_mouse, set by _with_freeze on the worker


def _gesture_cfg(base: dict | None = None) -> dict:
    cfg = base or CFG
    if _FREEZE_OVERRIDE[0] is None:
        return cfg
    return {**cfg, "input": {**cfg.get("input", {}), "freeze_user_mouse_during_gestures": bool(_FREEZE_OVERRIDE[0])}}


def _with_freeze(fn, freeze):
    def run(*a, **k):
        _FREEZE_OVERRIDE[0] = freeze
        try:
            return fn(*a, **k)
        finally:
            _FREEZE_OVERRIDE[0] = None
    run.__name__ = getattr(fn, "__name__", "job")
    return run


def _sendinput_click(x, y, button, double):
    with inp.Gesture(GUARD, STOP, _gesture_cfg()) as g:
        g.move(x, y)
        time.sleep(0.03)
        for i in range(2 if double else 1):
            g.down(button)
            time.sleep(0.02)
            g.up(button)
            if double and i == 0:
                time.sleep(0.05)
        time.sleep(0.03)
    return g.report


def _click(x, y, element, capture_id, button, double, mechanism):
    t0 = time.perf_counter()
    _require_running()
    mons = _monitors()
    report = {"cursor_moved": False}
    if element:
        kind, elem, hwnd, info, tgt = _element_target(element, mons)
        POLICY.check_write(tgt["process"], tgt["title"])
        if not tgt["rect"]:
            raise RuntimeError("element has no on-screen rectangle")
        px, py = _center(tgt["rect"])
        _announce(tgt, (px, py), tgt["rect"])
        used = None
        if mechanism in ("auto", "uia") and button == "left" and not double:
            try:
                used = uia.invoke(kind, elem, hwnd)["via"]
            except Exception:  # noqa: BLE001
                if mechanism == "uia":
                    raise
        if used is None and mechanism in ("auto", "message") and kind == "win32" and button == "left" and not double \
                and "button" in w.class_name(hwnd).lower():
            w.user32.PostMessageW(hwnd, w.BM_CLICK, 0, 0)
            used = "Win32.BM_CLICK"
        if used is None and mechanism == "message":
            child = inp.deepest_child_at(tgt["hwnd"], px, py)
            inp.post_click(child, px, py, button, double)
            used = "Win32.WM_LBUTTONDOWN/UP" if button == "left" else f"Win32.{button} button messages"
        if used is None:
            report = _sendinput_click(px, py, button, double)
            used = "SendInput"
    else:
        px, py = _resolve_point(x, y, capture_id)
        tgt = _target_from_point(px, py, mons)
        POLICY.check_write(tgt["process"], tgt["title"])
        used = None
        rect = None
        if mechanism in ("auto", "uia") and button == "left" and not double:
            try:
                el = uia.element_from_point(px, py)
                info = uia.live_info(el)
                if info["type"] in CLICKABLE_TYPES and info["pid"] == w.window_pid(tgt["hwnd"]):
                    rect = info["rect"]
                    _announce(tgt, (px, py), rect)
                    used = uia.invoke("uia", el, info["hwnd"])["via"]
            except Exception:  # noqa: BLE001
                if mechanism == "uia":
                    raise
                used = None
        if used is None:
            _announce(tgt, (px, py), rect)
            if mechanism == "message":
                child = inp.deepest_child_at(tgt["hwnd"], px, py)
                inp.post_click(child, px, py, button, double)
                used = "Win32.mouse button messages (client coords)"
            else:
                report = _sendinput_click(px, py, button, double)
                used = "SendInput"
    OVERLAY.click_pulse()
    _audit("click", t0, tgt, point=(px, py), element=element, mechanism=used, button=button, double=double,
           **report)
    return {"ok": True, "point": (px, py), "mechanism": used, **report}


def _focus_hwnd_of(root: int) -> int:
    """The control that has keyboard focus inside root's GUI thread - valid
    even when that window is in the background, so we can type into it
    without activating it."""
    gti = w.GUITHREADINFO()
    gti.cbSize = ctypes.sizeof(gti)
    if w.user32.GetGUIThreadInfo(w.window_thread(root), ctypes.byref(gti)) and gti.hwndFocus:
        if wins.root_window(int(gti.hwndFocus)) == root:
            return int(gti.hwndFocus)
    found = []

    @w.WNDENUMPROC
    def _cb(h, _):
        if any(w.class_name(h).lower().startswith(c) for c in uia.WIN32_EDIT_CLASSES) and w.user32.IsWindowVisible(h):
            found.append(int(h))
            return False
        return True
    w.user32.EnumChildWindows(root, _cb, 0)
    return found[0] if found else 0


def _type_text(text, element, window, mechanism):
    t0 = time.perf_counter()
    _require_running()
    mons = _monitors()
    edit = 0
    uia_elem = None
    if element:
        kind, elem, hwnd, info, tgt = _element_target(element, mons)
        if info.get("password") or uia.is_password(kind, elem, hwnd):
            raise PolicyError("password field: Claude never types passwords or credentials")
        edit = info.get("hwnd") or (hwnd if kind == "win32" else 0)
        uia_elem = elem if kind == "uia" else None
    elif window:
        root = wins.find_window(window, mons, OWN_PID)
        tgt = _target_from_window(root, mons)
    else:
        root = int(w.user32.GetForegroundWindow() or 0)
        if not root:
            raise RuntimeError("no foreground window: pass window= or element=")
        tgt = _target_from_window(root, mons)
    POLICY.check_write(tgt["process"], tgt["title"])
    if not edit and tgt.get("hwnd"):
        edit = _focus_hwnd_of(tgt["hwnd"])
    if edit and uia._is_win32_password(edit):
        raise PolicyError("password field: Claude never types passwords or credentials")
    point = _center(w.window_rect(edit)) if edit else _center(tgt["rect"])
    _announce(tgt, point, w.window_rect(edit) if edit else None)
    used = None
    report = {"cursor_moved": False}
    is_edit = bool(edit) and any(w.class_name(edit).lower().startswith(c) for c in uia.WIN32_EDIT_CLASSES)
    if mechanism in ("auto", "message") and edit and (is_edit or mechanism == "message"):
        used = uia.insert_text_win32(edit, text) if is_edit else None
        if used is None:
            for ch in text:
                w.user32.PostMessageW(edit, w.WM_CHAR, 13 if ch == "\n" else ord(ch), 1)
            used = "Win32.WM_CHAR"
    if used is None:
        if mechanism == "message":
            raise RuntimeError("no focusable edit control found for message-based typing")
        # SendInput needs the keyboard focus: this is the one case where we take it.
        fr = wins.focus(tgt["hwnd"])
        if not fr["focused"]:
            raise RuntimeError("could not focus the target window for SendInput typing")
        if uia_elem is not None:
            # keystrokes go to whatever has keyboard focus inside the window:
            # put it on the requested element first (UIA SetFocus, no cursor),
            # verify, and only if the app ignored it click the element for real.
            focus_via = uia.give_focus(uia_elem)
            if focus_via is None:
                if not tgt.get("rect"):
                    raise RuntimeError(f"element {element} refused keyboard focus and has no rectangle to click")
                report = _sendinput_click(*_center(tgt["rect"]), "left", False)
                time.sleep(0.1)
                focus_via = "SendInput click" if uia.has_focus(uia_elem) else None
                if focus_via is None:
                    raise RuntimeError(f"element {element} did not take the keyboard focus (SetFocus + click)")
        try:
            focused = uia.auto().GetFocusedControl()
            if focused is not None and getattr(focused, "IsPassword", False):
                raise PolicyError("focused control is a password field: refused")
        except PolicyError:
            raise
        except Exception:  # noqa: BLE001
            pass
        inp.sendinput_text(text, STOP)
        used = "SendInput(KEYEVENTF_UNICODE) after focus"
        if uia_elem is not None:
            used += f" (element focus: {focus_via})"
        report = {**report, "keyboard_focus_taken": True}
    _audit("type_text", t0, tgt, mechanism=used, chars=len(text), edit_hwnd=edit)
    return {"ok": True, "mechanism": used, "chars": len(text), "target_hwnd": edit or tgt["hwnd"], **report}


def _key(combo, window, element, mechanism):
    t0 = time.perf_counter()
    _require_running()
    mons = _monitors()
    edit = 0
    if element:
        kind, elem, hwnd, info, tgt = _element_target(element, mons)
        edit = info.get("hwnd") or (hwnd if kind == "win32" else 0)
    elif window:
        tgt = _target_from_window(wins.find_window(window, mons, OWN_PID), mons)
    else:
        root = int(w.user32.GetForegroundWindow() or 0)
        tgt = _target_from_window(root, mons)
    POLICY.check_write(tgt["process"], tgt["title"])
    mods, _vk = inp.parse_combo(combo)
    if not edit:
        edit = _focus_hwnd_of(tgt["hwnd"])
    _announce(tgt, _center(w.window_rect(edit)) if edit else _center(tgt["rect"]), None)
    report = {"cursor_moved": False}
    if mechanism in ("auto", "message") and not mods and edit:
        inp.post_key(edit, combo)
        used = "Win32.WM_KEYDOWN/UP"
    else:
        if mechanism == "message":
            raise RuntimeError("combos with modifiers need SendInput (mechanism='sendinput' or 'auto')")
        fr = wins.focus(tgt["hwnd"])
        if not fr["focused"]:
            raise RuntimeError("could not focus the target window for SendInput")
        inp.sendinput_combo(combo)
        used = "SendInput after focus"
        report["keyboard_focus_taken"] = True
    _audit("key", t0, tgt, combo=combo, mechanism=used)
    return {"ok": True, "combo": combo, "mechanism": used, **report}


def _scroll(dy, dx, x, y, element, capture_id, mechanism):
    t0 = time.perf_counter()
    _require_running()
    mons = _monitors()
    used = None
    report = {"cursor_moved": False}
    if element:
        kind, elem, hwnd, info, tgt = _element_target(element, mons)
        POLICY.check_write(tgt["process"], tgt["title"])
        px, py = _center(tgt["rect"])
        _announce(tgt, (px, py), tgt["rect"])
        if mechanism in ("auto", "uia"):
            r = uia.scroll(kind, elem, hwnd, int(dx), int(dy))
            used = r["via"] if r else None
    else:
        px, py = _resolve_point(x, y, capture_id)
        tgt = _target_from_point(px, py, mons)
        POLICY.check_write(tgt["process"], tgt["title"])
        _announce(tgt, (px, py), None)
        if mechanism in ("auto", "uia"):
            try:
                el = uia.element_from_point(px, py)
                r = uia.scroll("uia", el, 0, int(dx), int(dy))
                used = r["via"] if r else None
            except Exception:  # noqa: BLE001
                used = None
    if used is None and mechanism in ("auto", "message"):
        child = inp.deepest_child_at(tgt["hwnd"], px, py)
        inp.post_wheel(child, px, py, int(dy), int(dx))
        used = "Win32.WM_MOUSEWHEEL"
    if used is None:
        with inp.Gesture(GUARD, STOP, _gesture_cfg()) as g:
            g.move(px, py)
            g.wheel(int(dy), int(dx))
        used, report = "SendInput", g.report
    _audit("scroll", t0, tgt, dy=dy, dx=dx, mechanism=used)
    return {"ok": True, "mechanism": used, **report}


def _drag(path, button, capture_id, hold_ms, step_ms):
    t0 = time.perf_counter()
    _require_running()
    if not path or len(path) < 2:
        raise ValueError("path needs at least 2 points [[x,y], ...]")
    pts = [_resolve_point(p[0], p[1], capture_id) for p in path]
    mons = _monitors()
    # Every point must fall on the same allowed top-level window.
    tgt = _target_from_point(*pts[0], mons)
    POLICY.check_write(tgt["process"], tgt["title"])
    for p in pts[1:]:
        h = wins.window_at(*p)
        root = wins.root_window(h) if h else 0
        if root != tgt["hwnd"] and w.window_pid(root) != w.window_pid(tgt["hwnd"]):
            raise PolicyError(f"path leaves the target window at {p} (over '{w.window_text(root)}') - refused")
    _announce(tgt, pts[0], None)
    cfg = CFG
    if step_ms is not None:
        cfg = {**CFG, "input": {**CFG.get("input", {}), "move_interval_ms": step_ms}}
    g = inp.Gesture(GUARD, STOP, _gesture_cfg(cfg))
    try:
        with g:
            g.move(*pts[0])
            time.sleep(0.03)
            g.down(button)
            time.sleep(max(0, hold_ms) / 1000 + 0.02)
            g.glide(pts)
            time.sleep(0.03)
            g.up(button)
    except StoppedError as e:
        held = [b for b in ("left", "right", "middle")
                if w.user32.GetAsyncKeyState({"left": 1, "right": 2, "middle": 4}[b]) & 0x8000]
        raise StoppedError(f"stopped mid-gesture by {e}: mouse button released={not held}, "
                           f"cursor restored={g.report['cursor_restored']}, "
                           f"user events swallowed={g.report['user_events_swallowed']}. Paused.") from None
    OVERLAY.post("ghost_move", x=pts[-1][0], y=pts[-1][1])
    _audit("drag", t0, tgt, points=len(pts), button=button, mechanism="SendInput", **g.report)
    return {"ok": True, "mechanism": "SendInput", "points": len(pts), **g.report}


def _wait_for(condition, timeout):
    t0 = time.perf_counter()
    deadline = time.time() + float(timeout)
    cond = dict(condition or {})
    mons = _monitors()
    last = None
    while True:
        if "window" in cond and not ("text" in cond or "element_enabled" in cond):
            try:
                hwnd = wins.find_window(cond["window"], _monitors(), OWN_PID)
                return {"ok": True, "met": True, "hwnd": hwnd, "title": w.window_text(hwnd),
                        "waited_ms": round((time.perf_counter() - t0) * 1000)}
            except ValueError:
                pass
        elif "window_gone" in cond:
            try:
                wins.find_window(cond["window_gone"], _monitors(), OWN_PID)
            except ValueError:
                return {"ok": True, "met": True, "waited_ms": round((time.perf_counter() - t0) * 1000)}
        elif "text" in cond:
            try:
                hwnd = wins.find_window(cond.get("window"), _monitors(), OWN_PID)
                res = uia.find(hwnd, {"text": cond["text"]}, 1)
                if res:
                    return {"ok": True, "met": True, "element": res[0],
                            "waited_ms": round((time.perf_counter() - t0) * 1000)}
            except ValueError:
                pass
        elif "element_enabled" in cond:
            kind, elem, hwnd = uia.resolve(cond["element_enabled"])
            en = uia.live_info(elem)["enabled"] if kind == "uia" else bool(w.user32.IsWindowEnabled(hwnd))
            if en:
                return {"ok": True, "met": True, "waited_ms": round((time.perf_counter() - t0) * 1000)}
        elif "screen_stable" in cond:
            spec = cond["screen_stable"] or {}
            stable_ms = float(spec.get("ms", 600))
            target = spec.get("target", "active")
            mon = resolve_monitor(mons, target, _active_monitor(mons)) if not str(target).startswith("window:") else None
            rect = mon.rect if mon else _target_from_window(
                wins.find_window(str(target).split(":", 1)[1], mons, OWN_PID), mons)["rect"]
            import numpy as np
            arr = WORKER.capturer.grab_rect(rect, mons)[::4, ::4, :3].astype(np.int16)
            if last is not None and last[0].shape == arr.shape and np.abs(arr - last[0]).max() <= 8:
                if (time.time() - last[1]) * 1000 >= stable_ms:
                    return {"ok": True, "met": True, "waited_ms": round((time.perf_counter() - t0) * 1000)}
            else:
                last = (arr, time.time())
        else:
            raise ValueError("condition: {window}, {window_gone}, {text, window}, {element_enabled} "
                             "or {screen_stable: {target, ms}}")
        if time.time() >= deadline:
            return {"ok": True, "met": False, "waited_ms": round((time.perf_counter() - t0) * 1000)}
        time.sleep(0.2)


# =============================================================================
# FastMCP server
# =============================================================================

os.environ.setdefault("FASTMCP_STATELESS_HTTP", "true")
from fastmcp import FastMCP  # noqa: E402
from fastmcp.utilities.types import Image  # noqa: E402

mcp = FastMCP("desktop-mcp", auth=mcp_common.make_auth_provider(Config.API_KEY, logger))


def _image_result(res):
    if isinstance(res, dict) and "__image__" in res:
        data, fmt = res["__image__"]
        return [Image(data=data, format=fmt), res["meta"]]
    return res


def _recording_monitor_rect():
    mons = OVERLAY.monitors or enumerate_monitors()
    m = next((m for m in mons if m.device == OVERLAY.active), None) or         next((m for m in mons if m.primary), mons[0])
    return m.rect


async def _run_action(name, fn, *args, recent_frames=None, timeout: float = 120):
    """_run + optional "what happened" recording returned as one extra image."""
    n = CFG.get("capture", {}).get("recent_frames_default", 0) if recent_frames is None else recent_frames
    n = max(0, min(int(n or 0), 9))
    if not n:
        return await _run(name, fn, *args, timeout=timeout)
    rcfg = CFG.get("capture", {})
    rec = Recorder(_recording_monitor_rect, fps=float(rcfg.get("recent_frames_fps", 8)),
                   window_s=float(rcfg.get("recent_frames_window_s", 2.0)),
                   post_roll_s=float(rcfg.get("recent_frames_post_roll_s", 0.4)),
                   cursor_fn=lambda: OVERLAY.ghost_pos).start()
    try:
        res = await _run(name, fn, *args, timeout=timeout)
    finally:
        await asyncio.to_thread(rec.stop)
    sheet, info = await asyncio.to_thread(rec.contact_sheet, max(2, n))
    if isinstance(res, dict):
        res = {**res, "recent_frames": info}
    if sheet is None:
        return res
    data, fmt = WORKER.capturer.encode(sheet, "jpeg", int(rcfg.get("jpeg_quality", 70))) if WORKER.capturer         else cap.Capturer.encode(sheet, "jpeg", 70)
    return [res, Image(data=data, format=fmt)]


@mcp.tool()
async def screenshot(target: str = "active", region: list[int] | None = None, scale: float | None = None,
                     format: str | None = None, quality: int | None = None, diff: str = "none",
                     window_mode: str = "auto", show_cursor: bool = True):
    """Capture the screen (the Claude overlay/border never appears in captures).

    target: 'active' (monitor Claude last worked on), 'primary', 'all' (3 monitors / virtual desktop),
            a monitor index (1..n as in Windows Settings) or name, or 'window:<hwnd or title substring>'.
    region: [left, top, right, bottom] in virtual-desktop physical px (overrides target).
    scale: 0.05-2.0 (default 0.5 to save tokens). format: 'jpeg' (default) or 'png'. quality: JPEG 1-95.
    diff: 'none' | 'changed' (return no image if nothing changed since the last same capture) |
          'region' (return only the changed area).
    window_mode: 'auto' | 'printwindow' (content even if covered) | 'screen' (visible pixels).
    show_cursor: draw Claude's cursor (orange cross + 'Claude') where it is - the overlay itself never
    appears in captures. meta.claude_cursor gives its virtual/image coords; meta.user_cursor_virtual
    is where the user's real pointer is.
    Returns the image + capture_id and the coordinate mapping; pass capture_id to click/drag to use
    image coordinates directly.
    """
    return _image_result(await _run("screenshot", _screenshot, target, region, scale, format, quality, diff,
                                    window_mode, show_cursor))


@mcp.tool()
async def list_monitors() -> dict:
    """Monitors: index (Windows display number), name, rect in the virtual desktop (physical px),
    DPI / scale %, primary flag, and which one is 'active' (where Claude is working)."""
    mons = enumerate_monitors()
    return {"ok": True, "dpi_awareness": DPI_MODE, "virtual_bounds": virtual_bounds(mons),
            "active": next((m.index for m in mons if m.device == OVERLAY.active), None),
            "monitors": [m.to_dict() for m in mons]}


@mcp.tool()
async def list_windows(include_all: bool = False) -> dict:
    """Top-level windows: hwnd, title, class, process, pid, rect, visible/minimized/maximized, monitor,
    and whether Claude may write to it (allow list) or not even read it (deny list)."""
    return await _run("list_windows", _list_windows, include_all)


@mcp.tool()
async def launch_app(name: str, args: list[str] | None = None, monitor: str | None = None,
                     maximize: bool = False,
        recent_frames: int | None = None):
    """Start an app (must not be in the deny list, e.g. 'mspaint', 'notepad', 'msedge') and optionally
    place it on a monitor (index/name/'active'). Returns the new window's hwnd.
    recent_frames: 2-9 = also return ONE image with the frames that changed during the action
    (last ~2 s + ~0.4 s after), cropped to the changed area. 0 = off.
    """
    return await _run_action("launch_app", _launch_app, name, args, monitor, maximize, recent_frames=recent_frames)


@mcp.tool()
async def focus_window(window: str,
        recent_frames: int | None = None):
    """Bring a window to the foreground (hwnd or title substring). Avoid unless needed: it takes the
    keyboard focus from the user. Most tools work on background windows without focusing.
    recent_frames: 2-9 = also return ONE image with the frames that changed during the action
    (last ~2 s + ~0.4 s after), cropped to the changed area. 0 = off.
    """
    return await _run_action("focus_window", _focus_window, window, recent_frames=recent_frames)


@mcp.tool()
async def move_window(window: str, monitor: str = "active", rect: list[int] | None = None,
                      maximize: bool = False,
        recent_frames: int | None = None):
    """Move a window to a monitor without activating it. rect = [x, y, width, height] relative to
    the monitor's work area (default: centred, 70% x 75%).
    recent_frames: 2-9 = also return ONE image with the frames that changed during the action
    (last ~2 s + ~0.4 s after), cropped to the changed area. 0 = off.
    """
    return await _run_action("move_window", _move_window, window, monitor, rect, maximize, recent_frames=recent_frames)


@mcp.tool()
async def ui_tree(window: str, depth: int = 4, filter: str | None = None, max_nodes: int = 300,
                  backend: str = "auto") -> dict:
    """Compact UI Automation tree of a window. Each node: id (stable while the element lives, use it
    with read_value/set_value/invoke/select/click), type, name, aid (AutomationId), cls, rect,
    pat (patterns), value. filter: 'interactive' or a text substring. backend: 'auto' | 'uia' |
    'win32' (EnumChildWindows + WM_GETTEXT, for old apps whose controls UIA doesn't expose)."""
    return await _run("ui_tree", _ui_tree, window, depth, filter, max_nodes, backend)


@mcp.tool()
async def find_element(window: str, query: dict | str, max_results: int = 10) -> dict:
    """Find elements in a window. query: a text (matches name / AutomationId / value) or a dict with
    any of name, type (e.g. 'Button'), automation_id, class, text, exact (bool), index (int)."""
    return await _run("find_element", _find_element, window, query, max_results)


@mcp.tool()
async def read_value(element: str) -> dict:
    """Read an element's value/text (UIA Value -> Text -> LegacyIAccessible -> WM_GETTEXT).
    Never moves the cursor."""
    return await _run("read_value", _read_value, element)


@mcp.tool()
async def set_value(element: str, text: str,
        recent_frames: int | None = None):
    """Replace an element's value (UIA Value.SetValue -> LegacyIAccessible -> WM_SETTEXT).
    Never moves the cursor. Refused on password fields.
    recent_frames: 2-9 = also return ONE image with the frames that changed during the action
    (last ~2 s + ~0.4 s after), cropped to the changed area. 0 = off.
    """
    return await _run_action("set_value", _set_value, element, text, recent_frames=recent_frames)


@mcp.tool()
async def invoke(element: str,
        recent_frames: int | None = None):
    """Activate an element without the mouse (Invoke -> Toggle -> SelectionItem -> ExpandCollapse ->
    LegacyIAccessible.DoDefaultAction -> BM_CLICK).
    recent_frames: 2-9 = also return ONE image with the frames that changed during the action
    (last ~2 s + ~0.4 s after), cropped to the changed area. 0 = off.
    """
    return await _run_action("invoke", _invoke, element, recent_frames=recent_frames)


@mcp.tool()
async def select(element: str, item: str | None = None,
        recent_frames: int | None = None):
    """Select `item` (by name) inside a list/combo/tab element, or the element itself if item is None.
    UIA SelectionItem / Win32 CB_SETCURSEL / LB_SETCURSEL; no cursor.
    recent_frames: 2-9 = also return ONE image with the frames that changed during the action
    (last ~2 s + ~0.4 s after), cropped to the changed area. 0 = off.
    """
    return await _run_action("select", _select, element, item, recent_frames=recent_frames)


@mcp.tool()
async def click(x: float | None = None, y: float | None = None, element: str | None = None,
                capture_id: int | None = None, button: str = "left", double: bool = False,
                mechanism: str = "auto",
        freeze_user_mouse: bool | None = None,
        recent_frames: int | None = None):
    """Click an element or a point. Point = virtual-desktop px, or image px of `capture_id`.
    mechanism: 'auto' (UIA Invoke when the target is a button-like control, else SendInput),
    'uia', 'message' (WM_LBUTTONDOWN/UP to the HWND, no cursor), 'sendinput' (real cursor, restored).
    Returns the mechanism used and whether the real cursor moved.
    recent_frames: 2-9 = also return ONE image with the frames that changed during the action
    (last ~2 s + ~0.4 s after), cropped to the changed area. 0 = off.
    freeze_user_mouse: only for SendInput - freeze the user's physical mouse during the gesture
    (default from config / set_option). False = the user's movement may mix with Claude's.
    """
    return await _run_action("click", _with_freeze(_click, freeze_user_mouse), x, y, element, capture_id, button, double, mechanism, recent_frames=recent_frames)


@mcp.tool()
async def type_text(text: str, element: str | None = None, window: str | None = None,
                    mechanism: str = "auto",
        recent_frames: int | None = None):
    """Type text into an element / a window's focused control. 'auto': EM_REPLACESEL / WM_CHAR to the
    control in the background (no focus, no cursor) when it is an edit control, else SendInput
    unicode after focusing the window. Refused on password fields.
    recent_frames: 2-9 = also return ONE image with the frames that changed during the action
    (last ~2 s + ~0.4 s after), cropped to the changed area. 0 = off.
    """
    return await _run_action("type_text", _type_text, text, element, window, mechanism, recent_frames=recent_frames)


@mcp.tool()
async def key(combo: str, window: str | None = None, element: str | None = None,
              mechanism: str = "auto",
        recent_frames: int | None = None):
    """Press a key or combo, e.g. 'enter', 'ctrl+a', 'alt+f4', 'f5'. Single keys go as WM_KEYDOWN/UP
    to the control (no focus); combos with modifiers need SendInput (focuses the window).
    recent_frames: 2-9 = also return ONE image with the frames that changed during the action
    (last ~2 s + ~0.4 s after), cropped to the changed area. 0 = off.
    """
    return await _run_action("key", _key, combo, window, element, mechanism, recent_frames=recent_frames)


@mcp.tool()
async def scroll(dy: int = 3, dx: int = 0, x: float | None = None, y: float | None = None,
                 element: str | None = None, capture_id: int | None = None, mechanism: str = "auto",
        freeze_user_mouse: bool | None = None,
        recent_frames: int | None = None):
    """Scroll by notches (dy>0 = down). UIA ScrollPattern -> WM_MOUSEWHEEL -> SendInput.
    recent_frames: 2-9 = also return ONE image with the frames that changed during the action
    (last ~2 s + ~0.4 s after), cropped to the changed area. 0 = off.
    """
    return await _run_action("scroll", _with_freeze(_scroll, freeze_user_mouse), dy, dx, x, y, element, capture_id, mechanism, recent_frames=recent_frames)


@mcp.tool()
async def drag(path: list[list[float]], button: str = "left", capture_id: int | None = None,
               hold_ms: int = 0, step_ms: float | None = None,
        freeze_user_mouse: bool | None = None,
        recent_frames: int | None = None):
    """Press, follow `path` [[x,y], ...] and release - for drawing strokes and drag & drop (SendInput).
    The whole path must stay on the same allowed window. The cursor is put back where the user had
    it. freeze_user_mouse=True (off by default) freezes the user's physical mouse during the stroke.
    Stop button / hotkey aborts cleanly (button released).
    recent_frames: 2-9 = also return ONE image with the frames that changed during the action
    (last ~2 s + ~0.4 s after), cropped to the changed area. 0 = off.
    """
    return await _run_action("drag", _with_freeze(_drag, freeze_user_mouse), path, button, capture_id, hold_ms, step_ms, recent_frames=recent_frames, timeout=300)


@mcp.tool()
async def wait_for(condition: dict, timeout: float = 10) -> dict:
    """Wait until: {'window': title} appears, {'window_gone': title}, {'text': s, 'window': w} shows up
    in the UIA tree, {'element_enabled': id}, or {'screen_stable': {'target': 'active', 'ms': 600}}.
    screen_stable never becomes true on pages with continuous animation (players, spinners,
    waveforms): you get met=false at the timeout - wait on a text/element instead there."""
    return await _run("wait_for", _wait_for, condition, timeout, timeout=float(timeout) + 30)


@mcp.tool()
async def ghost_cursor(action: str = "show", x: int | None = None, y: int | None = None,
                       rect: list[int] | None = None) -> dict:
    """Claude's visible (fake) cursor: 'show' | 'hide' | 'move_to' (x,y) | 'highlight' (rect).
    Purely visual - never moves the real cursor."""
    if action == "highlight":
        if not rect:
            return {"ok": False, "error": "rect required"}
        OVERLAY.highlight(tuple(rect))
    elif action == "move_to":
        if x is None or y is None:
            return {"ok": False, "error": "x,y required"}
        mons = enumerate_monitors()
        m = nearest_monitor(mons, int(x), int(y))
        OVERLAY.activity(m.device, OVERLAY.label_text.split("  ·  ")[0] or "cursor", "")
        await asyncio.to_thread(OVERLAY.ghost_move, int(x), int(y), True)
    else:
        OVERLAY.post("ghost", action=action)
    return {"ok": True, "overlay": OVERLAY.snapshot()}


@mcp.tool()
async def screen_border(action: str = "show", monitor: str | None = None, color: str | None = None,
                        width: int | None = None) -> dict:
    """Orange glow on the edges of the monitor Claude is working on.
    action: 'show' | 'hide' | 'monitor' (move it to `monitor`). color: '#RRGGBB'. width: px (physical)."""
    device = None
    if monitor is not None:
        device = resolve_monitor(enumerate_monitors(), monitor, None).device
    OVERLAY.post("border", wait=True, action=action, device=device, color=color, width=width)
    return {"ok": True, "overlay": OVERLAY.snapshot(), "border": CFG.get("border")}


@mcp.tool()
async def set_option(freeze_user_mouse: bool | None = None, persist: bool = False) -> dict:
    """Change runtime options. freeze_user_mouse: freeze the user's physical mouse during SendInput
    gestures (strokes/real clicks, 1-3 s) so it can't mix with Claude's. persist=True also writes it
    to the user config file (%LOCALAPPDATA%/desktop-mcp/config.json), so it survives restarts."""
    changed = {}
    if freeze_user_mouse is not None:
        CFG.setdefault("input", {})["freeze_user_mouse_during_gestures"] = bool(freeze_user_mouse)
        changed["freeze_user_mouse_during_gestures"] = bool(freeze_user_mouse)
    if persist and changed:
        import json as _json
        from desktop.safety import DATA_DIR as _DD
        f = _DD / "config.json"
        data = _json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        data.setdefault("input", {}).update(changed)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(_json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    AUDIT.write({"tool": "set_option", **changed, "persist": persist})
    return {"ok": True, "changed": changed, "persisted": bool(persist and changed),
            "input": CFG.get("input", {})}


@mcp.tool()
async def status() -> dict:
    """Server state: paused or not (and why), active monitor, overlay state, allow/deny lists,
    stop hotkey, automation thread activity, last audited actions."""
    return {
        "ok": True, "version": VERSION, "paused": STOP.stopped, "pause_reason": STOP.reason,
        "stop_controls": {"on_screen_button": CFG.get("stop_button", {}).get("enabled", True),
                          "hotkey": CFG.get("stop_hotkey"), "hotkey_registered": GUARD.hotkey_registered,
                          "hotkey_error": GUARD.hotkey_error},
        "busy": bool(WORKER.busy_since), "busy_with": WORKER.current,
        "freeze_user_mouse_during_gestures": CFG.get("input", {}).get("freeze_user_mouse_during_gestures", False),
        "overlay": OVERLAY.snapshot(), "policy": POLICY.describe(),
        "capture_backend": WORKER.capturer.last_backend if WORKER.capturer else "",
        "audit_log": str(AUDIT.path), "recent_actions": AUDIT.tail(8),
        "dpi_awareness": DPI_MODE, "host": Config.HOST, "port": Config.PORT,
    }


@mcp.tool()
async def pause(reason: str = "pause()") -> dict:
    """Stop immediately (aborts a running gesture, releases held buttons) and stay paused."""
    STOP.stop(reason)
    return {"ok": True, "paused": True}


@mcp.tool()
async def resume() -> dict:
    """Leave pause mode."""
    STOP.resume("resume()")
    return {"ok": True, "paused": False}


@mcp.tool()
async def audit_tail(n: int = 20) -> dict:
    """Last n lines of the append-only audit log (JSONL)."""
    return {"ok": True, "path": str(AUDIT.path), "entries": AUDIT.tail(min(max(1, n), 200))}


from starlette.responses import JSONResponse  # noqa: E402


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    return JSONResponse({"status": "healthy", "server": "desktop-mcp", "version": VERSION,
                         "transport": "streamable-http", "paused": STOP.stopped,
                         "timestamp": datetime.now().isoformat()})


# =============================================================================
# Main
# =============================================================================

def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Desktop MCP Server")
    parser.add_argument("--port", type=int, default=Config.PORT)
    parser.add_argument("--host", default=Config.HOST)
    args = parser.parse_args()
    Config.PORT, Config.HOST = args.port, args.host
    if not _is_loopback(Config.HOST) and not Config.API_KEY:
        logger.critical("Refusing to listen on %s without DESKTOP_MCP_API_KEY (LAN exposure needs a key).",
                        Config.HOST)
        sys.exit(2)
    logger.info("desktop-mcp %s on http://%s:%s/mcp (auth %s, dpi %s, hotkey %s registered=%s, overlay %s)",
                VERSION, Config.HOST, Config.PORT, "on" if Config.API_KEY else "OFF", DPI_MODE,
                CFG.get("stop_hotkey"), GUARD.hotkey_registered, OVERLAY.error or "ok")
    mcp_common.start_self_watchdog(Config.PORT, logger)
    mcp.run(transport="streamable-http", host=Config.HOST, port=Config.PORT)


if __name__ == "__main__":
    main()
