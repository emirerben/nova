"""KRI-842: one clip's own sound on a phone Voiceover edit, end to end.

Café N1 (real footage, measured with ffprobe and whisper): eight clips, a Cotopaxi photo, a
23.202 s voiceover and a brief of two asks: the bite clip's own sound faintly under the voice,
and, once the voice ends, the last clip playing on so Caro's "bye bye" is heard. Neither was
possible and neither was said: the Turkish wording had no checker, the English wording was
"met" by the global bed.

Drives `_run_phone_narrated_job` with the same fakes as `test_narrated_alignment_receipts.py`
and an approved brief binding carrying the two asks as the Main Creator now records them
(`facts.clip_sound`), then reads the pinned recipe and the finished video's receipts.
"""

from __future__ import annotations

import dataclasses
import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.tasks.generative_build as gb
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import (
    build_receipts,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
)
from app.pipeline.phone_clip_sound import ground_trigger_line
from app.services.device_render import device_status
from app.services.phone_sources import PHONE_SOURCES_FIELD
from app.services.phone_voiceover_timeline import (
    _STEP_ID,
    narrated_timings_and_assignments,
    source_pool_paths,
)
from app.tasks import kria_runtime
from tests.pipeline.test_phone_voiceover_montage_plan import _binding
from tests.tasks.test_generative_build import _Meta
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _mock_alignment_agent,
    _setup_narrated,
    _words,
)

# Café N1's measured facts live in a fixture (ffprobe / ffmpeg astats / local whisper-medium on the
# kit files, see its `provenance`): this test reads its expectations from there, not from numbers
# typed beside the code under test.
N1 = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "kri842_cafe_n1_narrated.json").read_text("utf-8")
)
N1_CLIPS = [(u["media_id"], u["duration_s"]) for u in N1["uploads"] if u["kind"] == "video"]
VOICE_S = N1["voiceover"]["duration_s"]
# Where each of the eight clips starts under the voice; the last one plays 21.0 -> 23.202.
STARTS = [0.0, 2.6, 5.4, 8.8, 13.0, 17.2, 19.4, 21.0]
# N1_08: "thank you x3, Caro", a pause, "bye bye", room tone.
BYE_BYE = (N1["line"]["start_s"], N1["line"]["end_s"])
CLIP_DURATION_S = dict(N1_CLIPS)

TURKISH_UNDER = N1["prompt"]["tr_under_voice"]
TURKISH_AFTER = N1["prompt"]["tr_after_voice"]


def _speech_segments(media_id: str, *, coarse: bool) -> list[dict]:
    """The analysis segments of a measured clip. ``coarse``: one segment from the first word to
    the last (what a coarse Gemini analysis can return), instead of one per spoken sentence."""
    spoken = [span for span in N1["speech"].get(media_id, []) if span["kind"] == "speech"]
    if coarse and len(spoken) > 1:
        return [
            {
                "start_s": spoken[0]["start_s"],
                "end_s": spoken[-1]["end_s"],
                "text": " ".join(span["text"] for span in spoken),
            }
        ]
    return [{k: span[k] for k in ("start_s", "end_s", "text")} for span in spoken]


def _row(media_id: str, duration_s: float | None, *, coarse: bool = False, kind="video") -> dict:
    row = {
        "media_id": media_id,
        "kind": kind,
        "gcs_path": f"user/analysis-proxy-{media_id}.mp4",
        **({"duration_s": duration_s} if duration_s else {}),
    }
    segments = _speech_segments(media_id, coarse=coarse)
    if segments:
        row["analysis"] = {"understanding": {"speech": {"segments": segments}}}
    return row


def n1_media_snapshot(*, coarse: bool = False) -> dict:
    """The approved media rows of the N1 job. ``coarse`` collapses N1_08's two spoken sentences
    into one analysis segment (0.8-4.4 "Thank you, ... Caro. Bye bye.")."""
    return {
        "clip_assignments": [
            *(_row(mid, d, coarse=coarse) for mid, d in N1_CLIPS),
            _row("N1_09", None, kind="image"),  # the Cotopaxi photo
        ]
    }


