#!/usr/bin/env python3
"""Render the narrated archetype locally — no Celery, no GCS, no cloud keys.

Mirrors the `narrated_ready` branch of `_render_narrated_variant`
(transcribe → auto-segment → reflow short clips → burn captions → mix voice)
so you can eyeball captions + no-freeze + voiceover on this machine before the
full pipeline runs. Transcription uses the LOCAL faster-whisper backend, so no
OpenAI key is required.

Setup (one time):
    pip install faster-whisper

Run:
    cd src/apps/api
    python scripts/narrated_local_render.py \
        --voiceover ~/Downloads/Spurs.m4a \
        --clips ~/Downloads/IMG_0075.MOV ~/Downloads/IMG_0087.MOV ~/Downloads/IMG_0088.MOV \
        --out /tmp/narrated_out.mp4

Then open /tmp/narrated_out.mp4. You should see plain captions of your spoken
words, clips that fill the full voiceover length (no freeze), and your voice on top.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile

# Force the offline whisper backend BEFORE importing app.config (pydantic reads
# env at settings construction). Keeps this runnable without an OpenAI key.
os.environ.setdefault("WHISPER_BACKEND", "local")
os.environ.setdefault("TRANSCRIBER_BACKEND", "whisper")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--voiceover", required=True, help="voiceover audio (m4a/wav/mp3)")
    ap.add_argument("--clips", required=True, nargs="+", help="video clips, in order")
    ap.add_argument("--out", default="/tmp/narrated_out.mp4", help="output mp4 path")
    ap.add_argument(
        "--bed-level",
        type=float,
        default=None,
        help="original-audio bed under the voice: 0=off, 1=loudest (default: Nova's level)",
    )
    args = ap.parse_args()

    for p in [args.voiceover, *args.clips]:
        if not os.path.exists(p):
            print(f"ERROR: not found: {p}", file=sys.stderr)
            return 1

    try:
        from faster_whisper import WhisperModel  # noqa: F401
    except ImportError:
        print(
            "ERROR: faster-whisper not installed. Run: pip install faster-whisper",
            file=sys.stderr,
        )
        return 1

    from app.pipeline.narrated_alignment import StepTiming
    from app.pipeline.narrated_assembler import NarratedClip, assemble_narrated
    from app.pipeline.phrase_sequence import split_phrases
    from app.pipeline.transcribe import transcribe_whisper

    print(f"[1/4] transcribing {os.path.basename(args.voiceover)} (local whisper)…")
    transcript = transcribe_whisper(args.voiceover)
    words = transcript.words
    if len(words) < 2:
        print(f"ERROR: transcript too short ({len(words)} words)", file=sys.stderr)
        return 1
    print(f"      {len(words)} words: {transcript.full_text[:120]}…")

    # ── narrated_ready segmentation (mirrors _render_narrated_variant) ─────────
    total_s = max((w.end_s for w in words), default=60.0)
    phrases = split_phrases(words, video_duration_s=total_s)
    n_clips = len(args.clips)
    target_count = max(2, min(n_clips, len(phrases)))

    if len(phrases) > target_count:
        speech_start = phrases[0]["speech_start_s"]
        speech_end = phrases[-1]["speech_end_s"]
        bucket_dur = max(speech_end - speech_start, 0.1) / target_count
        buckets: list[dict] = []
        bucket_open = phrases[0].copy()
        for p in phrases[1:]:
            if (p["speech_end_s"] - bucket_open["speech_start_s"]) >= bucket_dur and len(
                buckets
            ) < target_count - 1:
                buckets.append({**bucket_open, "speech_end_s": bucket_open["speech_end_s"]})
                bucket_open = p.copy()
            else:
                bucket_open = {**bucket_open, "speech_end_s": p["speech_end_s"]}
        buckets.append(bucket_open)
        phrases = buckets

    step_timings = [
        StepTiming(
            step_id=f"seg_{i}",
            start_s=p["speech_start_s"],
            end_s=p["speech_end_s"],
            confidence=1.0,
        )
        for i, p in enumerate(phrases)
    ]
    clip_assignments = [
        NarratedClip(step_id=t.step_id, clip_path=args.clips[i % n_clips])
        for i, t in enumerate(step_timings)
    ]
    print(
        f"[2/4] {len(step_timings)} segments over {n_clips} clips; "
        f"durations={[round(t.end_s - t.start_s, 1) for t in step_timings]}"
    )

    bed_desc = "Nova default" if args.bed_level is None else f"{args.bed_level:.2f}"
    print(f"[3/4] assembling (reflow + captions + voice + ducked bed @ {bed_desc})…")
    with tempfile.TemporaryDirectory() as tmp:
        assemble_narrated(
            step_timings,
            clip_assignments,
            args.voiceover,
            args.out,
            tmp,
            transcript=transcript,
            bed_level=args.bed_level,
        )

    print(f"[4/4] done → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
