"""KRI-1598: a montage that plays a named effect AND carries a general sound-effect ask.

"A whoosh when the dog jumps, and some sound effects wherever they fit": the planner sets both
`licensed_sfx` and `sfx_ask`. The guided writer plays the named effect and chooses nothing beside
it, because the named-effect check (`validate_licensed_sfx_runtime`) refuses any other row in the
plan on the next Save. Before this it said nothing about the general ask: no receipt, so the
finished video's review stayed neutral.

Failure modes, written before the tests:

1. No receipt: the general ask is neither met nor declined on the finished video (the gap).
2. The receipt claims "added none" / "nothing fit" while the named effect plays.
3. Chosen effects are added beside the named one, and the next Save fails the named-effect check.
4. The model is called for an ask the writer will not serve (cost for nothing).
5. A named effect with no general ask gains a receipt (not byte-identical with before).
6. A live "no sound effects" is overridden by the named-effect reason.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.kria.brief_checks import (
    build_receipts,
    needs_creator_choice,
    plan_facts_from_phone_variant,
)
from app.services.sfx_capability import NAMED_EFFECT_EN
from app.tasks import generative_build as gb
from tests.kria.test_sfx_ask_judge import ASK_EN
from tests.tasks import test_kri1561_guided_montage_sfx_worker as guided

harness = guided.harness
NAMED = {"effect_id": "fx-pop", "semantics": "funny_moments", "max_placements": 1}
NAMED_ROW = {
    "id": "named-0",
    "sound_effect_id": "fx-pop",
    "src_gcs_path": "sound-effects/fx-pop/a.wav",
    "at_s": 1.0,
    "gain": 1.0,
    "duration_s": 0.4,
    "label": "Pop",
}


def _run(harness, monkeypatch, *, ask=guided.ASK, named=True, binding=None):
    job, _snapshot, session, _bindings, _plain = harness(analysis=guided.SEEN)
    session.get = lambda *_a, **_k: SimpleNamespace(audio_gcs_path="sound-effects/fx-pop/a.wav")
    guided._gates(monkeypatch)
    payloads = guided._picks(monkeypatch)
    if named:
        # What `_guided_execution_plan.materialize_licensed_sfx` leaves on the canonical plan.
        compile_plan = gb._guided_execution_plan

        def with_named_effect(job_id, snapshot):
            plan, extra = compile_plan(job_id, snapshot)
            plan = {**plan, "licensed_sfx_intent": NAMED, "editor_sound_effects": [NAMED_ROW]}
            job.assembly_plan["guided_story_execution_plan"] = plan
            return plan, extra

        monkeypatch.setattr(gb, "_guided_execution_plan", with_named_effect)
        job.all_candidates["creator_strategy"]["licensed_sfx"] = NAMED
    if ask is not None:
        job.all_candidates["creator_strategy"]["sfx_ask"] = ask
    if binding is not None:
        job.assembly_plan["creator_brief_binding"] = binding
    gb._run_generative_job(str(job.id))
    return job, payloads


def test_the_named_effect_plays_alone_and_the_ask_is_answered(harness, monkeypatch):
    job, payloads = _run(harness, monkeypatch)

    assert job.status == "awaiting_device"
    assert payloads == []  # no model call for an ask this writer does not serve
    track, _recipe = guided._sfx_track(job)
    assert track is not None and len(track.clips) == 1  # the named effect, nothing beside it
    variant = job.assembly_plan["variants"][0]
    receipt = variant["phone_sfx_choice_receipt"]
    assert receipt["declined"] == "named_effect" and receipt["placed"] == []
    assert [row["id"] for row in variant["sound_effects"]] == ["named-0"]
    [review] = build_receipts([ASK_EN], plan_facts_from_phone_variant(variant))
    assert review.status == "not_possible" and review.stage == "checked"
    assert review.reason == NAMED_EFFECT_EN
    assert "added none" not in review.reason
    assert not needs_creator_choice(review)


def test_a_named_effect_without_the_ask_leaves_no_receipt(harness, monkeypatch):
    job, payloads = _run(harness, monkeypatch, ask=None)

    assert job.status == "awaiting_device" and payloads == []
    assert "phone_sfx_choice_receipt" not in job.assembly_plan["variants"][0]


def test_a_live_no_sound_effects_still_outranks_the_named_effect_reason(harness, monkeypatch):
    from tests.kria.test_no_effects_gate import NO_SFX, _binding

    job, payloads = _run(harness, monkeypatch, binding=_binding(NO_SFX))

    assert payloads == []
    receipt = job.assembly_plan["variants"][0]["phone_sfx_choice_receipt"]
    assert receipt["declined"] == "creator_said_no"


def test_the_ask_without_a_named_effect_is_still_chosen(harness, monkeypatch):
    """Negative control: the plan the chooser serves is untouched by the new receipt."""
    job, payloads = _run(harness, monkeypatch, named=False)

    assert payloads
    receipt = job.assembly_plan["variants"][0]["phone_sfx_choice_receipt"]
    assert receipt["declined"] is None and len(receipt["placed"]) == 2
