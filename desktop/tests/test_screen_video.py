"""Unit tests: screen video logic (no Win32, no ffmpeg: fake grabber and sink).

Run:  desktop\\.venv\\Scripts\\python.exe -m unittest discover -s desktop\\tests -t .
"""

import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

import numpy as np

from desktop import screen_video as sv
from desktop.safety import CALLOUT_TITLE, Policy, StopState

VB = (-1920, 0, 3840, 1080)
POLICY = Policy({"allow_write_processes": ["*"], "deny_processes": ["bitwarden.exe", "keepass.exe"],
                 "deny_window_title_regex": ["(?i)password|contrase(n|ñ)a"]})


class FakeSink:
    def __init__(self):
        self.frames = []
        self.closed = False

    def write(self, data):
        self.frames.append(data)

    def size(self):
        return sum(len(f) for f in self.frames) // 100  # pretend compression

    def close(self, timeout=0):
        self.closed = True
        return {"returncode": 0, "stderr": ""}


def make_rec(rect=(0, 0, 64, 32), fps=50, duration=None, deny=None, limits=None, sink=None, **kw):
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    grab = lambda r: np.full((h, w, 4), 200, dtype=np.uint8)  # noqa: E731
    return sv.Recording(1, rect, fps, duration, Path(tempfile.gettempdir()) / "x.mp4", (w, h),
                        sink or FakeSink(), grab, deny_fn=deny, limits=limits, **kw)


class TestRegions(unittest.TestCase):
    def test_clip_and_even(self):
        self.assertEqual(sv.normalize_rect([-2000, -5, 101, 51], VB), (-1920, 0, 100, 50))
        self.assertEqual(sv.normalize_rect((0, 0, 1921, 1081), VB), (0, 0, 1920, 1080))

    def test_invalid(self):
        for bad in ([10, 10, 5, 50], [0, 0, 0, 0], [5000, 0, 6000, 100], [0, 0, 10, 10], "abc", [1, 2, 3]):
            with self.assertRaises(sv.RecordingError, msg=bad):
                sv.normalize_rect(bad, VB)

    def test_output_size_even(self):
        self.assertEqual(sv.output_size(1920, 1080, 1.0), (1920, 1080))
        self.assertEqual(sv.output_size(3840, 2160, 0.5), (1920, 1080))
        w, h = sv.output_size(1001, 703, 0.33)
        self.assertEqual((w % 2, h % 2), (0, 0))


