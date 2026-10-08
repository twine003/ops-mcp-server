"""Device token storage on Windows: DPAPI (CurrentUser scope) via ctypes.

The token file is only readable by the same Windows user on the same machine;
copying it elsewhere yields nothing. On other platforms (tests, CI) the token
comes from the ALEJANDRO_DEVICE_TOKEN environment variable instead.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_FALLBACK = "ALEJANDRO_DEVICE_TOKEN"


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    entropy_raw = b"alejandro-connector/v1"
    ebuf = ctypes.create_string_buffer(entropy_raw, len(entropy_raw))
    entropy = DATA_BLOB(len(entropy_raw), ctypes.cast(ebuf, ctypes.POINTER(ctypes.c_char)))
    CRYPTPROTECT_UI_FORBIDDEN = 0x01
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, ctypes.byref(entropy), None, None,
            CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise OSError(f"DPAPI call failed: {ctypes.FormatError(ctypes.get_last_error())}")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def save_token(path: Path, token: str) -> None:
    if sys.platform != "win32":
        raise RuntimeError(f"DPAPI is Windows-only; set {ENV_FALLBACK} instead")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_dpapi(token.encode("utf-8"), protect=True))


def load_token(path: Path) -> str:
    env = os.environ.get(ENV_FALLBACK, "")
    if env:
        return env
    if sys.platform != "win32" or not path.exists():
        return ""
    return _dpapi(path.read_bytes(), protect=False).decode("utf-8")
