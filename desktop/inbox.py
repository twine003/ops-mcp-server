"""Messages from the user to Claude: a small always-on-top button and a compose window.

The user clicks the floating button (bottom-right of the primary monitor by default, it
can be dragged anywhere and remembers its place), types a message and sends it. The
message lands in the Inbox; the server hands it to the agent:
  - appended to the result of the NEXT desktop tool call (whatever the tool), so an
    agent busy on the screen sees it within seconds;
  - with the messages() tool (wait=N blocks until one arrives);
  - in an append-only JSONL file (inbox.jsonl in the data dir), which an idle agent can
    watch (e.g. tail -f) to be woken up.

The windows live on the callout UI thread (one Tk interpreter for the whole server).
Their titles start with CALLOUT_TITLE, so safety.Policy denies every tool on them:
Claude can neither read the draft nor click "Send". Only PHYSICAL clicks / keys count
(SendInput carries INJECT_MAGIC), so Claude can never send itself a message.
"""

from __future__ import annotations

import itertools
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from ._plat import w
from .callout import give_keyboard, make_draggable
from .monitors import enumerate_monitors, nearest_monitor
from .safety import CALLOUT_TITLE, agent_name

log = logging.getLogger("desktop-mcp.inbox")

GA_ROOT = 2

DEFAULT_LABELS = {
    "tooltip": "Write to {agent}",
    "title": "Message for {agent}",
    "hint": "Enter = send · Shift+Enter = new line",
    "send": "Send",
    "sent": "Sent ✓  {agent} will read it at its next step.",
    "pending": "{n} message(s) not read yet by {agent}",
}


