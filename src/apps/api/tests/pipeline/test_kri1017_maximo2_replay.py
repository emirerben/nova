"""KRI-1017 incident replay: Plaza de Mayo "maximo 2" speech cleanup.

The fixture holds the job's recorded word timings (placeholder text) and the silence spans
the detector saw on the source. Ways this replay could mislead, written before it:

- the planner no longer reproduces the live cuts, so a passing guard test would be judging
  a different plan than the one that shipped (pinned by the first test);
- the voice/pause labels came from the planner's own transcript (they did not: spectrograms
  plus an independent faster-whisper pass on the source audio, see the fixture's "about");
- the guard passes by dropping everything (the real-audio test also requires every pause
  cut to survive);
- the real-audio test silently runs on a different file (it checks the source hash).

The real-audio test needs the kit file, which never goes in git:
``KRI1017_SOURCE=/path/to/T2_translate_1810_casa_rosada_landscape.mp4 pytest <this file>``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from app.pipeline.audio_pcm import decode_pcm_f32
from app.pipeline.silence_cut import build_cut_plan
from app.pipeline.speech_voice_guard import guard_audible_speech

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "kri1017_maximo2_cleanup.json").read_text()
)
SOURCE = os.environ.get("KRI1017_SOURCE", "")


def _span(cut) -> tuple[float, float]:
    return (round(cut.start_s, 3), round(cut.end_s, 3))


def _plan():
    return build_cut_plan(
        FIXTURE["words"],
        [tuple(span) for span in FIXTURE["silence_spans"]],
        FIXTURE["duration_s"],
        mixed_gap_enabled=True,
        over_budget_policy="clamp",
    )


def _labelled(label: str) -> set[tuple[float, float]]:
    return {(cut["start_s"], cut["end_s"]) for cut in FIXTURE["cuts"] if cut["label"] == label}


def test_unguarded_planner_reproduces_the_live_cuts_including_six_over_a_voice():
    plan = _plan()

    assert [(*_span(r), r.reason) for r in plan.removed] == [
        (cut["start_s"], cut["end_s"], cut["reason"]) for cut in FIXTURE["cuts"]
    ]
    live_voiced = {
        (cut["start_s"], cut["end_s"])
        for cut in FIXTURE["cuts"]
        if cut["live"] and cut["label"] == "voice"
    }
    assert len(live_voiced) == 6


@pytest.mark.skipif(
    not SOURCE or shutil.which("ffmpeg") is None,
    reason="needs the kit source in KRI1017_SOURCE and ffmpeg",
)
def test_voice_guard_gives_back_every_voice_and_keeps_every_pause_on_the_real_take():
    digest = hashlib.sha256(Path(SOURCE).read_bytes()).hexdigest()
    assert digest.startswith(FIXTURE["source_sha256_prefix"]), "not the KRI-1017 kit file"

    guarded, report = guard_audible_speech(_plan(), decode_pcm_f32(SOURCE))

    judged = {(e["start_s"], e["end_s"]) for e in [*report["dropped"], *report["trimmed"]]}
    assert judged == _labelled("voice")
    surviving = {_span(r) for r in guarded.removed}
    assert _labelled("pause") <= surviving
    voice = [span for cut in FIXTURE["cuts"] for span in cut["voice"]]
    for removal in guarded.removed:
        assert not any(removal.start_s < hi and removal.end_s > lo for lo, hi in voice), (
            f"{_span(removal)} still cuts a voice"
        )