def under_voice_req(media_id: str = "N1_05", **facts) -> BriefRequirement:
    return BriefRequirement(
        id="r1",
        kind="audio",
        scope=f"clip:{media_id}",
        description=TURKISH_UNDER,
        facts={"clip_sound": "under_voice", **facts},
    )


def after_voice_req(
    media_id: str = "N1_08", triggers: list[str] | None = None, **facts
) -> BriefRequirement:
    return BriefRequirement(
        id="r2",
        kind="audio",
        scope=f"clip:{media_id}",
        description=TURKISH_AFTER,
        facts={
            "clip_sound": "after_voice",
            **({"triggers": triggers if triggers is not None else ["bye bye"]}),
            **facts,
        },
    )


def setup_n1(
    monkeypatch, *, requirements, starts=STARTS, extra_candidates=None, bound=True, coarse=False
):
    """The N1 job: eight real-length clips, a 23.202 s voiceover, the brief pinned on the job."""
    words = _words(
        *[
            word
            for k, start in enumerate(starts)
            for word in ((f"w{k}a", start + 0.0, start + 0.4), (f"w{k}b", start + 0.5, start + 1.0))
        ]
    )
    job, snapshot, session, _ = _setup_narrated(
        monkeypatch,
        edit_format="narrated_ready",
        n_clips=len(N1_CLIPS),
        spoken_words=words,
        voiceover_s=VOICE_S,
        real_phrases=True,
        alignment=True,
        extra_candidates=extra_candidates,
    )
    bindings = tuple(_binding(mid, duration_s=d) for mid, d in N1_CLIPS)
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
        monkeypatch,
        [f"c{i}" for i in range(len(bindings))],
        [2 * k for k in range(len(starts))],
    )
    binding = None
    if bound:
        binding = BriefBinding.create(
            uuid.uuid4(),
            CreativeBrief(version=3, requirements=list(requirements)),
            media_snapshot=n1_media_snapshot(coarse=coarse),
        )
        snapshot["creator_brief_binding"] = binding.model_dump(mode="json")
        job.assembly_plan["creator_brief_binding"] = snapshot["creator_brief_binding"]
    return job, snapshot, bindings, binding


def run(job, snapshot):
    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    assert job.status == "awaiting_device"
    recipe = device_status(job, "narrated").request.recipe
    [variant] = job.assembly_plan["variants"]
    return recipe, variant


def video_clips(recipe):
    return next(track for track in recipe.tracks if track.id == "narrated").clips


def receipts(brief_requirements, variant):
    return {
        r.requirement_id: r
        for r in build_receipts(
            brief_requirements, plan_facts_from_phone_variant(variant), include_unchecked=True
        )
    }


