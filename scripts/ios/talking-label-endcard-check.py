#!/usr/bin/env python3
"""Independent frame check of a rendered phone Talking export (no app code imported).

Samples the MP4 at FPS frames/s with ffmpeg, OCRs every frame + detects faces (Apple Vision,
tools/ocr), and checks:
  label   : the label text is read inside the asked corner on every frame of its window, and its
            box never overlaps a detected face box;
  card    : frames after the last spoken word are the asked colour (corner pixels) and the card
            words are read on them;
  speech  : the export's audio up to the take's end matches the take's own audio (no speech cut),
            and the card time is silent.
Usage: talking-label-endcard-check.py DIR LABEL CORNER CARD_TEXT CARD_HEX
  DIR = the --out folder of talking-label-endcard-render-e2e.py after the simulator render.
  KRIA_OCR_BIN = an Apple Vision OCR binary printing one JSON row per text/face box (the Çiğdem
  kit's tools/ocr.swift, `swiftc -O -o ocr ocr.swift`).
Writes DIR/check.json and DIR/sheet.jpg.
"""

import json
import os
import math
import re
import subprocess
import sys
import wave
from pathlib import Path

FPS = 5
OCR_BIN = os.environ.get("KRIA_OCR_BIN", str(Path(__file__).with_name("tools") / "ocr"))


def fold(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def frames(video: Path, out: Path) -> list[Path]:
    out.mkdir(exist_ok=True)
    for old in out.glob("f_*.png"):
        old.unlink()
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(video),
            "-vf",
            f"fps={FPS}",
            str(out / "f_%05d.png"),
        ],
        check=True,
    )
    return sorted(out.glob("f_*.png"))


def ocr(paths: list[Path]) -> dict[str, list[dict]]:
    rows: dict[str, list[dict]] = {}
    for i in range(0, len(paths), 40):
        chunk = paths[i : i + 40]
        out = subprocess.run(
            [OCR_BIN, *map(str, chunk)], check=True, capture_output=True, text=True
        ).stdout
        for line in out.splitlines():
            row = json.loads(line)
            rows.setdefault(row["file"], []).append(row)
    return rows


def pixel(path: Path, x: int, y: int) -> list[int]:
    out = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-vf",
            f"crop=1:1:{x}:{y}",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout
    return list(out[:3])


def audio(path: Path, wav: Path) -> list[float]:
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(path),
            "-ac",
            "1",
            "-ar",
            "16000",
            str(wav),
        ],
        check=True,
    )
    with wave.open(str(wav)) as w:
        raw = w.readframes(w.getnframes())
    import array

    samples = array.array("h", raw)
    return [s / 32768 for s in samples]


def rms(xs: list[float]) -> float:
    return math.sqrt(sum(x * x for x in xs) / len(xs)) if xs else 0.0


def overlaps(a: dict, b: dict) -> bool:
    return not (
        a["x"] + a["w"] <= b["x"]
        or b["x"] + b["w"] <= a["x"]
        or a["y"] + a["h"] <= b["y"]
        or b["y"] + b["h"] <= a["y"]
    )


def in_corner(box: dict, corner: str) -> bool:
    cx, cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
    v, h = corner.split("_")
    ok_v = cy < 0.3 if v == "top" else cy > 0.7
    ok_h = cx < 0.4 if h == "left" else cx > 0.6 if h == "right" else 0.3 < cx < 0.7
    return ok_v and ok_h


def main() -> None:
    folder, label, corner, card_text, card_hex = sys.argv[1:6]
    folder = Path(folder)
    plan = json.loads((folder / "plan.json").read_text())
    video = folder / "frames" / "talking_head.mp4"
    take_end = float(plan["take_duration_s"])
    last_word = float(plan["last_word_end_s"] or take_end)
    paths = frames(video, folder / "check_frames")
    found = ocr(paths)
    want_label, want_card = fold(label), fold(card_text)
    card_rgb = [int(card_hex[i : i + 2], 16) for i in (1, 3, 5)]
    label_hits = label_face = 0
    label_frames = card_frames = card_text_hits = card_colour_hits = 0
    misses: list[float] = []
    for index, path in enumerate(paths):
        t = index / FPS
        rows = found.get(path.name, [])
        texts = [r for r in rows if r["kind"] == "text"]
        faces = [r for r in rows if r["kind"] == "face"]
        if t < take_end - 0.05:
            label_frames += 1
            boxes = [
                r
                for r in texts
                if want_label in fold(r["text"]) and in_corner(r, corner)
            ]
            if boxes:
                label_hits += 1
                if any(overlaps(b, f) for b in boxes for f in faces):
                    label_face += 1
            else:
                misses.append(round(t, 2))
        elif t > take_end + 0.3:
            card_frames += 1
            joined = fold(" ".join(r["text"] for r in texts))
            if want_card and want_card in joined:
                card_text_hits += 1
            corners = [
                pixel(path, 20, 20),
                pixel(path, 1060, 1900),
                pixel(path, 20, 1900),
            ]
            if all(
                max(abs(a - b) for a, b in zip(c, card_rgb, strict=True)) <= 24
                for c in corners
            ):
                card_colour_hits += 1
    src = audio(folder / "take.mp4", folder / "take.wav")
    out = audio(video, folder / "out.wav")
    n = min(len(src), len(out), int(take_end * 16000))
    step = 1600  # 100 ms envelope
    env_s = [rms(src[i : i + step]) for i in range(0, n - step, step)]
    env_o = [rms(out[i : i + step]) for i in range(0, n - step, step)]
    ms, mo = sum(env_s) / len(env_s), sum(env_o) / len(env_o)
    cov = sum((a - ms) * (b - mo) for a, b in zip(env_s, env_o, strict=True))
    corr = cov / math.sqrt(
        sum((a - ms) ** 2 for a in env_s) * sum((b - mo) ** 2 for b in env_o)
    )
    lw0, lw1 = int(max(0, last_word - 0.4) * 16000), int(last_word * 16000)
    last_ratio = rms(out[lw0:lw1]) / max(rms(src[lw0:lw1]), 1e-9)
    diff = corr
    tail = out[int((take_end + 0.2) * 16000) : int((take_end + 2.5) * 16000)]
    result = {
        "video_s": len(paths) / FPS,
        "take_end_s": take_end,
        "last_word_end_s": last_word,
        "label": {
            "frames": label_frames,
            "read_in_corner": label_hits,
            "share": round(label_hits / label_frames, 4) if label_frames else None,
            "on_face_frames": label_face,
            "missed_at_s": misses[:40],
        },
        "card": {
            "frames": card_frames,
            "colour_frames": card_colour_hits,
            "text_frames": card_text_hits,
        },
        "speech": {
            "source_rms": round(rms(src[:n]), 5),
            "envelope_corr_up_to_take_end": round(diff, 4),
            "last_word_energy_ratio": round(last_ratio, 3),
            "card_rms": round(rms(tail), 5),
        },
    }
    (folder / "check.json").write_text(json.dumps(result, indent=2))
    picks = [paths[int(len(paths) * f)] for f in (0.02, 0.25, 0.5, 0.75, 0.9)] + [
        paths[-3]
    ]
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            *sum((["-i", str(p)] for p in picks), []),
            "-filter_complex",
            f"{''.join(f'[{i}]scale=270:480[s{i}];' for i in range(len(picks)))}"
            f"{''.join(f'[s{i}]' for i in range(len(picks)))}hstack=inputs={len(picks)}",
            str(folder / "sheet.jpg"),
        ],
        check=True,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
