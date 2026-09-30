"""UI Automation tree, element registry and pattern-based actions (no cursor).

Only ever called from the automation worker thread: UIA COM objects are
apartment-bound, and `uiautomation` is imported lazily there.

Element ids:
  e<hash>  - UIA element; hash of its RuntimeId (stable while the element
             lives). Kept in a registry so later calls can act on it.
  w<hwnd>  - plain Win32 child window (fallback enumeration).
"""

from __future__ import annotations

import ctypes
import zlib
from collections import OrderedDict

from . import winapi as w

_auto = None


def auto():
    global _auto
    if _auto is None:
        # the inner module exposes _AutomationClient as well as the public API
        import uiautomation.uiautomation as uiautomation
        _auto = uiautomation
    return _auto


TREE_SCOPE_SUBTREE = 7
MAX_REGISTRY = 20000

# pattern short name -> Is<Pattern>Available property name in uiautomation.PropertyId
PATTERN_PROPS = {
    "invoke": "IsInvokePatternAvailableProperty",
    "value": "IsValuePatternAvailableProperty",
    "toggle": "IsTogglePatternAvailableProperty",
    "select_item": "IsSelectionItemPatternAvailableProperty",
    "selection": "IsSelectionPatternAvailableProperty",
    "expand": "IsExpandCollapsePatternAvailableProperty",
    "text": "IsTextPatternAvailableProperty",
    "scroll": "IsScrollPatternAvailableProperty",
    "range": "IsRangeValuePatternAvailableProperty",
    "legacy": "IsLegacyIAccessiblePatternAvailableProperty",
}
ACTIONABLE = {"invoke", "value", "toggle", "select_item", "selection", "expand", "scroll", "range", "text"}
WIN32_EDIT_CLASSES = ("edit", "richedit", "richedit20w", "richedit50w", "richeditd2dpt", "tmemo", "tedit")


class ElementNotFound(LookupError):
    pass


class Registry:
    def __init__(self):
        self._items: "OrderedDict[str, tuple]" = OrderedDict()

    def put(self, eid: str, elem, hwnd: int) -> None:
        self._items[eid] = (elem, hwnd)
        self._items.move_to_end(eid)
        while len(self._items) > MAX_REGISTRY:
            self._items.popitem(last=False)

    def get(self, eid: str):
        if eid not in self._items:
            raise ElementNotFound(f"Unknown element id '{eid}' - call ui_tree/find_element again (ids are "
                                  "per server run and per element lifetime).")
        return self._items[eid]


REGISTRY = Registry()


def _rid_to_id(rid) -> str:
    key = ",".join(str(int(x)) for x in (rid or ()))
    return "e" + format(zlib.crc32(key.encode()) & 0xFFFFFFF, "07x")


def _cache_request():
    a = auto()
    uia = a._AutomationClient.instance().IUIAutomation
    cr = uia.CreateCacheRequest()
    P = a.PropertyId
    for name in ("RuntimeIdProperty", "NameProperty", "ControlTypeProperty", "AutomationIdProperty",
                 "ClassNameProperty", "BoundingRectangleProperty", "NativeWindowHandleProperty",
                 "IsEnabledProperty", "IsOffscreenProperty", "IsPasswordProperty", "ProcessIdProperty",
                 "ValueValueProperty", "ValueIsReadOnlyProperty", "ToggleToggleStateProperty",
                 "LegacyIAccessibleValueProperty", "SelectionItemIsSelectedProperty",
                 "ExpandCollapseExpandCollapseStateProperty", *PATTERN_PROPS.values()):
        pid = getattr(P, name, None)
        if pid is not None:
            cr.AddProperty(pid)
    cr.TreeScope = TREE_SCOPE_SUBTREE
    return uia, cr


def _cached(elem, prop_name, default=None):
    pid = getattr(auto().PropertyId, prop_name, None)
    if pid is None:
        return default
    try:
        v = elem.GetCachedPropertyValue(pid)
        return default if v is None else v
    except Exception:
        return default


def _current(elem, prop_name, default=None):
    pid = getattr(auto().PropertyId, prop_name, None)
    if pid is None:
        return default
    try:
        v = elem.GetCurrentPropertyValue(pid)
        return default if v is None else v
    except Exception:
        return default


