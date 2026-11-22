#!/usr/bin/env python3
"""Compare local vision models for the on-screen names fallback (synthetic frames only).

  transcriber/.venv/bin/python scripts/benchmark_vision_models.py --models qwen3-vl:4b-instruct gemma4:e4b

Draws video-call frames with known answers: 2-6 tiles, the active speaker highlighted in the ways
different apps draw it (Teams purple, thin purple, white, Zoom green, Meet blue), truncated name
labels, frames with no highlight and shared screens. Each model gets the same question the
transcriber asks (screen_names.ask_vision, with the labels on screen as the only allowed answers).

Scores per model:
  correct      the right name, "no highlight" when there is none, "screen share" for a shared screen
  wrong name   a name, but not the highlighted one (the costly mistake: it becomes speaker evidence)
  missed       no name although a tile was highlighted (harmless: the frame stays unresolved)
The text reader's own result on each frame is shown too, so you can see which frames reach the
vision model in practice.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from transcriber import screen_names as sn  # noqa: E402
from transcriber.tests import synthetic_meeting as synth  # noqa: E402

NAMES = ["Priya Sharma", "Daniel Okafor", "Mei Lin Chen", "Tomás Rivera", "Aisha Bello", "Lukas Novak"]
STYLES = {  # (border colour, width)
    "teams": (synth.HIGHLIGHT, 5),
    "thin": (synth.HIGHLIGHT, 2),
    "white": ((245, 245, 245), 4),
    "zoom": ((35, 217, 89), 4),
    "meet": ((26, 115, 232), 3),
}


def hard_frames() -> list[dict]:
    """Closer to real calls: busy "camera" content in every tile, thin borders, and decoys (a
    patch in the highlight colour inside a tile that is NOT the active speaker)."""
    out = []
    for i, (style, width, decoy) in enumerate([("teams", 2, False), ("teams", 1, False), ("zoom", 2, False),
                                               ("meet", 2, False), ("white", 2, False), ("teams", 2, True),
                                               ("zoom", 2, True), ("none", 0, True)]):
        for n in (4, 6):
            names = NAMES[:n]
            active = None if style == "none" else (i + n) % n
            colour = STYLES["teams" if style == "none" else style][0]
            out.append({"id": f"hard-{n}-{style}{width or ''}{'-decoy' if decoy else ''}", "names": names,
                        "active": active, "colour": colour, "width": width, "noise": i * 10 + n,
                        "decoy": ((active or 0) + 1) % n if decoy else None,
                        "expected": {"name": names[active] if active is not None else None, "screen_share": False}})
    return out


def frames() -> list[dict]:
    """[{id, image args, expected}] — expected: {"name": str|None, "screen_share": bool}"""
    out = []
    for n in (2, 3, 4, 6):
        names = NAMES[:n]
        for i, (style, (colour, width)) in enumerate(STYLES.items()):
            active = (i + n) % n
            out.append({"id": f"{n}tiles-{style}", "names": names, "active": active, "colour": colour,
                        "width": width, "expected": {"name": names[active], "screen_share": False}})
        out.append({"id": f"{n}tiles-none", "names": names, "active": None,
                    "expected": {"name": None, "screen_share": False}})
    long_names = ["Mei Lin Chen-Watanabe", "Daniel Okafor-Mensah", "Priya Sharma", "Tomás Rivera Gutiérrez"]
    truncated = ["Mei Lin Chen-W…", "Daniel Okafor-M…", "Priya Sharma", "Tomás Rivera G…"]
    for active, style in ((0, "teams"), (1, "zoom"), (3, "thin")):
        colour, width = STYLES[style]
        out.append({"id": f"truncated-{style}", "names": long_names, "labels": truncated, "active": active,
                    "colour": colour, "width": width, "expected": {"name": truncated[active], "screen_share": False}})
    for i in range(3):
        out.append({"id": f"share-{i}", "names": NAMES[:4], "active": i, "mode": "share",
                    "expected": {"name": None, "screen_share": True}})
    return out


def draw(frame: dict, dest: Path) -> list[str]:
    """Draws one test frame to `dest` and returns its name labels as the text reader would clean them (these are
    the only answers the vision model may give).
    """
    if frame.get("noise") is None:
        image = synth.draw_frame(frame["names"], frame["active"], frame.get("mode", "gallery"), frame.get("labels"),
                                 border_color=frame.get("colour"), border_width=frame.get("width", 5))
    else:
        from PIL import ImageDraw
        image = synth.draw_frame(frame["names"], None)
        d = ImageDraw.Draw(image)
        rng = random.Random(frame["noise"])
        rects = synth.tile_rects(len(frame["names"]))
        for k, (x0, y0, x1, y1) in enumerate(rects):
            for _ in range(60):  # blocks of "video": faces are never drawn, just colour and texture
                bx, by = rng.randint(x0 + 8, x1 - 40), rng.randint(y0 + 8, y1 - 70)
                d.rectangle([bx, by, min(bx + rng.randint(8, 60), x1 - 8), min(by + rng.randint(8, 50), y1 - 50)],
                            fill=tuple(rng.randint(30, 200) for _ in range(3)))
            if k == frame.get("decoy"):
                d.rectangle([x0 + 30, y0 + 30, x0 + 150, y0 + 110], fill=frame["colour"])
            d.rectangle([x0 + 8, y1 - 44, x0 + 230, y1 - 8], fill=(20, 20, 22))
            d.text((x0 + 16, y1 - 36), frame["names"][k], font=synth._font(22), fill="white")
            if k == frame["active"]:
                d.rectangle((x0, y0, x1, y1), outline=frame["colour"], width=frame["width"])
    image.save(dest, quality=85)
    # As the transcriber does: the labels cleaned by the text reader ("Mei Lin Chen-W…" → "Mei Lin Chen-W").
    return [sn.clean_name(label)[0] for label in frame.get("labels") or frame["names"]]


def score(expected: dict, reply: dict) -> str:
    """Grades one answer: 'correct', 'wrong name' (the costly mistake) or 'missed'."""
    name = (reply.get("name") or "").lower()
    if expected["screen_share"]:
        return "correct" if reply.get("screen_share") or not name else "wrong name"
    if expected["name"] is None:
        return "wrong name" if name else "correct"
    if not name:
        return "missed"
    return "correct" if name == sn.clean_name(expected["name"])[0].lower() else "wrong name"


def text_reader(paths: list[Path]) -> dict[str, str]:
    """What the on-device text reader alone finds on each frame (a name, or why not)."""
    texts = sn.run_ocr(paths)
    out = {}
    for path in paths:
        status, found, _tiles = sn.read_frame(sn.load_rgb(path), texts.get(str(path)) or [])
        out[str(path)] = found[0] if found else status
    return out


def main() -> None:
    """Draws the frames, runs the text reader and each model on them, and prints the scores and a per-frame
    table.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="+", default=["gemma4:e4b"])
    parser.add_argument("--json", type=Path, help="also write the per-frame results here")
    parser.add_argument("--hard", action="store_true", help="the harder set: busy tiles, thin borders, decoys")
    args = parser.parse_args()
    cases = hard_frames() if args.hard else frames()
    report: dict = {"frames": len(cases), "models": {}}
    with tempfile.TemporaryDirectory() as tmp:
        for case in cases:
            case["path"] = Path(tmp) / f"{case['id']}.jpg"
            case["candidates"] = draw(case, case["path"])
        reader = text_reader([c["path"] for c in cases])
        report["text_reader"] = {c["id"]: reader[str(c["path"])] for c in cases}
        for model in args.models:
            t0 = time.time()
            sn.ask_vision(model, cases[0]["path"], candidates=cases[0]["candidates"])  # load the model
            load = time.time() - t0
            rows, times = [], []
            for case in cases:
                t0 = time.time()
                try:
                    reply = sn.ask_vision(model, case["path"], candidates=case["candidates"])
                except Exception as exc:  # report it as a miss, keep going
                    reply = {"name": "", "screen_share": False, "error": f"{type(exc).__name__}: {exc}"}
                times.append(time.time() - t0)
                rows.append({"id": case["id"], "expected": case["expected"], "reply": reply,
                             "result": score(case["expected"], reply), "seconds": round(times[-1], 2)})
            sn.unload_model(model)
            counts = {k: sum(r["result"] == k for r in rows) for k in ("correct", "wrong name", "missed")}
            highlighted = [r for r in rows if r["expected"]["name"]]
            report["models"][model] = {
                **counts,
                "highlighted_named": f"{sum(r['result'] == 'correct' for r in highlighted)} of {len(highlighted)}",
                "load_seconds": round(load, 1),
                "seconds_per_frame": round(sum(times) / len(times), 2),
                "rows": rows,
            }
            print(f"{model}: {counts} · highlighted named {report['models'][model]['highlighted_named']} · "
                  f"{report['models'][model]['seconds_per_frame']} s/frame · load {load:.1f} s", flush=True)
    if args.json:
        args.json.write_text(json.dumps(report, indent=1, ensure_ascii=False))
    print("\nPer frame (text reader → each model):")
    for case in cases:
        line = f"  {case['id']:18} expected={case['expected']['name'] or ('share' if case['expected']['screen_share'] else '-'):22.22}"
        line += f" reader={report['text_reader'][case['id']][:16]:16}"
        for model, data in report["models"].items():
            row = next(r for r in data["rows"] if r["id"] == case["id"])
            mark = {"correct": "✓", "wrong name": "✗", "missed": "·"}[row["result"]]
            line += f" | {model.split(':')[0][:10]} {mark} {(row['reply'].get('name') or '')[:14]:14}"
        print(line)


if __name__ == "__main__":
    main()