def test_n1_plays_the_bite_clip_under_the_voice_and_the_goodbye_after_it(monkeypatch):
    requirements = [under_voice_req(), after_voice_req()]
    job, snapshot, bindings, binding = setup_n1(monkeypatch, requirements=requirements)

    recipe, variant = run(job, snapshot)

    clips = video_clips(recipe)
    voiced = [c for c in clips if c.timeline_start < VOICE_S - 0.01]
    tail = clips[-1]
    # Only the bite clip's own sound reaches the mix under the voice (FM2), at the bed level.
    assert {c.source_asset_id for c in voiced if c.volume > 0} == {"N1_05"}
    assert [c.volume for c in voiced if c.source_asset_id == "N1_05"] == [pytest.approx(0.25)]
    assert recipe.audio.original_volume == 1.0
    assert not recipe.audio.duck_original_during_music  # the verified-feature set has no duck
    # The goodbye plays on after the voice: its own clip, full volume, holding the whole line.
    assert recipe.duration == pytest.approx(VOICE_S + (tail.source_duration), abs=0.01)
    assert recipe.duration > VOICE_S
    assert tail.source_asset_id == "N1_08" and tail.volume == 1.0 and tail.rate == 1.0
    assert tail.timeline_start == pytest.approx(VOICE_S, abs=0.01)
    assert (
        tail.source_start <= BYE_BYE[0] and tail.source_start + tail.source_duration >= BYE_BYE[1]
    )
    assert tail.source_start + tail.source_duration <= CLIP_DURATION_S["N1_08"]
    # ... and the part of N1_08 under the voice ends before the line starts (FM7).
    last_voiced = clips[-2]
    assert last_voiced.source_asset_id == "N1_08" and last_voiced.volume == 0
    assert last_voiced.source_start + last_voiced.source_duration <= BYE_BYE[0]
    assert last_voiced.source_start + last_voiced.source_duration == pytest.approx(
        tail.source_start, abs=0.01
    )
    # The voice keeps its own length and fade; no step was stretched past it (FM5, FM9).
    narration = next(t for t in recipe.tracks if t.id == "narration").clips[0]
    assert narration.source_duration == pytest.approx(VOICE_S)
    assert all(re.match(_STEP_ID, c.id) for c in clips)
    assert variant["duration_s"] == recipe.duration
    assert all(layer.end <= recipe.duration for layer in recipe.text_layers)
    # The editor timeline still projects: the tail is one more clip, after the last step.
    projected = narrated_timings_and_assignments(
        recipe, list(bindings), source_pool_paths(job.assembly_plan, job.all_candidates)
    )
    assert projected is not None
    timings = projected[0]
    assert len(timings) == len(N1_CLIPS) + 1
    assert timings[-1]["start_s"] == pytest.approx(VOICE_S, abs=0.01)
    assert variant["narrated_timings"] == timings

    # What was built is on the variant, read back from the pinned recipe.
    receipt = variant["clip_sound"]
    assert [(r["requirement_id"], r["media_id"], r["status"]) for r in receipt["under_voice"]] == [
        ("r1", "N1_05", "built")
    ]
    assert receipt["under_voice"][0]["gain"] == pytest.approx(0.25)
    after = receipt["after_voice"]
    assert (after["status"], after["media_id"], after["line"]) == ("built", "N1_08", "bye bye")
    assert after["tail_start_s"] == pytest.approx(VOICE_S, abs=0.01)
    assert after["tail_end_s"] == pytest.approx(recipe.duration, abs=0.01)
    assert after["source_start_s"] == pytest.approx(tail.source_start, abs=0.01)
    assert after["source_end_s"] == pytest.approx(
        tail.source_start + tail.source_duration, abs=0.01
    )

    # The finished video says Done for both, in the creator's own two sentences.
    thread = SimpleNamespace(id=uuid.UUID(binding.thread_id), creator_id=uuid.uuid4())
    text, payload = kria_runtime._approved_generation_review(
        None, thread, job, variant, SimpleNamespace(result={}), "Ready."
    )
    by_id = {row["requirement_id"]: row for row in payload}
    assert by_id["r1"]["status"] == "met" and by_id["r1"]["verification"] == "checked"
    assert by_id["r2"]["status"] == "met" and by_id["r2"]["verification"] == "checked"
    assert "softly under your voice" in by_id["r1"]["reason"]
    assert '"bye bye" is heard' in by_id["r2"]["reason"]
    assert "Done:" in text and "Couldn't" not in text

    # A tail needs no capability beyond what the same Voiceover recipe already asks for (FM11).
    baseline, _ = run(*setup_n1(monkeypatch, requirements=[], bound=False)[:2])
    assert recipe.required_capabilities == baseline.required_capabilities


