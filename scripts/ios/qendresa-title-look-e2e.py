#!/usr/bin/env python3
"""Repeatable before/after render of Qendresa item 20 (her stacked title look + a logo above it).

Her Q2 prompt asks for the title "5 PROJECTS / the AI builder community / can't stop talking
about!!!!" with the GitHub logo above it. Behind `settings.title_look_enabled` the phone
Talking edit draws her 3 lines stacked (own face + colour each, never on her face) with the
logo sticker right above the block; off draws today's single block with " / " literal.

Three steps, run from the repo root (any worktree):

  1. prepare (compile both cases through the REAL worker, no provider calls):
     src/apps/api/.venv/bin/python scripts/ios/qendresa-title-look-e2e.py \
       prepare OUT --foot FOOT_DIR
  2. render (iOS simulator, production exporter; run under the shared build lock):
     TEST_RUNNER_KRIA_TITLE_LOOK_E2E_DIR=OUT xcodebuild test -project ... -scheme ... \
       -destination '...' \
       -only-testing:KriaTests/DeviceTitleLookRenderE2ETests/testRendersEveryPreparedTitleLookCase
  3. score (kit rules + OCR line check + face check + logo check, montage):
     src/apps/api/.venv/bin/python scripts/ios/qendresa-title-look-e2e.py \
       score OUT --kit ~/Downloads/kria-qendresa-test

FOOT_DIR holds `take.mp4` (1080x1920 talking head), `words.json` (whisper.cpp `-ml 1 -sow
-oj`), `faces.json` (Apple Vision face boxes every 0.25 s) and `visuals/logo.png`. prepare runs
`tests/tasks/test_qendresa_title_look_device_cases.py` twice on the same footage (`q2_before`:
flag off, `q2_after`: flag on) and writes `cases/<name>/{recipe.json,assets.json,title.json}`.

score asserts: the kit's `check_result.sh MP4 Q2 --ignore-tail-s 1.6` `hook` rule PASSES after and
no rule that passed before fails after; her 3 lines are read by Apple Vision as separate OCR
lines in her order, top to bottom, on the after frames (before evidence recorded: one block with
" / " drawn literally); no title or logo box covers the largest face by more than 5% of its own
area (with a negative control that moves the logo onto the face and must fail); the logo's bottom
edge is at or above the first title line. Writes `frame_before.png`, `frame_after.png`,
`montage.png` and `result.json`.

Limits: stand-in footage and a synthetic voice (not her real take); the face sampler is stubbed
and fed by Apple Vision boxes from the footage; no provider calls (no Gemini, no font model);
the logo is a stand-in sticker; OCR line wrapping is fuzzy-matched, not exact.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
API = REPO / "src/apps/api"
VENV_PYTHON = Path(
    "/Users/yasinberkyesilyurt/projects/nova/src/apps/api/.venv/bin/python"
)
CASES = ("q2_before", "q2_after")
IGNORE_TAIL_S = "1.6"
SAMPLE_TIMES = (0.5, 1.0, 1.5, 2.0, 2.5)
FRAME_AT_S = 1.5
MAX_OVERLAP = 0.05
LOGO_SLACK = 0.01
LINES = ("5 PROJECTS", "the AI builder community", "can't stop talking about!!!!")
MIN_LINE_SIMILARITY = 0.75
TEST_ENV = {
    "STORAGE_BUCKET": "nova-test",
    "STORAGE_PROVIDER": "gcs",
    "OPENAI_API_KEY": "sk-test",
    "TOKEN_ENCRYPTION_KEY": "x",
    "DATABASE_URL": "postgresql://yasinberkyesilyurt@localhost:5432/nova_test",
    "ALLOWED_ORIGINS": '["http://localhost:3000"]',
}


# ---------------------------------------------------------------------------- prepare


def prepare(out: Path, foot: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        **TEST_ENV,
        "KRIA_TITLE_LOOK_E2E_OUT": str(out.resolve()),
        "KRIA_TITLE_LOOK_E2E_FOOT": str(foot.resolve()),
        "PYTHONPATH": ".",
    }
    command = [
        str(VENV_PYTHON),
        "-m",
        "pytest",
        "-q",
        "--noconftest",
        "-p",
        "tests.conftest",
        "--rootdir=.",
        "-p",
        "no:cacheprovider",
        "tests/tasks/test_qendresa_title_look_device_cases.py",
    ]
    code = subprocess.run(command, cwd=API, env=env, check=False).returncode
    if code != 0:
        return code
    for name in CASES:
        data = json.loads((out / "cases" / name / "title.json").read_text())
        rows = data["title_rows"]
        logos = data["logo_cards"]
        print(
            f"{name}: {len(rows)} title row(s) "
            f"{[(r['text'], r['font_family'], r['size_px']) for r in rows]}, "
            f"{len(logos)} logo card(s)"
        )
    print(f"Prepared {out}")
    return 0


# ---------------------------------------------------------------------------- score

Box = tuple[float, float, float, float]  # left, top, right, bottom (normalized)


def run_check(kit: Path, movie: Path) -> dict[str, Any]:
    subprocess.run(
        [
            str(kit / "tools/check_result.sh"),
            str(movie),
            "Q2",
            "--ignore-tail-s",
            IGNORE_TAIL_S,
        ],
        check=False,
    )
    return json.loads((movie.with_suffix(".check") / "report.json").read_text())


def extract_frame(movie: Path, at: float, dest: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-ss",
            f"{at}",
            "-i",
            str(movie),
            "-frames:v",
            "1",
            str(dest),
        ],
        check=True,
    )


def vision(kit: Path, frames: list[Path]) -> list[dict[str, Any]]:
    """The kit's Apple Vision tool: one JSON line per frame, in order."""
    proc = subprocess.run(
        [str(kit / "tools/frame_text_faces"), *map(str, frames)],
        check=True,
        capture_output=True,
        text=True,
    )
    lines = [
        json.loads(x) for x in proc.stdout.splitlines() if x.strip().startswith("{")
    ]
    if len(lines) != len(frames):
        raise RuntimeError(
            f"frame_text_faces gave {len(lines)} lines for {len(frames)} frames"
        )
    return lines


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def ocr_lines(texts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OCR rows top to bottom; each {text, box [x, y, w, h]}."""
    return sorted(texts, key=lambda t: t["box"][1])


def merge(rows: list[dict[str, Any]]) -> tuple[str, Box]:
    text = " ".join(r["text"] for r in rows)
    left = min(r["box"][0] for r in rows)
    top = min(r["box"][1] for r in rows)
    right = max(r["box"][0] + r["box"][2] for r in rows)
    bottom = max(r["box"][1] + r["box"][3] for r in rows)
    return text, (left, top, right, bottom)


def match_lines(rows: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Match her 3 expected lines to consecutive OCR rows (a line may wrap over up to 2 rows),
    in order. Returns [{expected, text, box, score, y}] or None when a line is not found."""
    found: list[dict[str, Any]] = []
    cursor = 0
    for expected in LINES:
        best: tuple[float, int, int] | None = None
        for start in range(cursor, len(rows)):
            for span in (1, 2):
                chunk = rows[start : start + span]
                if len(chunk) < span:
                    continue
                text, _ = merge(chunk)
                score = similarity(text, expected)
                if best is None or score > best[0]:
                    best = (score, start, span)
        if best is None or best[0] < MIN_LINE_SIMILARITY:
            return None
        score, start, span = best
        text, box = merge(rows[start : start + span])
        found.append(
            {
                "expected": expected,
                "text": text,
                "box": box,
                "score": score,
                "y": box[1],
            }
        )
        cursor = start + span
    return found


def overlap(box: Box, face: Box) -> float:
    """Share of `box`'s own area covered by `face`."""
    w = min(box[2], face[2]) - max(box[0], face[0])
    h = min(box[3], face[3]) - max(box[1], face[1])
    area = (box[2] - box[0]) * (box[3] - box[1])
    return 0.0 if w <= 0 or h <= 0 or area <= 0 else (w * h) / area


def largest_face(faces: list[list[float]]) -> Box | None:
    if not faces:
        return None
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    return (x, y, x + w, y + h)


def on_screen(rows: list[dict[str, Any]], t: float) -> list[dict[str, Any]]:
    return [r for r in rows if r["start_s"] <= t < r["end_s"]]


def logo_box(card: dict[str, Any], shift: tuple[float, float] = (0.0, 0.0)) -> Box:
    cx, cy = card["x"] + shift[0], card["y"] + shift[1]
    return (
        cx - card["width"] / 2,
        cy - card["height"] / 2,
        cx + card["width"] / 2,
        cy + card["height"] / 2,
    )


def face_check(
    frames: list[dict[str, Any]],
    logos: list[dict[str, Any]],
    *,
    onto_face: bool = False,
) -> tuple[int, list[str]]:
    """(boxes checked, descriptions of boxes covering the face by > 5% of their own area).
    `onto_face` moves each logo onto the face centre (the negative control, logos only)."""
    checked = 0
    hits: list[str] = []
    for frame in frames:
        face = largest_face(frame["faces"])
        if face is None:
            continue
        t = frame["t"]
        boxes: list[tuple[str, Box]] = []
        if not onto_face:
            boxes += [(f"title '{r['text']}'", r["rect"]) for r in frame["title_ocr"]]
        for card in logos:
            if card["window_start"] <= t < card["window_end"]:
                shift = (0.0, 0.0)
                if onto_face:
                    shift = (
                        (face[0] + face[2]) / 2 - card["x"],
                        (face[1] + face[3]) / 2 - card["y"],
                    )
                boxes.append(("logo", logo_box(card, shift)))
        for label, box in boxes:
            checked += 1
            share = overlap(box, face)
            if share > MAX_OVERLAP:
                hits.append(f"t={t:.1f}s {label} covers face {share:.0%}")
    return checked, hits


def analyse_case(kit: Path, out: Path, name: str) -> dict[str, Any]:
    directory = out / "cases" / name
    movie = directory / "phone.mp4"
    title = json.loads((directory / "title.json").read_text())
    frames_dir = directory / "frames"
    frames_dir.mkdir(exist_ok=True)
    paths = []
    for t in SAMPLE_TIMES:
        dest = frames_dir / f"t{t:.1f}.png"
        extract_frame(movie, t, dest)
        paths.append(dest)
    sensed = vision(kit, paths)
    frames = []
    for t, line in zip(SAMPLE_TIMES, sensed, strict=True):
        rows = ocr_lines(line.get("texts", []))
        title_ocr = []
        for row in rows:
            x, y, w, h = row["box"]
            title_ocr.append({"text": row["text"], "rect": (x, y, x + w, y + h)})
        frames.append(
            {
                "t": t,
                "texts": [{"text": r["text"], "box": r["box"]} for r in rows],
                "title_ocr": [
                    r
                    for r in title_ocr
                    if any(similarity(r["text"], x) > 0.5 for x in LINES)
                    or "/" in r["text"]
                ],
                "faces": line.get("faces", []),
            }
        )
    return {"title": title, "frames": frames, "movie": movie}


def score(out: Path, kit: Path) -> int:
    kit = kit.expanduser()
    failures: list[str] = []
    result: dict[str, Any] = {"cases": {}}
    analysed: dict[str, dict[str, Any]] = {}
    for name in CASES:
        movie = out / "cases" / name / "phone.mp4"
        if not movie.exists():
            failures.append(f"{name}: no phone.mp4 (render it first)")
            continue
        report = run_check(kit, movie)
        analysed[name] = analyse_case(kit, out, name)
        result["cases"][name] = {
            "passed": sum(bool(r["pass"]) for r in report["rules"]),
            "total": len(report["rules"]),
            "rules": {
                r["id"]: {"pass": bool(r["pass"]), "measured": r.get("measured")}
                for r in report["rules"]
            },
        }
    if len(analysed) < len(CASES):
        (out / "result.json").write_text(
            json.dumps({**result, "failures": failures}, indent=2)
        )
        for failure in failures:
            print("FAIL:", failure)
        return 1

    before, after = result["cases"]["q2_before"], result["cases"]["q2_after"]
    # 1. kit rules
    if not after["rules"].get("hook", {}).get("pass", False):
        failures.append("hook should PASS after")
    for rid, rule in before["rules"].items():
        if rule["pass"] and not after["rules"].get(rid, {}).get("pass", False):
            failures.append(f"{rid} passed before and fails after")

    # 2. line check on the rendered frames (independent of the recipe)
    lines: dict[str, Any] = {}
    for name in CASES:
        per_frame = []
        for frame in analysed[name]["frames"]:
            rows = ocr_lines(frame["texts"])
            matched = match_lines(rows)
            per_frame.append(
                {
                    "t": frame["t"],
                    "ocr": [r["text"] for r in rows],
                    "slash_visible": any("/" in r["text"] for r in rows),
                    "matched": matched,
                }
            )
        lines[name] = per_frame
    result["lines"] = lines
    ok_frames = []
    for frame in lines["q2_after"]:
        matched = frame["matched"]
        if matched is None:
            continue
        ys = [m["y"] for m in matched]
        if ys == sorted(ys) and len(set(ys)) == len(ys):
            ok_frames.append(frame["t"])
    result["lines_ok_after_frames"] = ok_frames
    if not ok_frames:
        failures.append(
            "line check: her 3 lines were not read as separate OCR lines in her order on any after frame"
        )
    result["slash_visible_before"] = any(f["slash_visible"] for f in lines["q2_before"])

    # 3. face check + negative control
    faces: dict[str, Any] = {}
    for name in CASES:
        data = analysed[name]
        checked, hits = face_check(data["frames"], data["title"]["logo_cards"])
        faces[name] = {"boxes_checked": checked, "overlaps": hits}
    after_data = analysed["q2_after"]
    if faces["q2_after"]["boxes_checked"] == 0:
        failures.append("face check: nothing was checked on the after frames")
    if faces["q2_after"]["overlaps"]:
        failures.append(
            f"after: title/logo on her face: {faces['q2_after']['overlaps'][:5]}"
        )
    c_checked, c_hits = face_check(
        after_data["frames"], after_data["title"]["logo_cards"], onto_face=True
    )
    faces["negative_control"] = {"boxes_checked": c_checked, "overlaps": c_hits}
    if c_checked == 0 or not c_hits:
        failures.append(
            "negative control did not report overlaps: face check is broken"
        )
    result["faces"] = faces

    # 4. logo above the first title line
    logos = after_data["title"]["logo_cards"]
    logo_rows = []
    if not logos:
        failures.append("logo check: no logo card in the after recipe")
    for frame in after_data["frames"]:
        matched = match_lines(ocr_lines(frame["texts"]))
        t = frame["t"]
        for card in logos:
            if not card["window_start"] <= t < card["window_end"] or matched is None:
                continue
            bottom = logo_box(card)[3]
            top = matched[0]["box"][1]
            row = {
                "t": t,
                "logo_bottom": bottom,
                "first_line_top": top,
                "ok": bottom <= top + LOGO_SLACK,
            }
            logo_rows.append(row)
            if not row["ok"]:
                failures.append(
                    f"logo check: t={t}s logo bottom {bottom:.3f} > first line top {top:.3f}"
                )
    if logos and not logo_rows:
        failures.append("logo check: never both the logo and the title lines on screen")
    result["logo"] = logo_rows

    # 5. frames and montage
    frames_out = {}
    for tag, name in (("before", "q2_before"), ("after", "q2_after")):
        dest = out / f"frame_{tag}.png"
        extract_frame(analysed[name]["movie"], FRAME_AT_S, dest)
        frames_out[tag] = dest
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(frames_out["before"]), "-i", str(frames_out["after"]),
            "-filter_complex", "[0:v]scale=540:-1[a];[1:v]scale=540:-1[b];[a][b]hstack=inputs=2",
            str(out / "montage.png"),
        ],
        check=True,
    )  # fmt: skip

    result["failures"] = failures
    (out / "result.json").write_text(json.dumps(result, indent=2, default=str))
    print(f"{'case':10} {'rule':14} {'pass':5} measured")
    for name, data in result["cases"].items():
        for rid, rule in data["rules"].items():
            print(f"{name:10} {rid:14} {rule['pass']!s:5} {rule['measured']}")
        print(f"{name:10} {'TOTAL':14} {data['passed']}/{data['total']}")
    print(f"slash visible before: {result['slash_visible_before']}")
    print(f"after frames with her 3 lines in order: {ok_frames}")
    for key, row in faces.items():
        print(
            f"face check {key}: {len(row['overlaps'])} overlap(s) in {row['boxes_checked']} boxes"
        )
    for failure in failures:
        print("FAIL:", failure)
    print("OK" if not failures else f"{len(failures)} check(s) failed")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("out", type=Path)
    prep.add_argument("--foot", type=Path, required=True)
    sc = sub.add_parser("score")
    sc.add_argument("out", type=Path)
    sc.add_argument("--kit", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        return prepare(args.out, args.foot)
    return score(args.out, args.kit)


if __name__ == "__main__":
    sys.exit(main())
