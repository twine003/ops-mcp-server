"""Accessibility on Linux through AT-SPI 2 (gi.repository.Atspi) - the UI Automation of X desktops.

Same API as desktop/uia.py. Element ids look like "a123" and live while the
server runs (registry, like the UIA RuntimeId registry). Node fields keep the
UIA names: type (Button, Edit, MenuItem...), name, aid (accessible id), rect,
pat (invoke / value / text / toggle / selection / select_item / expand / range).

GTK 3/4, Qt 5/6 (QT_ACCESSIBILITY=1) and LibreOffice expose AT-SPI; Firefox and
Chromium only when an AT is detected. The session needs at-spi2-core (the bus)
and at-spi2-atk / libatk-adaptor for GTK.
"""

from __future__ import annotations

import itertools
import threading
from collections import OrderedDict

from . import winapi as w

MAX_REGISTRY = 20000
WIN32_EDIT_CLASSES: tuple = ()
ACTIONABLE = {"invoke", "value", "toggle", "select_item", "selection", "expand", "scroll", "range", "text"}

_atspi = None
_init_lock = threading.Lock()


def _A():
    global _atspi
    if _atspi is None:
        with _init_lock:
            if _atspi is None:
                import gi
                gi.require_version("Atspi", "2.0")
                from gi.repository import Atspi
                Atspi.init()
                _atspi = Atspi
    return _atspi


class ElementNotFound(LookupError):
    pass


class Registry:
    def __init__(self):
        self._d: "OrderedDict[str, tuple]" = OrderedDict()
        self._ids: dict[int, str] = {}
        self._n = itertools.count(1)
        self._lock = threading.Lock()

    def put(self, acc, hwnd: int) -> str:
        with self._lock:
            key = hash(acc)
            eid = self._ids.get(key)
            if eid is None or eid not in self._d:
                eid = f"a{next(self._n)}"
                self._ids[key] = eid
            self._d[eid] = (acc, hwnd)
            self._d.move_to_end(eid)
            while len(self._d) > MAX_REGISTRY:
                old, _ = self._d.popitem(last=False)
            return eid

    def get(self, eid: str):
        with self._lock:
            v = self._d.get(eid)
        if v is None:
            raise ElementNotFound(f"unknown element id '{eid}' (call ui_tree/find_element again)")
        return v


REGISTRY = Registry()

# AT-SPI role name -> UIA control type name (what the agent guide and CLICKABLE_TYPES use)
ROLE_TYPES = {
    "push button": "Button", "toggle button": "Button", "button": "Button", "menu item": "MenuItem",
    "check menu item": "MenuItem", "radio menu item": "MenuItem", "menu": "MenuItem", "menu bar": "MenuBar",
    "page tab": "TabItem", "page tab list": "Tab", "list item": "ListItem", "list": "List", "list box": "List",
    "check box": "CheckBox", "radio button": "RadioButton", "link": "Hyperlink", "tree item": "TreeItem",
    "tree": "Tree", "tree table": "Tree", "table cell": "DataItem", "table": "Table", "text": "Edit",
    "entry": "Edit", "password text": "Edit", "paragraph": "Text", "label": "Text", "static": "Text",
    "combo box": "ComboBox", "frame": "Window", "dialog": "Window", "window": "Window", "panel": "Pane",
    "filler": "Pane", "scroll pane": "Pane", "scroll bar": "ScrollBar", "slider": "Slider",
    "spin button": "Spinner", "tool bar": "ToolBar", "status bar": "StatusBar", "image": "Image",
    "icon": "Image", "heading": "Text", "document web": "Document", "document frame": "Document",
    "document text": "Document", "separator": "Separator", "progress bar": "ProgressBar",
}
_ACTION_NAMES = ("click", "press", "activate", "toggle", "jump", "open")


def auto():
    class _NoFocus:
        def GetFocusedControl(self):  # noqa: N802
            return None
    return _NoFocus()


# ---------------------------------------------------------------- helpers
def _role(acc) -> str:
    try:
        return acc.get_role_name() or ""
    except Exception:  # noqa: BLE001
        return ""


def _type(acc) -> str:
    r = _role(acc)
    return ROLE_TYPES.get(r, r.title().replace(" ", ""))


