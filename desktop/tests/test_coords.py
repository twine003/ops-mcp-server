"""Unit tests: coordinates, DPI and multi-monitor math (no Win32 calls, no windows opened).

Run:  desktop\\.venv\\Scripts\\python.exe -m unittest discover -s desktop\\tests -t .
"""

import unittest

from desktop.monitors import (CaptureFrame, Monitor, border_width_physical, client_to_virtual, densify_path,
                              interpolate, monitor_at, monitor_for_rect, nearest_monitor, resolve_monitor,
                              to_absolute_input, virtual_bounds, virtual_to_client)
from desktop.overlay import border_strips


def mon(i, rect, dpi=96, primary=False, name=""):
    return Monitor(index=i, device=f"\\\\.\\DISPLAY{i}", name=name or f"M{i}", rect=rect,
                   work=(rect[0], rect[1], rect[2], rect[3] - 40), dpi=dpi, primary=primary)


# The real layout of this machine (3 x 1080p in a row, all 100 %)...
REAL = [mon(1, (0, 0, 1920, 1080), primary=True), mon(2, (1920, 0, 3840, 1080)), mon(3, (3840, 0, 5760, 1080))]
# ...and a nasty one: a 4K monitor at 150 % on the LEFT (negative x), a laptop
# at 125 % ABOVE the primary (negative y), mixed resolutions.
MIXED = [
    mon(1, (0, 0, 1920, 1080), 96, True),
    mon(2, (-3840, -500, 0, 1660), 144, name="LG 4K"),
    mon(3, (200, -1200, 1920, 0), 120, name="Laptop"),
]


class TestMonitorMath(unittest.TestCase):
    def test_virtual_bounds(self):
        self.assertEqual(virtual_bounds(REAL), (0, 0, 5760, 1080))
        self.assertEqual(virtual_bounds(MIXED), (-3840, -1200, 1920, 1660))

    def test_monitor_at_edges_and_negative(self):
        self.assertEqual(monitor_at(REAL, 1919, 500).index, 1)
        self.assertEqual(monitor_at(REAL, 1920, 500).index, 2)   # right edge is exclusive
        self.assertEqual(monitor_at(REAL, 5759, 1079).index, 3)
        self.assertIsNone(monitor_at(REAL, 5760, 0))
        self.assertEqual(monitor_at(MIXED, -1, -1).index, 2)
        self.assertEqual(monitor_at(MIXED, 500, -1).index, 3)
        self.assertIsNone(monitor_at(MIXED, 100, -700))           # gap between monitors

    def test_nearest_monitor_in_gap(self):
        self.assertEqual(nearest_monitor(MIXED, 100, -700).index, 3)  # 100 px from the laptop, farther from 4K

    def test_monitor_for_rect_largest_overlap(self):
        # A window straddling monitors 1 and 2 with 70 % on monitor 2
        self.assertEqual(monitor_for_rect(REAL, (1620, 100, 2620, 600)).index, 2)
        self.assertEqual(monitor_for_rect(MIXED, (-200, 100, 100, 300)).index, 2)

    def test_scale_and_dpi(self):
        self.assertAlmostEqual(MIXED[1].scale, 1.5)
        self.assertEqual(MIXED[2].to_dict()["scale_percent"], 125)

    def test_resolve_monitor(self):
        self.assertEqual(resolve_monitor(MIXED, "primary").index, 1)
        self.assertEqual(resolve_monitor(MIXED, 2).index, 2)
        self.assertEqual(resolve_monitor(MIXED, "3").index, 3)
        self.assertEqual(resolve_monitor(MIXED, "lg").index, 2)
        self.assertEqual(resolve_monitor(MIXED, "active", MIXED[2]).index, 3)
        self.assertEqual(resolve_monitor(MIXED, "active", None).index, 1)
        with self.assertRaises(ValueError):
            resolve_monitor(MIXED, 9)