def test_no_clip_sound_fact_leaves_the_recipe_exactly_as_it_was(monkeypatch):
    """FM13: a brief without the fact (the Turkish prose, as it arrived before) changes nothing."""
    plain = [
        BriefRequirement(id="r1", kind="audio", scope="clip:N1_05", description=TURKISH_UNDER),
        BriefRequirement(id="r2", kind="audio", scope="clip:N1_08", description=TURKISH_AFTER),
    ]
    recipe_with_prose, variant_with_prose = run(*setup_n1(monkeypatch, requirements=plain)[:2])
    recipe_without_brief, variant_without_brief = run(
        *setup_n1(monkeypatch, requirements=[], bound=False)[:2]
    )

    assert recipe_with_prose.model_dump(mode="json") == recipe_without_brief.model_dump(mode="json")
    assert recipe_with_prose.audio.original_volume == pytest.approx(0.25)
    assert all(c.volume == 1.0 for c in video_clips(recipe_with_prose))
    assert recipe_with_prose.duration == pytest.approx(VOICE_S)
    assert "clip_sound" not in variant_with_prose
    assert "clip_sound" not in variant_without_brief


def test_a_line_that_is_not_in_the_last_clip_builds_no_tail_and_says_so(monkeypatch):
    """FM6: the receipt never says met for a tail that is not there."""
    requirements = [under_voice_req(), after_voice_req(triggers=["see you tomorrow"])]
    job, snapshot, _bindings, _binding_ = setup_n1(monkeypatch, requirements=requirements)

    recipe, variant = run(job, snapshot)

    assert recipe.duration == pytest.approx(VOICE_S)
    assert not any(c.id.endswith("-tail") for c in video_clips(recipe))
    # The under-voice half is still built on its own.
    assert {c.source_asset_id for c in video_clips(recipe) if c.volume > 0} == {"N1_05"}
    after = variant["clip_sound"]["after_voice"]
    assert (after["status"], after["reason_code"]) == ("not_possible", "line_not_found")
    by_id = receipts(requirements, variant)
    assert by_id["r1"].status == "met"
    assert by_id["r2"].status == "not_possible" and by_id["r2"].verification == "checked"
    assert 'couldn\'t find "see you tomorrow"' in by_id["r2"].reason


def test_a_clip_that_is_not_the_last_cannot_play_on(monkeypatch):
    requirements = [after_voice_req(media_id="N1_05")]
    job, snapshot, _bindings, _binding_ = setup_n1(monkeypatch, requirements=requirements)

    recipe, variant = run(job, snapshot)

    assert recipe.duration == pytest.approx(VOICE_S)
    assert variant["clip_sound"]["after_voice"]["reason_code"] == "not_last_clip"
    receipt = receipts(requirements, variant)["r2"]
    assert receipt.status == "not_possible"
    assert receipt.reason.startswith("Only the last clip can play on after your voice")


def test_no_footage_left_after_the_voiced_part_builds_no_tail(monkeypatch):
    """FM8: the last step is as long as its clip; there is nothing left to play on."""
    long_last = [0.0, 2.2, 4.4, 6.6, 8.8, 11.0, 13.2, 18.7]  # N1_08 fills 4.5 of its 4.77 s
    requirements = [after_voice_req(triggers=[])]
    job, snapshot, _bindings, _binding_ = setup_n1(
        monkeypatch, requirements=requirements, starts=long_last
    )

    recipe, variant = run(job, snapshot)

    assert recipe.duration == pytest.approx(VOICE_S)
    assert variant["clip_sound"]["after_voice"]["reason_code"] == "no_room"
    assert receipts(requirements, variant)["r2"].status == "not_possible"


def test_a_goodbye_with_no_named_line_plays_on_for_the_rest_of_the_clip(monkeypatch):
    requirements = [after_voice_req(triggers=[])]
    job, snapshot, _bindings, _binding_ = setup_n1(monkeypatch, requirements=requirements)

    recipe, variant = run(job, snapshot)

    clips = video_clips(recipe)
    tail = clips[-1]
    assert tail.id.endswith("-tail") and tail.source_asset_id == "N1_08"
    assert 0.8 <= tail.source_duration <= 3.0
    # The voiced part of N1_08 (2.2 s) is followed by the rest of the clip, to its end.
    assert tail.source_start == pytest.approx(clips[-2].source_start + clips[-2].source_duration)
    assert tail.source_start + tail.source_duration <= CLIP_DURATION_S["N1_08"]
    # With no under-voice ask the voiced clips keep today's bed level.
    assert all(c.volume == pytest.approx(0.25) for c in clips[:-1])
    assert recipe.audio.original_volume == 1.0
    assert receipts(requirements, variant)["r2"].status == "met"


