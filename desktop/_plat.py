"""Platform switch for the desktop package.

Windows uses the Win32 modules of this folder (winapi, windows, inputs, uia,
LayeredWindow in overlay). Linux (X11) uses desktop/x11/, which exposes the
same functions with the same names, so desktop_server.py, overlay.py,
callout.py and inbox.py run unchanged on both.
"""

from __future__ import annotations

import sys

IS_LINUX = sys.platform.startswith("linux")
IS_WINDOWS = sys.platform == "win32"

if IS_LINUX:
    from .x11 import winapi as w  # noqa: F401
else:
    from . import winapi as w  # noqa: F401