class TestCaptureFrame(unittest.TestCase):
    def test_roundtrip_scaled_monitor_with_negative_origin(self):
        f = CaptureFrame(origin_x=-3840, origin_y=-500, width=3840, height=2160, scale=0.25)
        self.assertEqual(f.image_size, (960, 540))
        self.assertEqual(f.image_to_virtual(0, 0), (-3840, -500))
        self.assertEqual(f.image_to_virtual(480, 270), (-1920, 580))
        for vx, vy in [(-3840, -500), (-1, 1659), (-2000, 0)]:
            ix, iy = f.virtual_to_image(vx, vy)
            bx, by = f.image_to_virtual(ix, iy)
            self.assertLessEqual(abs(bx - vx), 4)  # 1 image px = 4 physical px at 0.25
            self.assertLessEqual(abs(by - vy), 4)

    def test_clamped_inside_capture(self):
        f = CaptureFrame(1920, 0, 1920, 1080, 0.5)
        self.assertEqual(f.image_to_virtual(960, 540), (3839, 1079))  # last pixel never spills to monitor 3
        self.assertEqual(f.image_to_virtual(-5, -5), (1920, 0))

    def test_all_three_monitors_capture(self):
        f = CaptureFrame(0, 0, 5760, 1080, 0.5)
        self.assertEqual(f.image_size, (2880, 540))
        x, y = f.image_to_virtual(2000, 100)
        self.assertEqual(monitor_at(REAL, x, y).index, 3)

    def test_client_conversions(self):
        origin = (2000, 120)
        self.assertEqual(virtual_to_client(2100, 220, origin), (100, 100))
        self.assertEqual(client_to_virtual(100, 100, origin), (2100, 220))
        origin_neg = (-3000, -400)
        self.assertEqual(virtual_to_client(-2990, -390, origin_neg), (10, 10))


class TestSendInputNormalization(unittest.TestCase):
    def _pixel_from_normalized(self, n, lo, size):
        # How Windows maps ABSOLUTE|VIRTUALDESK coordinates back to pixels.
        return lo + (n * size) // 65536

    def test_every_monitor_corner_lands_exactly(self):
        for mons in (REAL, MIXED):
            vb = virtual_bounds(mons)
            for m in mons:
                for x, y in [(m.rect[0], m.rect[1]), (m.rect[2] - 1, m.rect[3] - 1),
                             ((m.rect[0] + m.rect[2]) // 2, (m.rect[1] + m.rect[3]) // 2)]:
                    nx, ny = to_absolute_input(x, y, vb)
                    self.assertTrue(0 <= nx <= 65535 and 0 <= ny <= 65535)
                    self.assertEqual(self._pixel_from_normalized(nx, vb[0], vb[2] - vb[0]), x)
                    self.assertEqual(self._pixel_from_normalized(ny, vb[1], vb[3] - vb[1]), y)


class TestBorderAndPaths(unittest.TestCase):
    def test_border_width_same_physical_px_on_every_monitor(self):
        widths = {border_width_physical(16, m, "physical") for m in MIXED}
        self.assertEqual(widths, {16})
        self.assertEqual(border_width_physical(16, MIXED[1], "logical"), 24)   # 150 %
        self.assertEqual(border_width_physical(16, MIXED[2], "logical"), 20)   # 125 %

    def test_border_strips_geometry_and_gradient(self):
        m = MIXED[1]  # negative origin, 4K
        s = border_strips(m, (255, 140, 0), 16, 0.65)
        top, pos_top = s["top"]
        left, pos_left = s["left"]
        right, pos_right = s["right"]
        bottom, pos_bottom = s["bottom"]
        self.assertEqual(top.shape, (16, 3840, 4))
        self.assertEqual(pos_top, (-3840, -500))
        self.assertEqual(pos_left, (-3840, -484))
        self.assertEqual(pos_right, (-16, -484))
        self.assertEqual(pos_bottom, (-3840, 1644))
        self.assertEqual(left.shape, (2160 - 32, 16, 4))
        # opacity: ~65 % at the edge, fading to ~0 inward
        self.assertAlmostEqual(top[0, 1920, 3] / 255, 0.65, delta=0.01)
        self.assertLess(top[15, 1920, 3], 10)
        self.assertGreater(left[500, 0, 3], left[500, 15, 3])
        self.assertGreater(right[500, 15, 3], right[500, 0, 3])
        # corners carry the side gradient too (no notch where strips meet)
        self.assertAlmostEqual(top[15, 0, 3] / 255, 0.65, delta=0.01)

    def test_interpolate_endpoints_and_monotonic(self):
        p0, p1 = (1800, 500), (2100, 520)          # crossing from monitor 1 to 2
        self.assertEqual(interpolate(p0, p1, 0), p0)
        self.assertEqual(interpolate(p0, p1, 1), p1)
        xs = [interpolate(p0, p1, t / 20)[0] for t in range(21)]
        self.assertEqual(xs, sorted(xs))

    def test_densify(self):
        pts = densify_path([(0, 0), (30, 0), (30, 40)], 6)
        self.assertEqual(pts[0], (0, 0))
        self.assertEqual(pts[-1], (30, 40))
        for a, b in zip(pts, pts[1:]):
            self.assertLessEqual(((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5, 6.01)


if __name__ == "__main__":
    unittest.main()