def test_a_photo_has_no_sound_to_play(monkeypatch):
    """FM14: asking for the Cotopaxi photo's own sound is a plain Couldn't, nothing changes."""
    requirements = [under_voice_req(media_id="N1_09")]
    job, snapshot, _bindings, _binding_ = setup_n1(monkeypatch, requirements=requirements)

    recipe, variant = run(job, snapshot)

    assert recipe.audio.original_volume == pytest.approx(0.25)
    assert all(c.volume == 1.0 for c in video_clips(recipe))
    row = variant["clip_sound"]["under_voice"][0]
    assert (row["status"], row["reason_code"]) == ("not_possible", "no_sound")
    receipt = receipts(requirements, variant)["r1"]
    assert receipt.status == "not_possible" and "no sound of its own" in receipt.reason


@pytest.mark.parametrize(
    ("bed", "gain"),
    [(0.4, 0.4), (0.0, 0.25)],
)
def test_the_faint_clip_uses_the_items_bed_or_the_default_when_it_is_zero(monkeypatch, bed, gain):
    """FM15."""
    requirements = [under_voice_req()]
    job, snapshot, _bindings, _binding_ = setup_n1(
        monkeypatch, requirements=requirements, extra_candidates={"voiceover_bed_level": bed}
    )

    recipe, variant = run(job, snapshot)

    assert [c.volume for c in video_clips(recipe) if c.volume > 0] == [pytest.approx(gain)]
    assert variant["clip_sound"]["under_voice"][0]["gain"] == pytest.approx(gain)


def test_the_ducked_bed_is_not_ducked_twice(monkeypatch):
    """FM4: with the device duck on, the faint clip sits at bed x 0.6 (today's resting level)
    and the tail, which the duck releases for once the voice is silent, stays at full volume."""
    requirements = [under_voice_req(), after_voice_req()]
    job, snapshot, _bindings, _binding_ = setup_n1(monkeypatch, requirements=requirements)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [*gb.settings.phone_render_verified_features, "audioDucking"],
    )

    recipe, _variant = run(job, snapshot)

    clips = video_clips(recipe)
    assert [c.volume for c in clips if 0 < c.volume < 1] == [pytest.approx(0.25 * 0.6)]
    assert clips[-1].volume == 1.0
    assert recipe.audio.duck_original_during_music is True
    assert "audioDucking" in recipe.required_capabilities


def test_a_second_ask_to_play_on_is_not_built(monkeypatch):
    second = BriefRequirement(
        id="r3",
        kind="audio",
        scope="clip:N1_08",
        description="And again at the end",
        facts={"clip_sound": "after_voice"},
    )
    requirements = [after_voice_req(), second]
    job, snapshot, _bindings, _binding_ = setup_n1(monkeypatch, requirements=requirements)

    recipe, variant = run(job, snapshot)

    assert sum(1 for c in video_clips(recipe) if c.id.endswith("-tail")) == 1
    by_id = receipts(requirements, variant)
    assert by_id["r2"].status == "met"
    assert by_id["r3"].status == "not_possible"
    assert by_id["r3"].reason.startswith("Only one clip can play on after your voice")


