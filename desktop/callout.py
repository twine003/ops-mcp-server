"""On-screen comment bubbles ("callouts") for guided tours and questions.

Claude shows a bubble next to what it is pointing at, with an "OK" button
and, for questions, a text field the user answers in. The MCP tool waits for
the user's click / answer and returns it.

The bubble is a normal (non-layered) Tk top-level window so it can host a real
text field: layered per-pixel-alpha windows cannot paint child controls. It
runs on its own thread with its own Tk interpreter (Tk objects are only touched
from that thread; other threads talk to it through a queue).

Safety:
  - only PHYSICAL input counts: a click or Enter carrying INJECT_MAGIC (our own
    SendInput) is ignored, so Claude can never accept its own bubble;
  - the window title starts with CALLOUT_TITLE, which safety.Policy always
    denies, so no tool can read, click or type into it;
  - like the rest of the overlay it is excluded from screen captures
    (callout.exclude_from_capture, default true; false with remote_visible).
"""

from __future__ import annotations

import ctypes
import itertools
import logging
import queue
import threading
import time

from ._plat import IS_LINUX, w
from .monitors import enumerate_monitors, nearest_monitor
from .safety import CALLOUT_TITLE, agent_name

log = logging.getLogger("desktop-mcp.callout")

GA_ROOT = 2


def _physical_event() -> bool:
    """True if the message being dispatched now came from real hardware, not from our SendInput."""
    return (w.user32.GetMessageExtraInfo() & 0xFFFFFFFF) != w.INJECT_MAGIC


def _wrapper_id(top) -> int:
    """Tk's toplevel wrapper (the X window the WM manages): the ancestor of winfo_id() whose
    parent is the root, or the one carrying WM_STATE once the WM reparented it."""
    from .x11 import xconn as xc
    from .x11 import winapi as xw
    cur = int(top.winfo_id())
    root = int(xc.root().id)
    for _ in range(8):
        if xw._wm_state(cur) is not None:
            return cur
        parent = xc.win(cur).query_tree().parent
        pid = int(parent.id) if parent else root
        if pid == root:
            return cur
        cur = pid
    return cur


def _managed_borderless(top) -> None:
    """X11: an override-redirect window never gets the keyboard from the window manager
    (xfwm4 keeps the focus on itself). Re-map it as a MANAGED window without decorations
    (_MOTIF_WM_HINTS) that stays above the others: same look, and the WM focuses it."""
    try:
        geo = top.geometry()
        top.withdraw()
        top.overrideredirect(False)
        top.update_idletasks()
        from .x11 import xconn as xc
        from Xlib import Xatom
        wid = _wrapper_id(top)
        win = xc.win(wid)
        hints = xc.atom("_MOTIF_WM_HINTS")
        win.change_property(hints, hints, 32, [2, 0, 0, 0, 0])   # flags=decorations, decorations=none
        win.change_property(xc.atom("_NET_WM_WINDOW_TYPE"), Xatom.ATOM, 32,
                            [xc.atom("_NET_WM_WINDOW_TYPE_DIALOG")])
        win.change_property(xc.atom("_NET_WM_STATE"), Xatom.ATOM, 32,
                            [xc.atom("_NET_WM_STATE_ABOVE"), xc.atom("_NET_WM_STATE_SKIP_TASKBAR"),
                             xc.atom("_NET_WM_STATE_SKIP_PAGER")])
        xc.d().flush()
        top.geometry(geo)
        top.deiconify()
        top.attributes("-topmost", True)
        top.lift()
        top.update_idletasks()

        def again():  # Tk rewrites the WM hints on map: set "no decorations" once more after it
            try:
                win.change_property(hints, hints, 32, [2, 0, 0, 0, 0])
                xc.d().flush()
                top.geometry(geo)
            except Exception:  # noqa: BLE001
                pass
        top.after(30, again)
        top.after(200, again)
    except Exception:  # noqa: BLE001
        log.exception("could not turn the window into a managed one")