def _type_name(ct) -> str:
    names = getattr(auto(), "ControlTypeNames", {})
    n = names.get(ct, str(ct))
    return n.replace("Control", "") if isinstance(n, str) else str(n)


def _rect(v) -> tuple | None:
    """BoundingRectangle arrives as (left, top, width, height) doubles."""
    try:
        l, t, wd, ht = [float(x) for x in v]
        if wd <= 0 or ht <= 0:
            return None
        return (round(l), round(t), round(l + wd), round(t + ht))
    except Exception:
        return None


def describe_cached(elem, depth: int, hwnd_hint: int) -> dict:
    rid = _cached(elem, "RuntimeIdProperty", ())
    eid = _rid_to_id(rid)
    native = int(_cached(elem, "NativeWindowHandleProperty", 0) or 0)
    REGISTRY.put(eid, elem, native or hwnd_hint)
    patterns = [k for k, prop in PATTERN_PROPS.items() if _cached(elem, prop, False)]
    node = {"id": eid, "d": depth, "type": _type_name(_cached(elem, "ControlTypeProperty", 0)),
            "name": (_cached(elem, "NameProperty", "") or "")[:120]}
    aid = _cached(elem, "AutomationIdProperty", "")
    if aid:
        node["aid"] = aid
    cls = _cached(elem, "ClassNameProperty", "")
    if cls:
        node["cls"] = cls
    r = _rect(_cached(elem, "BoundingRectangleProperty", None))
    if r:
        node["rect"] = r
    if patterns:
        node["pat"] = patterns
    if "value" in patterns:
        val = _cached(elem, "ValueValueProperty", "")
        if val:
            node["value"] = str(val)[:200]
    elif "legacy" in patterns:
        val = _cached(elem, "LegacyIAccessibleValueProperty", "")
        if val:
            node["value"] = str(val)[:200]
    if "toggle" in patterns:
        node["toggle"] = int(_cached(elem, "ToggleToggleStateProperty", 0) or 0)
    if "select_item" in patterns and _cached(elem, "SelectionItemIsSelectedProperty", False):
        node["selected"] = True
    if not _cached(elem, "IsEnabledProperty", True):
        node["disabled"] = True
    if _cached(elem, "IsOffscreenProperty", False):
        node["offscreen"] = True
    if _cached(elem, "IsPasswordProperty", False):
        node["password"] = True
    if native:
        node["hwnd"] = native
    return node


def _matches_filter(node: dict, flt: str | None) -> bool:
    if not flt or flt == "all":
        return True
    if flt == "interactive":
        return bool(set(node.get("pat", [])) & ACTIONABLE) and not node.get("offscreen")
    s = flt.lower()
    return any(s in str(node.get(k, "")).lower() for k in ("name", "aid", "value", "type", "cls"))


def uia_tree(hwnd: int, depth: int = 4, flt: str | None = None, max_nodes: int = 400) -> list[dict]:
    uia, cr = _cache_request()
    root = uia.ElementFromHandleBuildCache(hwnd, cr)
    out: list[dict] = []

    seen: set[str] = set()

    def walk(elem, d):
        if len(out) >= max_nodes:
            return
        node = describe_cached(elem, d, hwnd)
        if node["id"] in seen:   # XAML islands can surface the same element twice
            return
        seen.add(node["id"])
        if _matches_filter(node, flt):
            out.append(node)
        if d >= depth:
            return
        try:
            kids = elem.GetCachedChildren()
        except Exception:
            return
        if not kids:  # leaf: comtypes returns a NULL pointer, not None
            return
        for i in range(kids.Length):
            walk(kids.GetElement(i), d + 1)

    walk(root, 0)
    return out


def win32_tree(hwnd: int, flt: str | None = None, max_nodes: int = 400) -> list[dict]:
    """Classic EnumChildWindows + GetClassName + WM_GETTEXT (for apps with no UIA)."""
    out: list[dict] = []

    @w.WNDENUMPROC
    def _cb(h, _):
        if len(out) >= max_nodes:
            return False
        style = w.user32.GetWindowLongW(h, w.GWL_STYLE)
        cls = w.class_name(h)
        node = {"id": f"w{int(h)}", "hwnd": int(h), "cls": cls,
                "name": w.get_text_via_message(h, 200) if not (style & w.ES_PASSWORD and "edit" in cls.lower()) else "",
                "rect": w.window_rect(h), "ctrl_id": w.user32.GetDlgCtrlID(h)}
        parent = int(w.user32.GetParent(h) or 0)
        if parent and parent != int(hwnd):
            node["parent"] = parent
        if not w.user32.IsWindowVisible(h):
            node["hidden"] = True
        if not w.user32.IsWindowEnabled(h):
            node["disabled"] = True
        if style & w.ES_PASSWORD and "edit" in cls.lower():
            node["password"] = True
        if not flt or flt in ("all", "interactive") or flt.lower() in (cls + " " + node["name"]).lower():
            out.append(node)
        return True

    w.user32.EnumChildWindows(hwnd, _cb, 0)
    return out


