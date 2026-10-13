"""Keep a child process alive: restart it whenever it exits, with backoff.

Why this exists: a Windows scheduled task's "restart on failure" only covers a
task that fails to START. When the process it launched dies later (an access
violation inside a native capture/UIA call, or the self-watchdog's exit(3)),
Task Scheduler just marks the task as finished and nothing brings it back
until the next logon. This wrapper is what the scheduled task runs instead.

    pythonw supervise.py --name desktop-mcp --log <file> -- <program> [args...]

- Restarts the child after ANY exit, waiting 2s, 4s, 8s... up to 60s; the
  delay resets once a run has lasted 5 minutes.
- Logs every start and exit, with the exit code in hex (0xC0000005 = access
  violation), to --log.
- On Windows the child is put in a Job Object that kills it when the
  supervisor goes away (Stop-ScheduledTask, logoff), so an orphaned child can
  never keep holding the port.
- Stdlib only: it must run before (and without) any venv package.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

MIN_DELAY = 2.0
MAX_DELAY = 60.0
HEALTHY_RUN = 300.0

log = logging.getLogger("supervise")


def exit_text(code: int) -> str:
    """0 -> '0'; Windows NTSTATUS codes (negative as signed) -> '0xC0000005'."""
    if code is None:
        return "?"
    if code < 0 and sys.platform == "win32":
        return f"0x{code & 0xFFFFFFFF:08X}"
    if code > 0xFFFF:
        return f"0x{code:08X}"
    return str(code)


def next_delay(previous: float, ran_for: float) -> float:
    if ran_for >= HEALTHY_RUN:
        return MIN_DELAY
    return min(max(previous * 2, MIN_DELAY), MAX_DELAY)


class _KillOnCloseJob:
    """Windows Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (no-op elsewhere)."""

    def __init__(self):
        self.handle = None
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.OpenProcess.restype = wintypes.HANDLE

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount",
                        "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self._k32 = k32
        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            return
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(handle)
            return
        self.handle = handle

    def add(self, pid: int) -> bool:
        if not self.handle:
            return False
        proc = self._k32.OpenProcess(0x0001 | 0x0100, False, pid)   # PROCESS_TERMINATE | SET_QUOTA
        if not proc:
            return False
        try:
            return bool(self._k32.AssignProcessToJobObject(self.handle, proc))
        finally:
            self._k32.CloseHandle(proc)


def supervise(cmd: list[str], *, name: str, stop: threading.Event, cwd: str | None = None,
              spawn=subprocess.Popen, sleep=None) -> int:
    """Run cmd until `stop` is set. Returns the number of starts (for tests)."""
    job = _KillOnCloseJob()
    wait = sleep or (lambda seconds: stop.wait(seconds))
    delay = MIN_DELAY
    starts = 0
    while not stop.is_set():
        started = time.monotonic()
        try:
            # No std handles on purpose: under pythonw the child must see sys.stdout/stderr
            # as None (as when the task launched it directly) so it redirects them to its
            # log. Passing any of stdin/stdout/stderr makes Windows hand it the others as
            # invalid handles; the first print (FastMCP's banner) then fails and the
            # child exits with 120.
            child = spawn(cmd, cwd=cwd)
        except OSError as e:
            log.error("%s: cannot start %s: %s", name, cmd[0], e)
            delay = next_delay(delay, 0)
            wait(delay)
            continue
        starts += 1
        job.add(child.pid)
        log.info("%s: started pid %s (start #%d)", name, child.pid, starts)
        while child.poll() is None and not stop.is_set():
            stop.wait(1.0)
        if stop.is_set():
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(10)
                except subprocess.TimeoutExpired:
                    child.kill()
            log.info("%s: supervisor stopping", name)
            break
        ran_for = time.monotonic() - started
        delay = next_delay(delay, ran_for)
        log.warning("%s: pid %s exited with %s after %.0fs; restarting in %.0fs",
                    name, child.pid, exit_text(child.returncode), ran_for, delay)
        wait(delay)
    return starts


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in argv:
        print("usage: supervise.py --name NAME [--log FILE] -- program [args...]", file=sys.stderr)
        return 2
    split = argv.index("--")
    ap = argparse.ArgumentParser(prog="supervise")
    ap.add_argument("--name", required=True)
    ap.add_argument("--log", type=Path, default=None)
    ap.add_argument("--cwd", default=None)
    args = ap.parse_args(argv[:split])
    cmd = argv[split + 1:]
    if not cmd:
        print("nothing to run after --", file=sys.stderr)
        return 2

    handlers: list[logging.Handler] = []
    if args.log:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(args.log, maxBytes=1_000_000, backupCount=2, encoding="utf-8"))
    if sys.stderr is not None:                       # None under pythonw
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            signal.signal(sig, lambda *_: stop.set())
    log.info("%s: supervising %s (supervisor pid %s)", args.name, " ".join(cmd), os.getpid())
    supervise(cmd, name=args.name, stop=stop, cwd=args.cwd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
