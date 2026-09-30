"""Capture latency benchmark (numbers reported in README.txt).

    desktop\\.venv\\Scripts\\python.exe desktop\\bench_capture.py [--window "Notepad"]

Measures pure capture time (pixels in memory) separately from encoding
(JPEG/PNG), per backend: full virtual desktop, one monitor, one window.
"""

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desktop import winapi as w  # noqa: E402

w.set_dpi_awareness()

from desktop import windows as wins  # noqa: E402
from desktop.capture import Capturer  # noqa: E402
from desktop.monitors import enumerate_monitors, virtual_bounds  # noqa: E402


def bench(label, fn, n=20):
    fn()  # warm-up (device creation)
    times = []
    for _ in range(n):
        t = time.perf_counter()
        out = fn()
        times.append((time.perf_counter() - t) * 1000)
    print(f"  {label:<44} median {statistics.median(times):6.1f} ms   p90 {sorted(times)[int(n * 0.9) - 1]:6.1f} ms")
    return out


def main():
    import os
    title = None
    if "--window" in sys.argv:
        title = sys.argv[sys.argv.index("--window") + 1]
    mons = enumerate_monitors()
    vb = virtual_bounds(mons)
    m1 = next(m for m in mons if m.primary)
    for backend in ("dxgi", "gdi"):
        c = Capturer({"capture": {"backend": backend}})
        if backend == "gdi":
            c._dx_ok = False
        print(f"[{backend}]  monitors={len(mons)}  virtual={vb}")
        arr = bench(f"monitor {m1.index} ({m1.width}x{m1.height})", lambda: c.grab_rect(m1.rect, mons))
        bench(f"virtual desktop ({vb[2]-vb[0]}x{vb[3]-vb[1]})", lambda: c.grab_rect(vb, mons))
        c.reset()
    c = Capturer({"capture": {"backend": "auto"}})
    if title:
        hwnd = wins.find_window(title, mons, os.getpid())
        a, r = c.grab_window_printwindow(hwnd)
        print(f"[window '{w.window_text(hwnd)}' {r[2]-r[0]}x{r[3]-r[1]}]")
        bench("PrintWindow(PW_RENDERFULLCONTENT)", lambda: c.grab_window_printwindow(hwnd))
        bench("screen crop of window rect (dxgi)", lambda: c.grab_rect(r, mons))
    print("[encoding one 1080p monitor]")
    img = c.to_image(c.grab_rect(m1.rect, mons))
    bench("to PIL image", lambda: c.to_image(arr), n=10)
    bench("JPEG q70 @ scale 1.0", lambda: c.encode(img, "jpeg", 70), n=10)
    half = c.scale_image(img, 0.5)
    bench("resize to 0.5", lambda: c.scale_image(img, 0.5), n=10)
    bench("JPEG q70 @ scale 0.5", lambda: c.encode(half, "jpeg", 70), n=10)
    bench("PNG (compress 1) @ scale 1.0", lambda: c.encode(img, "png", 0), n=5)
    c.reset()


if __name__ == "__main__":
    main()
