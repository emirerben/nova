"""KRI-1561 PR 3: the Talking worker places effects for a general ask.

Failure modes, written before the tests:

1. A general ask never reaches the compiled recipe (no sfx track), or sits off every word.
2. A closed gate still calls the model, or leaves no receipt saying why.
3. Several speakers produce effects on one speaker's words.
4. A chosen effect lands on top of a named cue's slot.
5. The agent or one catalogue row failing fails the job.
6. No ask: the variant is byte-identical (no receipt key).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.pipeline.phone_subtitled_plan as subtitled_plan_mod
import app.tasks.autoplace as autoplace_mod
from app.agents.sfx_placement import RawSfxSuggestion, SfxPlacementAgent, SfxPlacementOutput
from app.pipeline.phone_subtitled_lanes import SubtitledSoundEffect
from app.services.sfx_autoplace import MIN_SPACING_S
from app.tasks import generative_build as gb
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _fake_resolve_sfx,
    _lanes_features,
    _setup_subtitled,
    _words,
)

SPOKEN = _words(*[(f"word{i}", i * 2.0, i * 2.0 + 0.5) for i in range(8)])
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
ASK = {"request_text": "Uygun yerlere ses efekti ekle", "max_placements": 6}


def _open_gates(monkeypatch) -> None:
    monkeypatch.setattr(gb.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_subtitled_editor_lanes_enabled", True)
    monkeypatch.setattr(gb.settings, "sound_effects_enabled", True)
    monkeypatch.setattr(gb.settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        _lanes_features("stillImages", "visualBlocks", "alphaOverlay", "soundEffects"),
    )
    monkeypatch.setattr(gb, "_resolve_phone_sound_effect", _fake_resolve_sfx)
    monkeypatch.setattr(autoplace_mod, "_load_glossary", lambda _db: CATALOG)
    import app.agents._model_client as model_client

    monkeypatch.setattr(model_client, "default_client", lambda: SimpleNamespace())


def _agent_picks(monkeypatch, *picks: tuple[str, float]) -> list:
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


def _spy_compile(monkeypatch) -> list:
    real = subtitled_plan_mod.compile_phone_subtitled_plan
    compiled: list = []

    def spy(*args, **kwargs):
        recipe = real(*args, **kwargs)
        compiled.append(recipe)
        return recipe

    monkeypatch.setattr(subtitled_plan_mod, "compile_phone_subtitled_plan", spy)
    return compiled


def _run(monkeypatch, *, ask=ASK, picks=(("fx-pop", 2.0), ("fx-pop", 6.0)), **setup):
    job, _snapshot, _session, _binding = _setup_subtitled(
        monkeypatch, transcript_words=SPOKEN, duration_s=16.0, **setup
    )
    _open_gates(monkeypatch)
    payloads = _agent_picks(monkeypatch, *picks)
    compiled = _spy_compile(monkeypatch)
    if ask is not None:
        job.all_candidates["creator_strategy"] = {"sfx_ask": ask}
    gb._run_generative_job(str(job.id))
    return job, payloads, compiled


def test_a_general_ask_puts_effects_on_spoken_words_in_the_recipe(monkeypatch):
    job, payloads, compiled = _run(monkeypatch)

    assert job.status == "awaiting_device"
    sfx_track = next(t for t in compiled[-1].tracks if t.id == "sfx")
    starts = sorted(round(clip.timeline_start, 2) for clip in sfx_track.clips)
    assert starts == [2.0, 6.0]
    (payload,) = payloads
    assert payload.request_text == ASK["request_text"]
    variant = job.assembly_plan["variants"][0]
    receipt = variant["phone_sfx_choice_receipt"]
    assert receipt["declined"] is None
    assert [row["at_s"] for row in receipt["placed"]] == [2.0, 6.0]
    assert variant["phone_lane_receipt"]["applied"] == ["sound_effects"]


def test_no_ask_leaves_the_variant_without_a_receipt(monkeypatch):
    job, payloads, compiled = _run(monkeypatch, ask=None)

    assert job.status == "awaiting_device"
    assert payloads == []
    assert "phone_sfx_choice_receipt" not in job.assembly_plan["variants"][0]
    assert not any(t.id == "sfx" for t in compiled[-1].tracks)


def test_a_closed_gate_declines_with_its_reason_and_never_calls_the_model(monkeypatch):
    job, _snapshot, _session, _binding = _setup_subtitled(
        monkeypatch, transcript_words=SPOKEN, duration_s=16.0
    )
    _open_gates(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_subtitled_editor_lanes_enabled", False)
    payloads = _agent_picks(monkeypatch, ("fx-pop", 2.0))
    job.all_candidates["creator_strategy"] = {"sfx_ask": ASK}
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    receipt = job.assembly_plan["variants"][0]["phone_sfx_choice_receipt"]
    assert receipt["declined"] == "flag_off" and receipt["placed"] == []
    assert payloads == []


def test_a_named_cue_keeps_its_slot(monkeypatch):
    job, _snapshot, _session, _binding = _setup_subtitled(
        monkeypatch, transcript_words=SPOKEN, duration_s=16.0
    )
    _open_gates(monkeypatch)
    _agent_picks(monkeypatch, ("fx-pop", 2.0), ("fx-pop", 6.0), ("fx-pop", 10.0))
    compiled = _spy_compile(monkeypatch)
    from tests.tasks.test_phone_subtitled_narrated_dispatch import (
        PHONE_SUBTITLED_LANES_FIELD,
        _lane_request,
        _sfx_request_dict,
    )

    job.assembly_plan[PHONE_SUBTITLED_LANES_FIELD] = _lane_request(
        sound_effects=[_sfx_request_dict("named", "fx-pop", at_s=6.4)]
    )
    job.all_candidates["creator_strategy"] = {"sfx_ask": ASK}
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    sfx_track = next(t for t in compiled[-1].tracks if t.id == "sfx")
    times = sorted(clip.timeline_start for clip in sfx_track.clips)
    assert any(abs(t - 6.4) < 1e-6 for t in times), "the named cue stays"
    chosen = [t for t in times if abs(t - 6.4) > 1e-6]
    assert chosen and all(abs(t - 6.4) >= MIN_SPACING_S for t in chosen)


def test_an_agent_failure_is_a_receipt_not_a_failed_job(monkeypatch):
    job, _snapshot, _session, _binding = _setup_subtitled(
        monkeypatch, transcript_words=SPOKEN, duration_s=16.0
    )
    _open_gates(monkeypatch)

    def boom(self, payload, ctx=None):  # noqa: ARG001
        raise RuntimeError("model timed out")

    monkeypatch.setattr(SfxPlacementAgent, "run", boom)
    job.all_candidates["creator_strategy"] = {"sfx_ask": ASK}
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    receipt = job.assembly_plan["variants"][0]["phone_sfx_choice_receipt"]
    assert receipt["declined"] == "nothing_fit" and "timed out" in receipt["error"]


def test_one_catalogue_row_the_library_cannot_serve_drops_only_that_effect(monkeypatch):
    job, _snapshot, _session, _binding = _setup_subtitled(
        monkeypatch, transcript_words=SPOKEN, duration_s=16.0
    )
    _open_gates(monkeypatch)
    _agent_picks(monkeypatch, ("fx-pop", 2.0), ("fx-pop", 6.0))

    def resolver(sfx: SubtitledSoundEffect):
        if sfx.at_s == 2.0:
            raise RuntimeError("library asset size changed")
        return _fake_resolve_sfx(sfx)

    monkeypatch.setattr(gb, "_resolve_phone_sound_effect", resolver)
    job.all_candidates["creator_strategy"] = {"sfx_ask": ASK}
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    assert [row["at_s"] for row in variant["phone_sfx_choice_receipt"]["placed"]] == [6.0]
    assert variant["phone_lane_receipt"]["dropped"][0]["id"] == "ask-sfx-0"


@pytest.mark.parametrize("ask", [{"request_text": "  "}, {"request_text": "x", "bogus": 1}])
def test_a_malformed_ask_is_ignored(monkeypatch, ask):
    job, payloads, _compiled = _run(monkeypatch, ask=ask)

    assert job.status == "awaiting_device"
    assert payloads == []
    assert "phone_sfx_choice_receipt" not in job.assembly_plan["variants"][0]


def test_effects_follow_the_cut_timeline_not_the_raw_take(monkeypatch):
    """Two removals (0.88-1.42 and 2.5-4.4 s of a 6.5 s take) shift every later word left. The
    chooser must see the shifted words and place on them, or an effect lands where no word is."""
    from tests.tasks.test_generative_build_silence_cut import DURATION
    from tests.tasks.test_phone_subtitled_narrated_dispatch import _raw_preflight_snapshot

    job, snapshot, _session, binding = _setup_subtitled(monkeypatch, duration_s=DURATION)
    snapshot["speech_cleanup_contract"] = "required_v1"
    snapshot["_speech_cleanup_internal"] = {
        "preflight_snapshot": _raw_preflight_snapshot(storage_path=binding.proxy_path)
    }
    _open_gates(monkeypatch)
    seen: list = []
    picked: list = []

    def run(self, payload, ctx=None):  # noqa: ARG001
        seen.append(payload)
        # The last word that starts early enough to clear the 0.5 s end keep-out.
        pick = [w for w in payload.words if w["s"] <= payload.duration_s - 0.6][-1]
        picked.append(pick)
        return SfxPlacementOutput(
            placements=[RawSfxSuggestion(effect_id="fx-pop", at_s=pick["s"], anchor="word_start")]
        )

    monkeypatch.setattr(SfxPlacementAgent, "run", run)
    compiled = _spy_compile(monkeypatch)
    job.all_candidates["creator_strategy"] = {"sfx_ask": ASK}
    gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    assert job.status == "awaiting_device"
    (payload,) = seen
    removed_s = 0.54 + 1.9
    assert payload.duration_s == pytest.approx(DURATION - removed_s, abs=0.05)
    assert max(word["e"] for word in payload.words) <= payload.duration_s + 0.05
    sfx_track = next(t for t in compiled[-1].tracks if t.id == "sfx")
    (clip,) = sfx_track.clips
    assert clip.timeline_start == pytest.approx(picked[0]["s"])
    assert clip.timeline_start < DURATION - removed_s


def test_a_single_speaker_is_not_a_decline(monkeypatch):
    """The worker declines with `multi_speaker` only when it switched the lanes off for a second
    speaker (`talking_lanes_skipped`); one speaker hands the chooser no limit. (The
    multi-speaker branch itself is not exercised here: it needs the multi-take fixtures.)"""
    job, _snapshot, _session, _binding = _setup_subtitled(
        monkeypatch, transcript_words=SPOKEN, duration_s=16.0
    )
    _open_gates(monkeypatch)
    payloads = _agent_picks(monkeypatch, ("fx-pop", 2.0))
    import app.tasks.generative_build as worker

    original = worker._phone_choose_sound_effects
    captured: dict = {}

    def spy(job_id, **kwargs):
        captured.update(kwargs)
        return original(job_id, **kwargs)

    monkeypatch.setattr(worker, "_phone_choose_sound_effects", spy)
    job.all_candidates["creator_strategy"] = {"sfx_ask": ASK}
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    assert captured["limit"] is None, "a single speaker is not a decline"
    assert payloads, "one speaker places"


# --- the Voiceover (narrated) writer ----------------------------------------------------------

NARRATION = _words(*[(f"line{i}", i * 1.5, i * 1.5 + 0.5) for i in range(8)])


def _open_voiceover_gates(monkeypatch) -> None:
    _open_gates(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_voiceover_editor_lanes_enabled", True)
    monkeypatch.setattr(gb.settings, "media_overlays_enabled", True)


def _run_narrated(monkeypatch, *, ask=ASK, picks=(("fx-pop", 1.5), ("fx-pop", 6.0))):
    from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_narrated, device_status

    job, _snapshot, _session, _bindings = _setup_narrated(monkeypatch, spoken_words=NARRATION)
    _open_voiceover_gates(monkeypatch)
    payloads = _agent_picks(monkeypatch, *picks)
    if ask is not None:
        job.all_candidates["creator_strategy"] = {"sfx_ask": ask}
    gb._run_generative_job(str(job.id))
    return job, payloads, device_status


def test_a_general_ask_puts_effects_on_the_voiceover_words(monkeypatch):
    job, payloads, device_status = _run_narrated(monkeypatch)

    assert job.status == "awaiting_device"
    recipe = device_status(job, "narrated").request.recipe
    sfx_track = next(t for t in recipe.tracks if t.id == "sfx")
    assert sorted(round(c.timeline_start, 2) for c in sfx_track.clips) == [1.5, 6.0]
    (payload,) = payloads
    assert payload.request_text == ASK["request_text"]
    receipt = job.assembly_plan["variants"][0]["phone_sfx_choice_receipt"]
    assert receipt["declined"] is None and len(receipt["placed"]) == 2


def test_narrated_without_an_ask_has_no_effects_and_no_receipt(monkeypatch):
    job, payloads, device_status = _run_narrated(monkeypatch, ask=None)

    assert job.status == "awaiting_device"
    assert payloads == []
    recipe = device_status(job, "narrated").request.recipe
    assert not any(t.id == "sfx" for t in recipe.tracks)
    assert "phone_sfx_choice_receipt" not in job.assembly_plan["variants"][0]


def test_narrated_closed_gate_declines_with_its_reason(monkeypatch):
    from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_narrated

    job, _snapshot, _session, _bindings = _setup_narrated(monkeypatch, spoken_words=NARRATION)
    _open_voiceover_gates(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_voiceover_editor_lanes_enabled", False)
    payloads = _agent_picks(monkeypatch, ("fx-pop", 1.5))
    job.all_candidates["creator_strategy"] = {"sfx_ask": ASK}
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    receipt = job.assembly_plan["variants"][0]["phone_sfx_choice_receipt"]
    assert receipt["declined"] == "flag_off" and receipt["placed"] == []
    assert payloads == []


def test_narrated_compile_that_refuses_the_effects_drops_them_and_the_receipt_says_so(
    monkeypatch,
):
    """A chosen effect the compiler refuses is the lane's drop, not the job's: the retry compiles
    without it and the receipt no longer lists it as placed."""
    import app.pipeline.phone_narrated_plan as narrated_plan_mod
    from app.pipeline.phone_subtitled_lanes import SubtitledLaneError
    from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_narrated

    job, _snapshot, _session, _bindings = _setup_narrated(monkeypatch, spoken_words=NARRATION)
    _open_voiceover_gates(monkeypatch)
    _agent_picks(monkeypatch, ("fx-pop", 1.5), ("fx-pop", 6.0))
    real = narrated_plan_mod.compile_phone_narrated_plan
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SubtitledLaneError("sound_effects", "effect refused", capability="soundEffects")
        return real(*args, **kwargs)

    monkeypatch.setattr(narrated_plan_mod, "compile_phone_narrated_plan", flaky)
    job.all_candidates["creator_strategy"] = {"sfx_ask": ASK}
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    assert calls["n"] == 2
    receipt = job.assembly_plan["variants"][0]["phone_sfx_choice_receipt"]
    assert receipt["placed"] == [] and receipt["declined"] == "nothing_fit"


def _no_effects_binding() -> dict:
    from tests.kria.test_no_effects_gate import NO_SFX, _binding

    return _binding(NO_SFX)


def test_a_live_no_sound_effects_in_the_approved_brief_outranks_the_ask_talking(monkeypatch):
    """KRI-1561 PR 7: a plan that carries `sfx_ask` (stale, approved, or set by a model anyway)
    places nothing while the approved brief holds "no sound effects", and says why."""
    job, _snapshot, _session, _binding_ = _setup_subtitled(
        monkeypatch, transcript_words=SPOKEN, duration_s=16.0
    )
    _open_gates(monkeypatch)
    payloads = _agent_picks(monkeypatch, ("fx-pop", 2.0))
    compiled = _spy_compile(monkeypatch)
    job.assembly_plan["creator_brief_binding"] = _no_effects_binding()
    job.all_candidates["creator_strategy"] = {"sfx_ask": ASK}
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    assert payloads == []
    assert not any(t.id == "sfx" for t in compiled[-1].tracks)
    receipt = job.assembly_plan["variants"][0]["phone_sfx_choice_receipt"]
    assert receipt["declined"] == "creator_said_no" and receipt["placed"] == []


def test_a_live_no_sound_effects_in_the_approved_brief_outranks_the_ask_voiceover(monkeypatch):
    from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_narrated

    job, _snapshot, _session, _bindings = _setup_narrated(monkeypatch, spoken_words=NARRATION)
    _open_voiceover_gates(monkeypatch)
    payloads = _agent_picks(monkeypatch, ("fx-pop", 1.5))
    job.assembly_plan["creator_brief_binding"] = _no_effects_binding()
    job.all_candidates["creator_strategy"] = {"sfx_ask": ASK}
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    assert payloads == []
    receipt = job.assembly_plan["variants"][0]["phone_sfx_choice_receipt"]
    assert receipt["declined"] == "creator_said_no"


def test_without_that_brief_row_the_same_plan_places_effects(monkeypatch):
    """Control: the same job with the row withdrawn (no binding) places effects."""
    job, payloads, _compiled = _run(monkeypatch)
    assert job.status == "awaiting_device" and payloads
    assert job.assembly_plan["variants"][0]["phone_sfx_choice_receipt"]["declined"] is None