def _states(acc) -> set:
    A = _A()
    try:
        ss = acc.get_state_set()
        return {s for s in (A.StateType.ENABLED, A.StateType.SENSITIVE, A.StateType.SHOWING,
                            A.StateType.VISIBLE, A.StateType.FOCUSED, A.StateType.FOCUSABLE,
                            A.StateType.CHECKED, A.StateType.PRESSED, A.StateType.SELECTED,
                            A.StateType.EXPANDED, A.StateType.EXPANDABLE, A.StateType.CHECKABLE,
                            A.StateType.EDITABLE) if ss.contains(s)}
    except Exception:  # noqa: BLE001
        return set()


def _rect(acc) -> tuple | None:
    A = _A()
    try:
        e = acc.get_extents(A.CoordType.SCREEN)
        if e.width <= 0 or e.height <= 0:
            return None
        return (int(e.x), int(e.y), int(e.x + e.width), int(e.y + e.height))
    except Exception:  # noqa: BLE001
        return None


def _iface(acc, name: str):
    try:
        return getattr(acc, f"get_{name}_iface")()
    except Exception:  # noqa: BLE001
        return None


def _actions(acc) -> list[str]:
    a = _iface(acc, "action")
    if a is None:
        return []
    try:
        return [(a.get_action_name(i) or "").lower() for i in range(a.get_n_actions())]
    except Exception:  # noqa: BLE001
        return []


def _text_of(acc) -> str:
    t = _iface(acc, "text")
    if t is None:
        return ""
    try:
        return t.get_text(0, -1) or ""
    except Exception:  # noqa: BLE001
        return ""


def _patterns(acc, states: set) -> list[str]:
    A = _A()
    pats = []
    acts = _actions(acc)
    if acts:
        pats.append("invoke")
    if A.StateType.CHECKABLE in states or _role(acc) in ("check box", "toggle button", "radio button"):
        pats.append("toggle")
    if _iface(acc, "editable_text") is not None and A.StateType.EDITABLE in states:
        pats.append("value")
    elif _iface(acc, "text") is not None:
        pats.append("text")
    if _iface(acc, "selection") is not None:
        pats.append("selection")
    if A.StateType.EXPANDABLE in states:
        pats.append("expand")
    if _iface(acc, "value") is not None:
        pats.append("range")
    if _role(acc) in ("list item", "page tab", "menu item", "tree item", "table cell"):
        pats.append("select_item")
    return pats


def describe(acc, depth: int, hwnd_hint: int) -> dict:
    A = _A()
    eid = REGISTRY.put(acc, hwnd_hint)
    st = _states(acc)
    pats = _patterns(acc, st)
    try:
        name = acc.get_name() or ""
    except Exception:  # noqa: BLE001
        name = ""
    node = {"id": eid, "d": depth, "type": _type(acc), "name": name[:120]}
    try:
        aid = acc.get_accessible_id() or ""
    except Exception:  # noqa: BLE001
        aid = ""
    if aid:
        node["aid"] = aid
    role = _role(acc)
    if role:
        node["cls"] = role
    r = _rect(acc)
    if r:
        node["rect"] = r
    if pats:
        node["pat"] = pats
    if role != "password text" and ("value" in pats or "text" in pats):
        val = _text_of(acc)
        if val:
            node["value"] = val[:200]
    if "toggle" in pats:
        node["toggle"] = int(A.StateType.CHECKED in st or A.StateType.PRESSED in st)
    if A.StateType.SELECTED in st:
        node["selected"] = True
    if A.StateType.ENABLED not in st and A.StateType.SENSITIVE not in st:
        node["disabled"] = True
    if A.StateType.SHOWING not in st:
        node["offscreen"] = True
    if role == "password text":
        node["password"] = True
    return node


def _matches_filter(node: dict, flt: str | None) -> bool:
    if not flt or flt == "all":
        return True
    if flt == "interactive":
        return bool(set(node.get("pat", [])) & ACTIONABLE) and not node.get("offscreen")
    s = flt.lower()
    return any(s in str(node.get(k, "")).lower() for k in ("name", "aid", "value", "type", "cls"))


