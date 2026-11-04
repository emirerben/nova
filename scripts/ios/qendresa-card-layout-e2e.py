#!/usr/bin/env python3
"""Repeatable before/after render of Qendresa item 15 (card size and place) on the phone engine.

Qendresa (@qbuilder) shows screenshots and screen recordings over her talking head up to the
full width, in the TOP band (above her face) or the BOTTOM band (below her captions), never on
her face or captions; a named screenshot may open the video and a recording starts at its best
moment. Behind `settings.talking_card_layout_enabled` + `cardLayout` verified +
`creator_client_protocol >= 40`; off draws today's small top-right cards.

Run from the repo root (any worktree):

  src/apps/api/.venv/bin/python scripts/ios/qendresa-card-layout-e2e.py \
    prepare OUT --foot FOOT_DIR
  KRIA_E2E_DIR=OUT xcodebuild test ... \
    -only-testing:KriaTests/DeviceTalkingCardLayoutRenderE2ETests/testRendersEveryPreparedCardLayoutCase
  src/apps/api/.venv/bin/python scripts/ios/qendresa-card-layout-e2e.py score OUT --kit KIT_DIR

prepare: FOOT_DIR holds `take.mp4` (1080x1920 talking head), `words.json` (whisper.cpp
`-ml 1 -sow -oj`), `faces.json` (Apple Vision face boxes every 0.25 s) and
`visuals/visuals.json` + the screenshot / recording files. Runs
`tests/tasks/test_qendresa_card_layout_device_cases.py` (the REAL worker chain, no provider
calls, free heuristic card matcher) twice on the same footage: `q1_before` (layout off,
`cardLayout` unverified) and `q1_after` (on, verified, protocol 40). Writes
`cases/<name>/{recipe.json,assets.json,cards.json}` and prints one summary line per case.
Footage is never copied; assets.json points at the real files.

score: runs the kit's `tools/check_result.sh MP4 Q1 --ignore-tail-s 1.6` on each
`cases/<name>/phone.mp4`, writes `result.json`, and asserts the done check: `screen` and `hook`
FAIL before and PASS after, `full_speaker` PASSES after, and no rule that passed before fails
after. Plus an independent check on the RENDERED frames (the kit's per-frame Apple Vision faces,
2 fps): no card box ever intersects the largest face while the card is on screen. A negative
control moves each after-card onto the frame's face; it must report overlaps, else the check
is broken and the run fails.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
API = REPO / "src/apps/api"
VENV_PYTHON = Path(
    "/Users/yasinberkyesilyurt/projects/nova/src/apps/api/.venv/bin/python"
)
CASES = ("q1_before", "q1_after")
IGNORE_TAIL_S = "1.6"
FPS = 2.0
MUST_FLIP = ("screen", "hook")
TEST_ENV = {
    "STORAGE_BUCKET": "nova-test",
    "STORAGE_PROVIDER": "gcs",
    "OPENAI_API_KEY": "sk-test",
    "TOKEN_ENCRYPTION_KEY": "x",
    "DATABASE_URL": "postgresql://yasinberkyesilyurt@localhost:5432/nova_test",
    "ALLOWED_ORIGINS": '["http://localhost:3000"]',
}


# ---------------------------------------------------------------------------- prepare


def summarize(cards: list[dict[str, Any]]) -> str:
    if not cards:
        return "0 cards"
    widths = [c["width"] for c in cards]
    top = sum(c["y"] < 1 / 3 for c in cards)
    bottom = sum(c["y"] > 2 / 3 for c in cards)
    first = min(c["window_start"] for c in cards)
    recs = [c["source_start"] for c in cards if c["kind"] == "video"]
    return (
        f"{len(cards)} cards, width {min(widths):.2f}-{max(widths):.2f}, top {top}, "
        f"bottom {bottom}, first at {first:.2f}s, recording source_start {recs}"
    )


def prepare(out: Path, foot: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        **TEST_ENV,
        "KRIA_CARD_E2E_OUT": str(out.resolve()),
        "KRIA_CARD_E2E_FOOT": str(foot.resolve()),
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
        "tests/tasks/test_qendresa_card_layout_device_cases.py",
    ]
    code = subprocess.run(command, cwd=API, env=env, check=False).returncode
    if code != 0:
        return code
    for name in CASES:
        cards = json.loads((out / "cases" / name / "cards.json").read_text())["cards"]
        print(f"{name}: {summarize(cards)}")
    print(f"Prepared {out}")
    return 0


# ---------------------------------------------------------------------------- score


def run_check(kit: Path, movie: Path) -> dict[str, Any]:
    subprocess.run(
        [
            str(kit / "tools/check_result.sh"),
            str(movie),
            "Q1",
            "--ignore-tail-s",
            IGNORE_TAIL_S,
        ],
        check=False,
    )
    return json.loads((movie.with_suffix(".check") / "report.json").read_text())


def load_frames(movie: Path) -> list[list[list[float]]]:
    """Per output frame (2 fps): the face boxes [x, y, w, h] (normalized, top-left origin)."""
    path = movie.with_suffix(".check") / "frames.jsonl"
    return [json.loads(line).get("faces", []) for line in path.read_text().splitlines()]


def card_box(card: dict[str, Any]) -> tuple[float, float, float, float]:
    return (
        card["x"] - card["width"] / 2,
        card["y"] - card["height"] / 2,
        card["x"] + card["width"] / 2,
        card["y"] + card["height"] / 2,
    )


def face_overlaps(
    cards: list[dict[str, Any]],
    frames: list[list[list[float]]],
    *,
    onto_face: bool = False,
) -> tuple[int, int, list[str]]:
    """(frames checked, frames with an overlap, descriptions). `onto_face` is the control."""
    checked = hits = 0
    notes: list[str] = []
    for index, faces in enumerate(frames):
        t = index / FPS
        if not faces:
            continue
        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
        for card in cards:
            if not card["window_start"] <= t < card["window_end"]:
                continue
            left, top, right, bottom = card_box(card)
            if onto_face:  # move the card's centre onto the face's centre
                dx, dy = x + w / 2 - card["x"], y + h / 2 - card["y"]
                left, right, top, bottom = left + dx, right + dx, top + dy, bottom + dy
            checked += 1
            if left < x + w and x < right and top < y + h and y < bottom:
                hits += 1
                notes.append(f"t={t:.1f}s {card['media_key']}")
    return checked, hits, notes


def score(out: Path, kit: Path) -> int:
    results: dict[str, Any] = {}
    failures: list[str] = []
    frames_by_case: dict[str, list] = {}
    for name in CASES:
        movie = out / "cases" / name / "phone.mp4"
        if not movie.exists():
            failures.append(f"{name}: no phone.mp4 (render it first)")
            continue
        report = run_check(kit, movie)
        frames_by_case[name] = load_frames(movie)
        results[name] = {
            "passed": sum(bool(r["pass"]) for r in report["rules"]),
            "total": len(report["rules"]),
            "rules": {
                r["id"]: {"pass": bool(r["pass"]), "measured": r.get("measured")}
                for r in report["rules"]
            },
        }
    before, after = results.get("q1_before"), results.get("q1_after")
    if before and after:
        for rid in MUST_FLIP:
            if before["rules"].get(rid, {}).get("pass", True):
                failures.append(f"{rid} should FAIL before")
            if not after["rules"].get(rid, {}).get("pass", False):
                failures.append(f"{rid} should PASS after")
        if not after["rules"].get("full_speaker", {}).get("pass", False):
            failures.append("full_speaker should PASS after")
        for rid, rule in before["rules"].items():
            if rule["pass"] and not after["rules"].get(rid, {}).get("pass", False):
                failures.append(f"{rid} passed before and fails after")

    faces: dict[str, Any] = {}
    for name, frames in frames_by_case.items():
        cards = json.loads((out / "cases" / name / "cards.json").read_text())["cards"]
        checked, hits, notes = face_overlaps(cards, frames)
        faces[name] = {
            "card_frames_checked": checked,
            "overlaps": hits,
            "where": notes[:20],
        }
        if name == "q1_after":
            if checked == 0:
                failures.append(
                    "face check: no frame with both a card and a face was checked"
                )
            if hits:
                failures.append(
                    f"q1_after: {hits} frame(s) with a card on her face: {notes[:5]}"
                )
            c_checked, c_hits, _ = face_overlaps(cards, frames, onto_face=True)
            faces["negative_control"] = {
                "card_frames_checked": c_checked,
                "overlaps": c_hits,
            }
            if c_checked == 0 or c_hits == 0:
                failures.append(
                    "negative control did not report overlaps: face check is broken"
                )
    (out / "result.json").write_text(
        json.dumps(
            {"cases": results, "faces": faces, "failures": failures},
            indent=2,
            default=str,
        )
    )
    print(f"{'case':10} {'rule':14} {'pass':5} measured")
    for name, data in results.items():
        for rid, rule in data["rules"].items():
            print(f"{name:10} {rid:14} {rule['pass']!s:5} {rule['measured']}")
        print(f"{name:10} {'TOTAL':14} {data['passed']}/{data['total']}")
    for key, row in faces.items():
        print(
            f"face check {key}: {row['overlaps']} overlap(s) in {row['card_frames_checked']} frames"
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