@pytest.mark.parametrize(("confirmed_s", "tail"), [(24.0, True), (22.0, False)])
def test_a_confirmed_length_the_tail_would_break_builds_no_tail(monkeypatch, confirmed_s, tail):
    """FM10: the render contract pins the length within ten percent; a bonus sound must never
    be what fails the whole render. 22 s: the 23.2 s voice meets it, voice + tail (24.8 s) does
    not."""
    import app.services.creator_render_contract as contract_mod

    requirements = [after_voice_req()]
    job, snapshot, _bindings, _binding_ = setup_n1(monkeypatch, requirements=requirements)
    monkeypatch.setattr(
        contract_mod, "read_render_contract", lambda _: SimpleNamespace(duration_s=confirmed_s)
    )

    recipe, variant = run(job, snapshot)

    assert any(c.id.endswith("-tail") for c in video_clips(recipe)) is tail
    after = variant["clip_sound"]["after_voice"]
    assert after["status"] == ("built" if tail else "not_possible")
    if not tail:
        assert after["reason_code"] == "length_target"
        assert recipe.duration == pytest.approx(VOICE_S)
        assert receipts(requirements, variant)["r2"].reason.startswith(
            "Playing that clip on after your voice would take the video past the length"
        )


def test_a_coarse_analysis_still_builds_the_tail_over_the_measured_line(monkeypatch):
    """A coarse clip analysis returns ONE segment for "Thank you x3, Caro. Bye bye." (0.8-4.4).
    Grounding that whole sentence as the line pushed the voiced part before 0.8 s and ended in
    "no footage left"; the line is narrowed to its place in the sentence, as the draft does."""
    requirements = [after_voice_req()]
    snapshot_rows = n1_media_snapshot(coarse=True)["clip_assignments"]
    [coarse_row] = [r for r in snapshot_rows if r["media_id"] == "N1_08"]
    assert [
        (s["start_s"], s["end_s"])
        for s in coarse_row["analysis"]["understanding"]["speech"]["segments"]
    ] == [(0.8, 4.4)]
    job, snapshot, _bindings, _binding_ = setup_n1(
        monkeypatch, requirements=requirements, coarse=True
    )

    recipe, variant = run(job, snapshot)

    tail = video_clips(recipe)[-1]
    assert tail.id.endswith("-tail") and tail.source_asset_id == "N1_08"
    assert (
        tail.source_start <= BYE_BYE[0] and tail.source_start + tail.source_duration >= BYE_BYE[1]
    )
    assert tail.source_start + tail.source_duration <= CLIP_DURATION_S["N1_08"]
    after = variant["clip_sound"]["after_voice"]
    assert after["status"] == "built"
    # The window is narrower than the whole sentence (0.8-4.4), so the voiced part still fits.
    assert after["line_start_s"] > 0.8
    assert receipts(requirements, variant)["r2"].status == "met"
    # The draft grounds the same line, so it never promises what the render then refuses.
    draft = dataclasses.replace(
        plan_facts_from_strategy(
            {"edit_format": "narrated_planned", "audio_strategy": "voiceover"},
            clip_ids=[mid for mid, _ in N1_CLIPS],
        ),
        phone_narrated=True,
        media_snapshot=n1_media_snapshot(coarse=True),
    )
    assert build_receipts(requirements, draft, include_unchecked=True)[0].status == "met"
    grounded = ground_trigger_line(["bye bye"], "N1_08", n1_media_snapshot(coarse=True))
    assert (grounded.start_s, grounded.end_s) == (after["line_start_s"], after["line_end_s"])


def test_a_sentence_split_analysis_keeps_the_exact_measured_line(monkeypatch):
    grounded = ground_trigger_line(["bye bye"], "N1_08", n1_media_snapshot())

    assert (grounded.status, grounded.start_s, grounded.end_s) == ("found", *BYE_BYE)


def test_a_line_said_too_early_for_the_voiced_part_has_its_own_reason(monkeypatch):
    """B: the last clip's voiced part is 3.5 s, longer than the 3.42 s the clip takes to get to
    the line: "bye bye" would start under the voice. Never "no footage left"."""
    too_long = [0.0, 2.6, 5.4, 8.8, 13.0, 16.5, 18.2, 19.7]
    requirements = [after_voice_req()]
    job, snapshot, _bindings, _binding_ = setup_n1(
        monkeypatch, requirements=requirements, starts=too_long
    )

    recipe, variant = run(job, snapshot)

    assert recipe.duration == pytest.approx(VOICE_S)
    assert variant["clip_sound"]["after_voice"]["reason_code"] == "line_too_early"
    reason = receipts(requirements, variant)["r2"].reason
    assert reason == '"bye bye" is said too early in that clip to come after your voice'
    assert "no footage left" not in reason


