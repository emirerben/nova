"""Render one local clip through the REAL speech-cleanup engine and cut it.

Operator/dev tool for listening to what a detector change actually does before
it ships (the 2026-09-08 investigation had only plan arithmetic to look at).
It runs the exact production analysis boundary — the task's bounded 16 kHz
PCM extraction, ``transcribe_whisper`` with the verbatim prompt, FFmpeg
``silencedetect``, ``analyze_speech_cleanup`` (V1 baseline + V2 candidate,
``clamp`` policy) — then applies the selected plan's ``keep_segments`` to the
source video+audio with one trim/atrim+concat graph.  No captions, music bed,
or reframe: the artifact isolates the cut engine so a human can A/B it.

The clamp budget's fraction cap defaults to this process's
``SPEECH_CLEANUP_MAX_REMOVAL_FRAC_REQUIRED`` (1.0 since 2026-09-08 — MIN_OUTPUT_S
is then the only rail), so the preview matches what prod would actually cut.
``--max-removal-frac`` overrides it to audition a rollback value (e.g. 0.55)
without touching the environment.

Usage (from ``src/apps/api``; needs OPENAI_API_KEY + WHISPER_BACKEND=openai-api):

    python3 -m scripts.speech_cleanup_preview IN.mp4 --out OUT.mp4 [--plan plan.json]
        [--mixed-gap-mode apply|shadow|off] [--policy clamp|bailout]
        [--max-removal-frac 0.55]

Point PYTHONPATH at another checkout of ``src/apps/api`` to render the same
clip with a different detector build for comparison.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from app.pipeline.probe import probe_video
from app.pipeline.speech_cleanup_analysis import (
    SpeechCleanupAnalysisInput,
    analyze_speech_cleanup,
)

SAMPLE_RATE_HZ = 16_000  # mirrors app.tasks.speech_cleanup_analysis
CHANNELS = 1


def _extract_task_pcm(source: Path, wav: Path, duration_s: float) -> None:
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        "0.000000",
        "-i",
        str(source),
        "-t",
        f"{duration_s:.6f}",
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        str(CHANNELS),
        "-ar",
        str(SAMPLE_RATE_HZ),
        "-c:a",
        "pcm_s16le",
        "-threads",
        "1",
        "-y",
        str(wav),
    ]
    subprocess.run(command, check=True)


def _cut_video(source: Path, keep_segments: list[tuple[float, float]], out: Path) -> None:
    chains: list[str] = []
    labels: list[str] = []
    for index, (start_s, end_s) in enumerate(keep_segments):
        chains.append(
            f"[0:v]trim=start={start_s:.6f}:end={end_s:.6f},setpts=PTS-STARTPTS[v{index}]"
        )
        chains.append(
            f"[0:a]atrim=start={start_s:.6f}:end={end_s:.6f},asetpts=PTS-STARTPTS[a{index}]"
        )
        labels.append(f"[v{index}][a{index}]")
    chains.append(f"{''.join(labels)}concat=n={len(keep_segments)}:v=1:a=1[outv][outa]")
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-filter_complex",
        ";".join(chains),
        "-map",
        "[outv]",
        "-map",
        "[outa]",
        "-c:v",
        "libx264",
        # Final-output encode: never ultrafast (CLAUDE.md encoder policy).
        "-preset",
        "fast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        "-y",
        str(out),
    ]
    subprocess.run(command, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--plan", type=Path, default=None, help="write the analysis JSON here")
    parser.add_argument("--mixed-gap-mode", choices=("apply", "shadow", "off"), default="apply")
    parser.add_argument("--policy", choices=("clamp", "bailout"), default="clamp")
    parser.add_argument(
        "--max-removal-frac",
        type=float,
        default=None,
        help=(
            "Fraction-of-runtime cap on the explicit-consent clamp budget. "
            "Defaults to SPEECH_CLEANUP_MAX_REMOVAL_FRAC_REQUIRED so the preview "
            "matches production; pass 0.55 to audition the rollback value."
        ),
    )
    args = parser.parse_args(argv)
    max_removal_frac = args.max_removal_frac
    if max_removal_frac is None:
        from app.config import settings

        max_removal_frac = settings.speech_cleanup_max_removal_frac_required
    if not 0 < float(max_removal_frac) <= 1:
        parser.error("--max-removal-frac must be in (0, 1]")

    probe = probe_video(str(args.source))
    duration_s = float(probe.duration_s)
    with tempfile.TemporaryDirectory(prefix="speech-cleanup-preview-") as tmp:
        wav = Path(tmp) / "source.wav"
        _extract_task_pcm(args.source, wav, duration_s)
        result = analyze_speech_cleanup(
            SpeechCleanupAnalysisInput(
                source_fingerprint=f"preview:{args.source.name}",
                local_media_path=str(wav),
                duration_s=duration_s,
                source_window_start_s=0.0,
                source_window_end_s=duration_s,
                mixed_gap_mode=args.mixed_gap_mode,
                include_silence_and_fillers=True,
                over_budget_policy=args.policy,
                max_removal_frac_required=float(max_removal_frac),
            )
        )
    plan = result.cut_plan
    keep = [(segment.start_s, segment.end_s) for segment in plan.keep_segments]
    if args.plan is not None:
        args.plan.write_text(json.dumps(result.to_payload(), ensure_ascii=False, indent=1))
    _cut_video(args.source, keep, args.out)
    print(
        json.dumps(
            {
                "source": str(args.source),
                "out": str(args.out),
                "duration_s": duration_s,
                "detector_version": result.detector_version,
                "max_removal_frac_required": float(max_removal_frac),
                "selected_plan": result.safety_signals.selected_plan,
                "candidate_status": result.safety_signals.candidate_status,
                # True means the budget bound the plan; with frac 1.0 that is
                # the MIN_OUTPUT_S floor, not a fraction-of-runtime rail.
                "clamped": plan.clamped,
                "clamp_budget_s": plan.clamp_budget_s,
                "time_saved_s": round(plan.time_saved_s, 3),
                "removed": [
                    [round(item.start_s, 3), round(item.end_s, 3), item.reason]
                    for item in plan.removed
                ],
                "words": [
                    [word.text, round(word.start_s, 2), round(word.end_s, 2)]
                    for word in result.timed_words
                ],
                "token_adjustments": result.diagnostics.get("token_adjustments"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
