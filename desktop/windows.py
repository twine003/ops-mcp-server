"""Top-level window enumeration, launch, focus and move."""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import time

from . import winapi as w
from .monitors import Monitor, monitor_for_rect


def _is_app_window(hwnd) -> bool:
    if not w.user32.IsWindowVisible(hwnd) or w.is_cloaked(hwnd):
        return False
    ex = w.user32.GetWindowLongW(hwnd, w.GWL_EXSTYLE)
    if ex & w.WS_EX_TOOLWINDOW:
        return False
    if w.user32.GetWindow(hwnd, w.GW_OWNER) and not w.window_text(hwnd):
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

    @w.WNDENUMPROC
    def _cb(hwnd, _):
        if w.window_pid(hwnd) == own_pid:
            return True
        if include_all or _is_app_window(hwnd):
            out.append(window_info(hwnd, monitors))
        return True

    w.user32.EnumWindows(_cb, 0)
    return out


def find_window(spec, monitors: list[Monitor], own_pid: int) -> int:
    """hwnd (int / numeric str) or a case-insensitive title substring / regex 're:...'."""
    if spec is None or spec == "":
        raise ValueError("window spec required (hwnd or title)")
    if isinstance(spec, int) or (isinstance(spec, str) and spec.strip().isdigit()):
        h = int(spec)
        if not w.user32.IsWindow(h):
            raise ValueError(f"hwnd {h} is not a window (closed?)")
        return h
    s = str(spec)
    wins = list_windows(monitors, own_pid)
    if s.startswith("re:"):
        rx = re.compile(s[3:], re.I)
        match = [x for x in wins if rx.search(x["title"])]
    else:
        match = [x for x in wins if s.lower() in x["title"].lower()]
        if not match:
            match = [x for x in wins if x["process"].lower() in (s.lower(), s.lower() + ".exe")]
    if not match:
        raise ValueError(f"No window matching '{s}'")
    match.sort(key=lambda x: (not x["foreground"], x["minimized"]))
    return match[0]["hwnd"]


def root_window(hwnd: int) -> int:
    r = w.user32.GetAncestor(hwnd, w.GA_ROOT)
    return int(r or hwnd)


def window_at(x: int, y: int) -> int:
    return int(w.user32.WindowFromPoint(w.POINT(x, y)) or 0)


# ---------------------------------------------------------------- launch
def launch(name_or_path: str, args: list[str] | None, monitors: list[Monitor], own_pid: int,
           timeout: float = 15.0) -> int:
    """Start an app and return the hwnd of its first new top-level window.

    Store apps (Paint, Notepad on Windows 11) are started through their App
    Execution Alias, so the launched pid is not the window's pid: we detect
    the new window by process name instead.
    """
    exe = name_or_path
    target_name = os.path.basename(exe).lower()
    if not target_name.endswith(".exe"):
        target_name += ".exe"
    existing = [x for x in list_windows(monitors, own_pid, include_all=True)]
    before = {x["hwnd"] for x in existing}
    # Processes of this app that were already running = the USER's instances.
    # Single-instance apps (Windows 11 Notepad restores its whole session)
    # answer a launch by opening a window in THAT process: never adopt it.
    user_pids = {x["pid"] for x in existing if x["process"].lower() == target_name}
    # prefer "<name>.exe": a bare name can hit an extension-less file on PATH
    # (e.g. Git's shell scripts), which CreateProcess rejects (WinError 193).
    resolved = (shutil.which(exe if exe.lower().endswith(".exe") else exe + ".exe")
                or shutil.which(exe) or exe)
    subprocess.Popen([resolved, *(args or [])], close_fds=True,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    deadline = time.time() + timeout
    reused = None
    while time.time() < deadline:
        for x in list_windows(monitors, own_pid):
            if x["hwnd"] in before or x["process"].lower() != target_name:
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
    """Bring to front. Windows restricts SetForegroundWindow; the documented
    workaround is that the caller just received input, so we send a no-op
    ALT key-up first (does not type anything)."""
    if w.user32.IsIconic(hwnd):
        w.user32.ShowWindow(hwnd, w.SW_RESTORE)
    ok = bool(w.user32.SetForegroundWindow(hwnd))
    method = "SetForegroundWindow"
    if not ok or int(w.user32.GetForegroundWindow() or 0) != int(hwnd):
        inp = w.INPUT(type=w.INPUT_KEYBOARD)
        inp.ki = w.KEYBDINPUT(wVk=0x12, wScan=0, dwFlags=w.KEYEVENTF_KEYUP, time=0, dwExtraInfo=w.INJECT_MAGIC)
        w.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(w.INPUT))
        ok = bool(w.user32.SetForegroundWindow(hwnd))
        method = "SetForegroundWindow+alt-unlock"
    time.sleep(0.05)
    return {"focused": int(w.user32.GetForegroundWindow() or 0) == int(hwnd), "method": method}


def move_to_monitor(hwnd: int, mon: Monitor, rect: tuple | None = None, maximize: bool = False) -> tuple:
    """rect: (x, y, width, height) relative to the monitor's work area.
    Default: centred, 70% x 75% of the work area. Never activates the window."""
    if w.user32.IsZoomed(hwnd) or w.user32.IsIconic(hwnd):
        w.user32.ShowWindow(hwnd, w.SW_RESTORE)
        time.sleep(0.1)
    wl, wt_, wr, wb = mon.work
    ww, wh = wr - wl, wb - wt_
    if rect:
        x, y, cw, ch = rect
        x, y = wl + int(x), wt_ + int(y)
    else:
        cw, ch = int(ww * 0.70), int(wh * 0.75)
        x, y = wl + (ww - cw) // 2, wt_ + (wh - ch) // 2
    # Two passes: a window crossing to a monitor with another DPI gets
    # WM_DPICHANGED and resizes itself after the first move.
    for _ in range(2):
        w.user32.SetWindowPos(hwnd, None, int(x), int(y), int(cw), int(ch), w.SWP_NOZORDER | w.SWP_NOACTIVATE)
        time.sleep(0.12)
    if maximize:
        w.user32.ShowWindow(hwnd, w.SW_SHOWMAXIMIZED)
        time.sleep(0.1)
    return w.window_rect(hwnd)
