"""KRI-1561 PR 4: the montage-with-voiceover worker places effects for a general ask.

Failure modes, written before the tests:

1. The voice is never transcribed, so the chooser has nothing to place on (no effects, no reason).
2. A closed gate still pays for a transcription or a model call, or leaves no receipt.
3. An effect lands where no word is, or the recipe loses its effects lane on a Save.
4. The transcription, the agent or the compiler failing fails the render.
5. No ask: the variant gains a receipt or a track (not byte-identical).
"""

from __future__ import annotations

from types import SimpleNamespace

import app.pipeline.phone_narrated_plan as narrated_plan_mod
import app.pipeline.transcribe as transcribe_mod
import app.tasks.autoplace as autoplace_mod
from app.agents.sfx_placement import RawSfxSuggestion, SfxPlacementAgent, SfxPlacementOutput
from app.pipeline.transcribe import Transcript, Word
from app.tasks import generative_build as gb
from tests.tasks.test_phone_montage_dispatch import setup
from tests.tasks.test_phone_subtitled_narrated_dispatch import _fake_resolve_sfx, device_status

ASK = {"request_text": "Add sound effects where they fit", "max_placements": 6}
CATALOG = [
    {
        "id": "fx-pop",
        "name": "Pop",
        "audio_gcs_path": "sound-effects/fx-pop/a.wav",
        "duration_s": 0.4,
        "role_tags": ["appear"],
        "contains_voice": False,
    }
]
VOICE = [Word(text=f"w{i}", start_s=i * 1.0, end_s=i * 1.0 + 0.4, confidence=1.0) for i in range(7)]
FEATURES = [
    "basicComposition",
    "local1080Export",
    "crossfade",
    "audioMix",
    "musicBed",
    "narrationAudio",
    "stillImages",
    "visualBlocks",
    "alphaOverlay",
    "soundEffects",
]