def give_keyboard(top, entry) -> None:
    """Put the keyboard in `entry`. On X11 our borderless (override-redirect) windows are
    never focused by the window manager: ask the X server directly once the window is
    really mapped, and again on every click in the field (the WM may take it back)."""
    def go(_e=None):
        try:
            top.focus_force()
            entry.focus_set()
        except Exception:  # noqa: BLE001 - window closed meanwhile
            pass
    if IS_LINUX and top.overrideredirect():
        _managed_borderless(top)
    go()
    if IS_LINUX:
        top.after(60, go)
        top.after(250, go)
        entry.bind("<Button-1>", go, add="+")
        top.bind("<Button-1>", go, add="+")


def make_draggable(top, widgets) -> None:
    """Let the user move a borderless window by pressing on any of `widgets` (labels, frames)."""
    st = {}

    def press(e):
        st.update(dx=e.x_root - top.winfo_x(), dy=e.y_root - top.winfo_y())

    def motion(e):
        if st:
            top.geometry(f"+{e.x_root - st['dx']}+{e.y_root - st['dy']}")

    def release(_e):
        st.clear()

    for wdg in widgets:
        wdg.bind("<ButtonPress-1>", press, add="+")
        wdg.bind("<B1-Motion>", motion, add="+")
        wdg.bind("<ButtonRelease-1>", release, add="+")
        try:
            wdg.configure(cursor="fleur")
        except Exception:  # noqa: BLE001
            pass