def _overlap(a, b) -> int:
    if not a or not b:
        return 0
    ox = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    oy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return ox * oy


def window_accessible(hwnd: int):
    """The AT-SPI frame of an X window: same pid, then best title / geometry match."""
    A = _A()
    pid = w.window_pid(hwnd)
    title = w.window_text(hwnd)
    rect = w.client_rect(hwnd) if hwnd else None
    desktop = A.get_desktop(0)
    best, score = None, -1
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue
        try:
            if pid and app.get_process_id() != pid:
                continue
        except Exception:  # noqa: BLE001
            continue
        for j in range(app.get_child_count()):
            fr = app.get_child_at_index(j)
            if fr is None:
                continue
            try:
                nm = fr.get_name() or ""
            except Exception:  # noqa: BLE001
                nm = ""
            s = (1_000_000_000 if nm and title and nm == title else 0) + \
                (500_000_000 if nm and title and (nm in title or title in nm) else 0) + _overlap(_rect(fr), rect)
            if s > score:
                best, score = fr, s
    if best is None:
        raise ElementNotFound(f"no AT-SPI accessible for window {hwnd} (pid {pid}): the app does not expose "
                              "accessibility (GTK needs at-spi2-atk, Qt needs QT_ACCESSIBILITY=1)")
    return best


def uia_tree(hwnd: int, depth: int = 4, flt: str | None = None, max_nodes: int = 400) -> list[dict]:
    root = window_accessible(hwnd)
    out: list[dict] = []

    def walk(acc, d):
        if len(out) >= max_nodes or acc is None:
            return
        node = describe(acc, d, hwnd)
        if _matches_filter(node, flt):
            out.append(node)
        if d >= depth:
            return
        try:
            n = acc.get_child_count()
        except Exception:  # noqa: BLE001
            return
        for i in range(min(n, 2000)):
            if len(out) >= max_nodes:
                return
            try:
                walk(acc.get_child_at_index(i), d + 1)
            except Exception:  # noqa: BLE001
                continue

    walk(root, 0)
    return out


def win32_tree(hwnd: int, flt: str | None = None, max_nodes: int = 400) -> list[dict]:
    return []


def find(hwnd: int, query, max_results: int = 10) -> list[dict]:
    """query: str (substring on name/aid/value) or dict with any of
    name, type, automation_id, class, text, exact (bool), index (int)."""
    nodes = uia_tree(hwnd, depth=60, flt=None, max_nodes=5000)
    q = {"text": query} if isinstance(query, str) else dict(query or {})
    exact = bool(q.get("exact", False))

    def eq(a, b):
        a, b = str(a or "").lower(), str(b or "").lower()
        return a == b if exact else b in a

    res = []
    for n in nodes:
        if "name" in q and not eq(n.get("name"), q["name"]):
            continue
        if "type" in q and str(n.get("type", "")).lower() != str(q["type"]).lower().replace("control", ""):
            continue
        if "automation_id" in q and not eq(n.get("aid"), q["automation_id"]):
            continue
        if "class" in q and not eq(n.get("cls"), q["class"]):
            continue
        if "text" in q and not any(eq(n.get(k), q["text"]) for k in ("name", "aid", "value")):
            continue
        res.append(n)
    if "index" in q:
        i = int(q["index"])
        res = res[i:i + 1]
    return res[:max_results]


# ---------------------------------------------------------------- resolve & props
def resolve(eid: str):
    """-> ('uia', Atspi.Accessible, hwnd)."""
    acc, hwnd = REGISTRY.get(str(eid))
    return "uia", acc, hwnd


def element_from_point(x: int, y: int):
    """Not used for clicks on Linux (XTEST at the point is more faithful); kept for the API."""
    raise RuntimeError("element_from_point: not available on X11 (use find_element / ui_tree)")


def live_info(acc) -> dict:
    A = _A()
    st = _states(acc)
    try:
        pid = int(acc.get_process_id())
    except Exception:  # noqa: BLE001
        pid = 0
    try:
        name = acc.get_name() or ""
    except Exception:  # noqa: BLE001
        name = ""
    return {"type": _type(acc), "name": name, "aid": "", "cls": _role(acc), "rect": _rect(acc), "pid": pid,
            "hwnd": 0, "enabled": A.StateType.ENABLED in st or A.StateType.SENSITIVE in st,
            "password": _role(acc) == "password text"}