def test_a_compile_that_refuses_the_tail_still_makes_the_voiced_edit(monkeypatch):
    """C: an ask never costs the creator their video. The compile refuses a plan with a tail
    (and only then): the retry drops the tail, keeps the gains, and the receipt says Couldn't."""
    import app.pipeline.phone_narrated_plan as plan_mod
    from app.pipeline.phone_guided_plan import UnsupportedPhonePlan

    real = plan_mod.compile_phone_narrated_plan
    calls = []

    def refusing(*args, **kwargs):
        calls.append(kwargs.get("voice_tail"))
        if kwargs.get("voice_tail") is not None:
            raise UnsupportedPhonePlan("the device cannot play this tail")
        return real(*args, **kwargs)

    monkeypatch.setattr(plan_mod, "compile_phone_narrated_plan", refusing)
    requirements = [under_voice_req(), after_voice_req()]
    job, snapshot, _bindings, _binding_ = setup_n1(monkeypatch, requirements=requirements)

    recipe, variant = run(job, snapshot)

    assert [tail is not None for tail in calls] == [True, False]
    clips = video_clips(recipe)
    assert not any(c.id.endswith("-tail") for c in clips)
    assert recipe.duration == pytest.approx(VOICE_S)
    assert {c.source_asset_id for c in clips if c.volume > 0} == {"N1_05"}
    # The last step is back where it was, not cut short for a tail that is not there.
    assert clips[-1].source_start == 0.0
    after = variant["clip_sound"]["after_voice"]
    assert (after["status"], after["reason_code"]) == ("not_possible", "render_refused")
    by_id = receipts(requirements, variant)
    assert by_id["r1"].status == "met"
    assert by_id["r2"].status == "not_possible"
    assert by_id["r2"].reason.startswith("Your iPhone couldn't build that clip's own sound")


def test_a_compile_that_refuses_the_gains_too_drops_both(monkeypatch):
    import app.pipeline.phone_narrated_plan as plan_mod

    real = plan_mod.compile_phone_narrated_plan

    def refusing(*args, **kwargs):
        if kwargs.get("voice_tail") is not None or kwargs.get("under_voice_media_ids"):
            raise ValueError("refused")
        return real(*args, **kwargs)

    monkeypatch.setattr(plan_mod, "compile_phone_narrated_plan", refusing)
    requirements = [under_voice_req(), after_voice_req()]
    job, snapshot, _bindings, _binding_ = setup_n1(monkeypatch, requirements=requirements)

    recipe, variant = run(job, snapshot)

    baseline, _ = run(*setup_n1(monkeypatch, requirements=[], bound=False)[:2])
    assert recipe.model_dump(mode="json") == baseline.model_dump(mode="json")
    receipt = variant["clip_sound"]
    assert receipt["under_voice"][0]["reason_code"] == "render_refused"
    assert receipt["after_voice"]["reason_code"] == "render_refused"
    assert {r.status for r in receipts(requirements, variant).values()} == {"not_possible"}


def test_a_planner_that_fails_costs_nothing_but_the_asks(monkeypatch):
    import app.pipeline.phone_clip_sound as sound_mod

    def broken(*args, **kwargs):
        raise RuntimeError("planner bug")

    monkeypatch.setattr(sound_mod, "plan_narrated_clip_sound", broken)
    requirements = [under_voice_req(), after_voice_req()]
    job, snapshot, _bindings, _binding_ = setup_n1(monkeypatch, requirements=requirements)

    recipe, variant = run(job, snapshot)

    assert recipe.audio.original_volume == pytest.approx(0.25)
    assert all(c.volume == 1.0 for c in video_clips(recipe))
    by_id = receipts(requirements, variant)
    assert by_id["r1"].status == by_id["r2"].status == "not_possible"
    assert variant["clip_sound"]["after_voice"]["reason_code"] == "render_refused"
