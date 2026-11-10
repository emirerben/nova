"""KRI-1350 colour moments: wire parity, digests, the phone Save path, rollout gates, chat op.

Failure modes (written before the code): docs/reviews/colour-moments-failure-modes.md.
The engine side (pixels, timing, audio) is pinned by ColorMomentsCompositionTests.swift and
measured independently by scripts/verify-colour-moments.py.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.agents.editor_ops_v2 import color_moments as lane
from app.kria.device_render import canonical_recipe_document
from app.kria.portable_color_moments import ColorMoment
from app.kria.recipes_v2 import EditRecipeV2
from app.pipeline.phone_recipe_shared import with_color_moments
from app.routes import generative_jobs as gj
from app.services import phone_rollout
from app.services.device_render import device_status
from tests.evals.runners.eval_runner import load_fixture, run_eval
from tests.routes.test_phone_subtitled_editor_commit import (
    _VERIFIED_FEATURES,
    phone_job,
    save,
)

ROOT = Path(__file__).resolve().parents[5]
PARITY = json.loads((ROOT / "tests/fixtures/colour-moments/parity-v1.json").read_text())
GOLDEN = Path(__file__).resolve().parents[1] / "fixtures/agent_evals/edit_copilot/golden"

LANE = {
    "moments": [
        {
            "id": "cm1",
            "start": 1.0,
            "end": 2.5,
            "tone": "mono",
            "enter": {"style": "light_leak", "duration": 1 / 3},
            "exit": {"style": "dip_black", "duration": 1 / 3},
        }
    ],
    "end_fade": {"duration": 1.0},
}


def _open_gate(monkeypatch, *, enabled=True, verified=True):
    monkeypatch.setattr(gj.settings, "color_moments_enabled", enabled)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        list(_VERIFIED_FEATURES) + (["colorMoments"] if verified else []),
    )


# --- wire parity (failure mode 13) -----------------------------------------------------------


def _base_recipe(job) -> dict:
    return device_status(job, "subtitled").request.recipe.model_dump(mode="json")


@pytest.mark.parametrize("case", PARITY["valid"], ids=lambda c: c["name"])
def test_parity_valid_cases_validate_and_round_trip_byte_for_byte(monkeypatch, case):
    job = phone_job(monkeypatch)
    base = _base_recipe(job)
    for key, value in case.items():
        if key != "name":
            base[key] = value
    if base.get("color_moments"):
        # Shrink the fixture's 4 s base onto this recipe's timeline if needed.
        assert (
            max(m["end"] for m in base["color_moments"])
            <= EditRecipeV2(**_base_recipe(job)).duration
        )
    recipe = EditRecipeV2.model_validate(base)
    assert "colorMoments" in recipe.required_capabilities
    dumped = recipe.model_dump(mode="json")
    assert dumped.get("color_moments") == case.get("color_moments", dumped.get("color_moments"))
    assert dumped.get("end_fade") == case.get("end_fade", dumped.get("end_fade"))


@pytest.mark.parametrize("case", PARITY["invalid"], ids=lambda c: c["name"])
def test_parity_invalid_cases_are_refused(monkeypatch, case):
    job = phone_job(monkeypatch)
    base = _base_recipe(job)
    for key, value in case.items():
        if key != "name":
            base[key] = value
    # The Swift side decodes into a 4 s recipe; pin the same length here.
    with pytest.raises(ValueError):
        recipe = EditRecipeV2.model_validate(base)
        if recipe.duration > 4.0:
            from app.kria.portable_color_moments import validate_color_moments

            validate_color_moments(recipe.color_moments, 4.0)
            if recipe.end_fade is not None and recipe.end_fade.duration > 4.0:
                raise ValueError("end fade too long for the parity base")


# --- byte identity (failure mode 3) ----------------------------------------------------------


def test_a_recipe_without_moments_keeps_its_bytes_and_digest(monkeypatch):
    job = phone_job(monkeypatch)
    recipe = device_status(job, "subtitled").request.recipe
    document = canonical_recipe_document(recipe)
    assert "color_moments" not in json.dumps(document)
    assert "end_fade" not in json.dumps(document)
    assert with_color_moments(recipe, None) is recipe
    assert with_color_moments(recipe, {"moments": [], "end_fade": None}) is recipe


def test_with_color_moments_clamps_to_a_shorter_cut_and_strips_on_none(monkeypatch):
    job = phone_job(monkeypatch)
    recipe = device_status(job, "subtitled").request.recipe
    total = recipe.duration
    stored = {
        "moments": [
            {"id": "a", "start": 1.0, "end": 2.0, "tone": "mono"},
            # Runs past the end: shortened to the end.
            {"id": "b", "start": total - 1.0, "end": total + 5.0, "tone": "warm"},
            # Starts too close to the end once clamped: dropped.
            {"id": "c", "start": total - 0.2, "end": total + 3.0, "tone": "faded"},
        ],
        "end_fade": {"duration": total + 10},
    }
    out = with_color_moments(recipe, stored)
    assert [(m.id, m.end) for m in out.color_moments] == [("a", 2.0), ("b", total)]
    assert out.end_fade is None, "an end fade longer than the video is dropped"
    assert "colorMoments" in out.required_capabilities
    stripped = with_color_moments(out, None)
    assert not stripped.color_moments and "colorMoments" not in stripped.required_capabilities


# --- the phone Save path ---------------------------------------------------------------------


def test_save_stores_the_lane_and_pins_a_recipe_carrying_it(monkeypatch):
    job = phone_job(monkeypatch)
    _open_gate(monkeypatch)
    save(job, color_moments=gj.EditorCommitColorMoments(**LANE))
    variant = job.assembly_plan["variants"][0]
    assert variant["color_moments"]["moments"][0]["exit"]["style"] == "dip_black"
    recipe = device_status(job, "subtitled").request.recipe
    assert [m.id for m in recipe.color_moments] == ["cm1"]
    assert recipe.end_fade is not None and recipe.end_fade.duration == 1.0
    assert "colorMoments" in recipe.required_capabilities


def test_a_later_phone_save_that_never_mentions_the_lane_keeps_it(monkeypatch):
    from tests.routes.test_phone_subtitled_editor_commit import _sfx_payload

    job = phone_job(monkeypatch)
    _open_gate(monkeypatch)
    save(job, color_moments=gj.EditorCommitColorMoments(**LANE))
    base = job.assembly_plan["variants"][0]["render_generation_id"]
    gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(base_generation=base, sound_effects=[_sfx_payload(at_s=4.0)]),
        user_id="owner",
        plan_item_id="item",
    )
    recipe = device_status(job, "subtitled").request.recipe
    assert [m.id for m in recipe.color_moments] == ["cm1"], "the creator's own Save keeps them"


def test_clearing_works_with_the_switch_off_and_setting_does_not(monkeypatch):
    job = phone_job(monkeypatch)
    _open_gate(monkeypatch)
    save(job, color_moments=gj.EditorCommitColorMoments(**LANE))
    _open_gate(monkeypatch, enabled=False)
    base = job.assembly_plan["variants"][0]["render_generation_id"]
    changed = copy.deepcopy(LANE)
    changed["moments"][0]["tone"] = "warm"
    with pytest.raises(HTTPException) as refused:
        gj.prepare_editor_commit(
            job,
            "subtitled",
            gj.EditorCommitRequest(
                base_generation=base, color_moments=gj.EditorCommitColorMoments(**changed)
            ),
            user_id="owner",
            plan_item_id="item",
        )
    assert refused.value.detail["code"] == "color_moments_disabled"
    # Clearing never needs the switch (failure mode 5's escape hatch).
    _open_gate(monkeypatch, enabled=False)
    gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(base_generation=base, color_moments=None),
        user_id="owner",
        plan_item_id="item",
    )
    assert "color_moments" not in job.assembly_plan["variants"][0]
    assert not device_status(job, "subtitled").request.recipe.color_moments


def test_overlapping_windows_are_refused_at_save(monkeypatch):
    job = phone_job(monkeypatch)
    _open_gate(monkeypatch)
    overlapping = copy.deepcopy(LANE)
    overlapping["moments"].append({"id": "cm2", "start": 2.0, "end": 3.0, "tone": "warm"})
    with pytest.raises(HTTPException) as refused:
        save(job, color_moments=gj.EditorCommitColorMoments(**overlapping))
    assert refused.value.detail["code"] == "color_moments_invalid"


# --- rollout gates (failure modes 4, 5) ------------------------------------------------------


def test_an_unverified_token_refuses_the_recipe(monkeypatch):
    job = phone_job(monkeypatch)
    _open_gate(monkeypatch)
    save(job, color_moments=gj.EditorCommitColorMoments(**LANE))
    recipe = device_status(job, "subtitled").request.recipe
    _open_gate(monkeypatch, verified=False)
    with pytest.raises(phone_rollout.PhoneCapabilityUnavailable) as refused:
        phone_rollout.validate_phone_pilot_recipe(recipe, allow_editor_media=True)
    assert refused.value.capability == "colorMoments"


@pytest.mark.parametrize(("protocol", "allowed"), [(None, False), (47, False), (48, True)])
def test_old_builds_get_426_and_new_ones_the_recipe(monkeypatch, protocol, allowed):
    from app.routes import device_render
    from app.services.client_protocol import clear_request_context, set_client_protocol

    job = phone_job(monkeypatch)
    _open_gate(monkeypatch)
    save(job, color_moments=gj.EditorCommitColorMoments(**LANE))
    status = device_status(job, "subtitled")
    try:
        set_client_protocol(protocol)
        detail = device_render._client_update_detail(job, {}, status)
    finally:
        clear_request_context()
    if allowed:
        assert detail is None
    else:
        assert detail == {
            "code": "client_update_required",
            "feature": "color_moments",
            "minimum_client_protocol": 48,
        }


def test_supported_needs_switch_token_and_build(monkeypatch):
    from app.config import Settings

    assert Settings.model_fields["color_moments_enabled"].default is False
    assert Settings.model_fields["color_moments_min_client_protocol"].default == 48
    _open_gate(monkeypatch)
    assert phone_rollout.color_moments_supported(require_client=False)
    _open_gate(monkeypatch, enabled=False)
    assert not phone_rollout.color_moments_supported(require_client=False)
    _open_gate(monkeypatch, verified=False)
    assert not phone_rollout.color_moments_supported(require_client=False)


# --- chat op (failure modes 2, 6, 7) ---------------------------------------------------------


def _replay(name: str) -> dict:
    result = run_eval(load_fixture(GOLDEN / f"{name}.json"))
    assert result.passed, result.summary()
    return result.output


def test_explicit_ask_becomes_one_op_with_the_sound_on_the_exit_edge():
    output = _replay("color_moments_bw_light_leak_whoosh")
    assert [op["op"] for op in output["ops"]] == ["set_color_moments"]
    (moment,) = output["ops"][0]["moments"]
    assert (moment["start"], moment["end"], moment["tone"]) == (16.0, 22.0, "mono")
    assert moment["exit"]["style"] == "light_leak" and "enter" not in moment
    assert moment["sound"] == {"effect_id": "whoosh-fast", "at": "exit"}


def test_turkish_ask_sets_the_window_and_the_end_fade():
    output = _replay("color_moments_turkish_end_fade")
    (op,) = output["ops"]
    assert op["moments"][0]["enter"]["style"] == "flash_white"
    assert op["end_fade_s"] == 1.6


@pytest.mark.parametrize(
    ("name", "reason", "reply"),
    [
        ("color_moments_gate_closed_refusal", "capability_unavailable", "not available here yet"),
        ("color_moments_unasked_refusal", "not_requested", "only change the colours when you ask"),
    ],
)
def test_refusals_leave_no_op_and_never_claim_success(name, reason, reply):
    output = _replay(name)
    assert output["ops"] == []
    assert output["rejection_reasons"][0]["reason"] == reason
    assert reply in output["reply"]


def _draft_state(variant: dict, sound_effects: list | None = None):
    return SimpleNamespace(
        variant=variant,
        changed=set(),
        color_moments=None,
        sound_effects=list(sound_effects or []),
        summary=None,
    )


def test_the_sound_lands_exactly_on_its_edge_and_is_replaced_with_the_moments():
    hand_placed = {"id": "mine", "sound_effect_id": "pop", "at_s": 3.0}
    state = _draft_state({"variant_id": "subtitled"}, [hand_placed])
    op = {
        "moments": [
            {
                **ColorMoment(
                    id="cm1", start=16, end=22, tone="mono", exit={"style": "light_leak"}
                ).model_dump(mode="json"),
                "sound": {"effect_id": "whoosh-fast", "at": "exit"},
            }
        ]
    }
    lane._op_set_color_moments(state, op)
    assert state.changed == {"color_moments", "sound_effects"}
    assert state.color_moments["moments"][0]["exit"]["style"] == "light_leak"
    assert "sound" not in state.color_moments["moments"][0]
    sounds = [row for row in state.sound_effects if row["id"].startswith(lane.SOUND_ID_PREFIX)]
    assert [(s["sound_effect_id"], s["at_s"]) for s in sounds] == [("whoosh-fast", 22.0)]
    assert hand_placed in state.sound_effects
    # Replacing the moments replaces their sound, never the creator's own one.
    lane._op_set_color_moments(state, {"moments": []})
    assert state.sound_effects == [hand_placed]
    assert state.color_moments["moments"] == []


def test_the_commit_request_carries_the_lane_tri_state():
    request = gj.EditorCommitRequest(base_generation="x", color_moments=None)
    assert "color_moments" in request.model_fields_set
    untouched = gj.EditorCommitRequest(base_generation="x")
    assert "color_moments" not in untouched.model_fields_set
    from app.services.kria_editor_ops import editor_payload_dump

    assert editor_payload_dump(request)["color_moments"] is None
    assert "color_moments" not in editor_payload_dump(untouched)


# --- Range trims move the moments and their sounds together (review of 8e2a3615e) -----------


def _range_job_with_moment(monkeypatch, *, start: float, end: float, sound_at: float):
    """The Range fixture's 10 s Talking take (kept [0,4) and [5,10), so output 4.0 s = source
    5.0 s) with one moment and its own enter sound."""
    from app.kria.device_render import make_device_request
    from app.services.device_render import pin_device_request
    from tests.routes import test_phone_talking_range_commit as rng

    job = rng.talking_job(monkeypatch)
    _open_gate(monkeypatch)
    status = device_status(job, "subtitled")
    recipe = status.request.recipe
    sfx_track = next(t for t in recipe.tracks if t.id == "sfx")
    clip = sfx_track.clips[0]
    moved = clip.model_copy(update={"id": "sfx-cmsfx-cm1-enter", "timeline_start": sound_at})
    tracks = [
        t.model_copy(update={"clips": [moved]}) if t.id == "sfx" else t for t in recipe.tracks
    ]
    recipe = recipe.model_copy(update={"tracks": tracks})
    variant = job.assembly_plan["variants"][0]
    variant["color_moments"] = {
        "moments": [
            {
                "id": "cm1",
                "start": start,
                "end": end,
                "tone": "mono",
                "enter": {"style": "light_leak", "duration": 1 / 3},
                "exit": {"style": "cut", "duration": 1 / 3},
            }
        ],
        "end_fade": None,
    }
    variant["sound_effects"] = [
        {"id": "cmsfx-cm1-enter", "sound_effect_id": "pop", "at_s": sound_at, "source": "user"}
    ]
    from app.pipeline.phone_recipe_shared import with_color_moments as wcm

    pin_device_request(
        job,
        make_device_request(
            job_id=job.id,
            variant_id="subtitled",
            revision=2,
            recipe=wcm(recipe, variant["color_moments"]),
        ),
        base_generation="first",
    )
    return job, rng


def _sound_starts(job) -> list[float]:
    recipe = device_status(job, "subtitled").request.recipe
    return [c.timeline_start for t in recipe.tracks if t.id == "sfx" for c in t.clips]


def test_a_range_trim_moves_the_moment_and_its_sound_together(monkeypatch):
    # Moment + sound at output 6.0 s (source 7.0 s); trimming the first second moves both to 5.0.
    job, rng = _range_job_with_moment(monkeypatch, start=6.0, end=7.5, sound_at=6.0)
    rng.save(job, [rng._row(rng._SLOT0, 1.0, 3.0), rng._row(rng._SLOT1, 5.0, 5.0)])

    recipe = device_status(job, "subtitled").request.recipe
    variant = job.assembly_plan["variants"][0]
    assert [(m.start, m.end) for m in recipe.color_moments] == [pytest.approx((5.0, 6.5))]
    assert variant["color_moments"]["moments"][0]["start"] == pytest.approx(5.0)
    assert _sound_starts(job) == [pytest.approx(5.0)]
    assert [r["at_s"] for r in variant["sound_effects"]] == [pytest.approx(5.0)]


def test_a_moment_whose_first_frames_are_trimmed_keeps_its_sound_on_the_new_edge(monkeypatch):
    # Moment 0.5-3.0 s with its sound at 0.5 s; the trim removes source 0-1, so the moment now
    # starts at 0.0 and its sound must follow it there instead of vanishing with the footage.
    job, rng = _range_job_with_moment(monkeypatch, start=0.5, end=3.0, sound_at=0.5)
    rng.save(job, [rng._row(rng._SLOT0, 1.0, 3.0), rng._row(rng._SLOT1, 5.0, 5.0)])

    recipe = device_status(job, "subtitled").request.recipe
    assert [(m.start, m.end) for m in recipe.color_moments] == [pytest.approx((0.0, 2.0))]
    assert _sound_starts(job) == [pytest.approx(0.0)]


# --- removal with the gate closed: chat and old builds (review of 8e2a3615e) ----------------


def _stored_job(monkeypatch):
    job = phone_job(monkeypatch)
    _open_gate(monkeypatch)
    save(job, color_moments=gj.EditorCommitColorMoments(**LANE))
    return job


def _snapshot(job):
    from app.services.kria_editor_ops import _color_moments_snapshot, color_moments_removal_only

    variant = job.assembly_plan["variants"][0]
    return color_moments_removal_only(job, variant), _color_moments_snapshot(
        variant, removal_only=color_moments_removal_only(job, variant)
    )


def test_a_closed_gate_still_offers_chat_removal_of_stored_moments(monkeypatch):
    job = _stored_job(monkeypatch)
    _open_gate(monkeypatch, enabled=False)
    removal_only, snap = _snapshot(job)
    assert removal_only and snap["removal_only"] is True
    # An open gate, or a closed one on an edit with nothing stored, offers no removal-only lane.
    _open_gate(monkeypatch)
    assert _snapshot(job)[0] is False and "removal_only" not in _snapshot(job)[1]
    _open_gate(monkeypatch, enabled=False)
    empty = phone_job(monkeypatch)
    from app.services.kria_editor_ops import color_moments_removal_only

    assert color_moments_removal_only(empty, empty.assembly_plan["variants"][0]) is False


def _coerce(payload: dict, *, removal_only: bool, utterance="remove the black and white"):
    snapshot = {
        "allowed_op_families": ["color_moments"],
        "color_moments": {
            "moments": [],
            "duration_s": 10.0,
            **({"removal_only": True} if removal_only else {}),
        },
    }
    rejected: list = []
    state = SimpleNamespace(
        utterance=utterance,
        reply_notes=[],
        reject=lambda **kw: rejected.append(kw),
        invalid_value=lambda text: rejected.append({"reason": "invalid", "detail": text}),
    )
    return lane._coerce("set_color_moments", payload, snapshot, state), rejected


@pytest.mark.parametrize(
    "payload",
    [{"moments": []}, {"end_fade_s": 0}, {"moments": [], "end_fade_s": 0}],
)
def test_removal_only_chat_accepts_taking_moments_off(payload):
    clean, rejected = _coerce(payload, removal_only=True)
    assert clean is not None and not rejected


@pytest.mark.parametrize(
    "payload",
    [
        {"moments": [{"start_s": 1, "end_s": 3, "tone": "mono"}]},
        {"end_fade_s": 1.6},
        {"moments": [], "end_fade_s": 1.0},
    ],
)
def test_removal_only_chat_still_refuses_new_effects(payload):
    clean, rejected = _coerce(payload, removal_only=True, utterance="add a black and white fade")
    assert clean is None
    assert rejected[0]["reason"] == "capability_unavailable"
    assert rejected[0]["detail"] == lane.REMOVAL_ONLY_NOTE


def test_a_chat_removal_saves_with_the_switch_off_and_new_moments_do_not(monkeypatch):
    job = _stored_job(monkeypatch)
    _open_gate(monkeypatch, enabled=False)
    base = job.assembly_plan["variants"][0]["render_generation_id"]
    stored = job.assembly_plan["variants"][0]["color_moments"]
    # The chat op's document: moments taken off, the stored end fade kept.
    gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(
            base_generation=base,
            color_moments=gj.EditorCommitColorMoments(moments=[], end_fade=stored["end_fade"]),
        ),
        user_id="owner",
        plan_item_id="item",
    )
    variant = job.assembly_plan["variants"][0]
    assert variant["color_moments"]["moments"] == []
    recipe = device_status(job, "subtitled").request.recipe
    assert not recipe.color_moments and recipe.end_fade is not None
    # A longer fade is new, so it is still refused with the switch off.
    with pytest.raises(HTTPException) as refused:
        gj.prepare_editor_commit(
            job,
            "subtitled",
            gj.EditorCommitRequest(
                base_generation=variant["render_generation_id"],
                color_moments=gj.EditorCommitColorMoments(moments=[], end_fade={"duration": 2.0}),
            ),
            user_id="owner",
            plan_item_id="item",
        )
    assert refused.value.detail["code"] == "color_moments_disabled"


@pytest.mark.parametrize("protocol", [None, 47])
def test_an_old_build_can_clear_the_moments_but_not_keep_them(monkeypatch, protocol):
    from app.services.client_protocol import clear_request_context, set_client_protocol

    job = _stored_job(monkeypatch)
    try:
        set_client_protocol(protocol)
        base = job.assembly_plan["variants"][0]["render_generation_id"]
        # A Save that leaves the moments in place would pin a recipe this build cannot read.
        with pytest.raises(HTTPException) as refused:
            gj.prepare_editor_commit(
                job,
                "subtitled",
                gj.EditorCommitRequest(base_generation=base, caption_meta={"enabled": True}),
                user_id="owner",
                plan_item_id="item",
            )
        assert refused.value.status_code == 426
        # Clearing them goes through.
        gj.prepare_editor_commit(
            job,
            "subtitled",
            gj.EditorCommitRequest(base_generation=base, color_moments=None),
            user_id="owner",
            plan_item_id="item",
        )
    finally:
        clear_request_context()
    assert "color_moments" not in job.assembly_plan["variants"][0]
    recipe = device_status(job, "subtitled").request.recipe
    assert not recipe.color_moments and recipe.end_fade is None


def test_a_closed_gate_replay_takes_the_moment_off():
    output = _replay("color_moments_removal_only_clear")
    assert output["ops"] == [{"op": "set_color_moments", "moments": []}]
    assert output["rejection_reasons"] == []


def test_a_closed_gate_replay_still_refuses_a_new_fade():
    output = _replay("color_moments_removal_only_refuses_new")
    assert output["ops"] == []
    assert output["rejection_reasons"][0]["detail"] == lane.REMOVAL_ONLY_NOTE
    assert "fades slower" not in output["reply"]