def find(hwnd: int, query, max_results: int = 10) -> list[dict]:
    """query: str (substring on name/aid/value) or dict with any of
    name, type, automation_id, class, text, exact (bool), index (int)."""
    nodes = uia_tree(hwnd, depth=60, flt=None, max_nodes=5000)
    if isinstance(query, str):
        q = {"text": query}
    else:
        q = dict(query or {})
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
    """-> ('uia', IUIAutomationElement, hwnd) or ('win32', None, hwnd)."""
    if eid.startswith("w") and eid[1:].isdigit():
        h = int(eid[1:])
        if not w.user32.IsWindow(h):
            raise ElementNotFound(f"window {h} no longer exists")
        return "win32", None, h
    elem, hwnd = REGISTRY.get(eid)
    return "uia", elem, hwnd


def element_from_point(x: int, y: int):
    uia = auto()._AutomationClient.instance().IUIAutomation
    from comtypes.gen.UIAutomationClient import tagPOINT
    return uia.ElementFromPoint(tagPOINT(x, y))


def live_info(elem) -> dict:
    return {
        "type": _type_name(_current(elem, "ControlTypeProperty", 0)),
        "name": _current(elem, "NameProperty", ""),
        "aid": _current(elem, "AutomationIdProperty", ""),
        "cls": _current(elem, "ClassNameProperty", ""),
        "rect": _rect(_current(elem, "BoundingRectangleProperty", None)),
        "pid": int(_current(elem, "ProcessIdProperty", 0) or 0),
        "hwnd": int(_current(elem, "NativeWindowHandleProperty", 0) or 0),
        "enabled": bool(_current(elem, "IsEnabledProperty", True)),
        "password": bool(_current(elem, "IsPasswordProperty", False)),
    }


def _same_element(a, b) -> bool:
    try:
        uia = auto()._AutomationClient.instance().IUIAutomation
        return bool(uia.CompareElements(a, b))
    except Exception:
        return False


def has_focus(elem) -> bool:
    """True if elem (or one of its descendants) has the keyboard focus."""
    try:
        uia = auto()._AutomationClient.instance().IUIAutomation
        cur = uia.GetFocusedElement()
        walker = uia.ControlViewWalker
        for _ in range(8):
            if not cur:
                return False
            if _same_element(cur, elem):
                return True
            cur = walker.GetParentElement(cur)
    except Exception:
        pass
    return False


def give_focus(elem) -> str | None:
    """UIA SetFocus + verification. Returns the mechanism, or None if the app ignored it."""
    import time as _t
    if has_focus(elem):
        return "already focused"
    try:
        elem.SetFocus()
    except Exception:
        return None
    _t.sleep(0.08)
    return "UIA.SetFocus" if has_focus(elem) else None


def _control(elem):
    return auto().Control.CreateControlFromElement(elem)


def _pattern(elem, name: str):
    a = auto()
    pid = getattr(a.PatternId, name, None)
    if pid is None:
        return None
    try:
        return _control(elem).GetPattern(pid)
    except Exception:
        return None


def _is_win32_password(hwnd: int) -> bool:
    return bool(hwnd) and "edit" in w.class_name(hwnd).lower() and \
        bool(w.user32.GetWindowLongW(hwnd, w.GWL_STYLE) & w.ES_PASSWORD)


def is_password(kind: str, elem, hwnd: int) -> bool:
    if kind == "uia" and _current(elem, "IsPasswordProperty", False):
        return True
    return _is_win32_password(hwnd)


