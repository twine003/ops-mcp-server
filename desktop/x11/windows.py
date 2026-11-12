"""Top-level window enumeration, launch, focus and move on X11 (EWMH window managers)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time

from . import winapi as w
from ..monitors import Monitor, monitor_for_rect

_SKIP_TYPES = {"_NET_WM_WINDOW_TYPE_DOCK", "_NET_WM_WINDOW_TYPE_DESKTOP", "_NET_WM_WINDOW_TYPE_TOOLBAR",
               "_NET_WM_WINDOW_TYPE_MENU", "_NET_WM_WINDOW_TYPE_SPLASH", "_NET_WM_WINDOW_TYPE_NOTIFICATION",
               "_NET_WM_WINDOW_TYPE_TOOLTIP", "_NET_WM_WINDOW_TYPE_DROPDOWN_MENU", "_NET_WM_WINDOW_TYPE_POPUP_MENU"}


def _is_app_window(hwnd) -> bool:
    if not w.user32.IsWindowVisible(hwnd):
        return False
    if w.window_type(hwnd) & _SKIP_TYPES:
        return False
    if "_NET_WM_STATE_SKIP_TASKBAR" in w.net_wm_state(hwnd):
        return False
    return bool(w.window_text(hwnd))


def window_info(hwnd: int, monitors: list[Monitor] | None = None) -> dict:
    pid = w.window_pid(hwnd)
    rect = w.window_rect(hwnd)
    info = {
        "hwnd": int(hwnd),
        "title": w.window_text(hwnd),
        "class": w.class_name(hwnd),
        "pid": pid,
        "process": w.process_name(pid),
        "rect": rect,
        "visible": bool(w.user32.IsWindowVisible(hwnd)),
        "minimized": bool(w.user32.IsIconic(hwnd)),
        "maximized": bool(w.user32.IsZoomed(hwnd)),
        "foreground": int(w.user32.GetForegroundWindow() or 0) == int(hwnd),
    }
    if monitors:
        info["monitor"] = monitor_for_rect(monitors, rect).index
    return info


def list_windows(monitors: list[Monitor], own_pid: int, include_all: bool = False) -> list[dict]:
    out = []
    for hwnd in reversed(w.client_list_stacking()):  # top of the z-order first
        try:
            if w.window_pid(hwnd) == own_pid:
                continue
            if include_all or _is_app_window(hwnd):
                out.append(window_info(hwnd, monitors))
        except Exception:  # noqa: BLE001 - a window closing while we look at it
            continue
    return out


def find_window(spec, monitors: list[Monitor], own_pid: int) -> int:
    """X window id (int / numeric str, also 0x...) or a case-insensitive title substring / regex 're:...'."""
    if spec is None or spec == "":
        raise ValueError("window spec required (hwnd or title)")
    s = str(spec).strip()
    if isinstance(spec, int) or s.isdigit() or re.fullmatch(r"0x[0-9a-fA-F]+", s):
        h = int(s, 0)
        if not w.user32.IsWindow(h):
            raise ValueError(f"window {h} does not exist (closed?)")
        return h
    wins = list_windows(monitors, own_pid)
    if s.startswith("re:"):
        rx = re.compile(s[3:], re.I)
        match = [x for x in wins if rx.search(x["title"])]
    else:
        match = [x for x in wins if s.lower() in x["title"].lower()]
        if not match:
            low = s.lower().removesuffix(".exe")
            match = [x for x in wins if x["process"].lower() == low or x["class"].lower() == low]
    if not match:
        raise ValueError(f"No window matching '{s}'")
    match.sort(key=lambda x: (not x["foreground"], x["minimized"]))
    return match[0]["hwnd"]


def root_window(hwnd: int) -> int:
    return w.toplevel_of(hwnd)


def window_at(x: int, y: int) -> int:
    """Top-most managed client whose outer rect contains (x, y), the desktop window included."""
    for h in reversed(w.client_list_stacking()):
        try:
            if w.is_iconic(h) or not w.user32.IsWindowVisible(h):
                continue
            if w.client_rect(h) == (0, 0, 0, 0):
                continue
            l, t, r, b = w.window_rect(h)
            if l <= x < r and t <= y < b:
                return int(h)
        except Exception:  # noqa: BLE001
            continue
    return 0


# ---------------------------------------------------------------- launch
def launch(name_or_path: str, args: list[str] | None, monitors: list[Monitor], own_pid: int,
           timeout: float = 15.0) -> int:
    """Start an app and return the X id of its first new top-level window.

    Single-instance apps answer a launch with a window of the process that was
    already running (the USER's instance, maybe with their documents): never adopt it.
    """
    exe = name_or_path
    target_name = os.path.basename(exe).lower().removesuffix(".exe")
    existing = list_windows(monitors, own_pid, include_all=True)
    before = {x["hwnd"] for x in existing}
    user_pids = {x["pid"] for x in existing if x["process"].lower() == target_name}
    resolved = shutil.which(exe) or exe
    proc = subprocess.Popen([resolved, *(args or [])], close_fds=True, start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + timeout
    reused = None
    while time.time() < deadline:
        for x in list_windows(monitors, own_pid):
            if x["hwnd"] in before:
                continue
            ours = x["pid"] == proc.pid or x["process"].lower() == target_name or \
                x["class"].lower() == target_name
            if not ours:
                continue
            if x["pid"] in user_pids:
                reused = x
                continue
            return x["hwnd"]
        time.sleep(0.15)
    if reused is not None:
        raise AppReusedInstance(
            f"{target_name} did not start a new process: it opened a window in an instance that was "
            f"already running (pid {reused['pid']}, '{reused['title']}'), which may hold the user's "
            f"documents. Not taking it over. Close the app's windows first, or use one Claude opened.")
    raise TimeoutError(f"No new window of {target_name} appeared within {timeout}s")


class AppReusedInstance(RuntimeError):
    pass


# ---------------------------------------------------------------- focus / move
def focus(hwnd: int) -> dict:
    """_NET_ACTIVE_WINDOW with source=pager: EWMH window managers obey it without
    focus-stealing prevention."""
    w.activate(hwnd)
    t0 = time.time()
    while time.time() - t0 < 1.0:
        if w.active_window() == int(hwnd):
            break
        time.sleep(0.03)
    return {"focused": w.active_window() == int(hwnd), "method": "_NET_ACTIVE_WINDOW"}


def move_to_monitor(hwnd: int, mon: Monitor, rect: tuple | None = None, maximize: bool = False) -> tuple:
    """rect: (x, y, width, height) relative to the monitor's work area.
    Default: centred, 70% x 75% of the work area. Never activates the window."""
    if w.is_zoomed(hwnd) or w.is_iconic(hwnd):
        w.show_window(hwnd, w.SW_RESTORE)
        time.sleep(0.15)
    wl, wt_, wr, wb = mon.work
    ww, wh = wr - wl, wb - wt_
    if rect:
        x, y, cw, ch = rect
        x, y = wl + int(x), wt_ + int(y)
    else:
        cw, ch = int(ww * 0.70), int(wh * 0.75)
        x, y = wl + (ww - cw) // 2, wt_ + (wh - ch) // 2
    w.move_resize(hwnd, int(x), int(y), int(cw), int(ch))
    time.sleep(0.2)
    if maximize:
        w.show_window(hwnd, w.SW_SHOWMAXIMIZED)
        time.sleep(0.15)
    return w.window_rect(hwnd)