def _overlaps(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


class Callout:
    def __init__(self, cid: int, title: str, text: str, ask: bool, button: str, step: str,
                 anchor: tuple | None, placeholder: str):
        self.id = cid
        self.title, self.text, self.ask, self.button, self.step = title, text, ask, button, step
        self.anchor = anchor            # (l, t, r, b) virtual px, or None = centre of the active monitor
        self.placeholder = placeholder
        self.created = time.time()
        self.done = threading.Event()
        self.result: dict = {"state": "pending"}
        self.top = None                 # Tk Toplevel (UI thread only)

    def finish(self, state: str, answer: str | None = None) -> None:
        if self.done.is_set():
            return
        self.result = {"state": state, "after_s": round(time.time() - self.created, 1)}
        if answer is not None:
            self.result["answer"] = answer
        self.done.set()


class CalloutUI:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.q: "queue.Queue" = queue.Queue()
        self.items: dict[int, Callout] = {}
        self._ids = itertools.count(1)
        self.root = None
        self.error = ""
        self._ready = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ public (any thread)
    def show(self, text: str, title: str = "", ask: bool = False, button: str = "OK",
             step: str = "", anchor: tuple | None = None, placeholder: str = "") -> Callout:
        self._start()
        c = Callout(next(self._ids), title, text, ask, button, step, anchor, placeholder)
        with self._lock:
            for old in self.items.values():          # one bubble at a time: a new one replaces the old
                if not old.done.is_set():
                    old.finish("replaced")
            self.items[c.id] = c
        self.q.put(("show", c))
        return c

    def get(self, cid: int) -> Callout | None:
        return self.items.get(int(cid))

    def close(self, cid: int | None = None, state: str = "closed") -> None:
        with self._lock:
            targets = [self.items[cid]] if cid is not None and cid in self.items else list(self.items.values())
        for c in targets:
            c.finish(state)
        self.q.put(("sweep", None))

    def start_dock(self, inbox, data_dir) -> None:
        """Show the always-on "write to Claude" button (desktop/inbox.py) on this UI thread."""
        self._start()
        inbox.on_change = lambda n: self.q.put(("badge", n))
        self.q.put(("dock", (inbox, data_dir)))

    def snapshot(self) -> dict:
        open_ = [c.id for c in self.items.values() if not c.done.is_set()]
        return {"thread": bool(self._thread and self._thread.is_alive()), "open": open_, "error": self.error}

    # ------------------------------------------------------------ UI thread
    def _start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="callout-ui", daemon=True)
        self._thread.start()
        self._ready.wait(10)
        if self.error:
            raise RuntimeError(self.error)

    def _run(self):
        try:
            import tkinter as tk
            self.tk = tk
            self.root = tk.Tk()
            self.root.withdraw()
        except Exception as e:  # noqa: BLE001
            self.error = f"callout init failed: {e}"
            log.exception(self.error)
            self._ready.set()
            return
        self._ready.set()
        self.root.after(50, self._poll)
        self.root.mainloop()

    def _poll(self):
        try:
            while True:
                try:
                    cmd, c = self.q.get_nowait()
                except queue.Empty:
                    break
                if cmd == "show" and not c.done.is_set():
                    self._build(c)
                elif cmd == "dock" and getattr(self, "dock", None) is None:
                    from .inbox import MessageDock
                    self.dock = MessageDock(self, c[0], self.cfg, c[1])
                    self.dock.build()
                elif cmd == "badge" and getattr(self, "dock", None) is not None:
                    self.dock.set_badge(c)
            self._sweep()
        except Exception:  # noqa: BLE001
            log.exception("callout poll failed")
        self.root.after(50, self._poll)

    def _sweep(self):
        """Destroy the windows of finished callouts (finished from any thread)."""
        with self._lock:
            for cid, c in list(self.items.items()):
                if c.done.is_set():
                    if c.top is not None:
                        try:
                            c.top.destroy()
                        except Exception:  # noqa: BLE001
                            pass
                        c.top = None
                    if time.time() - c.created > 3600:
                        self.items.pop(cid, None)

    def _build(self, c: Callout):
        tk = self.tk
        ccfg = self.cfg.get("callout", {})
        accent = ccfg.get("color") or self.cfg.get("ghost", {}).get("color", "#FF8C00")
        bg, fg, muted = "#1E1E1E", "#F2F2F2", "#B8B8B8"
        mons = enumerate_monitors()
        if c.anchor:
            ax, ay = (c.anchor[0] + c.anchor[2]) // 2, (c.anchor[1] + c.anchor[3]) // 2
        else:
            m0 = next((m for m in mons if m.primary), mons[0])
            ax, ay = (m0.rect[0] + m0.rect[2]) // 2, (m0.rect[1] + m0.rect[3]) // 2
        mon = nearest_monitor(mons, ax, ay)
        s = mon.scale
        px = lambda v: max(1, round(v * s))  # noqa: E731
        f_title = ("Segoe UI", -px(14), "bold")
        f_body = ("Segoe UI", -px(14))
        f_small = ("Segoe UI", -px(12))

        top = tk.Toplevel(self.root)
        c.top = top
        top.withdraw()
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.title(f"{CALLOUT_TITLE} {c.id}")
        top.configure(bg=accent)
        frame = tk.Frame(top, bg=bg, padx=px(14), pady=px(10))
        frame.pack(padx=px(2), pady=px(2))
        wrap = px(int(ccfg.get("max_width_px", 420)))

        head = tk.Frame(frame, bg=bg)
        head.pack(fill="x")
        drag_parts = [frame, head]
        lbl = tk.Label(head, text=c.title or agent_name(self.cfg), fg=accent, bg=bg, font=f_title)
        lbl.pack(side="left")
        drag_parts.append(lbl)
        if c.step:
            lbl = tk.Label(head, text=f"  {c.step}", fg=muted, bg=bg, font=f_small)
            lbl.pack(side="left")
            drag_parts.append(lbl)
        close = tk.Label(head, text="✕", fg=muted, bg=bg, font=f_title, cursor="hand2")
        close.pack(side="right")

        body = tk.Label(frame, text=c.text, fg=fg, bg=bg, font=f_body, justify="left", anchor="w",
                        wraplength=wrap)
        body.pack(fill="x", pady=(px(6), px(8)))
        drag_parts.append(body)

        entry = None
        if c.ask:
            entry = tk.Text(frame, height=3, width=1, wrap="word", font=f_body, bg="#2B2B2B", fg=fg,
                            insertbackground=fg, relief="flat", highlightthickness=1,
                            highlightbackground="#555555", highlightcolor=accent, padx=px(6), pady=px(4))
            entry.pack(fill="x")
            if c.placeholder:
                tk.Label(frame, text=c.placeholder, fg=muted, bg=bg, font=f_small, anchor="w",
                         wraplength=wrap).pack(fill="x", pady=(px(2), 0))

        foot = tk.Frame(frame, bg=bg)
        foot.pack(fill="x", pady=(px(8), 0))
        drag_parts.append(foot)
        if c.ask:
            lbl = tk.Label(foot, text="Enter = send · Shift+Enter = new line", fg=muted, bg=bg, font=f_small)
            lbl.pack(side="left")
            drag_parts.append(lbl)
        make_draggable(top, drag_parts)          # the user can move the bubble out of the way
        btn = tk.Button(foot, text=f"  {c.button}  ", bg=accent, fg="white", activebackground="#E07B00",
                        activeforeground="white", relief="flat", bd=0, font=f_title, cursor="hand2",
                        padx=px(10), pady=px(3))
        btn.pack(side="right")

        def accept(_e=None):
            if not _physical_event():          # our own SendInput: never accept our own bubble
                return "break"
            if c.ask and entry is not None:
                txt = entry.get("1.0", "end").strip()
                if not txt:
                    entry.configure(highlightbackground="#E24B4A")
                    return "break"
                c.finish("answered", txt)
            else:
                c.finish("accepted")
            self._sweep()
            return "break"

        def dismiss(_e=None):
            if not _physical_event():
                return "break"
            c.finish("dismissed")
            self._sweep()
            return "break"

        btn.configure(command=accept)
        close.bind("<Button-1>", dismiss)
        if entry is not None:
            entry.bind("<Return>", accept)
            entry.bind("<Shift-Return>", lambda e: None)
            entry.bind("<Escape>", dismiss)

        # ---- place next to the anchor, inside the monitor's work area
        top.update_idletasks()
        bw, bh = top.winfo_reqwidth(), top.winfo_reqheight()
        wl, wt, wr, wb = mon.work
        gap = px(18)
        dock = getattr(self, "dock", None)
        avoid = dock.rect(pad=px(10)) if dock is not None else None   # never cover the message button
        clamp = lambda x, y: (min(max(x, wl + px(8)), wr - bw - px(8)),  # noqa: E731
                              min(max(y, wt + px(8)), wb - bh - px(8)))
        if c.anchor:
            l, t, r, b = c.anchor
            options = [(r + gap, (t + b) // 2 - bh // 2), (l - gap - bw, (t + b) // 2 - bh // 2),
                       ((l + r) // 2 - bw // 2, b + gap), ((l + r) // 2 - bw // 2, t - gap - bh)]
        else:
            options = [(ax - bw // 2, ay - bh // 2)]
        fits = [(x, y) for x, y in options if x >= wl and x + bw <= wr and y >= wt and y + bh <= wb]
        free = [o for o in fits if not (avoid and _overlaps((o[0], o[1], o[0] + bw, o[1] + bh), avoid))]
        x, y = clamp(*(free or fits or options)[0])
        if avoid and _overlaps((x, y, x + bw, y + bh), avoid):      # still on it: go above, else left of it
            x2, y2 = clamp(x, avoid[1] - bh - px(4))
            if _overlaps((x2, y2, x2 + bw, y2 + bh), avoid):
                x2, y2 = clamp(avoid[0] - bw - px(4), y)
            x, y = x2, y2
        top.geometry(f"+{int(x)}+{int(y)}")
        top.deiconify()
        top.update_idletasks()

        try:
            hwnd = w.user32.GetAncestor(top.winfo_id(), GA_ROOT) or top.winfo_id()
            w.user32.SetWindowTextW(hwnd, f"{CALLOUT_TITLE} {c.id}")
            if ccfg.get("exclude_from_capture", not self.cfg.get("remote_visible", False)):
                w.user32.SetWindowDisplayAffinity(hwnd, w.WDA_EXCLUDEFROMCAPTURE)
        except Exception:  # noqa: BLE001
            log.exception("callout window setup failed")
        if dock is not None:
            dock.raise_button()
        if entry is not None:                   # a question: give the field the keyboard
            give_keyboard(top, entry)
