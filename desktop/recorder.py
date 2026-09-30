"""Short "what happened during the action" recording, returned as ONE image.

While an action runs, a separate thread grabs the monitor Claude is working
on (~8 fps, its own mss/GDI instance so it never competes with the DXGI
objects of the automation thread) and keeps only frames that differ from
the previous one, for the last `window_s` seconds. After the action ends it
keeps recording `post_roll_s` more (to catch the app's reaction: a tooltip,
a popup, a flash), then builds a contact sheet:

  - cropped to the union of the areas that changed (+ margin), so text stays
    legible instead of shrinking the whole screen;
  - up to N frames, evenly spread, each labelled with its time relative to
    the end of the action (t-1.20 s ... t+0.35 s);
  - no image at all when nothing visible changed (that is information too).

The overlay (border, ghost cursor) is excluded from capture, so frames show
only the real screen.
"""

from __future__ import annotations

import threading
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont


class Recorder:
    def __init__(self, monitor_rect_fn, fps: float = 8.0, window_s: float = 2.0, post_roll_s: float = 0.4,
                 cursor_fn=None, change_threshold: int = 36):
        self.monitor_rect_fn = monitor_rect_fn  # () -> (l, t, r, b) of the monitor to record now
        self.cursor_fn = cursor_fn              # () -> (x, y) of Claude's ghost cursor, or None
        # per-channel delta that counts as a real change: high enough to ignore
        # translucent (Mica/Acrylic) backgrounds re-tinting on focus changes
        self.change_threshold = change_threshold
        self.interval = 1.0 / max(1.0, fps)
        self.window_s = window_s
        self.post_roll_s = post_roll_s
        self.frames: list[tuple[float, tuple, np.ndarray]] = []   # (t, rect, BGRA)
        self.cursors: dict[int, tuple] = {}                       # id(frame array) -> ghost (x, y)
        self._stop = threading.Event()
        self._t = None
        self.t_end = None

    def start(self) -> "Recorder":
        self._t = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._t.start()
        return self

    def _run(self):
        import mss
        prev_small = None
        prev_rect = None
        with mss.mss() as sct:
            while not self._stop.is_set():
                t0 = time.perf_counter()
                try:
                    rect = tuple(self.monitor_rect_fn())
                    l, t, r, b = rect
                    arr = np.asarray(sct.grab({"left": l, "top": t, "width": r - l, "height": b - t}))
                    small = arr[::8, ::8, :3].astype(np.int16)
                    cur = self.cursor_fn() if self.cursor_fn else None
                    last_cur = next(reversed(self.cursors.values()), None) if self.cursors else None
                    moved = cur is not None and cur != last_cur
                    if prev_small is None or rect != prev_rect or prev_small.shape != small.shape \
                            or np.abs(small - prev_small).max() > 10 or moved:
                        frame = arr.copy()
                        self.frames.append((t0, rect, frame))
                        if cur is not None:
                            self.cursors[id(frame)] = tuple(cur)
                        prev_small, prev_rect = small, rect
                    horizon = t0 - self.window_s - self.post_roll_s - 0.5
                    while len(self.frames) > 2 and self.frames[1][0] < horizon:
                        self.frames.pop(0)
                except Exception:  # noqa: BLE001 - recording is best effort
                    pass
                self._stop.wait(max(0.0, self.interval - (time.perf_counter() - t0)))

    def stop(self) -> None:
        """Call when the action finished: keeps post-roll, then stops."""
        self.t_end = time.perf_counter()
        time.sleep(self.post_roll_s)
        self._stop.set()
        if self._t:
            self._t.join(2)

    # ------------------------------------------------------------ contact sheet
    def contact_sheet(self, n: int = 6, tile_max_w: int = 640, margin: int = 60):
        """-> (PIL image or None, info dict)."""
        t_end = self.t_end or time.perf_counter()
        frames = [f for f in self.frames if f[0] >= t_end - self.window_s]
        if self.frames and (not frames or frames[0] is not self.frames[0]):
            # keep the last frame before the window as the "before" reference
            before = [f for f in self.frames if f[0] < t_end - self.window_s]
            if before:
                frames = [before[-1]] + frames
        info = {"frames_recorded": len(self.frames), "frames_in_window": len(frames)}
        if len(frames) < 2:
            info["changed"] = False
            info["note"] = "no visible change on the working monitor during the action"
            return None, info
        same_monitor = len({f[1] for f in frames}) == 1
        if same_monitor:
            ref = frames[0][2][..., :3].astype(np.int16)
            mask = np.zeros(ref.shape[:2], dtype=bool)
            for _, _, a in frames[1:]:
                mask |= np.abs(a[..., :3].astype(np.int16) - ref).max(axis=2) > self.change_threshold
            if not mask.any():
                info["changed"] = False
                info["note"] = "frames differ only by noise: no visible change"
                return None, info
            ys, xs = np.nonzero(mask)
            h, w = ref.shape[:2]
            l, t = max(0, xs.min() - margin), max(0, ys.min() - margin)
            r, b = min(w, xs.max() + 1 + margin), min(h, ys.max() + 1 + margin)
            # never crop smaller than 480x270: keep some context around a tiny change
            if r - l < 480:
                cx = (l + r) // 2
                l, r = max(0, cx - 240), min(w, cx + 240)
            if b - t < 270:
                cy = (t + b) // 2
                t, b = max(0, cy - 135), min(h, cy + 135)
            mon_rect = frames[0][1]
            info["crop_virtual_rect"] = (mon_rect[0] + int(l), mon_rect[1] + int(t),
                                         mon_rect[0] + int(r), mon_rect[1] + int(b))
            crop = (int(l), int(t), int(r), int(b))
        else:
            crop = None
            info["note"] = "action crossed monitors: each frame shows its whole monitor"
        # choose up to n frames: always first ("before") and last, the rest evenly spread
        if len(frames) > n:
            idx = sorted({0, len(frames) - 1, *[round(i * (len(frames) - 1) / (n - 1)) for i in range(n)]})
            frames = [frames[i] for i in idx][:n]
        tiles = []
        for t_f, rect, arr in frames:
            img = Image.frombuffer("RGBA", (arr.shape[1], arr.shape[0]), np.ascontiguousarray(arr).tobytes(),
                                   "raw", "BGRA", 0, 1).convert("RGB")
            ghost = self.cursors.get(id(arr))
            if ghost is not None:  # Claude's (overlay) cursor is excluded from capture: mark it
                gx, gy = ghost[0] - rect[0], ghost[1] - rect[1]
                d = ImageDraw.Draw(img)
                for rr, col in ((13, (255, 255, 255)), (11, (255, 140, 0))):
                    d.line((gx - rr, gy, gx + rr, gy), fill=col, width=5 if col[1] == 255 else 3)
                    d.line((gx, gy - rr, gx, gy + rr), fill=col, width=5 if col[1] == 255 else 3)
                d.ellipse((gx - 5, gy - 5, gx + 5, gy + 5), outline=(255, 140, 0), width=2)
            if crop:
                img = img.crop(crop)
            if img.width > tile_max_w:
                img = img.resize((tile_max_w, round(img.height * tile_max_w / img.width)), Image.Resampling.BILINEAR)
            tiles.append((t_f - t_end, img))
        cols = 2 if len(tiles) <= 4 else 3
        rows = (len(tiles) + cols - 1) // cols
        tw = max(t.width for _, t in tiles)
        th = max(t.height for _, t in tiles)
        label_h = 20
        sheet = Image.new("RGB", (cols * tw + (cols - 1) * 4, rows * (th + label_h) + (rows - 1) * 4), (40, 40, 40))
        dr = ImageDraw.Draw(sheet)
        try:
            font = ImageFont.truetype("segoeuib.ttf", 13)
        except OSError:
            font = ImageFont.load_default()
        for i, (dt, img) in enumerate(tiles):
            cx, cy = (i % cols) * (tw + 4), (i // cols) * (th + label_h + 4)
            sheet.paste(img, (cx, cy + label_h))
            tag = f"{i + 1}.  t{dt:+.2f} s" + ("  (antes)" if i == 0 else "  (final)" if i == len(tiles) - 1 else "")
            dr.rectangle((cx, cy, cx + tw, cy + label_h - 1), fill=(255, 140, 0) if dt > 0 else (70, 70, 70))
            dr.text((cx + 6, cy + 2), tag, font=font, fill=(255, 255, 255))
        info.update({"changed": True, "frames_shown": len(tiles),
                     "legend": "frames in time order; t = seconds relative to the end of the action "
                               "(t>0 = app reaction after it); orange cross = Claude's cursor"})
        return sheet, info