# ---------------------------------------------------------------- actions (no cursor)
def read_value(kind: str, elem, hwnd: int) -> dict:
    if kind == "uia":
        p = _pattern(elem, "ValuePattern")
        if p is not None:
            try:
                return {"value": p.Value, "via": "UIA.Value"}
            except Exception:
                pass
        p = _pattern(elem, "TextPattern")
        if p is not None:
            try:
                return {"value": p.DocumentRange.GetText(-1), "via": "UIA.Text"}
            except Exception:
                pass
        p = _pattern(elem, "LegacyIAccessiblePattern")
        if p is not None:
            try:
                v = p.Value
                if v:
                    return {"value": v, "via": "UIA.LegacyIAccessible"}
            except Exception:
                pass
        native = int(_current(elem, "NativeWindowHandleProperty", 0) or 0)
        if native:
            txt = w.get_text_via_message(native)
            if txt:
                return {"value": txt, "via": "Win32.WM_GETTEXT"}
        return {"value": _current(elem, "NameProperty", ""), "via": "UIA.Name"}
    return {"value": w.get_text_via_message(hwnd), "via": "Win32.WM_GETTEXT"}


def set_value(kind: str, elem, hwnd: int, text: str) -> dict:
    if kind == "uia":
        p = _pattern(elem, "ValuePattern")
        if p is not None:
            try:
                if not p.IsReadOnly:
                    p.SetValue(text)
                    return {"via": "UIA.Value.SetValue"}
            except Exception:
                pass
        p = _pattern(elem, "LegacyIAccessiblePattern")
        if p is not None:
            try:
                p.SetValue(text)
                return {"via": "UIA.LegacyIAccessible.SetValue"}
            except Exception:
                pass
        # editable combo boxes: the writable value lives in an inner Edit
        a = auto()
        for c, _d in a.WalkControl(_control(elem), maxDepth=3):
            try:
                vp = c.GetPattern(a.PatternId.ValuePattern)
                if vp is not None and not vp.IsReadOnly:
                    vp.SetValue(text)
                    return {"via": "UIA.Value.SetValue(inner edit)"}
            except Exception:
                continue
        hwnd = int(_current(elem, "NativeWindowHandleProperty", 0) or 0) or 0
    if hwnd:
        buf = ctypes.create_unicode_buffer(text)
        w.user32.SendMessageW(hwnd, w.WM_SETTEXT, 0, ctypes.cast(buf, ctypes.c_void_p).value)
        return {"via": "Win32.WM_SETTEXT"}
    raise RuntimeError("element has no Value pattern and no window handle for WM_SETTEXT")


def insert_text_win32(hwnd: int, text: str) -> str:
    """Insert at the caret without focus: EM_REPLACESEL for edit/rich-edit
    controls, otherwise WM_CHAR per character."""
    cls = w.class_name(hwnd).lower()
    if any(cls.startswith(c) for c in WIN32_EDIT_CLASSES):
        buf = ctypes.create_unicode_buffer(text.replace("\n", "\r\n") if "rich" not in cls else text.replace("\n", "\r"))
        w.user32.SendMessageW(hwnd, w.EM_REPLACESEL, 1, ctypes.cast(buf, ctypes.c_void_p).value)
        return "Win32.EM_REPLACESEL"
    for ch in text:
        code = 13 if ch == "\n" else ord(ch)
        w.user32.PostMessageW(hwnd, w.WM_CHAR, code, 1)
    return "Win32.WM_CHAR"


def invoke(kind: str, elem, hwnd: int) -> dict:
    if kind == "uia":
        for pname, call, label in (
            ("InvokePattern", lambda p: p.Invoke(), "UIA.Invoke"),
            ("TogglePattern", lambda p: p.Toggle(), "UIA.Toggle"),
            ("SelectionItemPattern", lambda p: p.Select(), "UIA.SelectionItem.Select"),
            ("ExpandCollapsePattern",
             lambda p: p.Collapse() if p.ExpandCollapseState == 1 else p.Expand(), "UIA.ExpandCollapse"),
            ("LegacyIAccessiblePattern", lambda p: p.DoDefaultAction(), "UIA.LegacyIAccessible.DoDefaultAction"),
        ):
            p = _pattern(elem, pname)
            if p is None:
                continue
            try:
                call(p)
                return {"via": label}
            except Exception:
                continue
        hwnd = int(_current(elem, "NativeWindowHandleProperty", 0) or 0)
    if hwnd and "button" in w.class_name(hwnd).lower():
        w.user32.PostMessageW(hwnd, w.BM_CLICK, 0, 0)
        return {"via": "Win32.BM_CLICK"}
    raise RuntimeError("no invokable pattern / message for this element")


