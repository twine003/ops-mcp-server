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
    (callout.exclude_from_capture, default true).
"""

from __future__ import annotations

import ctypes
import itertools
import logging
import queue
import threading
import time

from . import winapi as w
from .monitors import enumerate_monitors, nearest_monitor
from .safety import CALLOUT_TITLE

log = logging.getLogger("desktop-mcp.callout")

GA_ROOT = 2


def _physical_event() -> bool:
    """True if the message being dispatched now came from real hardware, not from our SendInput."""
    return (w.user32.GetMessageExtraInfo() & 0xFFFFFFFF) != w.INJECT_MAGIC


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
        tk.Label(head, text=c.title or "Claude", fg=accent, bg=bg, font=f_title).pack(side="left")
        if c.step:
            tk.Label(head, text=f"  {c.step}", fg=muted, bg=bg, font=f_small).pack(side="left")
        close = tk.Label(head, text="✕", fg=muted, bg=bg, font=f_title, cursor="hand2")
        close.pack(side="right")

        tk.Label(frame, text=c.text, fg=fg, bg=bg, font=f_body, justify="left", anchor="w",
                 wraplength=wrap).pack(fill="x", pady=(px(6), px(8)))

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
        if c.ask:
            tk.Label(foot, text="Enter = send · Shift+Enter = new line", fg=muted, bg=bg,
                     font=f_small).pack(side="left")
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
        if c.anchor:
            l, t, r, b = c.anchor
            options = [(r + gap, (t + b) // 2 - bh // 2), (l - gap - bw, (t + b) // 2 - bh // 2),
                       ((l + r) // 2 - bw // 2, b + gap), ((l + r) // 2 - bw // 2, t - gap - bh)]
            x, y = next(((x, y) for x, y in options if x >= wl and x + bw <= wr and y >= wt and y + bh <= wb),
                        options[0])
        else:
            x, y = ax - bw // 2, ay - bh // 2
        x = min(max(x, wl + px(8)), wr - bw - px(8))
        y = min(max(y, wt + px(8)), wb - bh - px(8))
        top.geometry(f"+{int(x)}+{int(y)}")
        top.deiconify()
        top.update_idletasks()

        try:
            hwnd = w.user32.GetAncestor(top.winfo_id(), GA_ROOT) or top.winfo_id()
            w.user32.SetWindowTextW(hwnd, f"{CALLOUT_TITLE} {c.id}")
            if ccfg.get("exclude_from_capture", True):
                w.user32.SetWindowDisplayAffinity(hwnd, w.WDA_EXCLUDEFROMCAPTURE)
        except Exception:  # noqa: BLE001
            log.exception("callout window setup failed")
        if entry is not None:                   # a question: give the field the keyboard
            top.focus_force()
            entry.focus_set()
