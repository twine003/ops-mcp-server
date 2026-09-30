"""Thin ctypes bindings for the Win32 calls used by the desktop server.

Plain ctypes (no pywin32) keeps the venv small and every signature explicit:
on 64-bit Windows a missing argtypes/restype silently truncates handles and
LPARAMs to 32 bits.
"""

import ctypes
from ctypes import wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shcore = ctypes.WinDLL("shcore", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t
HANDLE = wt.HANDLE

# ---------------------------------------------------------------- DPI
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)


def set_dpi_awareness() -> str:
    """Per-monitor v2 so every coordinate is a physical pixel. Must run first."""
    try:
        user32.SetProcessDpiAwarenessContext.restype = wt.BOOL
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
            return "per_monitor_v2"
        if ctypes.get_last_error() == 5:  # ACCESS_DENIED: already set (by us earlier)
            return "already_set"
    except AttributeError:
        pass
    try:
        shcore.SetProcessDpiAwareness(2)
        return "per_monitor_v1"
    except Exception:
        return "unaware"


# ---------------------------------------------------------------- structs
class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG), ("right", wt.LONG), ("bottom", wt.LONG)]

    def as_tuple(self):
        return (self.left, self.top, self.right, self.bottom)


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wt.LONG), ("cy", wt.LONG)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT), ("rcWork", RECT),
                ("dwFlags", wt.DWORD), ("szDevice", wt.WCHAR * 32)]


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("DeviceName", wt.WCHAR * 32), ("DeviceString", wt.WCHAR * 128),
                ("StateFlags", wt.DWORD), ("DeviceID", wt.WCHAR * 128), ("DeviceKey", wt.WCHAR * 128)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wt.HWND), ("message", wt.UINT), ("wParam", WPARAM), ("lParam", LPARAM),
                ("time", wt.DWORD), ("pt", POINT), ("lPrivate", wt.DWORD)]


WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, WPARAM, LPARAM)
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)
MONITORENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(RECT), LPARAM)
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE),
                ("hIcon", wt.HICON), ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", wt.UINT), ("flags", wt.UINT), ("showCmd", wt.UINT),
                ("ptMinPosition", POINT), ("ptMaxPosition", POINT), ("rcNormalPosition", RECT)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hwndActive", wt.HWND),
                ("hwndFocus", wt.HWND), ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND),
                ("hwndMoveSize", wt.HWND), ("hwndCaret", wt.HWND), ("rcCaret", RECT)]


# ---------------------------------------------------------------- constants
WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011
ULW_ALPHA = 0x02
AC_SRC_ALPHA = 0x01
SW_HIDE, SW_SHOWNOACTIVATE, SW_RESTORE, SW_SHOWMINIMIZED, SW_SHOWMAXIMIZED = 0, 4, 9, 2, 3
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x1, 0x2, 0x4, 0x10, 0x40
HWND_TOPMOST = wt.HWND(-1)
HWND_MESSAGE = wt.HWND(-3)

