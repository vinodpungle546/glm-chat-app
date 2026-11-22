"""Synthetic video-call recordings for tests and benchmarks (no real meetings, no real people).

A frame looks like a gallery-view call: one tile per participant with a name label, and a
coloured border around the active speaker. Optional: a shared screen (lots of text, no tiles)
and speaker view (one large video with the name bottom-left).

    python -m transcriber.tests.synthetic_meeting out.mp4 --minutes 10
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SIZE = (1280, 720)
BACKGROUND = (32, 33, 36)
TILE = (60, 64, 67)
HIGHLIGHT = (98, 100, 167)  # a Teams-like purple
NAMES = ["Priya Sharma", "Daniel Okafor", "Mei Lin Chen", "Tomás Rivera"]


def _font(size: int):
    """A system font for the name labels (PIL's built-in font if none is found)."""
    for path in ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/SFNS.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def tile_rects(count: int, size=SIZE) -> list[tuple[int, int, int, int]]:
    """The rectangles of `count` participant tiles in a 2-column gallery: (left, top, right, bottom)."""
    w, h = size
    cols = 2 if count > 1 else 1
    rows = (count + cols - 1) // cols
    pad = 24
    tw, th = (w - pad * (cols + 1)) // cols, (h - 80 - pad * (rows + 1)) // rows
    return [(pad + c * (tw + pad), pad + r * (th + pad), pad + c * (tw + pad) + tw, pad + r * (th + pad) + th)
            for i in range(count) for r, c in [divmod(i, cols)]]


def draw_frame(names: list[str], active: int | None, mode: str = "gallery", labels: list[str] | None = None,
               highlight_all: bool = False, border_color: tuple[int, int, int] | None = None,
               border_width: int = 5) -> Image.Image:
    """mode: gallery | white (gallery with a white highlight border) | share | speaker.
    `labels` overrides the text shown (e.g. truncated names); `border_color` / `border_width` draw
    other apps' highlights (Zoom green, Meet blue, thin borders)."""
    labels = labels or names
    im = Image.new("RGB", SIZE, BACKGROUND)
    d = ImageDraw.Draw(im)
    font = _font(22)
    if mode == "share":
        d.rectangle([40, 30, 1240, 650], fill=(250, 250, 250))
        body = _font(20)
        for i in range(18):
            d.text((70, 50 + i * 32), f"Quarterly roadmap item {i + 1}: migrate the billing service and update docs",
                   font=body, fill=(20, 20, 20))
        return im
    if mode == "speaker":
        d.rectangle([40, 30, 1240, 650], fill=TILE)
        if active is not None:
            d.text((60, 610), labels[active], font=font, fill="white")
        return im
    for i, rect in enumerate(tile_rects(len(names))):
        d.rectangle(rect, fill=TILE)
        d.ellipse([(rect[0] + rect[2]) // 2 - 50, (rect[1] + rect[3]) // 2 - 70,
                   (rect[0] + rect[2]) // 2 + 50, (rect[1] + rect[3]) // 2 + 30], fill=(95, 99, 104))
        d.text((rect[0] + 16, rect[3] - 36), labels[i], font=font, fill="white")
        if highlight_all or i == active:
            colour = border_color or ((245, 245, 245) if mode == "white" else HIGHLIGHT)
            d.rectangle(rect, outline=colour, width=border_width)
    d.rectangle([0, 660, 1280, 720], fill=(24, 24, 27))
    return im


def make_video(path: Path, turns: list[tuple[int, float, float]], names: list[str] | None = None,
               duration: float | None = None, fps: float = 2, modes: dict[int, str] | None = None,
               audio: bool = True) -> Path:
    """Encode a call where `turns` = (speaker index, start, end) drive the highlight.
    `modes` maps a turn index to "share" / "speaker" / "white" / "none" (no highlight) for that turn."""
    names = names or NAMES[: 1 + max(t[0] for t in turns)]
    modes = modes or {}
    duration = duration or max(end for _, _, end in turns) + 1
    with tempfile.TemporaryDirectory() as tmp:
        frames = Path(tmp)
        cache: dict[tuple, Path] = {}
        n = int(duration * fps)
        for i in range(n):
            t = i / fps
            idx = next((k for k, (_, s, e) in enumerate(turns) if s <= t < e), None)
            mode = modes.get(idx, "gallery") if idx is not None else "gallery"
            active = turns[idx][0] if idx is not None and mode != "none" else None
            key = ("gallery" if mode == "none" else mode, active)
            if key not in cache:
                cache[key] = frames / f"img_{len(cache)}.png"
                draw_frame(names, active, key[0]).save(cache[key])
            (frames / f"f_{i:06d}.png").symlink_to(cache[key])
        cmd = ["ffmpeg", "-v", "error", "-y", "-framerate", str(fps), "-i", str(frames / "f_%06d.png")]
        if audio:
            cmd += ["-f", "lavfi", "-i", f"sine=frequency=220:duration={duration}"]
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps)]
        if audio:
            cmd += ["-c:a", "aac", "-shortest"]
        subprocess.run(cmd + [str(path)], check=True)
    return path


def meeting_turns(minutes: float, speakers: int = 4, turn_seconds: float = 12.0) -> list[tuple[int, float, float]]:
    """A rotating conversation: deterministic turn lengths between 4 and 20 s."""
    turns, t, i = [], 0.0, 0
    order = [0, 1, 0, 2, 3, 1, 2, 0, 3, 2, 1, 3]
    while t < minutes * 60 - 1:
        length = min(4 + (i * 7) % 17, minutes * 60 - t)
        turns.append((order[i % len(order)] % speakers, round(t, 2), round(t + length, 2)))
        t += length + 0.5
        i += 1
    return turns


def segments_for(turns: list[tuple[int, float, float]]) -> list[dict]:
    """Transcript segments with diarization-style labels for these turns."""
    return [{"start": s, "end": e, "speaker": f"SPEAKER_{spk:02d}", "text": f"Turn {i} words."}
            for i, (spk, s, e) in enumerate(turns)]


def main() -> None:
    """Command line: writes a synthetic call video of the given length."""
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    parser.add_argument("--minutes", type=float, default=10)
    args = parser.parse_args()
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg is needed")
    make_video(args.out, meeting_turns(args.minutes))
    print(args.out)


if __name__ == "__main__":
    main()