class Inbox:
    """Thread-safe message queue + JSONL journal."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._ids = itertools.count(int(time.time() * 1000) % 1_000_000_000)
        self.unread: list[dict] = []
        self.on_change = None          # called (from any thread) when the unread count changes

    def _journal(self, rec: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            log.exception("inbox journal write failed")

    def add(self, text: str) -> dict:
        msg = {"id": next(self._ids), "at": datetime.now().isoformat(timespec="seconds"), "text": text}
        with self._cv:
            self.unread.append(msg)
            self._cv.notify_all()
        self._journal({"event": "message", **msg})
        self._changed()
        return msg

    def take(self) -> list[dict]:
        with self._cv:
            out, self.unread = self.unread, []
        if out:
            self._journal({"event": "delivered", "ids": [m["id"] for m in out],
                           "at": datetime.now().isoformat(timespec="seconds")})
            self._changed()
        return out

    def wait(self, timeout: float) -> list[dict]:
        end = time.monotonic() + max(0.0, float(timeout))
        with self._cv:
            while not self.unread:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self._cv.wait(min(left, 0.5))
        return self.take()

    def count(self) -> int:
        with self._lock:
            return len(self.unread)

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change(self.count())
            except Exception:  # noqa: BLE001
                log.exception("inbox on_change failed")


def format_for_agent(msgs: list[dict]) -> str:
    lines = ["MESSAGE FROM THE USER (typed in the on-screen 'write to Claude' window). "
             "Read it and answer or act on it before going on:"]
    for m in msgs:
        lines.append(f"[{m['at']}] {m['text']}")
    return "\n".join(lines)


def _physical_event() -> bool:
    return (w.user32.GetMessageExtraInfo() & 0xFFFFFFFF) != w.INJECT_MAGIC


class MessageDock:
    """Floating button + compose window. Every method runs on the Tk (callout) thread."""

    def __init__(self, ui, inbox: Inbox, cfg: dict, data_dir: Path):
        self.ui, self.inbox, self.cfg = ui, inbox, cfg
        self.mcfg = cfg.get("message_button", {})
        who = agent_name(cfg)
        self.labels = {k: v.replace("{agent}", who)
                       for k, v in {**DEFAULT_LABELS, **(self.mcfg.get("labels") or {})}.items()}
        self.pos_file = Path(data_dir) / "message_button.json"
        self.btn = None
        self.btn_hwnd = 0
        self.compose = None
        self.badge_n = 0

    # ------------------------------------------------------------ helpers
    def _accent(self) -> str:
        return self.mcfg.get("color") or self.cfg.get("ghost", {}).get("color", "#FF8C00")

    def _setup_hwnd(self, top, title: str) -> int:
        top.update_idletasks()
        hwnd = 0
        try:
            hwnd = w.user32.GetAncestor(top.winfo_id(), GA_ROOT) or top.winfo_id()
            w.user32.SetWindowTextW(hwnd, title)
            if self.mcfg.get("exclude_from_capture", not self.cfg.get("remote_visible", False)):
                w.user32.SetWindowDisplayAffinity(hwnd, w.WDA_EXCLUDEFROMCAPTURE)
        except Exception:  # noqa: BLE001
            log.exception("message window setup failed")
        return hwnd

    def rect(self, pad: int = 0):
        """The button's screen rect (l, t, r, b), grown by pad - other windows keep clear of it."""
        if self.btn is None:
            return None
        x, y, wd, ht = self.btn.winfo_x(), self.btn.winfo_y(), self.btn.winfo_width(), self.btn.winfo_height()
        return (x - pad, y - pad, x + wd + pad, y + ht + pad)

    def raise_button(self) -> None:
        """Put the button back on top of every other topmost window (bubbles, the compose window...)."""
        if self.btn_hwnd:
            w.user32.SetWindowPos(self.btn_hwnd, w.HWND_TOPMOST, 0, 0, 0, 0,
                                  w.SWP_NOMOVE | w.SWP_NOSIZE | w.SWP_NOACTIVATE)

    def _load_pos(self):
        try:
            d = json.loads(self.pos_file.read_text(encoding="utf-8"))
            return int(d["x"]), int(d["y"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _save_pos(self, x: int, y: int) -> None:
        try:
            self.pos_file.parent.mkdir(parents=True, exist_ok=True)
            self.pos_file.write_text(json.dumps({"x": x, "y": y}), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------ floating button
    def build(self) -> None:
        tk = self.ui.tk
        mons = enumerate_monitors()
        m0 = next((m for m in mons if m.primary), mons[0])
        s = m0.scale
        px = lambda v: max(1, round(v * s))  # noqa: E731
        size = px(int(self.mcfg.get("size_px", 46)))
        accent = self._accent()

        top = tk.Toplevel(self.ui.root)
        self.btn = top
        top.withdraw()
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.configure(bg=accent)
        cv = tk.Canvas(top, width=size, height=size, bg="#1E1E1E", highlightthickness=0, cursor="hand2")
        cv.pack(padx=px(2), pady=px(2))
        self.cv, self.size, self.px = cv, size, px
        self._draw()

        pos = self._load_pos()
        wl, wt, wr, wb = m0.work
        if pos is None or not any(m.rect[0] <= pos[0] < m.rect[2] and m.rect[1] <= pos[1] < m.rect[3] for m in mons):
            pos = (wr - size - px(28), wb - size - px(90))
        top.geometry(f"+{pos[0]}+{pos[1]}")
        top.deiconify()
        self.btn_hwnd = self._setup_hwnd(top, f"{CALLOUT_TITLE} message-button")

        drag = {}

        def press(e):
            self._hide_tip()
            drag.update(x=e.x_root, y=e.y_root, wx=top.winfo_x(), wy=top.winfo_y(), moved=False)

        def motion(e):
            if not drag:
                return
            dx, dy = e.x_root - drag["x"], e.y_root - drag["y"]
            if abs(dx) + abs(dy) > px(4):
                drag["moved"] = True
            if drag["moved"]:
                top.geometry(f"+{drag['wx'] + dx}+{drag['wy'] + dy}")

        def release(_e):
            moved = drag.get("moved")
            drag.clear()
            if moved:
                self._save_pos(top.winfo_x(), top.winfo_y())
            elif _physical_event():
                self.open_compose()

        cv.bind("<ButtonPress-1>", press)
        cv.bind("<B1-Motion>", motion)
        cv.bind("<ButtonRelease-1>", release)
        self._tip = None
        cv.bind("<Enter>", lambda e: self._show_tip())
        cv.bind("<Leave>", lambda e: self._hide_tip())
        self.ui.root.after(1500, self._keep_on_top)

    def _draw(self) -> None:
        cv, size, px = self.cv, self.size, self.px
        cv.delete("all")
        accent = self._accent()
        pad = px(9)
        # envelope
        cv.create_rectangle(pad, pad + px(4), size - pad, size - pad - px(2), outline=accent, width=px(2))
        cv.create_line(pad, pad + px(4), size // 2, size // 2 + px(2), size - pad, pad + px(4),
                       fill=accent, width=px(2))
        if self.badge_n:
            r = px(8)
            cv.create_oval(size - 2 * r - 1, 1, size - 1, 2 * r + 1, fill="#E24B4A", outline="")
            cv.create_text(size - r, r + 1, text=str(min(self.badge_n, 9)), fill="white",
                           font=("Segoe UI", -px(10), "bold"))

    def _show_tip(self) -> None:
        if self._tip is not None:
            return
        tk = self.ui.tk
        txt = self.labels["tooltip"]
        if self.badge_n:
            txt += "\n" + self.labels["pending"].format(n=self.badge_n)
        tip = tk.Toplevel(self.ui.root)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(tip, text=txt, bg="#1E1E1E", fg="#F2F2F2", font=("Segoe UI", -self.px(12)),
                 padx=self.px(8), pady=self.px(4), justify="left").pack()
        tip.update_idletasks()
        x = self.btn.winfo_x() - tip.winfo_reqwidth() - self.px(8)
        y = self.btn.winfo_y() + (self.btn.winfo_height() - tip.winfo_reqheight()) // 2
        tip.geometry(f"+{x}+{y}")
        self._setup_hwnd(tip, f"{CALLOUT_TITLE} message-tip")
        self._tip = tip
        self.ui.root.after(300, self._tip_check)

    def _tip_check(self) -> None:
        """Tk sometimes misses <Leave> on borderless topmost windows: hide the tip once the pointer is away."""
        if self._tip is None:
            return
        px_, py_ = self.btn.winfo_pointerxy()
        l, t, r, b = self.rect()
        if not (l <= px_ < r and t <= py_ < b):
            self._hide_tip()
        else:
            self.ui.root.after(300, self._tip_check)

    def _hide_tip(self) -> None:
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None

    def _keep_on_top(self) -> None:
        """Other topmost windows (full-screen apps, the taskbar) can cover it: lift it now and then."""
        try:
            self.raise_button()
        except Exception:  # noqa: BLE001
            pass
        self.ui.root.after(1500, self._keep_on_top)

    def set_badge(self, n: int) -> None:
        self.badge_n = n
        if self.btn is not None:
            self._draw()

    # ------------------------------------------------------------ compose window
    def open_compose(self) -> None:
        self._hide_tip()
        if self.compose is not None:
            try:
                self.compose.deiconify()
                self.compose.lift()
                give_keyboard(self.compose, self.entry)
                return
            except Exception:  # noqa: BLE001
                self.compose = None
        tk = self.ui.tk
        mons = enumerate_monitors()
        bx, by = self.btn.winfo_x(), self.btn.winfo_y()
        mon = nearest_monitor(mons, bx, by)
        s = mon.scale
        px = lambda v: max(1, round(v * s))  # noqa: E731
        accent = self._accent()
        bg, fg, muted = "#1E1E1E", "#F2F2F2", "#B8B8B8"
        f_title, f_body, f_small = ("Segoe UI", -px(14), "bold"), ("Segoe UI", -px(14)), ("Segoe UI", -px(12))

        top = tk.Toplevel(self.ui.root)
        self.compose = top
        top.withdraw()
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.configure(bg=accent)
        frame = tk.Frame(top, bg=bg, padx=px(14), pady=px(10))
        frame.pack(padx=px(2), pady=px(2))
        head = tk.Frame(frame, bg=bg)
        head.pack(fill="x")
        title_lbl = tk.Label(head, text=self.labels["title"], fg=accent, bg=bg, font=f_title)
        title_lbl.pack(side="left")
        close = tk.Label(head, text="✕", fg=muted, bg=bg, font=f_title, cursor="hand2")
        close.pack(side="right")
        entry = tk.Text(frame, height=5, width=46, wrap="word", font=f_body, bg="#2B2B2B", fg=fg,
                        insertbackground=fg, relief="flat", highlightthickness=1,
                        highlightbackground="#555555", highlightcolor=accent, padx=px(6), pady=px(4))
        entry.pack(fill="both", pady=(px(8), 0))
        self.entry = entry
        status = tk.Label(frame, text="", fg="#7BD88F", bg=bg, font=f_small, anchor="w")
        status.pack(fill="x", pady=(px(4), 0))
        foot = tk.Frame(frame, bg=bg)
        foot.pack(fill="x", pady=(px(6), 0))
        hint_lbl = tk.Label(foot, text=self.labels["hint"], fg=muted, bg=bg, font=f_small)
        hint_lbl.pack(side="left")
        send = tk.Button(foot, text=f"  {self.labels['send']}  ", bg=accent, fg="white",
                         activebackground="#E07B00", activeforeground="white", relief="flat", bd=0,
                         font=f_title, cursor="hand2", padx=px(10), pady=px(3))
        send.pack(side="right")

        def do_send(_e=None):
            if not _physical_event():
                return "break"
            txt = entry.get("1.0", "end").strip()
            if not txt:
                entry.configure(highlightbackground="#E24B4A")
                return "break"
            self.inbox.add(txt)
            entry.delete("1.0", "end")
            status.configure(text=self.labels["sent"])
            top.after(1600, hide)
            return "break"

        def hide(_e=None):
            if _e is not None and not _physical_event():
                return "break"
            try:
                status.configure(text="")
                top.withdraw()
            except Exception:  # noqa: BLE001
                pass
            return "break"

        send.configure(command=do_send)
        close.bind("<Button-1>", hide)
        entry.bind("<Return>", do_send)
        entry.bind("<Shift-Return>", lambda e: None)
        entry.bind("<Escape>", hide)

        make_draggable(top, [frame, head, title_lbl, status, foot, hint_lbl])   # move it out of the way

        top.update_idletasks()
        cw, ch = top.winfo_reqwidth(), top.winfo_reqheight()
        wl, wt, wr, wb = mon.work
        x = min(max(bx + self.btn.winfo_width() - cw, wl + px(8)), wr - cw - px(8))
        y = by - ch - px(10)
        if y < wt + px(8):
            y = by + self.btn.winfo_height() + px(10)
        y = min(max(y, wt + px(8)), wb - ch - px(8))
        top.geometry(f"+{int(x)}+{int(y)}")
        top.deiconify()
        self._setup_hwnd(top, f"{CALLOUT_TITLE} message")
        self.raise_button()
        give_keyboard(top, entry)