WM_DESTROY, WM_CLOSE, WM_TIMER, WM_HOTKEY = 0x0002, 0x0010, 0x0113, 0x0312
WM_DISPLAYCHANGE, WM_DPICHANGED, WM_SETTINGCHANGE = 0x007E, 0x02E0, 0x001A
WM_MOUSEACTIVATE, MA_NOACTIVATE = 0x0021, 3
WM_NCHITTEST, HTCLIENT = 0x0084, 1
WM_SETCURSOR = 0x0020
WM_APP = 0x8000
WM_QUIT = 0x0012
WM_SETTEXT, WM_GETTEXT, WM_GETTEXTLENGTH = 0x000C, 0x000D, 0x000E
WM_KEYDOWN, WM_KEYUP, WM_CHAR, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0102, 0x0104, 0x0105
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0200, 0x0201, 0x0202, 0x0203
WM_RBUTTONDOWN, WM_RBUTTONUP, WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0204, 0x0205, 0x0207, 0x0208
WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 0x020A, 0x020E
WM_COMMAND = 0x0111
MK_LBUTTON, MK_RBUTTON, MK_MBUTTON = 0x1, 0x2, 0x10
BM_CLICK = 0x00F5
CB_SETCURSEL, CB_FINDSTRINGEXACT, CB_GETCOUNT = 0x014E, 0x0158, 0x0146
LB_SETCURSEL, LB_FINDSTRINGEXACT = 0x0186, 0x01A2
CBN_SELCHANGE, LBN_SELCHANGE = 1, 1
EM_REPLACESEL, EM_SETSEL = 0x00C2, 0x00B1
ES_PASSWORD = 0x0020
GWL_STYLE, GWL_EXSTYLE = -16, -20
GA_ROOT, GA_ROOTOWNER = 2, 3
GW_OWNER = 4
SMTO_ABORTIFHUNG = 0x2

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x1, 0x2, 0x4
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x8, 0x10
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x20, 0x40
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x800, 0x1000
MOUSEEVENTF_VIRTUALDESK, MOUSEEVENTF_ABSOLUTE = 0x4000, 0x8000
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x1, 0x2, 0x4
WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
LLMHF_INJECTED, LLKHF_INJECTED = 0x1, 0x10
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
MONITORINFOF_PRIMARY = 0x1
MONITOR_DEFAULTTONEAREST = 2
MDT_EFFECTIVE_DPI = 0
PW_RENDERFULLCONTENT = 0x2
DWMWA_EXTENDED_FRAME_BOUNDS, DWMWA_CLOAKED = 9, 14
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
DIB_RGB_COLORS, BI_RGB = 0, 0

# Marks every SendInput event we inject, so our own windows and hooks can
# tell our input from the user's ("DMCP" in ASCII).
INJECT_MAGIC = 0x444D4350


# ---------------------------------------------------------------- signatures
def _sig(dll, name, restype, *argtypes):
    f = getattr(dll, name)
    f.restype = restype
    f.argtypes = list(argtypes)
    return f