def _gates(monkeypatch, *, open_gate: bool = True):
    monkeypatch.setattr(gb.settings, "sound_effects_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_voiceover_editor_lanes_enabled", open_gate)
    monkeypatch.setattr(gb.settings, "media_overlays_enabled", True)
    monkeypatch.setattr(gb.settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(gb.settings, "phone_render_verified_features", FEATURES)
    monkeypatch.setattr(gb, "_resolve_phone_sound_effect", _fake_resolve_sfx)
    monkeypatch.setattr(autoplace_mod, "_load_glossary", lambda _db: CATALOG)
    import app.agents._model_client as model_client

    monkeypatch.setattr(model_client, "default_client", lambda: SimpleNamespace())


def _transcribe(monkeypatch, words=VOICE) -> list:
    calls: list = []

    def fake(path, **kwargs):  # noqa: ARG001
        calls.append(path)
        return Transcript(words=list(words), language="en")

    monkeypatch.setattr(transcribe_mod, "transcribe_whisper_cached", fake)
    return calls


def _picks(monkeypatch, *picks: tuple[str, float]) -> list:
    payloads: list = []

    def run(self, payload, ctx=None):  # noqa: ARG001
        payloads.append(payload)
        return SfxPlacementOutput(
            placements=[
                RawSfxSuggestion(effect_id=e, at_s=t, anchor="word_start") for e, t in picks
            ]
        )

    monkeypatch.setattr(SfxPlacementAgent, "run", run)
    return payloads


def _run(monkeypatch, *, ask=ASK, open_gate=True, picks=(("fx-pop", 1.0), ("fx-pop", 4.0))):
    job, _snapshot, _session, _bindings, _cloud = setup(monkeypatch)
    _gates(monkeypatch, open_gate=open_gate)
    calls = _transcribe(monkeypatch)
    payloads = _picks(monkeypatch, *picks)
    if ask is not None:
        job.all_candidates["creator_strategy"] = {"sfx_ask": ask}
    gb._run_generative_job(str(job.id))
    return job, calls, payloads


def test_a_general_ask_puts_effects_on_the_voice_words_in_the_recipe(monkeypatch):
    job, calls, payloads = _run(monkeypatch)

    assert job.status == "awaiting_device"
    recipe = device_status(job, "voiceover_only").request.recipe
    sfx_track = next(t for t in recipe.tracks if t.id == "sfx")
    assert sorted(round(c.timeline_start, 2) for c in sfx_track.clips) == [1.0, 4.0]
    assert {"soundEffects", "audioMix"} <= recipe.required_capabilities
    assert len(calls) == 1
    (payload,) = payloads
    assert payload.request_text == ASK["request_text"]
    assert payload.duration_s == recipe.duration
    receipt = job.assembly_plan["variants"][0]["phone_sfx_choice_receipt"]
    assert receipt["declined"] is None and [r["at_s"] for r in receipt["placed"]] == [1.0, 4.0]


def test_no_ask_means_no_transcription_no_track_and_no_receipt(monkeypatch):
    job, calls, payloads = _run(monkeypatch, ask=None)

    assert job.status == "awaiting_device"
    assert calls == [] and payloads == []
    recipe = device_status(job, "voiceover_only").request.recipe
    assert not any(t.id == "sfx" for t in recipe.tracks)
    assert "phone_sfx_choice_receipt" not in job.assembly_plan["variants"][0]


def test_a_closed_gate_declines_with_its_reason_and_pays_for_nothing(monkeypatch):
    job, calls, payloads = _run(monkeypatch, open_gate=False)

    assert job.status == "awaiting_device"
    assert calls == [] and payloads == []
    receipt = job.assembly_plan["variants"][0]["phone_sfx_choice_receipt"]
    assert receipt["declined"] == "flag_off" and receipt["placed"] == []


def test_a_voice_with_no_words_declines_for_lack_of_timed_evidence(monkeypatch):
    job, _snapshot, _session, _bindings, _cloud = setup(monkeypatch)
    _gates(monkeypatch)
    _transcribe(monkeypatch, words=[])
    payloads = _picks(monkeypatch, ("fx-pop", 1.0))
    job.all_candidates["creator_strategy"] = {"sfx_ask": ASK}
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    assert payloads == []
    receipt = job.assembly_plan["variants"][0]["phone_sfx_choice_receipt"]
    assert receipt["declined"] == "no_timed_evidence"


def test_a_failed_transcription_is_a_receipt_not_a_failed_render(monkeypatch):
    job, _snapshot, _session, _bindings, _cloud = setup(monkeypatch)
    _gates(monkeypatch)

    def boom(path, **kwargs):  # noqa: ARG001
        raise RuntimeError("whisper unreachable")

    monkeypatch.setattr(transcribe_mod, "transcribe_whisper_cached", boom)
    _picks(monkeypatch, ("fx-pop", 1.0))
    job.all_candidates["creator_strategy"] = {"sfx_ask": ASK}
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    receipt = job.assembly_plan["variants"][0]["phone_sfx_choice_receipt"]
    assert receipt["declined"] == "no_timed_evidence" and receipt["placed"] == []


def test_a_compiler_that_refuses_the_effects_keeps_the_recipe_and_says_so(monkeypatch):
    job, _snapshot, _session, _bindings, _cloud = setup(monkeypatch)
    _gates(monkeypatch)
    _transcribe(monkeypatch)
    _picks(monkeypatch, ("fx-pop", 1.0), ("fx-pop", 4.0))

    def refuse(*args, **kwargs):  # noqa: ARG001
        raise ValueError("effect refused")

    monkeypatch.setattr(narrated_plan_mod, "replace_editor_lanes", refuse)
    job.all_candidates["creator_strategy"] = {"sfx_ask": ASK}
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    recipe = device_status(job, "voiceover_only").request.recipe
    assert not any(t.id == "sfx" for t in recipe.tracks)
    receipt = job.assembly_plan["variants"][0]["phone_sfx_choice_receipt"]
    assert receipt["placed"] == [] and receipt["declined"] == "nothing_fit"


def test_a_live_no_sound_effects_in_the_approved_brief_outranks_the_ask_montage(monkeypatch):
    """KRI-1561 PR 7: the montage-with-voiceover worker transcribes nothing and places nothing
    while the approved brief holds "no sound effects"."""
    from tests.kria.test_no_effects_gate import NO_SFX, _binding

    job, _snapshot, _session, _bindings, _cloud = setup(monkeypatch)
    _gates(monkeypatch)
    calls = _transcribe(monkeypatch)
    payloads = _picks(monkeypatch, ("fx-pop", 1.0))
    job.assembly_plan["creator_brief_binding"] = _binding(NO_SFX)
    job.all_candidates["creator_strategy"] = {"sfx_ask": ASK}
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    assert calls == [] and payloads == []
    receipt = job.assembly_plan["variants"][0]["phone_sfx_choice_receipt"]
    assert receipt["declined"] == "creator_said_no" and receipt["placed"] == []
