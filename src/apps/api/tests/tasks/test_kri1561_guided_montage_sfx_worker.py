"""KRI-1561 PR 5b: the guided montage (no voiceover) places effects for a general ask.

Failure modes, written before the tests:

1. The montage has no speech, so the chooser is fed nothing and a request with real clips declines
   (or worse, places evenly). The clips' moments are the evidence; without any, it declines with
   the reason and renders the plain montage.
2. A closed gate or the creator's "no sound effects" still reaches the model or leaves no receipt.
3. The chosen effects never reach the recipe, or only the first render has them and the editor's
   first Save drops them (the variant's private plan must carry the rows).
4. The approved canonical plan is rewritten with the effects (its validators then refuse it).
5. The compile refusing the effects fails the render instead of falling back to the plain compile.
6. No ask: the variant gains a receipt, a row or a track (not byte-identical).
"""

from __future__ import annotations

from types import SimpleNamespace

import app.tasks.autoplace as autoplace_mod
from app.agents.sfx_placement import RawSfxSuggestion, SfxPlacementAgent, SfxPlacementOutput
from app.pipeline.phone_plan_repair import compile_phone_guided_repaired as real_compile
from app.services.device_render import device_status
from app.services.phone_editor import PHONE_EDITOR_PLAN_FIELD
from app.tasks import generative_build as gb
from tests.tasks import test_unified_montage_dispatch as unified
from tests.tasks.test_phone_subtitled_narrated_dispatch import _fake_resolve_sfx

harness = unified.harness
ASK = {"request_text": "uygun yerlere ses efekti ekle", "max_placements": 6}
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
SEEN = {
    f"clip-{i}": {
        "description": f"a ferry crossing {i}",
        "best_moments": [
            {"start_s": 0.2, "end_s": 3.0, "description": "the wake behind the ferry"}
        ],
    }
    for i in range(unified.CLIPS)
}


