"""CLI: cut a phone Talking take to the lines the creator quoted, exactly as the worker does,
and write what it kept -- BEFORE opening a PR that touches the start / end cut (KRI-790).

    cd src/apps/api
    python -m app.cli.verify_take_window --take tests/fixtures/kri790_merope_t1_take.json \\
        [--brief brief_v2] [--cleanup off|on] [--video ~/path/to/the/take.mp4]

``--take`` is a JSON file with the take's ``duration_s``, its word timings (``words``), its
timed sentences (``segments``) and the brief rows (``brief_v1`` / ``brief_v2``: id -> row), the
shape of ``tests/fixtures/kri790_merope_t1_take.json``. No model call, no network.

Outputs to ``--out`` (default ``.take-window-verify/``):
  - ``report.json`` -- the ask, the kept window and receipts (what the worker records), the first
    and last words kept, and what the draft tells the creator before approval.
  - with ``--video``: ``window.mp4`` (the kept parts joined, as the phone plays them) and
    ``first.jpg`` / ``last.jpg`` (its first and last frames). Watch it: it must open on the
    start line and stop right after the end line.

Exit code: 0 when every asked line was found and cut, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.kria.brief import BriefRequirement
from app.kria.brief_checks import PlanFacts, build_receipts
from app.kria.opening_cut_ask import estimate_take_window, take_window_ask
from app.pipeline.phone_opening_cut import apply_take_window
from app.pipeline.silence_cut import CutPlan, build_cut_plan, no_op_plan, remap_words

# A pause this long between words is what a cleanup of the take would cut (``--cleanup on``).
_CLEANUP_PAUSE_S = 0.6


def _cleanup_plan(words: list[dict[str, Any]], duration_s: float) -> CutPlan:
    gaps = [
        (a["end_s"], b["start_s"])
        for a, b in zip(words, words[1:], strict=False)
        if b["start_s"] - a["end_s"] >= _CLEANUP_PAUSE_S
    ]
    return build_cut_plan(words, gaps, duration_s)


def _cut_video(video: Path, keep: list[tuple[float, float]], out: Path) -> dict[str, Any]:
    parts = []
    for i, (start, end) in enumerate(keep):
        parts.append(
            f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,scale=540:-2[v{i}];"
            f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{i}];"
        )
    joined = "".join(f"[v{i}][a{i}]" for i in range(len(keep)))
    graph = "".join(parts) + f"{joined}concat=n={len(keep)}:v=1:a=1[v][a]"
    window = out / "window.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-filter_complex", graph]
        + ["-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "fast", "-crf", "23"]
        + ["-c:a", "aac", str(window)],
        check=True,
    )
    probed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0"]
        + [str(window)],
        check=True,
        capture_output=True,
        text=True,
    )
    duration = float(probed.stdout.strip())
    for name, seek in (("first.jpg", ["-ss", "0"]), ("last.jpg", ["-sseof", "-0.1"])):
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", *seek, "-i", str(window)]
            + ["-frames:v", "1", str(out / name)],
            check=True,
        )
    return {
        "path": str(window),
        "duration_s": round(duration, 3),
        "first_frame": str(out / "first.jpg"),
        "last_frame": str(out / "last.jpg"),
    }


def run(args: argparse.Namespace) -> int:
    take = json.loads(Path(args.take).read_text())
    duration = float(take["duration_s"])
    words = take["words"]
    reqs = [BriefRequirement.model_validate(row) for row in take[args.brief].values()]
    ask = take_window_ask(reqs)
    if ask is None:
        print("no start / end line in this brief", file=sys.stderr)
        return 1
    base = no_op_plan(duration) if args.cleanup == "off" else _cleanup_plan(words, duration)
    plan, start, end = apply_take_window(
        base,
        words,
        start_quote=ask.start_quote,
        end_quote=ask.end_quote,
        duration_s=duration,
        start_ids=ask.start_ids,
        end_ids=ask.end_ids,
    )
    snapshot = {
        "clip_assignments": [
            {
                "kind": "video",
                "duration_s": duration,
                "analysis": {
                    "duration_s": duration,
                    "understanding": {"speech": {"segments": take["segments"]}},
                },
            }
        ]
    }
    estimate = estimate_take_window(reqs, snapshot)
    facts = PlanFacts(
        edit_format="subtitled", phone_talking=True, take_duration_s=duration, take_window=estimate
    )
    draft = build_receipts(reqs, facts, include_unchecked=True, strict_order=True)
    kept_words = [w["text"] for w in remap_words(words, plan)]
    receipts = [r for r in (start, end) if r is not None]
    ok = all(r["status"] == "applied" for r in receipts)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "verdict": "PASS" if ok else "FAIL",
        "take": args.take,
        "brief": args.brief,
        "cleanup": args.cleanup,
        "start_quote": ask.start_quote,
        "end_quote": ask.end_quote,
        "keep_segments": [[round(s, 3), round(e, 3)] for s, e in plan.keep_segments],
        "kept_s": round(sum(e - s for s, e in plan.keep_segments), 3),
        "removed": [
            {"start_s": round(r.start_s, 3), "end_s": round(r.end_s, 3), "reason": r.reason}
            for r in plan.removed
        ],
        "opening_cut": start,
        "end_cut": end,
        "first_words": kept_words[:6],
        "last_words": kept_words[-6:],
        "draft_estimate": estimate.__dict__ if estimate is not None else None,
        "draft_receipts": {r.requirement_id: [r.status, r.reason] for r in draft},
    }
    if args.video:
        report["video"] = _cut_video(Path(args.video).expanduser(), plan.keep_segments, out)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in ("verdict", "keep_segments", "kept_s")}))
    print(f"wrote {out / 'report.json'}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--take", required=True, help="take JSON (words, segments, brief rows)")
    parser.add_argument("--brief", default="brief_v2", help="which brief rows to read")
    parser.add_argument("--cleanup", choices=("off", "on"), default="off")
    parser.add_argument("--video", help="the take's video file, to write window.mp4")
    parser.add_argument("--out", default=".take-window-verify")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
