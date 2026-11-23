"""KRI-1598 item 3: named sound cues on a phone montage with a recorded voiceover, sound only.

"Play a whoosh when I say Barcelona" on a montage-family edit with a recorded voiceover. The
voice plays from the montage's first frame, so its words are on the output clock; the cue's
sound joins the KRI-281 sound-effect lane. Photos, stickers and closing shots stay unavailable
on this edit (only sound), behind PHONE_VOICEOVER_MONTAGE_SOUND_CUES_ENABLED (default off).

Failure modes, written down before the code and the tests:

1. Flag off: anything changes -- the manifest (and so its hashes and the creator prompt), the
   repair notice, the whisper call, the recipe or the variant's keys.
2. The manifest advertises sound cues where this writer will not play them (gate closed, no
   voiceover, a narrated or Talking format, a cloud manifest), or the worker's gate disagrees
   with the manifest's.
3. A photo, sticker, full-screen photo, photo-then-clip sequence, title picture or closing
   shot rides along on this sound-only edit, or is dropped without telling the creator.
4. A sound cue is still stripped as "not available" while the gate is open.
5. A guided-voiceover contract (rendered by the guided writer, which never reads beats) keeps
   cues nothing will play.
6. The voice is transcribed twice (cues + a general sound-effect ask), or without the
   creator's cue words as a whisper hint (the cue is misheard and never fires).
7. A cue lands on a word the montage never plays (the voice is capped shorter than the
   recording) or off the montage clock.
8. The general ask places an effect on the moment a named cue already holds.
9. The general ask's lane call replaces the named cues (or the timed cards): lanes not merged.
10. A compile refusal fails the job, or leaves a receipt claiming sounds the recipe lacks.
11. Transcription, grounding or a sound lookup failing fails the render.
12. A cue the voice never says is reported nowhere.
13. Judge: a finished montage's cue times are not quoted (its archetype is "voiceover", not
    "narrated"); a draft's sound cue reads "not available"; a picture this edit cannot show
    reads "couldn't find the photo".
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

import app.pipeline.phone_narrated_plan as narrated_plan_mod
import app.pipeline.transcribe as transcribe_mod
import app.services.phone_reaction_grounding as grounding_mod
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import check_requirement, plan_facts_from_phone_variant
from app.pipeline.transcribe import Transcript, Word
from app.services.device_render import device_status
from app.services.phone_sources import PHONE_VISUALS_FIELD
from app.services.sfx_catalog import SfxEntry
from app.tasks import generative_build as gb
from tests.tasks.test_kri1561_phone_montage_sfx_worker import ASK, _gates, _picks
from tests.tasks.test_phone_montage_dispatch import setup

# One word a second, so "Barcelona" starts at 3.0 s and "goodbye" at 11.0 s.
SPOKEN = ["We", "landed", "in", "Barcelona", "and", "ate", "tapas", "by", "the", "sea", "then"]
SPOKEN += ["goodbye"]
VOICE = [
    Word(text=text, start_s=index * 1.0, end_s=index * 1.0 + 0.4, confidence=1.0)
    for index, text in enumerate(SPOKEN)
]
WHOOSH = {"beat_id": "barcelona", "trigger": "Barcelona", "sound": "whoosh"}


def _open(monkeypatch, *, flag: bool = True) -> None:
    """Every gate the voiceover montage's sound cues read, with only DB seams faked."""
    _gates(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_subtitled_reaction_beats_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_voiceover_reaction_beats_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_voiceover_montage_sound_cues_enabled", flag)
    # The meaning lookup for unheard cues is a model call: off, so nothing here is paid.
    monkeypatch.setattr(gb.settings, "spoken_cue_resolver_enabled", False)
    monkeypatch.setattr(
        grounding_mod,
        "_load_sfx_entries",
        lambda *a, **k: [SfxEntry(id="sfx-whoosh", name="Whoosh", search_terms=("whoosh",))],
    )
    monkeypatch.setattr(grounding_mod, "_load_ready_pool_assets", lambda *a, **k: [])
    monkeypatch.setattr("app.services.pipeline_trace.record_pipeline_event", Mock())


def _whisper(monkeypatch, words=VOICE) -> list[dict]:
    calls: list[dict] = []

    def fake(path, **kwargs):  # noqa: ARG001
        calls.append(kwargs)
        return Transcript(words=list(words), language="en")

    monkeypatch.setattr(transcribe_mod, "transcribe_whisper_cached", fake)
    return calls


def _run(monkeypatch, strategy: dict, *, flag: bool = True, words=VOICE):
    job, _snapshot, _session, _bindings, _cloud = setup(monkeypatch)
    _open(monkeypatch, flag=flag)
    calls = _whisper(monkeypatch, words)
    job.all_candidates["creator_strategy"] = strategy
    gb._run_generative_job(str(job.id))
    return job, calls


def _sfx_starts(job) -> list[float]:
    recipe = device_status(job, "voiceover_only").request.recipe
    track = next((t for t in recipe.tracks if t.id == "sfx"), None)
    return sorted(round(c.timeline_start, 2) for c in track.clips) if track else []


def _voice_end_s(job) -> float:
    recipe = device_status(job, "voiceover_only").request.recipe
    (clip,) = next(t for t in recipe.tracks if t.id == "narration").clips
    return clip.timeline_start + clip.source_duration


def test_a_named_cue_plays_its_sound_on_the_word_of_the_voice(monkeypatch):
    job, calls = _run(monkeypatch, {"reaction_beats": [WHOOSH]})

    assert job.status == "awaiting_device"
    assert _sfx_starts(job) == [3.0]
    recipe = device_status(job, "voiceover_only").request.recipe
    assert {"soundEffects", "audioMix"} <= recipe.required_capabilities
    assert not any(t.id == "subtitled-overlays" for t in recipe.tracks)
    # Whisper is asked once, seeded with the cue word so the name comes back spelled the same.
    assert [call.get("verbatim_prompt") for call in calls] == ["Barcelona."]
    [variant] = job.assembly_plan["variants"]
    receipt = variant["phone_beat_receipt"]
    [placed] = receipt["placed"]
    assert placed["beat_id"] == "barcelona" and placed["at_s"] == pytest.approx(3.0)
    assert receipt["unplaced"] == []
    assert PHONE_VISUALS_FIELD not in job.assembly_plan


def test_flag_off_keeps_the_render_byte_identical(monkeypatch):
    job, calls = _run(monkeypatch, {"reaction_beats": [WHOOSH]}, flag=False)

    assert job.status == "awaiting_device"
    assert calls == []
    assert _sfx_starts(job) == []
    [variant] = job.assembly_plan["variants"]
    assert "phone_beat_receipt" not in variant
    baseline, _ = _run(monkeypatch, {})
    assert (
        device_status(job, "voiceover_only").request.recipe
        == device_status(baseline, "voiceover_only").request.recipe
    )


def test_pictures_never_ride_along_only_the_sound_plays(monkeypatch):
    """A stored or hand-written strategy still gets sound only: the photo of a photo+sound beat,
    a photo-only beat, a title picture and the closing shot draw nothing."""
    strategy = {
        "reaction_beats": [
            {**WHOOSH, "visual_id": "asset-photo", "display_mode": "fullscreen"},
            {"beat_id": "tapas", "trigger": "tapas", "visual_id": "asset-tapas"},
            {
                "beat_id": "logo",
                "trigger": "logo",
                "visual_id": "asset-logo",
                "anchor": "title",
                "sound": "whoosh",
            },
        ],
        "closing_media": {"visual_id": "asset-photo"},
    }
    job, calls = _run(monkeypatch, strategy)

    assert job.status == "awaiting_device"
    assert _sfx_starts(job) == [3.0]
    recipe = device_status(job, "voiceover_only").request.recipe
    assert {t.id for t in recipe.tracks}.isdisjoint({"subtitled-overlays", "subtitled-fullscreen"})
    receipt = job.assembly_plan["variants"][0]["phone_beat_receipt"]
    assert [p["beat_id"] for p in receipt["placed"]] == ["barcelona"]
    assert receipt["closing"]["status"] == "none"
    assert [call.get("verbatim_prompt") for call in calls] == ["Barcelona."]


def test_a_cue_after_the_voice_stops_is_reported_never_placed(monkeypatch):
    """The compiler caps the voice to the montage; a word past that is never heard."""
    goodbye = {"beat_id": "bye", "trigger": "goodbye", "sound": "whoosh"}
    job, _calls = _run(monkeypatch, {"reaction_beats": [WHOOSH, goodbye]})

    assert _voice_end_s(job) < 11.0  # "goodbye" is in the recording, not in the video
    assert _sfx_starts(job) == [3.0]
    receipt = job.assembly_plan["variants"][0]["phone_beat_receipt"]
    assert [p["beat_id"] for p in receipt["placed"]] == ["barcelona"]
    assert [(u["trigger"], u["reason"]) for u in receipt["unplaced"]] == [
        ("goodbye", "never_heard")
    ]


def test_with_a_general_ask_the_voice_is_heard_once_and_the_cue_keeps_its_moment(monkeypatch):
    job, _snapshot, _session, _bindings, _cloud = setup(monkeypatch)
    _open(monkeypatch)
    calls = _whisper(monkeypatch)
    payloads = _picks(monkeypatch, ("fx-pop", 3.0), ("fx-pop", 6.0))
    job.all_candidates["creator_strategy"] = {"reaction_beats": [WHOOSH], "sfx_ask": ASK}
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    assert len(calls) == 1 and calls[0]["verbatim_prompt"] == "Barcelona."
    (payload,) = payloads
    assert payload.request_text == ASK["request_text"]
    # The general pick on the cue's word is dropped; both sounds share one lane.
    assert _sfx_starts(job) == [3.0, 6.0]
    variant = job.assembly_plan["variants"][0]
    assert [r["at_s"] for r in variant["phone_sfx_choice_receipt"]["placed"]] == [6.0]
    assert [p["beat_id"] for p in variant["phone_beat_receipt"]["placed"]] == ["barcelona"]


def test_a_compile_refusal_keeps_the_video_and_both_receipts_say_so(monkeypatch):
    job, _snapshot, _session, _bindings, _cloud = setup(monkeypatch)
    _open(monkeypatch)
    _whisper(monkeypatch)
    _picks(monkeypatch, ("fx-pop", 6.0))

    def refuse(*args, **kwargs):  # noqa: ARG001
        raise ValueError("effects refused")

    monkeypatch.setattr(narrated_plan_mod, "replace_editor_lanes", refuse)
    job.all_candidates["creator_strategy"] = {"reaction_beats": [WHOOSH], "sfx_ask": ASK}
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    assert _sfx_starts(job) == []
    variant = job.assembly_plan["variants"][0]
    beats = variant["phone_beat_receipt"]
    assert beats["placed"] == []
    assert [(u["trigger"], u["reason"]) for u in beats["unplaced"]] == [
        ("Barcelona", "compile_dropped")
    ]
    assert variant["phone_sfx_choice_receipt"]["placed"] == []


def test_grounding_failing_is_a_receipt_not_a_failed_render(monkeypatch):
    job, _snapshot, _session, _bindings, _cloud = setup(monkeypatch)
    _open(monkeypatch)
    _whisper(monkeypatch)
    monkeypatch.setattr(
        grounding_mod, "ground_phone_reaction_beats", Mock(side_effect=RuntimeError("boom"))
    )
    job.all_candidates["creator_strategy"] = {"reaction_beats": [WHOOSH]}
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    assert _sfx_starts(job) == []
    assert job.assembly_plan["variants"][0]["phone_beat_receipt"]["matcher"] == "failed"


def test_transcription_failing_reports_the_cue_unheard_not_a_failed_render(monkeypatch):
    job, _snapshot, _session, _bindings, _cloud = setup(monkeypatch)
    _open(monkeypatch)

    def deaf(path, **kwargs):  # noqa: ARG001
        raise RuntimeError("whisper unreachable")

    monkeypatch.setattr(transcribe_mod, "transcribe_whisper_cached", deaf)
    job.all_candidates["creator_strategy"] = {"reaction_beats": [WHOOSH]}
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    assert _sfx_starts(job) == []
    receipt = job.assembly_plan["variants"][0]["phone_beat_receipt"]
    assert receipt["placed"] == [] and [u["trigger"] for u in receipt["unplaced"]] == ["Barcelona"]


def test_the_finished_video_is_judged_with_the_cue_time_on_the_montage_clock(monkeypatch):
    job, _calls = _run(monkeypatch, {"reaction_beats": [WHOOSH]})
    [variant] = job.assembly_plan["variants"]
    variant = {**variant, "ok": True, "render_status": "ready"}

    facts = plan_facts_from_phone_variant(variant)
    [beat] = facts.reaction_beats
    assert beat.trigger == "Barcelona" and beat.sound and beat.at_s == pytest.approx(3.0)
    assert facts.beats_sound_only is True

    req = BriefRequirement(
        id="r1",
        kind="audio",
        scope="global",
        description='Play a whoosh when I say "Barcelona"',
    )
    receipt = check_requirement(req, facts)
    assert receipt.status == "met", receipt
