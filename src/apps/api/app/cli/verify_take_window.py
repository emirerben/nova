"""CLI: cut a phone Talking take to the lines the creator quoted, exactly as the worker does,
and write what it kept -- BEFORE opening a PR that touches the start / end cut (KRI-790).

    cd src/apps/api
    python -m app.cli.verify_take_window --take tests/fixtures/kri790_merope_t1_take.json \\
        [--brief brief_v2] [--cleanup off|on] [--video ~/path/to/the/take.mp4]

``--take`` is a JSON file with the take's ``duration_s``, its word timings (``words``), its
timed sentences (``segments``) and the brief rows (``brief_v1`` / ``brief_v2``: id -> row), the
shape of ``tests/fixtures/kri790_merope_t1_take.json``. No model call, no network. A row may
carry the lines picked for a quote the take doesn't hold (``facts.start_line_spoken`` /
``end_line_spoken``, KRI-844); they are cut exactly as the worker does.

Outputs to ``--out`` (default ``.take-window-verify/``):
  - ``report.json`` -- the ask, the kept window and receipts (what the worker records), the first
    and last words kept, and what the draft tells the creator before approval.
  - with ``--video``: ``window.mp4`` (the kept parts joined, as the phone plays them) and
    ``first.jpg`` / ``last.jpg`` (its first and last frames). Watch it: it must open on the
    start line and stop right after the end line.

KRI-724: ``--answer occ_2`` (``--spoken-text "MEROPE::Show MEROPE in big letters the first
time she says Merope"`` when the brief rows carry no such ask) runs the speech-occurrence path
too: the question the creator is asked before approval (built from ``segments``), their pick,
the answered brief, and where the worker places the text on the cut timeline (``words``). The
report gains ``speech_occurrence``; with ``--video`` also ``spoken_text.jpg``, the frame of
``window.mp4`` at the placed time.

Exit code: 0 when every asked line was found and cut (and, with ``--answer``, the picked
occurrence was placed), 1 otherwise.
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

_MEDIA_ID = "take"

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


def _speech_occurrence(
    args: argparse.Namespace,
    reqs: list[BriefRequirement],
    snapshot: dict[str, Any],
    words: list[dict[str, Any]],
    plan: CutPlan,
) -> tuple[dict[str, Any], bool]:
    """KRI-724: the question, the creator's pick and where the worker places the text."""
    from app.kria.spoken_text_ask import brief_spoken_texts  # noqa: PLC0415
    from app.pipeline.phone_spoken_text import find_pinned_word  # noqa: PLC0415
    from app.pipeline.speech_cut_state import remap_time  # noqa: PLC0415
    from app.services.choice_questions import (  # noqa: PLC0415
        CONFLICT_SPEECH_OCCURRENCE,
        ChoiceCapability,
        answered_brief,
        build_choice_question,
        choice_question_text,
        collect_conflicts,
        resolve_choices,
    )

    if args.spoken_text:
        literal, _sep, description = args.spoken_text.partition("::")
        reqs = [
            *reqs,
            BriefRequirement(
                id="spoken",
                kind="text",
                scope="global",
                literal=literal.strip(),
                description=(description or literal).strip(),
            ),
        ]
    from app.kria.brief import CreativeBrief  # noqa: PLC0415

    brief = CreativeBrief(version=1, requirements=reqs)
    strategy = {"edit_format": "subtitled", "audio_strategy": "original_audio"}
    cap = ChoiceCapability(speech_occurrence=True)
    asked = [
        c
        for c in collect_conflicts(strategy, brief, snapshot, cap)
        if c.kind == CONFLICT_SPEECH_OCCURRENCE
    ]
    if not asked:
        return {"verdict": "FAIL", "reason": "no speech_occurrence question"}, False
    choice = asked[0]
    key = next(
        (o.key for o in choice.options if o.key == args.answer or o.key.startswith(args.answer)),
        None,
    )
    if key is None:
        return {"verdict": "FAIL", "reason": f"no option {args.answer}"}, False
    question = build_choice_question(choice.candidate())
    events = [
        ("assistant", {"choice_question": question}),
        ("user", {"choice_selection": {"question_id": question["question_id"], "option_key": key}}),
    ]
    resolution = resolve_choices(strategy, brief, snapshot, events, cap)
    # Every answer so far (a later question, e.g. the length, may still be open).
    answers = [dict(a) for a in resolution.answers]
    final = answered_brief(brief, {**resolution.strategy, "choice_answers": answers})
    asks = [
        a for a in brief_spoken_texts(final, answers) if a.requirement_id in choice.requirement_ids
    ]
    report: dict[str, Any] = {
        "question": choice_question_text(choice.candidate()),
        "options": [o.key for o in choice.options],
        "answer": answers,
        "notices": list(resolution.notices),
        "next_question": resolution.question.kind if resolution.question else None,
    }
    if not asks or asks[0].pin is None:
        report.update(verdict="PASS" if key == "waive" else "FAIL", placed=None)
        return report, key == "waive"
    ask = asks[0]
    timeline = remap_words(words, plan)
    hit, missed = find_pinned_word(
        timeline, ask, to_timeline=lambda t: remap_time(t, list(plan.removed))
    )
    report["pin"] = ask.pin.__dict__
    if hit is None:
        report.update(verdict="FAIL", placed=None, reason=missed)
        return report, False
    trigger, start, end = hit
    report.update(
        verdict="PASS",
        placed={
            "trigger": trigger,
            "timeline_start_s": round(start, 3),
            "timeline_end_s": round(end, 3),
        },
        # Where the first match (KRI-791's placement) would have put it, for contrast.
        first_heard_timeline_s=next(
            (
                round(w["start_s"], 3)
                for w in timeline
                if w["text"].strip(",.!?").casefold() == ask.triggers[0].casefold()
            ),
            None,
        ),
    )
    return report, True


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
        # KRI-844: the lines the creator picked for quotes the take doesn't hold.
        start_spoken=ask.start_spoken,
        end_spoken=ask.end_spoken,
    )
    snapshot = {
        "clip_assignments": [
            {
                "media_id": _MEDIA_ID,
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
    if args.answer:
        report["speech_occurrence"], placed = _speech_occurrence(args, reqs, snapshot, words, plan)
        ok = ok and placed
        report["verdict"] = "PASS" if ok else "FAIL"
    if args.video:
        report["video"] = _cut_video(Path(args.video).expanduser(), plan.keep_segments, out)
        placed_at = ((report.get("speech_occurrence") or {}).get("placed") or {}).get(
            "timeline_start_s"
        )
        if placed_at is not None:
            frame = out / "spoken_text.jpg"
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{placed_at + 0.2:.3f}"]
                + ["-i", report["video"]["path"], "-frames:v", "1", str(frame)],
                check=True,
            )
            report["video"]["spoken_text_frame"] = str(frame)
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
    parser.add_argument(
        "--answer", help="KRI-724: the creator's pick, e.g. occ_2 (the second occurrence)"
    )
    parser.add_argument(
        "--spoken-text",
        help='KRI-724: a text-at-a-spoken-word ask, "LITERAL::description", when the brief '
        "rows carry none",
    )
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