class TestLimits(unittest.TestCase):
    L = dict(sv.DEFAULTS)

    def test_params(self):
        self.assertEqual(sv.check_params(None, None, None, self.L), (30.0, None, 1.0))
        self.assertEqual(sv.check_params(15, 10, 0.5, self.L), (15.0, 10.0, 0.5))
        for fps, dur, sc in ((0, None, 1), (61, None, 1), (30, 0, 1), (30, 901, 1), (30, None, 1.5), (30, None, 0)):
            with self.assertRaises(sv.RecordingError):
                sv.check_params(fps, dur, sc, self.L)

    def test_output_path(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            now = datetime(2026, 10, 29, 10, 0, 0)
            p = sv.resolve_output_path(None, d, now)
            self.assertEqual(p, d / "grabacion_20261029_100000.mp4")
            p.write_bytes(b"x")
            self.assertEqual(sv.resolve_output_path(None, d, now).name, "grabacion_20261029_100000_2.mp4")
            self.assertEqual(sv.resolve_output_path("..\\..\\evil<>name", d).parent, d)
            self.assertEqual(sv.resolve_output_path("demo", d).name, "demo.mp4")

    def test_dir_limit(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "a.mp4").write_bytes(b"0" * 2_000_000)
            sv.check_disk(d, {**self.L, "max_dir_mb": 10})
            with self.assertRaises(sv.RecordingError):
                sv.check_disk(d, {**self.L, "max_dir_mb": 1})
            with self.assertRaises(sv.RecordingError):
                sv.check_disk(d, {**self.L, "min_free_disk_mb": 10 ** 12})

    def test_ffmpeg_cmd(self):
        cmd = sv.ffmpeg_cmd("ffmpeg", (100, 50), 30, Path("o.mp4"), (50, 26))
        self.assertIn("libx264", cmd)
        self.assertIn("yuv420p", cmd)
        self.assertIn("scale=50:26:flags=bicubic", cmd)
        self.assertNotIn("-vf", sv.ffmpeg_cmd("ffmpeg", (100, 50), 30, Path("o.mp4"), (100, 50)))


class TestDenied(unittest.TestCase):
    WINS = [
        {"pid": 10, "process": "Bitwarden.exe", "title": "Bitwarden", "rect": (50, 50, 400, 400)},
        {"pid": 11, "process": "msedge.exe", "title": "Cambiar contraseña - Edge", "rect": (0, 0, 100, 100)},
        {"pid": 12, "process": "msedge.exe", "title": "Kuadernito", "rect": (0, 0, 800, 600)},
        {"pid": 13, "process": "keepass.exe", "title": "KeePass", "rect": (2000, 0, 2400, 400)},
        {"pid": 99, "process": "pythonw.exe", "title": CALLOUT_TITLE + " 3", "rect": (0, 0, 50, 50)},
    ]

    def test_only_denied_inside_area(self):
        hits = sv.denied_rects(self.WINS, POLICY, own_pid=99, area=(0, 0, 300, 300))
        self.assertEqual({h["process"] for h in hits}, {"Bitwarden.exe", "msedge.exe"})
        bw = next(h for h in hits if h["process"] == "Bitwarden.exe")
        self.assertEqual(bw["rect"], (50, 50, 300, 300))  # clipped to the area

    def test_covered_denied_window_not_masked(self):
        """Bitwarden maximized BEHIND an opaque browser: nothing of it shows, nothing is masked
        (2026-10-29: the first real video was all black because of this)."""
        wins = [{"pid": 1, "process": "msedge.exe", "title": "Kuadernito", "rect": (0, 0, 1920, 1032),
                 "occludes": True},
                {"pid": 2, "process": "Bitwarden.exe", "title": "Bitwarden", "rect": (0, 0, 1920, 1032),
                 "occludes": True}]
        self.assertEqual(sv.denied_rects(wins, POLICY, 99, (0, 0, 1280, 720)), [])

    def test_partly_covered_and_translucent_cover(self):
        bw = {"pid": 2, "process": "Bitwarden.exe", "title": "Bitwarden", "rect": (0, 0, 400, 400),
              "occludes": True}
        top = {"pid": 1, "process": "notepad.exe", "title": "x", "rect": (0, 0, 200, 400), "occludes": True}
        hits = sv.denied_rects([top, bw], POLICY, 99, (0, 0, 1000, 1000))
        self.assertEqual([h["rect"] for h in hits], [(200, 0, 400, 400)])
        # a translucent (layered) window on top is not a cover: still fully masked
        glass = {**top, "occludes": False}
        hits = sv.denied_rects([glass, bw], POLICY, 99, (0, 0, 1000, 1000))
        self.assertEqual([h["rect"] for h in hits], [(0, 0, 400, 400)])
        # a window BELOW the denied one never covers it
        hits = sv.denied_rects([bw, top], POLICY, 99, (0, 0, 1000, 1000))
        self.assertEqual([h["rect"] for h in hits], [(0, 0, 400, 400)])

    def test_subtract_area_preserved(self):
        parts = sv.subtract((0, 0, 100, 100), [(20, 20, 50, 50), (40, 40, 120, 60)])
        area = sum((r - l) * (b - t) for l, t, r, b in parts)
        self.assertEqual(area, 100 * 100 - 30 * 30 - 60 * 20 + 10 * 10)
        for i, a in enumerate(parts):
            for b in parts[i + 1:]:
                self.assertIsNone(sv.intersect(a, b))

    def test_mask_follows_window_during_grab(self):
        """The window moves while the grab runs: both positions are masked in that frame."""
        pos = iter([(0, 0, 10, 10), (20, 0, 30, 10)] * 1000)
        deny = lambda area: [{"process": "Bitwarden.exe", "title": "", "rect": next(pos)}]  # noqa: E731
        sink = FakeSink()
        rec = make_rec(rect=(0, 0, 64, 32), deny=deny, duration=0.1, sink=sink,
                       limits={"grab_threads": 1}).start()
        rec._done.wait(5)
        fr = np.frombuffer(sink.frames[0], dtype=np.uint8).reshape(32, 64, 4)
        self.assertTrue((fr[0:10, 0:10, :3] == 0).all() and (fr[0:10, 20:30, :3] == 0).all())
        self.assertTrue((fr[0:10, 12:18, :3] == 200).all())

    def test_apply_masks(self):
        f = np.full((100, 200, 4), 255, dtype=np.uint8)
        n = sv.apply_masks(f, (1000, 500), [(1010, 510, 1020, 530), (0, 0, 5, 5)])
        self.assertEqual(n, 200)
        self.assertTrue((f[10:30, 10:20, :3] == 0).all())
        self.assertEqual(int(f[..., :3].sum()), (100 * 200 - 200) * 3 * 255)

    def test_recording_masks_every_frame(self):
        deny = lambda area: sv.denied_rects(self.WINS, POLICY, 99, area)  # noqa: E731
        sink = FakeSink()
        rec = make_rec(rect=(0, 0, 64, 32), deny=deny, duration=0.1, sink=sink).start()
        rec._done.wait(5)
        self.assertEqual(rec.state, "finished")
        self.assertGreater(len(sink.frames), 0)
        for data in sink.frames:
            fr = np.frombuffer(data, dtype=np.uint8).reshape(32, 64, 4)
            self.assertTrue((fr[:, :, :3] == 0).all())  # the "contraseña" tab covers (0,0,100,100)
        self.assertIn("msedge.exe", rec.status()["masked_denied_windows"])

    def test_sprites_blend_inside_bounds(self):
        f = np.zeros((40, 40, 4), dtype=np.uint8)
        spr, _ = sv.pointer_sprite(1.0)
        self.assertTrue(sv.blend_sprite(f, spr, 30, 30))  # partly outside: clipped, no error
        self.assertFalse(sv.blend_sprite(f, spr, 100, 100))
        self.assertGreater(int(f.sum()), 0)
        g, (hx, hy) = sv.ghost_sprite(1.0)
        self.assertEqual(g.shape[2], 4)


class TestStopping(unittest.TestCase):
    def test_duration_stops_by_itself(self):
        rec = make_rec(fps=50, duration=0.2).start()
        self.assertTrue(rec._done.wait(5))
        self.assertEqual(rec.stop_reason, "duration")
        self.assertEqual(rec.frames, 10)
        self.assertEqual(rec.status()["duration_s"], 0.2)

    def test_manual_stop(self):
        sink = FakeSink()
        rec = make_rec(fps=50, sink=sink).start()
        time.sleep(0.15)
        st = rec.stop("record_stop")
        self.assertEqual(st["state"], "finished")
        self.assertEqual(st["stop_reason"], "record_stop")
        self.assertTrue(sink.closed)
        self.assertGreater(rec.frames, 2)

    def test_max_duration_limit(self):
        rec = make_rec(fps=50, limits={"max_duration_s": 0.1}).start()
        self.assertTrue(rec._done.wait(5))
        self.assertEqual(rec.stop_reason, "max_duration_s")

    def test_file_size_limit(self):
        rec = make_rec(fps=50, limits={"max_file_mb": 0.00001}).start()
        self.assertTrue(rec._done.wait(5))
        self.assertEqual(rec.stop_reason, "max_file_mb")

    def test_pause_cuts_time(self):
        rec = make_rec(fps=50).start()
        time.sleep(0.1)
        self.assertTrue(rec.pause())
        time.sleep(0.05)
        n = rec.frames
        time.sleep(0.2)
        self.assertEqual(rec.frames, n)  # nothing recorded while paused
        self.assertTrue(rec.resume())
        time.sleep(0.1)
        rec.stop()
        self.assertLess(rec.video_seconds, 0.3)

    def test_stop_while_paused(self):
        rec = make_rec(fps=50).start()
        time.sleep(0.05)
        rec.pause()
        st = rec.stop("x", wait=5)
        self.assertEqual(st["state"], "finished")

    def test_screen_locked_skips(self):
        rec = make_rec(fps=50, screen_ok_fn=lambda: False).start()
        time.sleep(0.15)
        st = rec.stop()
        self.assertEqual(rec.frames, 0)
        self.assertEqual(st["state"], "failed")
        self.assertGreater(rec.skipped_locked, 0)

    def test_encoder_crash_reported(self):
        class Broken(FakeSink):
            def write(self, data):
                raise BrokenPipeError("pipe closed")
        rec = make_rec(sink=Broken()).start()
        rec._done.wait(5)
        self.assertEqual(rec.state, "failed")
        self.assertIn("encoder", rec.error)

    def test_stop_button_stops_all(self):
        """The server wires StopState (button / hotkey / pause()) to ScreenVideo.stop_all."""
        mgr = sv.ScreenVideo({"recording": {"max_concurrent": 2}})
        w, h = 32, 32
        grab = lambda r: np.zeros((h, w, 4), dtype=np.uint8)  # noqa: E731
        recs = [mgr.start((0, 0, w, h), 30, None, Path("v.mp4"), (w, h), sink=FakeSink(), grab_fn=grab)
                for _ in range(2)]
        with self.assertRaises(sv.RecordingError):  # max_concurrent
            mgr.start((0, 0, w, h), 30, None, Path("v.mp4"), (w, h), sink=FakeSink(), grab_fn=grab)
        stop = StopState()
        done = threading.Event()
        stop.on_change(lambda paused, reason: paused and (mgr.stop_all(f"stop: {reason}"), done.set()))
        time.sleep(0.1)
        stop.stop("button")
        self.assertTrue(done.wait(5))
        self.assertEqual([r.state for r in recs], ["finished", "finished"])
        self.assertEqual({r.stop_reason for r in recs}, {"stop: button"})
        self.assertEqual(mgr.active(), [])


if __name__ == "__main__":
    unittest.main()