def _gates(monkeypatch, *, open_gate: bool = True):
    features = list(gb.settings.phone_render_verified_features)
    monkeypatch.setattr(gb.settings, "phone_render_verified_features", [*features, "soundEffects"])
    monkeypatch.setattr(gb.settings, "sound_effects_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_guided_sfx_enabled", open_gate)
    monkeypatch.setattr(gb.settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(gb, "_resolve_phone_sound_effect", _fake_resolve_sfx)
    monkeypatch.setattr(autoplace_mod, "_load_glossary", lambda _db: CATALOG)
    import app.agents._model_client as model_client

    monkeypatch.setattr(model_client, "default_client", lambda: SimpleNamespace())


def _picks(monkeypatch) -> list:
    """An agent that places on the first cut of two different clips: real marks, spaced apart."""
    payloads: list = []

    def run(self, payload, ctx=None):  # noqa: ARG001
        payloads.append(payload)
        marks = sorted({m["end_s"] for m in payload.moments})
        return SfxPlacementOutput(
            placements=[
                RawSfxSuggestion(effect_id="fx-pop", at_s=marks[0], anchor="cut"),
                RawSfxSuggestion(effect_id="fx-pop", at_s=marks[2], anchor="cut"),
            ]
        )

    monkeypatch.setattr(SfxPlacementAgent, "run", run)
    return payloads


def _run(harness, monkeypatch, *, ask=ASK, open_gate=True, analysis=SEEN, binding=None):
    job, _snapshot, session, _bindings, _plain = harness(analysis=analysis)
    session.get = lambda *_a, **_k: SimpleNamespace(audio_gcs_path="sound-effects/fx-pop/a.wav")
    _gates(monkeypatch, open_gate=open_gate)
    payloads = _picks(monkeypatch)
    if ask is not None:
        job.all_candidates["creator_strategy"]["sfx_ask"] = ask
    if binding is not None:
        job.assembly_plan["creator_brief_binding"] = binding
    gb._run_generative_job(str(job.id))
    return job, payloads


def _sfx_track(job):
    recipe = device_status(job, "guided_story").request.recipe
    return next((t for t in recipe.tracks if t.id == "sfx"), None), recipe


def test_a_general_ask_places_effects_on_cuts_of_a_montage(harness, monkeypatch):
    job, payloads = _run(harness, monkeypatch)

    assert job.status == "awaiting_device"
    track, recipe = _sfx_track(job)
    assert track is not None and len(track.clips) == 2
    assert {"soundEffects", "audioMix"} <= recipe.required_capabilities
    (payload,) = payloads
    assert payload.request_text == ASK["request_text"]
    assert payload.words == [] and payload.moments
    variant = job.assembly_plan["variants"][0]
    receipt = variant["phone_sfx_choice_receipt"]
    assert receipt["declined"] is None and len(receipt["placed"]) == 2
    # Every effect sits on a mark the clips gave it, to the frame.
    ends = {round(m["end_s"], 3) for m in payload.moments}
    assert {round(c.timeline_start, 3) for c in track.clips} <= ends
    # The editor's first Save recompiles from this private plan, so it must hold the rows.
    rows = variant[PHONE_EDITOR_PLAN_FIELD]["editor_sound_effects"]
    assert [r["sound_effect_id"] for r in rows] == ["fx-pop", "fx-pop"]
    assert variant["sound_effects"] == rows


def test_the_approved_canonical_plan_is_left_as_approved(harness, monkeypatch):
    job, _payloads = _run(harness, monkeypatch)

    assert job.assembly_plan["guided_story_execution_plan"]["editor_sound_effects"] == []


def test_clips_with_nothing_described_decline_and_render_plain(harness, monkeypatch):
    job, payloads = _run(harness, monkeypatch, analysis={})

    assert job.status == "awaiting_device"
    assert payloads == []
    track, _recipe = _sfx_track(job)
    assert track is None
    assert job.assembly_plan["variants"][0]["phone_sfx_choice_receipt"]["declined"] == (
        "no_timed_evidence"
    )


def test_a_closed_gate_names_itself_and_never_calls_the_model(harness, monkeypatch):
    job, payloads = _run(harness, monkeypatch, open_gate=False)

    assert job.status == "awaiting_device"
    assert payloads == []
    assert job.assembly_plan["variants"][0]["phone_sfx_choice_receipt"]["declined"] == "flag_off"


def test_a_live_no_sound_effects_outranks_the_ask(harness, monkeypatch):
    from tests.kria.test_no_effects_gate import NO_SFX, _binding

    job, payloads = _run(harness, monkeypatch, binding=_binding(NO_SFX))

    assert payloads == []
    track, _recipe = _sfx_track(job)
    assert track is None
    receipt = job.assembly_plan["variants"][0]["phone_sfx_choice_receipt"]
    assert receipt["declined"] == "creator_said_no" and receipt["placed"] == []


def test_a_compile_that_refuses_the_effects_renders_plain_and_says_so(harness, monkeypatch):
    import app.pipeline.phone_plan_repair as repair_mod

    def refuse_effects(plan, bindings, **kwargs):
        if kwargs.get("sound_effects"):
            raise ValueError("no room for the effects")
        return real_compile(plan, bindings, **kwargs)

    monkeypatch.setattr(repair_mod, "compile_phone_guided_repaired", refuse_effects)
    job, payloads = _run(harness, monkeypatch)

    assert job.status == "awaiting_device" and payloads
    track, _recipe = _sfx_track(job)
    assert track is None
    variant = job.assembly_plan["variants"][0]
    assert variant["phone_sfx_choice_receipt"]["placed"] == []
    assert variant["phone_sfx_choice_receipt"]["declined"] == "nothing_fit"
    assert "sound_effects" not in variant


def test_no_ask_means_no_receipt_no_row_and_no_track(harness, monkeypatch):
    job, payloads = _run(harness, monkeypatch, ask=None)

    assert job.status == "awaiting_device" and payloads == []
    track, _recipe = _sfx_track(job)
    assert track is None
    variant = job.assembly_plan["variants"][0]
    assert "phone_sfx_choice_receipt" not in variant and "sound_effects" not in variant
