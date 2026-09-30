"""Unit tests: locked-screen detection and the bound on dxcam's endless recovery loop (no real capture)."""

import time
import unittest
from unittest import mock

from desktop import capture as cap


class FakeWait:
    def __init__(self):
        self.resets = 0

    def next_delay_seconds(self):
        return 5.0

    def reset(self):
        self.resets += 1


class FakeCam:
    def __init__(self):
        self._display_recovery = type("R", (), {})()
        self._display_recovery._wait = FakeWait()


class TestScreenLock(unittest.TestCase):
    def test_locked_or_secure_desktop_raises(self):
        for desk in (None, "Winlogon", "Screen-saver"):
            with mock.patch.object(cap.w, "input_desktop", return_value=desk):
                with self.assertRaises(cap.ScreenUnavailable):
                    cap.check_screen_available()

    def test_normal_desktop_passes(self):
        with mock.patch.object(cap.w, "input_desktop", return_value="Default"):
            cap.check_screen_available()

    def test_recovery_loop_gives_up(self):
        cam = FakeCam()
        cap._bound_dx_recovery(cam)
        cap._bound_dx_recovery(cam)  # idempotent
        wait = cam._display_recovery._wait
        self.assertEqual(wait.next_delay_seconds(), 1.0)  # capped from 5 s
        with mock.patch.object(cap, "DX_RECOVERY_GIVE_UP_S", 0.05):
            time.sleep(0.1)
            with self.assertRaises(cap.ScreenUnavailable):
                wait.next_delay_seconds()

    def test_success_resets_timer(self):
        cam = FakeCam()
        cap._bound_dx_recovery(cam)
        wait = cam._display_recovery._wait
        with mock.patch.object(cap, "DX_RECOVERY_GIVE_UP_S", 0.05):
            wait.next_delay_seconds()
            time.sleep(0.1)
            wait.reset()
            self.assertEqual(wait.next_delay_seconds(), 1.0)  # new recovery run, not a give-up
        self.assertEqual(wait.resets, 1)


if __name__ == "__main__":
    unittest.main()