def select(kind: str, elem, hwnd: int, item: str | None) -> dict:
    if kind == "uia":
        a = auto()
        if item is None:
            p = _pattern(elem, "SelectionItemPattern")
            if p is not None:
                p.Select()
                return {"via": "UIA.SelectionItem.Select"}
            raise RuntimeError("element is not selectable and no item given")
        ctrl = _control(elem)
        exp = _pattern(elem, "ExpandCollapsePattern")
        expanded = False
        if exp is not None:
            try:
                if exp.ExpandCollapseState == 0:
                    exp.Expand()
                    expanded = True
            except Exception:
                pass
        target = None
        want = item.lower()
        for c, _d in a.WalkControl(ctrl, maxDepth=4):
            if (c.Name or "").strip().lower() == want:
                target = c
                break
        if target is None:
            for c, _d in a.WalkControl(ctrl, maxDepth=4):
                if want in (c.Name or "").lower():
                    target = c
                    break
        via = None
        if target is not None:
            si = target.GetPattern(a.PatternId.SelectionItemPattern)
            if si is not None:
                si.Select()
                via = "UIA.SelectionItem.Select"
            else:
                ip = target.GetPattern(a.PatternId.InvokePattern)
                if ip is not None:
                    ip.Invoke()
                    via = "UIA.Invoke(item)"
        if via is None:
            vp = _pattern(elem, "ValuePattern")
            if vp is not None and not vp.IsReadOnly:
                vp.SetValue(item)
                via = "UIA.Value.SetValue(item)"
        if expanded and exp is not None:
            try:
                if exp.ExpandCollapseState == 1:
                    exp.Collapse()
            except Exception:
                pass
        if via:
            return {"via": via}
        hwnd = int(_current(elem, "NativeWindowHandleProperty", 0) or 0)
        if not hwnd:
            raise RuntimeError(f"item '{item}' not found")
    cls = w.class_name(hwnd).lower()
    buf = ctypes.create_unicode_buffer(item or "")
    parent = w.user32.GetParent(hwnd)
    cid = w.user32.GetDlgCtrlID(hwnd)
    if "combobox" in cls:
        idx = w.user32.SendMessageW(hwnd, w.CB_FINDSTRINGEXACT, ctypes.c_size_t(-1).value,
                                    ctypes.cast(buf, ctypes.c_void_p).value)
        if idx < 0:
            raise RuntimeError(f"item '{item}' not in combobox")
        w.user32.SendMessageW(hwnd, w.CB_SETCURSEL, idx, 0)
        w.user32.SendMessageW(parent, w.WM_COMMAND, (w.CBN_SELCHANGE << 16) | (cid & 0xFFFF), hwnd)
        return {"via": "Win32.CB_SETCURSEL"}
    if "listbox" in cls:
        idx = w.user32.SendMessageW(hwnd, w.LB_FINDSTRINGEXACT, ctypes.c_size_t(-1).value,
                                    ctypes.cast(buf, ctypes.c_void_p).value)
        if idx < 0:
            raise RuntimeError(f"item '{item}' not in listbox")
        w.user32.SendMessageW(hwnd, w.LB_SETCURSEL, idx, 0)
        w.user32.SendMessageW(parent, w.WM_COMMAND, (w.LBN_SELCHANGE << 16) | (cid & 0xFFFF), hwnd)
        return {"via": "Win32.LB_SETCURSEL"}
    raise RuntimeError(f"cannot select on a '{cls}' window")


def scroll(kind: str, elem, hwnd: int, dx: int, dy: int) -> dict | None:
    """UIA ScrollPattern (small increments). dy>0 = down. None if unsupported."""
    if kind != "uia":
        return None
    p = _pattern(elem, "ScrollPattern")
    if p is None:
        return None
    # ScrollAmount: 0 LargeDecrement, 1 SmallDecrement, 2 NoAmount, 3 LargeIncrement, 4 SmallIncrement
    for _ in range(abs(dy)):
        p.Scroll(2, 4 if dy > 0 else 1)
    for _ in range(abs(dx)):
        p.Scroll(4 if dx > 0 else 1, 2)
    return {"via": "UIA.Scroll"}
