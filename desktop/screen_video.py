"""Screen VIDEO recording (MP4 / H.264) of a monitor, a window's rect or a region.

Why our own capture loop + ffmpeg as a pure encoder (and not ffmpeg gdigrab/ddagrab):
  - deny list: windows of denied processes/titles (Bitwarden, KeePass, "password"...)
    must be blacked out FRAME BY FRAME, even inside a region; ffmpeg's grabbers
    cannot mask anything, here every frame is a numpy array we can paint on;
  - overlay: mss (GDI BitBlt) honours WDA_EXCLUDEFROMCAPTURE, so the border, the
    Stop button and the comment bubbles never reach the video (same as screenshots);
  - stability: mss runs in its own thread with its own GDI handles, it never touches
    the DXGI duplication objects of the automation thread (dxcam hangs while the PC is
    locked - capture.py) and never blocks the other tools;
  - ffmpeg comes from imageio-ffmpeg (a static build inside the venv, libx264).

Each recording = one capture thread that grabs at a fixed rate, paints masks /
pointer / optional ghost cursor and writes raw BGRA frames to ffmpeg's stdin.
If a grab is late the previous frame is repeated, so the video timeline matches
the wall clock (minus pauses). Stops: record_stop, duration, size / disk limits,
the "Detener Claude" button / stop hotkey / pause() (StopState listener).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from .safety import expand_path

DEFAULTS = {
    "dir": r"%LOCALAPPDATA%\desktop-mcp\grabaciones",
    "default_fps": 30,
    "max_fps": 60,
    "max_duration_s": 900,
    "max_file_mb": 2048,
    "max_dir_mb": 20480,
    "min_free_disk_mb": 2048,
    "max_concurrent": 2,
    "crf": 23,
    "preset": "veryfast",
    "deny_mode": "mask",
    "grab_threads": 3,
}
MIN_SIDE = 16


class RecordingError(ValueError):
    """Bad parameters or a limit that prevents starting."""


# ---------------------------------------------------------------- pure helpers (unit tested)
def intersect(a: tuple, b: tuple) -> tuple | None:
    l, t, r, b_ = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    return (l, t, r, b_) if l < r and t < b_ else None


def normalize_rect(rect, vbounds: tuple) -> tuple:
    """Clip [l,t,r,b] to the virtual desktop and make width/height even (H.264 4:2:0)."""
    try:
        l, t, r, b = (int(v) for v in rect)
    except (TypeError, ValueError):
        raise RecordingError("region must be [left, top, right, bottom] in virtual-desktop px") from None
    if r <= l or b <= t:
        raise RecordingError("region must be [left, top, right, bottom] with right>left and bottom>top")
    clipped = intersect((l, t, r, b), tuple(vbounds))
    if clipped is None:
        raise RecordingError(f"region {rect} is outside the screens {tuple(vbounds)}")
    l, t, r, b = clipped
    r -= (r - l) % 2
    b -= (b - t) % 2
    if r - l < MIN_SIDE or b - t < MIN_SIDE:
        raise RecordingError(f"region too small ({r - l}x{b - t}): at least {MIN_SIDE}x{MIN_SIDE} px")
    return (l, t, r, b)


def check_params(fps, duration, scale, limits: dict) -> tuple[float, float | None, float]:
    fps = float(fps if fps is not None else limits["default_fps"])
    if not 1 <= fps <= float(limits["max_fps"]):
        raise RecordingError(f"fps must be between 1 and {limits['max_fps']}")
    max_d = float(limits["max_duration_s"])
    if duration is not None:
        duration = float(duration)
        if duration <= 0:
            raise RecordingError("duration must be > 0 seconds (or omit it and call record_stop)")
        if duration > max_d:
            raise RecordingError(f"duration {duration:.0f}s exceeds the limit recording.max_duration_s={max_d:.0f}s")
    scale = float(scale if scale is not None else 1.0)
    if not 0.1 <= scale <= 1.0:
        raise RecordingError("scale must be between 0.1 and 1.0")
    return fps, duration, scale


def output_size(width: int, height: int, scale: float) -> tuple[int, int]:
    if scale >= 0.999:
        return width, height
    return max(MIN_SIDE, int(width * scale) // 2 * 2), max(MIN_SIDE, int(height * scale) // 2 * 2)


_SAFE_NAME = re.compile(r"[^\w\-. ]+")


def resolve_output_path(path: str | None, out_dir: Path, now: datetime | None = None) -> Path:
    """None -> <dir>/grabacion_<timestamp>.mp4; a bare name -> inside <dir>; an absolute path is
    accepted as is. Always .mp4, never overwrites an existing file."""
    now = now or datetime.now()
    if not path:
        p = out_dir / f"grabacion_{now:%Y%m%d_%H%M%S}.mp4"
    else:
        p = Path(expand_path(str(path)))
        if not p.is_absolute():
            name = _SAFE_NAME.sub("_", p.name).strip(" .") or f"grabacion_{now:%Y%m%d_%H%M%S}"
            p = out_dir / name
        if p.suffix.lower() != ".mp4":
            p = p.with_name(p.name + ".mp4")
    if p.exists():
        stem, i = p.stem, 2
        while p.exists():
            p = p.with_name(f"{stem}_{i}.mp4")
            i += 1
    return p


def dir_size_mb(d: Path) -> float:
    if not d.exists():
        return 0.0
    return sum(f.stat().st_size for f in d.glob("*.mp4") if f.is_file()) / 1e6


def check_disk(out_dir: Path, limits: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    free_mb = shutil.disk_usage(out_dir).free / 1e6
    if free_mb < float(limits["min_free_disk_mb"]):
        raise RecordingError(f"only {free_mb:.0f} MB free on the disk of {out_dir} "
                             f"(recording.min_free_disk_mb={limits['min_free_disk_mb']})")
    used = dir_size_mb(out_dir)
    if used >= float(limits["max_dir_mb"]):
        raise RecordingError(f"the recordings folder {out_dir} already holds {used:.0f} MB "
                             f"(recording.max_dir_mb={limits['max_dir_mb']}): move or delete old videos")


def subtract(rect: tuple, holes) -> list[tuple]:
    """rect minus every rect in holes -> list of disjoint rects."""
    parts = [tuple(rect)]
    for h in holes:
        nxt = []
        for p in parts:
            i = intersect(p, h)
            if i is None:
                nxt.append(p)
                continue
            l, t, r, b = p
            if t < i[1]:
                nxt.append((l, t, r, i[1]))
            if i[3] < b:
                nxt.append((l, i[3], r, b))
            if l < i[0]:
                nxt.append((l, i[1], i[0], i[3]))
            if i[2] < r:
                nxt.append((i[2], i[1], r, i[3]))
        parts = nxt
    return parts


def denied_rects(windows, policy, own_pid: int, area: tuple) -> list[dict]:
    """windows: {pid, process, title, rect, occludes} in z-order, TOP FIRST (EnumWindows order).
    -> the visible parts of denied windows inside `area`. A denied window covered by opaque
    windows ('occludes': not layered / transparent) is masked only where it can show; translucent
    windows above it never count as cover."""
    out, cover = [], []
    for win in windows:
        if win.get("pid") == own_pid:
            continue  # Claude's overlay / bubbles: excluded from capture, not "denied" content
        rect = tuple(win["rect"])
        if policy.is_denied(win.get("process", ""), win.get("title", "")):
            hit = intersect(rect, area)
            for part in subtract(hit, cover) if hit else []:
                out.append({"process": win.get("process", ""), "title": win.get("title", ""), "rect": part})
        if win.get("occludes", False):
            cover.append(rect)
    return out


def apply_masks(frame: np.ndarray, origin: tuple, rects) -> int:
    """Black out virtual-desktop rects on a frame whose top-left is `origin`. -> pixels masked."""
    h, wd = frame.shape[:2]
    n = 0
    for r in rects:
        l, t = max(0, r[0] - origin[0]), max(0, r[1] - origin[1])
        rr, bb = min(wd, r[2] - origin[0]), min(h, r[3] - origin[1])
        if l < rr and t < bb:
            frame[t:bb, l:rr, :3] = 0
            n += (rr - l) * (bb - t)
    return n


def blend_sprite(frame: np.ndarray, sprite: np.ndarray, x: int, y: int) -> bool:
    """Alpha-blend an RGBA->BGRA sprite (h, w, 4 with BGR order + alpha) at frame pixel (x, y)."""
    h, wd = frame.shape[:2]
    sh, sw = sprite.shape[:2]
    l, t = max(0, x), max(0, y)
    r, b = min(wd, x + sw), min(h, y + sh)
    if l >= r or t >= b:
        return False
    sp = sprite[t - y:b - y, l - x:r - x]
    a = sp[..., 3:4].astype(np.uint16)
    dst = frame[t:b, l:r, :3]
    dst[:] = ((sp[..., :3].astype(np.uint16) * a + dst.astype(np.uint16) * (255 - a)) // 255).astype(np.uint8)
    return True


def _to_bgra(img) -> np.ndarray:
    arr = np.asarray(img.convert("RGBA")).copy()
    arr[..., [0, 2]] = arr[..., [2, 0]]
    return arr


def pointer_sprite(scale: float = 1.0) -> tuple[np.ndarray, tuple[int, int]]:
    """Classic white arrow with black outline. -> (BGRA sprite, hotspot)."""
    from PIL import Image, ImageDraw
    s = max(1.0, scale)
    pts = [(0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12)]
    pts = [(1 + x * s, 1 + y * s) for x, y in pts]
    img = Image.new("RGBA", (int(14 * s) + 3, int(22 * s) + 3), (0, 0, 0, 0))
    ImageDraw.Draw(img).polygon(pts, fill=(255, 255, 255, 255), outline=(0, 0, 0, 255), width=max(1, round(s)))
    return _to_bgra(img), (1, 1)


def ghost_sprite(scale: float = 1.0) -> tuple[np.ndarray, tuple[int, int]]:
    """Orange cross + 'Claude' tag, like the marker drawn on screenshots. -> (sprite, hotspot)."""
    from PIL import Image, ImageDraw
    r = max(9, round(13 * scale))
    img = Image.new("RGBA", (2 * r + 60, 2 * r + 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx, cy = r + 2, r + 2
    for col, wd in (((255, 255, 255, 255), 5), ((255, 120, 0, 255), 3)):
        d.line((cx - r, cy, cx + r, cy), fill=col, width=wd)
        d.line((cx, cy - r, cx, cy + r), fill=col, width=wd)
    d.ellipse((cx - r // 2, cy - r // 2, cx + r // 2, cy + r // 2), outline=(255, 120, 0, 255), width=2)
    tx, ty = cx + r + 3, cy + 2
    d.rectangle((tx - 2, ty - 1, tx + 44, ty + 13), fill=(255, 120, 0, 255))
    d.text((tx + 1, ty), "Claude", fill=(255, 255, 255, 255))
    return _to_bgra(img), (cx, cy)


def ffmpeg_cmd(exe: str, size: tuple[int, int], fps: float, out: Path, out_size: tuple[int, int],
               crf: int = 23, preset: str = "veryfast") -> list[str]:
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{size[0]}x{size[1]}", "-r", f"{fps:g}", "-i", "-"]
    if tuple(out_size) != tuple(size):
        cmd += ["-vf", f"scale={out_size[0]}:{out_size[1]}:flags=bicubic"]
    cmd += ["-c:v", "libx264", "-preset", str(preset), "-crf", str(int(crf)), "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(out)]
    return cmd


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        exe = shutil.which("ffmpeg")
        if exe:
            return exe
    raise RecordingError("ffmpeg not found: install it in the venv (pip install imageio-ffmpeg)")


# ---------------------------------------------------------------- encoder sink
class FfmpegSink:
    def __init__(self, cmd: list[str], out: Path):
        self.out = out
        out.parent.mkdir(parents=True, exist_ok=True)
        self._err_path = out.with_name(out.name + ".ffmpeg.log")
        self._err = open(self._err_path, "wb")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self._err,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def write(self, data) -> None:
        self.proc.stdin.write(data)

    def size(self) -> int:
        try:
            return self.out.stat().st_size
        except OSError:
            return 0

    def close(self, timeout: float = 120) -> dict:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            rc = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            rc = self.proc.wait(10)
        self._err.close()
        err = ""
        try:
            err = self._err_path.read_text(encoding="utf-8", errors="replace").strip()[-800:]
            self._err_path.unlink()
        except OSError:
            pass
        return {"returncode": rc, "stderr": err}


# ---------------------------------------------------------------- one recording
class Recording:
    """grab_fn(rect) -> BGRA ndarray (or grab_factory() -> object with grab_fn/close, created inside each
    grabber thread); sink has write/size/close; deny_fn(rect) -> [{rect,...}];
    pointer_fn() -> (x, y) | None (real mouse); ghost_fn() -> (x, y) | None; screen_ok_fn() -> bool.

    Threads: `grab_threads` grabbers keep the newest screen frame in a slot (GDI BitBlt has a
    fixed latency on some PCs - 160 ms on the Intel UHD 620 laptop - so several in parallel give
    more distinct frames); the writer ticks at a fixed fps, copies the newest frame, paints masks,
    pointer and ghost (so the pointer moves smoothly even when the screen frame repeats) and feeds
    the encoder. Video time = ticks written / fps = wall time minus pauses and locked-screen time."""

    def __init__(self, rid: int, rect: tuple, fps: float, duration: float | None, path: Path, out_size,
                 sink, grab_fn=None, deny_fn=None, pointer_fn=None, ghost_fn=None, screen_ok_fn=None,
                 limits: dict | None = None, describe: dict | None = None, sprite_scale: float = 1.0,
                 on_end=None, grab_factory=None):
        self.id, self.rect, self.fps, self.duration, self.path = rid, tuple(rect), float(fps), duration, path
        self.size = (self.rect[2] - self.rect[0], self.rect[3] - self.rect[1])
        self.out_size = tuple(out_size)
        self.sink, self.grab_fn, self.grab_factory = sink, grab_fn, grab_factory
        self.deny_fn, self.pointer_fn, self.ghost_fn = deny_fn, pointer_fn, ghost_fn
        self.screen_ok_fn = screen_ok_fn
        self.limits = {**DEFAULTS, **(limits or {})}
        self.describe = describe or {}
        self.on_end = on_end
        self.pointer = pointer_sprite(sprite_scale) if pointer_fn else None
        self.ghost = ghost_sprite(sprite_scale) if ghost_fn else None
        self.state = "recording"
        self.stop_reason = ""
        self.error = ""
        self.frames = 0            # frames written (= video length * fps)
        self.grabbed = 0           # distinct screen frames captured
        self.used = 0              # distinct screen frames that reached the video
        self.skipped_locked = 0    # ticks skipped while Windows hid the screen
        self.masked_windows: dict[str, str] = {}
        self.started_at = datetime.now()
        self.ended_at = None
        self.result: dict | None = None
        self._latest: tuple | None = None   # (grab start, seq, frame, denied-window masks)
        self._slot = threading.Lock()
        self._grab_errors = 0
        self._screen_ok = True
        self._stop = threading.Event()
        self._resume = threading.Event()
        self._resume.set()
        self._done = threading.Event()
        self._t = threading.Thread(target=self._run, name=f"video-{rid}", daemon=True)
        self._grabbers = [threading.Thread(target=self._grab_loop, args=(i,), name=f"video-{rid}-grab{i}",
                                           daemon=True)
                          for i in range(max(1, int(self.limits["grab_threads"])))]

    # ------------------------------------------------------------ control
    def start(self) -> "Recording":
        for g in self._grabbers:
            g.start()
        self._t.start()
        return self

    def pause(self) -> bool:
        if self.state != "recording":
            return False
        self.state = "paused"
        self._resume.clear()
        return True

    def resume(self) -> bool:
        if self.state != "paused":
            return False
        self.state = "recording"
        self._resume.set()
        return True

    def stop(self, reason: str = "record_stop", wait: float = 120) -> dict:
        if not self._stop.is_set():
            self.stop_reason = self.stop_reason or reason
            self._stop.set()
            self._resume.set()
        self._done.wait(wait)
        return self.status()

    @property
    def video_seconds(self) -> float:
        return self.frames / self.fps

    def status(self) -> dict:
        st = {"id": self.id, "state": self.state, "path": str(self.path), "rect": self.rect,
              "capture_size": self.size, "resolution": self.out_size, "fps": self.fps,
              "duration_limit_s": self.duration, "video_seconds": round(self.video_seconds, 2),
              "frames": self.frames, "distinct_screen_frames": self.used,
              "effective_screen_fps": round(self.used / self.video_seconds, 1) if self.frames else 0.0,
              "skipped_screen_locked": self.skipped_locked,
              "bytes": self.sink.size() if self.sink else 0,
              "started_at": self.started_at.isoformat(timespec="seconds"), **self.describe}
        if self.masked_windows:
            st["masked_denied_windows"] = sorted(set(self.masked_windows.values()))
        if self.stop_reason:
            st["stop_reason"] = self.stop_reason
        if self.error:
            st["error"] = self.error
        if self.result:
            st.update(self.result)
        return st

    # ------------------------------------------------------------ grabber threads
    def _grab_loop(self, idx: int):
        grab, ctx = self.grab_fn, None
        try:
            if self.grab_factory is not None:  # e.g. GDI handles must belong to this thread
                ctx = self.grab_factory()
                grab = ctx.grab_fn
            # stagger the threads so their (fixed-latency) grabs interleave
            self._stop.wait(idx * 1.0 / (self.fps * len(self._grabbers)))
            seq = 0
            while not self._stop.is_set():
                if not self._resume.is_set() or not self._screen_ok:
                    self._stop.wait(0.05)
                    continue
                t0 = time.monotonic()
                try:
                    # denied windows at grab start AND end: one that moves or is covered while the
                    # (slow) grab runs stays masked where it was at either moment
                    masks = self.deny_fn(self.rect) if self.deny_fn else []
                    frame = np.asarray(grab(self.rect))
                    if self.deny_fn:
                        masks = masks + self.deny_fn(self.rect)
                    self._grab_errors = 0
                except Exception as e:  # noqa: BLE001
                    self._grab_errors += 1
                    if self._grab_errors > 30:
                        self.error = f"screen capture keeps failing: {type(e).__name__}: {e}"
                        self._stop.set()
                    self._stop.wait(0.1)
                    continue
                if frame.shape[1] != self.size[0] or frame.shape[0] != self.size[1]:
                    self.error = f"grab returned {frame.shape[1]}x{frame.shape[0]}, expected {self.size}"
                    self._stop.set()
                    break
                seq += 1
                with self._slot:
                    if self._latest is None or t0 > self._latest[0]:
                        self._latest = (t0, idx * 10**9 + seq, frame, masks)
                        self.grabbed += 1
                # never faster than the output fps per thread
                self._stop.wait(max(0.0, 1.0 / self.fps - (time.monotonic() - t0)))
        finally:
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------ writer thread
    def _limit_hit(self) -> str:
        if self.duration is not None and self.video_seconds >= self.duration - 1e-6:
            return "duration"
        if self.video_seconds >= float(self.limits["max_duration_s"]):
            return "max_duration_s"
        if self.frames % max(1, int(self.fps)) == 0:  # ~once per second of video
            if self.sink.size() / 1e6 >= float(self.limits["max_file_mb"]):
                return "max_file_mb"
            try:
                if shutil.disk_usage(self.path.parent).free / 1e6 < float(self.limits["min_free_disk_mb"]) / 2:
                    return "disk_low"
            except OSError:
                pass
        return ""

    def _paint(self, frame: np.ndarray, masks) -> None:
        o = (self.rect[0], self.rect[1])
        if masks:
            apply_masks(frame, o, [m["rect"] for m in masks])
        for fn, sprite in ((self.pointer_fn, self.pointer), (self.ghost_fn, self.ghost)):
            if fn is None or sprite is None:
                continue
            try:
                pos = fn()
            except Exception:  # noqa: BLE001
                pos = None
            if pos is not None:
                img, (hx, hy) = sprite
                blend_sprite(frame, img, int(pos[0]) - o[0] - hx, int(pos[1]) - o[1] - hy)

    def _run(self):
        interval = 1.0 / self.fps
        last_screen_check = -1e9
        last_seq = None
        try:
            next_t = None
            while not self._stop.is_set():
                if not self._resume.is_set():
                    self._resume.wait()
                    next_t = None
                    continue
                now = time.monotonic()
                if self.screen_ok_fn and now - last_screen_check >= 0.5:
                    last_screen_check = now
                    ok = bool(self.screen_ok_fn())
                    if ok and not self._screen_ok:
                        with self._slot:
                            self._latest = None   # never show the frame from before the lock
                    self._screen_ok = ok
                if not self._screen_ok:
                    self.skipped_locked += 1
                    next_t = None
                    self._stop.wait(interval)
                    continue
                with self._slot:
                    latest = self._latest
                if latest is None:      # nothing captured yet: the clock starts with the first frame
                    self._stop.wait(0.005)
                    continue
                if next_t is None:
                    next_t = now
                if now < next_t:
                    self._stop.wait(next_t - now)
                    continue
                frame = np.array(latest[2], dtype=np.uint8, order="C", copy=True)
                masks = latest[3]
                if latest[1] != last_seq:
                    last_seq = latest[1]
                    self.used += 1
                    for m in masks:
                        self.masked_windows[m["process"] + "|" + m["title"]] = m["process"] or "?"
                self._paint(frame, masks)
                data = frame.tobytes()
                # ticks this frame covers: 1, more if the writer/encoder was late (bounded to 1 s)
                late = int((time.monotonic() - next_t) // interval)
                n = 1 + max(0, min(late, int(self.fps)))
                for _ in range(n):
                    self.sink.write(data)
                    self.frames += 1
                    hit = self._limit_hit()
                    if hit:
                        self.stop_reason = self.stop_reason or hit
                        self._stop.set()
                        break
                next_t += n * interval
                if time.monotonic() - next_t > 1.0:  # hopelessly behind: restart the clock
                    next_t = time.monotonic()
        except (BrokenPipeError, OSError) as e:
            self.error = f"encoder stopped: {e}"
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self._stop.set()
            for g in self._grabbers:
                g.join(5)
            self._finish()

    def _finish(self):
        self.state = "finishing"
        res = self.sink.close() if self.sink else {"returncode": 0, "stderr": ""}
        self.ended_at = datetime.now()
        ok = res.get("returncode") == 0 and self.frames > 0 and not self.error
        if res.get("returncode") not in (0, None):
            self.error = (self.error + "; " if self.error else "") + f"ffmpeg exit {res['returncode']}: {res.get('stderr', '')}"
        elif self.frames == 0 and not self.error:
            self.error = "no frame was recorded"
        if not self.stop_reason:
            self.stop_reason = "error" if self.error else "record_stop"
        self.result = {"duration_s": round(self.video_seconds, 2), "bytes": self.sink.size() if self.sink else 0,
                       "size_mb": round((self.sink.size() if self.sink else 0) / 1e6, 2),
                       "ended_at": self.ended_at.isoformat(timespec="seconds")}
        self.state = "finished" if ok else "failed"
        if not ok and self.frames == 0:
            try:
                self.path.unlink()
            except OSError:
                pass
        self._done.set()
        if self.on_end:
            try:
                self.on_end(self)
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------- manager
class ScreenVideo:
    """All recordings of the server. Thread safe; recordings run on their own threads."""

    def __init__(self, cfg: dict, log=None):
        self.cfg = cfg
        self.log = log
        self.recs: dict[int, Recording] = {}
        self._n = 0
        self._lock = threading.Lock()

    @property
    def limits(self) -> dict:
        return {**DEFAULTS, **self.cfg.get("recording", {})}

    @property
    def out_dir(self) -> Path:
        return Path(expand_path(str(self.limits["dir"])))

    def active(self) -> list[Recording]:
        return [r for r in self.recs.values() if r.state in ("recording", "paused")]

    def start(self, rect: tuple, fps: float, duration: float | None, path: Path, out_size, **kw) -> Recording:
        lim = self.limits
        with self._lock:
            if len(self.active()) >= int(lim["max_concurrent"]):
                raise RecordingError(f"already {len(self.active())} recording(s) running "
                                     f"(recording.max_concurrent={lim['max_concurrent']}): stop one first")
            self._n += 1
            rid = self._n
            sink = kw.pop("sink", None)
            if sink is None:
                sink = FfmpegSink(ffmpeg_cmd(ffmpeg_exe(), (rect[2] - rect[0], rect[3] - rect[1]), fps, path,
                                             out_size, lim["crf"], lim["preset"]), path)
            rec = Recording(rid, rect, fps, duration, path, out_size, sink, limits=lim, **kw)
            self.recs[rid] = rec
            # forget old finished ones
            for old in [k for k, r in self.recs.items() if r.state in ("finished", "failed")][:-20]:
                self.recs.pop(old, None)
        return rec.start()

    def get(self, rid) -> Recording:
        if rid is None:
            act = self.active()
            if len(act) == 1:
                return act[0]
            raise RecordingError("pass the recording id" if act else "no recording running")
        rec = self.recs.get(int(rid))
        if rec is None:
            raise RecordingError(f"unknown recording id {rid}")
        return rec

    def stop_all(self, reason: str, wait: float = 120) -> list[dict]:
        out = []
        for r in self.active():
            out.append(r.stop(reason, wait=wait))
        return out

    def snapshot(self) -> list[dict]:
        return [r.status() for r in self.recs.values()]