def has_focus(acc) -> bool:
    return _A().StateType.FOCUSED in _states(acc)


def give_focus(acc) -> str | None:
    c = _iface(acc, "component")
    if c is None:
        return None
    try:
        if c.grab_focus() or has_focus(acc):
            return "AT-SPI Component.grab_focus"
    except Exception:  # noqa: BLE001
        return None
    return None


def _is_win32_password(hwnd: int) -> bool:
    return False


def is_password(kind: str, acc, hwnd: int) -> bool:
    return acc is not None and _role(acc) == "password text"


def read_value(kind: str, acc, hwnd: int) -> dict:
    t = _iface(acc, "text")
    if t is not None:
        try:
            return {"value": t.get_text(0, -1), "via": "AT-SPI.Text"}
        except Exception:  # noqa: BLE001
            pass
    v = _iface(acc, "value")
    if v is not None:
        try:
            return {"value": str(v.get_current_value()), "via": "AT-SPI.Value"}
        except Exception:  # noqa: BLE001
            pass
    try:
        return {"value": acc.get_name() or "", "via": "AT-SPI.Name"}
    except Exception:  # noqa: BLE001
        raise RuntimeError("element has no readable value") from None


def set_value(kind: str, acc, hwnd: int, text: str) -> dict:
    et = _iface(acc, "editable_text")
    if et is not None:
        try:
            if et.set_text_contents(text):
                return {"via": "AT-SPI.EditableText.set_text_contents"}
        except Exception:  # noqa: BLE001
            pass
    v = _iface(acc, "value")
    if v is not None:
        try:
            if v.set_current_value(float(text)):
                return {"via": "AT-SPI.Value"}
        except (ValueError, Exception):  # noqa: BLE001
            pass
    raise RuntimeError("element does not accept a value (not editable): use type_text")


def insert_text_win32(hwnd: int, text: str) -> str | None:
    return None


def invoke(kind: str, acc, hwnd: int) -> dict:
    a = _iface(acc, "action")
    if a is not None:
        names = _actions(acc)
        order = [i for n in _ACTION_NAMES for i, nm in enumerate(names) if nm == n] + list(range(len(names)))
        for i in order:
            try:
                if a.do_action(i):
                    return {"via": f"AT-SPI.Action({names[i] or i})"}
            except Exception:  # noqa: BLE001
                continue
    raise RuntimeError("no AT-SPI action for this element")


def select(kind: str, acc, hwnd: int, item: str | None) -> dict:
    if item is None:
        parent = acc.get_parent()
        sel = _iface(parent, "selection") if parent is not None else None
        if sel is not None:
            try:
                if sel.select_child(acc.get_index_in_parent()):
                    return {"via": "AT-SPI.Selection.select_child"}
            except Exception:  # noqa: BLE001
                pass
        return invoke(kind, acc, hwnd)
    # combo box / list: look for the item by name among the descendants
    target = None
    stack = [(acc, 0)]
    while stack and target is None:
        cur, dpt = stack.pop()
        try:
            n = cur.get_child_count()
        except Exception:  # noqa: BLE001
            continue
        for i in range(n):
            ch = cur.get_child_at_index(i)
            if ch is None:
                continue
            try:
                if (ch.get_name() or "").strip().lower() == item.strip().lower():
                    target = ch
                    break
            except Exception:  # noqa: BLE001
                pass
            if dpt < 4:
                stack.append((ch, dpt + 1))
    if target is None:
        raise RuntimeError(f"item '{item}' not found under the element")
    parent = target.get_parent()
    sel = _iface(parent, "selection") if parent is not None else None
    if sel is not None:
        try:
            if sel.select_child(target.get_index_in_parent()):
                return {"via": "AT-SPI.Selection.select_child"}
        except Exception:  # noqa: BLE001
            pass
    r = invoke(kind, target, hwnd)
    return {"via": r["via"] + " on the item"}


def scroll(kind: str, acc, hwnd: int, dx: int, dy: int) -> dict | None:
    return None  # AT-SPI has no generic scroll: the wheel (XTEST) does it