_sig(user32, "EnumDisplayMonitors", wt.BOOL, wt.HDC, ctypes.c_void_p, MONITORENUMPROC, LPARAM)
_sig(user32, "GetMonitorInfoW", wt.BOOL, wt.HMONITOR, ctypes.POINTER(MONITORINFOEXW))
_sig(user32, "MonitorFromPoint", wt.HMONITOR, POINT, wt.DWORD)
_sig(user32, "MonitorFromWindow", wt.HMONITOR, wt.HWND, wt.DWORD)
_sig(user32, "EnumDisplayDevicesW", wt.BOOL, wt.LPCWSTR, wt.DWORD, ctypes.POINTER(DISPLAY_DEVICEW), wt.DWORD)
_sig(shcore, "GetDpiForMonitor", ctypes.c_long, wt.HMONITOR, ctypes.c_int, ctypes.POINTER(wt.UINT), ctypes.POINTER(wt.UINT))
_sig(user32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
_sig(user32, "GetCursorPos", wt.BOOL, ctypes.POINTER(POINT))
_sig(user32, "SetCursorPos", wt.BOOL, ctypes.c_int, ctypes.c_int)
_sig(user32, "SendInput", wt.UINT, wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
_sig(user32, "GetLastInputInfo", wt.BOOL, ctypes.POINTER(LASTINPUTINFO))
_sig(user32, "GetMessageExtraInfo", LPARAM)
_sig(user32, "SetWindowsHookExW", HANDLE, ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD)
_sig(user32, "UnhookWindowsHookEx", wt.BOOL, HANDLE)
_sig(user32, "CallNextHookEx", LRESULT, HANDLE, ctypes.c_int, WPARAM, LPARAM)
_sig(user32, "RegisterHotKey", wt.BOOL, wt.HWND, ctypes.c_int, wt.UINT, wt.UINT)
_sig(user32, "UnregisterHotKey", wt.BOOL, wt.HWND, ctypes.c_int)
_sig(user32, "GetMessageW", wt.BOOL, ctypes.POINTER(MSG), wt.HWND, wt.UINT, wt.UINT)
_sig(user32, "PeekMessageW", wt.BOOL, ctypes.POINTER(MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT)
_sig(user32, "TranslateMessage", wt.BOOL, ctypes.POINTER(MSG))
_sig(user32, "DispatchMessageW", LRESULT, ctypes.POINTER(MSG))
_sig(user32, "PostThreadMessageW", wt.BOOL, wt.DWORD, wt.UINT, WPARAM, LPARAM)
_sig(user32, "PostQuitMessage", None, ctypes.c_int)
_sig(user32, "RegisterClassExW", wt.ATOM, ctypes.POINTER(WNDCLASSEXW))
_sig(user32, "CreateWindowExW", wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int,
     ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p)
_sig(user32, "DestroyWindow", wt.BOOL, wt.HWND)
_sig(user32, "DefWindowProcW", LRESULT, wt.HWND, wt.UINT, WPARAM, LPARAM)
_sig(user32, "ShowWindow", wt.BOOL, wt.HWND, ctypes.c_int)
_sig(user32, "SetWindowPos", wt.BOOL, wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT)
_sig(user32, "UpdateLayeredWindow", wt.BOOL, wt.HWND, wt.HDC, ctypes.POINTER(POINT), ctypes.POINTER(SIZE),
     wt.HDC, ctypes.POINTER(POINT), wt.COLORREF, ctypes.POINTER(BLENDFUNCTION), wt.DWORD)
_sig(user32, "SetWindowDisplayAffinity", wt.BOOL, wt.HWND, wt.DWORD)
_sig(user32, "SetTimer", ctypes.c_size_t, wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p)
_sig(user32, "KillTimer", wt.BOOL, wt.HWND, ctypes.c_size_t)
_sig(user32, "PostMessageW", wt.BOOL, wt.HWND, wt.UINT, WPARAM, LPARAM)
_sig(user32, "SendMessageW", LRESULT, wt.HWND, wt.UINT, WPARAM, LPARAM)
_sig(user32, "SendMessageTimeoutW", LRESULT, wt.HWND, wt.UINT, WPARAM, LPARAM, wt.UINT, wt.UINT, ctypes.POINTER(ctypes.c_size_t))
_sig(user32, "GetDC", wt.HDC, wt.HWND)
_sig(user32, "ReleaseDC", ctypes.c_int, wt.HWND, wt.HDC)
_sig(user32, "LoadCursorW", wt.HANDLE, wt.HINSTANCE, ctypes.c_void_p)
_sig(user32, "SetCursor", wt.HANDLE, wt.HANDLE)
_sig(user32, "EnumWindows", wt.BOOL, WNDENUMPROC, LPARAM)
_sig(user32, "EnumChildWindows", wt.BOOL, wt.HWND, WNDENUMPROC, LPARAM)
_sig(user32, "GetWindowTextW", ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int)
_sig(user32, "GetWindowTextLengthW", ctypes.c_int, wt.HWND)
_sig(user32, "GetClassNameW", ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int)
_sig(user32, "IsWindowVisible", wt.BOOL, wt.HWND)
_sig(user32, "IsWindowEnabled", wt.BOOL, wt.HWND)
_sig(user32, "IsWindow", wt.BOOL, wt.HWND)
_sig(user32, "IsIconic", wt.BOOL, wt.HWND)
_sig(user32, "IsZoomed", wt.BOOL, wt.HWND)
_sig(user32, "GetWindowRect", wt.BOOL, wt.HWND, ctypes.POINTER(RECT))
_sig(user32, "GetClientRect", wt.BOOL, wt.HWND, ctypes.POINTER(RECT))
_sig(user32, "ClientToScreen", wt.BOOL, wt.HWND, ctypes.POINTER(POINT))
_sig(user32, "ScreenToClient", wt.BOOL, wt.HWND, ctypes.POINTER(POINT))
_sig(user32, "GetWindowThreadProcessId", wt.DWORD, wt.HWND, ctypes.POINTER(wt.DWORD))
_sig(user32, "GetWindowLongW", wt.LONG, wt.HWND, ctypes.c_int)
_sig(user32, "GetAncestor", wt.HWND, wt.HWND, wt.UINT)
_sig(user32, "GetWindow", wt.HWND, wt.HWND, wt.UINT)
_sig(user32, "GetParent", wt.HWND, wt.HWND)
_sig(user32, "GetDlgCtrlID", ctypes.c_int, wt.HWND)
_sig(user32, "WindowFromPoint", wt.HWND, POINT)
_sig(user32, "ChildWindowFromPointEx", wt.HWND, wt.HWND, POINT, wt.UINT)
_sig(user32, "GetForegroundWindow", wt.HWND)
_sig(user32, "SetForegroundWindow", wt.BOOL, wt.HWND)
_sig(user32, "BringWindowToTop", wt.BOOL, wt.HWND)
_sig(user32, "GetWindowPlacement", wt.BOOL, wt.HWND, ctypes.POINTER(WINDOWPLACEMENT))
_sig(user32, "GetGUIThreadInfo", wt.BOOL, wt.DWORD, ctypes.POINTER(GUITHREADINFO))
_sig(user32, "MapVirtualKeyW", wt.UINT, wt.UINT, wt.UINT)
_sig(user32, "VkKeyScanW", ctypes.c_short, wt.WCHAR)
_sig(user32, "GetAsyncKeyState", ctypes.c_short, ctypes.c_int)
_sig(user32, "PrintWindow", wt.BOOL, wt.HWND, wt.HDC, wt.UINT)
_sig(user32, "GetDpiForWindow", wt.UINT, wt.HWND)
_sig(dwmapi, "DwmGetWindowAttribute", ctypes.c_long, wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD)
_sig(gdi32, "CreateCompatibleDC", wt.HDC, wt.HDC)
_sig(gdi32, "DeleteDC", wt.BOOL, wt.HDC)
_sig(gdi32, "CreateDIBSection", wt.HBITMAP, wt.HDC, ctypes.POINTER(BITMAPINFO), wt.UINT,
     ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD)
_sig(gdi32, "SelectObject", wt.HGDIOBJ, wt.HDC, wt.HGDIOBJ)
_sig(gdi32, "DeleteObject", wt.BOOL, wt.HGDIOBJ)
_sig(kernel32, "GetModuleHandleW", wt.HMODULE, wt.LPCWSTR)
_sig(kernel32, "GetCurrentThreadId", wt.DWORD)
_sig(kernel32, "GetTickCount", wt.DWORD)
_sig(kernel32, "OpenProcess", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
_sig(kernel32, "CloseHandle", wt.BOOL, wt.HANDLE)
_sig(kernel32, "QueryFullProcessImageNameW", wt.BOOL, wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD))


# ---------------------------------------------------------------- helpers
_sig(user32, "OpenInputDesktop", HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
_sig(user32, "CloseDesktop", wt.BOOL, HANDLE)
_sig(user32, "GetUserObjectInformationW", wt.BOOL, HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD))


def input_desktop() -> str | None:
    """Name of the desktop receiving input: 'Default' in a normal session;
    None when we cannot open it (lock screen, UAC secure desktop, Ctrl+Alt+Del).
    While it is not 'Default', Windows denies screen duplication (DXGI)."""
    h = user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(256)
        need = wt.DWORD(0)
        if not user32.GetUserObjectInformationW(h, 2, buf, ctypes.sizeof(buf), ctypes.byref(need)):  # UOI_NAME
            return None
        return buf.value
    finally:
        user32.CloseDesktop(h)


def window_text(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def class_name(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_rect(hwnd) -> tuple:
    """Visible frame bounds (DWM, without the invisible resize border)."""
    r = RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) == 0:
        return r.as_tuple()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.as_tuple()


def is_cloaked(hwnd) -> bool:
    v = wt.DWORD()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v)) == 0:
        return v.value != 0
    return False


def window_pid(hwnd) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def window_thread(hwnd) -> int:
    return user32.GetWindowThreadProcessId(hwnd, None)


def process_path(pid: int) -> str:
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(h)


def process_name(pid: int) -> str:
    p = process_path(pid)
    return p.rsplit("\\", 1)[-1] if p else ""


def cursor_pos() -> tuple:
    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


def get_text_via_message(hwnd, max_chars: int = 1_000_000) -> str:
    """WM_GETTEXT with a timeout so a hung target can't hang us."""
    res = ctypes.c_size_t()
    if not user32.SendMessageTimeoutW(hwnd, WM_GETTEXTLENGTH, 0, 0, SMTO_ABORTIFHUNG, 2000, ctypes.byref(res)):
        return ""
    n = min(int(res.value), max_chars)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.SendMessageTimeoutW(hwnd, WM_GETTEXT, n + 1, ctypes.cast(buf, ctypes.c_void_p).value or 0,
                               SMTO_ABORTIFHUNG, 2000, ctypes.byref(res))
    return buf.value


def make_lparam(lo: int, hi: int) -> int:
    return ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)
