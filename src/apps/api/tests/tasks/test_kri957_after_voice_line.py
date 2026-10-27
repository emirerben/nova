"""KRI-957: "After my last words "before we eat", play Atakan's "Super excited!"" was refused.

Don Julio walk-in N1 (thread 99627FBC, 2026-10-26). The Main Creator (v98) recorded the after-voice
ask on Atakan's clip with `triggers: ["before we eat"]`, the creator's own last voiceover words,
and never the line said in the clip. The draft then searched Atakan's clip for "before we eat",
found nothing and refused: "I couldn't find "before we eat" in that clip". The fixture holds the
requirement exactly as prod stored it and the clip's real analysis.

Failure modes:

- FM1 the shared brief section leaves "the line" ambiguous when the creator quotes both their
  own last words and the clip's line, so the model records the voiceover words (the bug; v99
  teaches the split with this very request).
- FM2 with the line recorded as taught, the draft or the render still fails to find it in the
  clip's real analysis (an analysis that heard "Farille" and "meet" elsewhere in the clip).
- FM3 the render plays the clip on but cuts the line, or plays it under the voice.
- FM4 the incident recording is silently "fixed" by the server reading wording (KRI-667): a
  trigger the clip does not say must stay an honest refusal that names it, never a guess.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.tasks.generative_build as gb
from app.agents.brief_extractor import _NARROW_BRIEF_SECTION
from app.agents.main_creator import _BRIEF_PROMPT_SECTION
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import build_receipts, plan_facts_from_strategy
from app.services.phone_sources import PHONE_SOURCES_FIELD
from app.tasks import kria_runtime
from tests.pipeline.test_phone_voiceover_montage_plan import _binding
from tests.tasks.test_generative_build import _Meta
from tests.tasks.test_kri842_narrated_clip_sound import run, video_clips
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _mock_alignment_agent,
    _setup_narrated,
    _words,
)

N1 = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "kri957_donjulio_n1_after_voice.json").read_text(
        "utf-8"
    )
)
CLIPS = [(u["media_id"], u["duration_s"]) for u in N1["uploads"]]
STARTS = N1["step_starts_s"]
VOICE_S = N1["voiceover"]["played_duration_s"]
LINE = N1["line"]
RECORDED = N1["recorded_requirement"]


def _snapshot() -> dict:
    rows = []
    for media_id, duration_s in CLIPS:
        row = {
            "media_id": media_id,
            "kind": "video",
            "gcs_path": f"user/analysis-proxy-{media_id}.mp4",
            "duration_s": duration_s,
        }
        if media_id == LINE["media_id"]:
            row["analysis"] = {
                "understanding": {"speech": {"segments": N1["atakan_speech_segments"]}}
            }
        rows.append(row)
    return {"clip_assignments": rows}


def _requirement(triggers: list[str]) -> BriefRequirement:
    return BriefRequirement(
        id=RECORDED["id"],
        kind=RECORDED["kind"],
        scope=RECORDED["scope"],
        description=RECORDED["description"],
        facts={**RECORDED["facts"], "triggers": triggers},
    )


def _setup(monkeypatch, requirement: BriefRequirement):
    words = _words(
        *[
            word
            for k, start in enumerate(STARTS)
            for word in ((f"w{k}a", start + 0.0, start + 0.4), (f"w{k}b", start + 0.5, start + 1.0))
        ]
    )
    job, snapshot, _session, _ = _setup_narrated(
        monkeypatch,
        edit_format="narrated_ready",
        n_clips=len(CLIPS),
        spoken_words=words,
        voiceover_s=VOICE_S,
        real_phrases=True,
        alignment=True,
    )
    # The 4.0 s sign clip holds the 4.15 s first step, so it plays a touch slower; prod verifies
    # `variableSpeed` (phone-render-config, 2026-10-25), the test default does not.
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [*gb.settings.phone_render_verified_features, "variableSpeed"],
    )
    bindings = tuple(_binding(mid, duration_s=d) for mid, d in CLIPS)
    snapshot[PHONE_SOURCES_FIELD] = [b.model_dump(mode="json") for b in bindings]
    job.assembly_plan[PHONE_SOURCES_FIELD] = snapshot[PHONE_SOURCES_FIELD]
    monkeypatch.setattr(
        gb,
        "_ingest_clips",
        lambda *a, **k: {
            "clip_metas": [_Meta(f"c{i}", 5.0) for i in range(len(bindings))],
            "clip_id_to_gcs": {f"c{i}": b.proxy_path for i, b in enumerate(bindings)},
            "clip_id_to_local": {f"c{i}": f"/tmp/c{i}.mp4" for i in range(len(bindings))},
            "probe_map": {},
            "hero": _Meta("c0", 5.0),
        },
        raising=False,
    )
    _mock_alignment_agent(
        monkeypatch, [f"c{i}" for i in range(len(bindings))], [2 * k for k in range(len(STARTS))]
    )
    binding = BriefBinding.create(
        uuid.uuid4(),
        CreativeBrief(version=1, requirements=[requirement]),
        media_snapshot=_snapshot(),
    )
    snapshot["creator_brief_binding"] = binding.model_dump(mode="json")
    job.assembly_plan["creator_brief_binding"] = snapshot["creator_brief_binding"]
    return job, snapshot, binding


def _draft(requirement: BriefRequirement):
    facts = dataclasses.replace(
        plan_facts_from_strategy(
            {"edit_format": "narrated_planned", "audio_strategy": "voiceover"},
            clip_ids=[mid for mid, _ in CLIPS],
        ),
        phone_narrated=True,
        media_snapshot=_snapshot(),
    )
    [receipt] = build_receipts([requirement], facts, include_unchecked=True)
    return receipt


def test_the_brief_section_teaches_the_voice_end_is_not_the_clip_line():
    """FM1: both prompts that write the brief carry the split, with the incident request."""
    for section in (_BRIEF_PROMPT_SECTION, _NARROW_BRIEF_SECTION):
        flat = " ".join(section.split())
        assert "the creator's own voiceover words that mark where the voice ends" in flat
        assert (
            'After my last words "before we eat", play Atakan\'s "Super excited!" with its own '
            'sound" => scope clip:<Atakan\'s clip\'s media_id>, {"clip_sound": "after_voice", '
            '"triggers": ["Super excited!"]}'
        ) in flat


def test_the_line_said_in_the_clip_plays_after_the_voice(monkeypatch):
    """FM2 + FM3: recorded as v99 teaches, the real analysis grounds the line, the draft says Done,
    the render plays Atakan's clip on with its sound over the line, and the review says Done."""
    requirement = _requirement([LINE["text"]])
    assert _draft(requirement).status == "met"

    job, snapshot, binding = _setup(monkeypatch, requirement)
    recipe, variant = run(job, snapshot)

    tail = video_clips(recipe)[-1]
    assert tail.source_asset_id == LINE["media_id"] and tail.volume == 1.0
    assert tail.timeline_start == pytest.approx(VOICE_S, abs=0.01)
    after = variant["clip_sound"]["after_voice"]
    assert (after["status"], after["line"]) == ("built", LINE["text"])
    # The independent window (kit whisper, 5.9-6.7 s) overlaps the line the tail holds whole.
    tail_end = tail.source_start + tail.source_duration
    assert tail.source_start <= after["line_start_s"] < after["line_end_s"] <= tail_end
    assert after["line_start_s"] < LINE["kit_end_s"] and after["line_end_s"] > LINE["kit_start_s"]

    thread = SimpleNamespace(id=uuid.UUID(binding.thread_id), creator_id=uuid.uuid4())
    text, payload = kria_runtime._approved_generation_review(
        None, thread, job, variant, SimpleNamespace(result={}), "Ready."
    )
    [row] = [r for r in payload if r["requirement_id"] == RECORDED["id"]]
    assert row["status"] == "met"
    assert '"Super excited!" is heard' in row["reason"]
    assert "Couldn't" not in text


def test_the_incident_recording_stays_an_honest_refusal(monkeypatch):
    """FM4 (wrong-content control): the requirement exactly as prod stored it is not rescued by
    reading wording; it is refused before render, names the words, and builds no tail."""
    requirement = _requirement(RECORDED["facts"]["triggers"])
    draft = _draft(requirement)
    assert draft.status == "not_possible"
    assert 'I couldn\'t find "before we eat" in that clip' in draft.reason

    job, snapshot, _binding_ = _setup(monkeypatch, requirement)
    recipe, variant = run(job, snapshot)
    assert video_clips(recipe)[-1].timeline_start < VOICE_S
    assert variant["clip_sound"]["after_voice"]["status"] != "built"
