"""supervise.py: restarts a child after any exit, with backoff, and stops cleanly."""

from __future__ import annotations

import subprocess
import sys
import threading

import supervise as S


def test_exit_text_shows_ntstatus_in_hex():
    assert S.exit_text(0) == "0"
    assert S.exit_text(3) == "3"
    assert S.exit_text(0xC0000005) == "0xC0000005"
    if sys.platform == "win32":
        assert S.exit_text(-1073741819) == "0xC0000005"


def test_backoff_doubles_caps_and_resets_after_a_healthy_run():
    assert S.next_delay(2, 1) == 4
    assert S.next_delay(40, 1) == S.MAX_DELAY
    assert S.next_delay(60, S.HEALTHY_RUN + 1) == S.MIN_DELAY


def test_child_is_restarted_after_it_crashes():
    stop = threading.Event()
    delays = []

    def fake_sleep(seconds):
        delays.append(seconds)
        if len(delays) >= 3:
            stop.set()

    cmd = [sys.executable, "-c", "import sys; sys.exit(3)"]
    starts = S.supervise(cmd, name="t", stop=stop, sleep=fake_sleep)
    assert starts == 3
    assert delays == [4, 8, 16]


def test_stop_terminates_a_running_child():
    stop = threading.Event()
    spawned = []

    def spawn(cmd, **kw):
        p = subprocess.Popen(cmd, **kw)
        spawned.append(p)
        threading.Timer(0.5, stop.set).start()
        return p

    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    assert S.supervise(cmd, name="t", stop=stop, spawn=spawn) == 1
    assert spawned[0].poll() is not None
