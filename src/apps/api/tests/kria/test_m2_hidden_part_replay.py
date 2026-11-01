"""Leverkusen M2 (2026-10-30): "don't show the kids at the start of the second goal clip".

Prod eed794157, thread d048973b, plan item 0f4b842f. IMG_1039 (18.3 s) opens on another
family's children waving scarves (0.0-2.5 s), then the "TOR" on the big screen (6.0-7.5 s)
and Taha's scream (12.0-13.8 s): the clip must stay, its first seconds must never play. Prod
had no way to say that: `exclude` drops whole files, the creator's plain answers ("keep that
clip, just start it after the kids (after about 2.5 seconds)", "don't show the first 3
seconds") were rejected as an invented op, and a montage window could open on the kids.

``tests/fixtures/m2_leverkusen_hidden_part.json`` holds the real uploads (ids, lengths,
hashes) and the kit answer key's timed observations of the footage, used as the clips'
analysed moments with the kids ranked highest, so a planner that ignores the ask opens on
them. The replay runs the real planner parser, the real length planner, the real montage
planner, the strict guided compiler, the phone recipe compiler and the pinned-contract check.

Failure modes (written before the fix; `.dev` list in the PR) pinned here:
P1 off-by-one frame at the edge; P3 the compiler or a writer ignoring the window (the pinned
recipe check); P4 a re-draft losing the part; P5 another writer / the cloud; P7 approved
length-plan windows; P8 the analysis fallback; P12 no number -> ask; P13 a number the creator
never wrote; P15 a part covering the clip; N1 never an `exclude`; N2 never an invented op.

Set M2_HIDDEN_PART_KEEP_DIR to keep the compiled recipe (JSON) for the frame check.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.agents.clip_intent_planner import ClipIntentPlannerAgent, ClipIntentPlannerInput
from app.config import settings
from app.kria.hidden_parts import carry_hidden_parts, hidden_part_question
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_execution_plan
from app.pipeline.hidden_parts import HIDDEN_EDGE_GUARD_S, HIDDEN_PART_REFUSAL, HiddenPartShown
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.unified_montage import UnifiedClip, plan_unified_montage
from app.schemas.clip_intents import ClipAssignment, ResolvedClipIntent
from app.services.clip_intent_resolution import IntentClip
from app.services.cloud_render_contract import CloudRenderContractError, preflight_cloud_contract
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    CreatorRenderContractError,
    build_render_contract,
    verify_phone_recipe,
)
from app.services.length_planning import build_length_plan
from app.services.phone_sources import PhoneSourceBinding

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "m2_leverkusen_hidden_part.json").read_text(
        encoding="utf-8"
    )
)
GOAL_1 = FIXTURE["clips"][0]["media_id"]  # IMG_1037
GOAL_2 = FIXTURE["clips"][1]["media_id"]  # IMG_1039: the kids, the TOR, the scream
KIDS_END_S = FIXTURE["answer_key"][GOAL_2]["kids"][1]
KEEP_DIR = os.environ.get("M2_HIDDEN_PART_KEEP_DIR")


def _keep(name: str, payload: object) -> None:
    if KEEP_DIR:
        Path(KEEP_DIR).mkdir(parents=True, exist_ok=True)
        (Path(KEEP_DIR) / name).write_text(json.dumps(payload, indent=2, default=str) + "\n")


def _hide(seconds: float | None, *, edge: str | None = "start") -> dict:
    """The resolved row the creator's ask becomes (picker or resolver settled the clip)."""
    return ResolvedClipIntent(
        intent_id="hide_kids",
        op="hide_part",
        attribute="the goal clip with the kids",
        part_edge=edge,
        part_s=seconds,
        part_attribute="the kids",
        source_kind="video",
        status="resolved",
        assignments=[ClipAssignment(media_id=GOAL_2, evidence="creator selected", confidence=1.0)],
    ).model_dump(mode="json")


def _strategy(*rows: dict) -> dict:
    return {
        "edit_format": "montage",
        "render_program": "guided",
        "audio_strategy": "original_audio",
        "media_scope": "all",
        "selected_media_ids": [],
        "target_duration_s": 12,
        "montage_audio": {
            "source_media_ids": [],
            "preserve_source_audio": True,
            "preview_source_beds": False,
        },
        "resolved_clip_intents": list(rows),
    }


def _clips() -> list[UnifiedClip]:
    return [
        UnifiedClip(
            media_id=row["media_id"],
            proxy_path=row["gcs_path"],
            generation="1",
            duration_s=row["original"]["duration_s"],
            width=1080,
            height=1920,
            analysis={"best_moments": row["best_moments"]},
        )
        for row in FIXTURE["clips"]
    ]


def _snapshot() -> dict:
    """The media snapshot approval pins: both goal clips with their phone originals."""
    return {
        "clip_paths": [row["gcs_path"] for row in FIXTURE["clips"]],
        "clip_assignments": [
            {
                "media_id": row["media_id"],
                "gcs_path": row["gcs_path"],
                "kind": "video",
                "duration_s": row["proxy_duration_s"],
                "has_audio": True,
                "generation": "1",
                "storage_generation": "1",
                "analysis": {"best_moments": row["best_moments"]},
                "upload_contract": {
                    "purpose": "analysis_proxy",
                    "proxy": {
                        "width": 320,
                        "height": 568,
                        "duration_s": row["proxy_duration_s"],
                        "frame_rate": 30.0,
                        "timing_version": 1,
                        "orientation_degrees": 0,
                        "original": row["original"],
                    },
                },
            }
            for row in FIXTURE["clips"]
        ],
    }


def _bindings() -> tuple[PhoneSourceBinding, ...]:
    return tuple(
        PhoneSourceBinding(
            media_id=row["media_id"],
            proxy_path=row["gcs_path"],
            generation="1",
            original=OriginalMediaDescriptor.model_validate(row["original"]),
        )
        for row in FIXTURE["clips"]
    )


def _goal_2_windows(cuts) -> list[tuple[float, float]]:  # noqa: ANN001
    return [(c.source_start_s, c.source_end_s) for c in cuts if c.media_id == GOAL_2]


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", True)


# -- the creator's words become one hidden part, never an invented op (N2) or an exclude (N1)


@pytest.mark.parametrize(
    ("turn", "messages", "seconds"),
    [("turn1", 1, None), ("turn3", 3, 2.5), ("turn4", 4, 3.0)],
    ids=["prompt-no-number", "start-it-after-2.5s", "first-3-seconds"],
)
def test_each_kids_message_is_one_hidden_part_at_the_start(
    turn: str, messages: int, seconds: float | None
) -> None:
    said = FIXTURE["creator_messages"][:messages]
    out = ClipIntentPlannerAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(FIXTURE["planner_replies"][turn]),
        ClipIntentPlannerInput(
            creator_request="\n\n".join(said), latest_user_message=said[-1], clip_exclusions=True
        ),
    )
    assert out.salvage_question is None, out.salvage_question
    hidden = [intent for intent in out.intents if intent.op == "hide_part"]
    assert len(hidden) == 1, out.intents
    assert (hidden[0].part_edge, hidden[0].part_s, hidden[0].source_kind) == (
        "start",
        seconds,
        "video",
    )
    assert not any(intent.op == "exclude" for intent in out.intents)


def test_a_number_the_creator_never_wrote_never_decides_the_frames() -> None:
    """P13: Flash writes 4 s; the creator said 2.5. The number is dropped, so Kria asks."""
    reply = json.loads(json.dumps(FIXTURE["planner_replies"]["turn3"]))
    reply["intents"][1]["part_s"] = 4
    said = FIXTURE["creator_messages"][:3]
    out = ClipIntentPlannerAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(reply),
        ClipIntentPlannerInput(creator_request="\n\n".join(said), latest_user_message=said[-1]),
    )
    assert [intent.part_s for intent in out.intents if intent.op == "hide_part"] == [None]


def test_the_planner_prompt_teaches_the_op() -> None:
    prompt = ClipIntentPlannerAgent(None).render_prompt(  # type: ignore[arg-type]
        ClipIntentPlannerInput(creator_request="x", clip_exclusions=True)
    )
    assert "- hide_part: a part at the START or the END of ONE clip" in prompt
    assert "caption|exclude|hide_part" in prompt
    assert ClipIntentPlannerAgent.spec.prompt_version == "2026-10-31-cap-40"


# -- no number: an honest question, and nothing renders around it (P12, P15) -----------------


def _intent_clips() -> list[IntentClip]:
    return [
        IntentClip(
            media_id=row["media_id"],
            kind="video",
            analysis={},
            file_name=row["file_name"],
            duration_s=row["original"]["duration_s"],
        )
        for row in FIXTURE["clips"]
    ]


def test_no_number_asks_how_many_seconds_of_that_clip_show_the_kids() -> None:
    question = hidden_part_question([ResolvedClipIntent(**_hide(None))], _intent_clips())
    assert question is not None
    assert "How many seconds at the start of IMG_1039 show the kids?" in question
    assert "leave the whole clip out" in question
    assert hidden_part_question([ResolvedClipIntent(**_hide(3.0))], _intent_clips()) is None


def test_a_part_covering_the_clip_asks_instead_of_showing_it() -> None:
    question = hidden_part_question([ResolvedClipIntent(**_hide(18.3))], _intent_clips())
    assert question is not None and "leaves nothing of it to show" in question


def test_an_unsettled_part_is_never_planned_around() -> None:
    with pytest.raises(HiddenPartShown) as refused:
        plan_unified_montage(_clips(), strategy=_strategy(_hide(None)))
    # The worker fails the job once, with the plain sentence, never a retry loop.
    from app.tasks.generative_build import _classify_phone_plan_failure  # noqa: PLC0415

    assert _classify_phone_plan_failure(refused.value) == ("phone_plan_unsupported", "plan_defect")
    assert str(refused.value) == HIDDEN_PART_REFUSAL
    contract = build_render_contract(_strategy(_hide(None)), generation_id="g")
    assert contract is not None and contract.unresolved


# -- the plan never opens on the kids (P1, P7, P8) -------------------------------------------


def test_the_footage_owned_plan_never_opens_on_the_kids() -> None:
    """Without the ask the selector opens IMG_1039 on the kids (energy 9): the control."""
    unprotected = plan_unified_montage(_clips(), strategy=_strategy())
    assert any(
        start < KIDS_END_S for start, _end in _goal_2_windows(unprotected.snapshot.fast_cuts)
    )

    plan = plan_unified_montage(_clips(), strategy=_strategy(_hide(3.0)))
    windows = _goal_2_windows(plan.snapshot.fast_cuts)
    assert windows, "the clip with the TOR and the scream stays in the edit"
    for start, _end in windows:
        assert start >= 3.0 + HIDDEN_EDGE_GUARD_S - 1e-6, windows
    # The receipt says which seconds of that clip play.
    (row,) = plan.record()["hidden_parts"]
    assert (row["media_id"], row["edge"], row["seconds"]) == (GOAL_2, "start", 3.0)
    assert row["shown_from_s"] == min(start for start, _end in windows)


def test_the_approved_12_second_length_plan_never_opens_on_the_kids() -> None:
    envelope = build_length_plan(_strategy(_hide(3.0)), _snapshot(), creator_target_s=12.0)
    assert envelope["conflicts"] == [], envelope["conflicts"]
    windows = [w for w in envelope["windows"] if w["media_id"] == GOAL_2]
    assert windows
    assert all(w["source_start_s"] >= 3.0 + HIDDEN_EDGE_GUARD_S - 1e-6 for w in windows), windows
    total = sum(w["source_end_s"] - w["source_start_s"] for w in envelope["windows"])
    assert total == pytest.approx(12.0, abs=0.05)

    plan = plan_unified_montage(
        _clips(), strategy=_strategy(_hide(3.0)), approved_length_plan=envelope
    )
    assert all(
        start >= 3.0 + HIDDEN_EDGE_GUARD_S - 1e-6
        for start, _end in _goal_2_windows(plan.snapshot.fast_cuts)
    )


def test_about_2_5_seconds_clears_the_child_still_in_frame_at_2_7s() -> None:
    """The creator's first number was "about 2.5 seconds"; a child is still at the left edge
    at 2.6-2.7 s of IMG_1039 (observed frames, fixture ``kids_last_seen_s``). The plan's
    margin past the creator's number clears it, on the 12 s plan and on the compiled recipe."""
    last_seen = FIXTURE["answer_key"][GOAL_2]["kids_last_seen_s"]
    strategy = _strategy(_hide(2.5))
    envelope = build_length_plan(strategy, _snapshot(), creator_target_s=12.0)
    windows = [w for w in envelope["windows"] if w["media_id"] == GOAL_2]
    assert windows and all(w["source_start_s"] > last_seen for w in windows), windows
    recipe = _recipe(strategy)
    _keep("recipe_about_2_5.json", recipe.model_dump(mode="json"))
    assert all(clip.source_start > last_seen for clip in _goal_2_clips(recipe))


def test_an_approved_window_on_the_kids_is_refused_not_moved() -> None:
    """P7: a creator-approved window never moves; one that shows the part never renders."""
    envelope = build_length_plan(_strategy(), _snapshot(), creator_target_s=12.0)
    assert any(
        w["media_id"] == GOAL_2 and w["source_start_s"] < KIDS_END_S for w in envelope["windows"]
    ), "control: planned without the ask, the length plan opens on the kids"
    with pytest.raises(HiddenPartShown):
        plan_unified_montage(
            _clips(), strategy=_strategy(_hide(3.0)), approved_length_plan=envelope
        )


# -- the pinned phone recipe is the last word (P1, P3, P5) -----------------------------------


def _recipe(strategy: dict):  # noqa: ANN202
    envelope = build_length_plan(strategy, _snapshot(), creator_target_s=12.0)
    plan = plan_unified_montage(_clips(), strategy=strategy, approved_length_plan=envelope)
    execution = GuidedStoryExecutionPlan.model_validate(
        compile_execution_plan(plan.guided_edit(), track=None)
    )
    return compile_phone_guided_plan(execution, _bindings(), ())


def _goal_2_clips(recipe):  # noqa: ANN001, ANN202
    ids = {
        asset.id
        for asset in recipe.asset_manifest.assets
        if getattr(asset, "media_id", None) == GOAL_2
    }
    return [clip for track in recipe.tracks for clip in track.clips if clip.source_asset_id in ids]


def test_the_compiled_recipe_passes_and_a_kids_frame_is_refused() -> None:
    strategy = _strategy(_hide(3.0))
    contract = build_render_contract(strategy, generation_id="g")
    assert contract is not None and [row.model_dump() for row in contract.hidden_parts] == [
        {"media_id": GOAL_2, "edge": "start", "seconds": 3.0}
    ]
    recipe = _recipe(strategy)
    _keep("recipe.json", recipe.model_dump(mode="json"))
    clips = _goal_2_clips(recipe)
    assert clips and all(clip.source_start >= 3.0 + HIDDEN_EDGE_GUARD_S - 1e-6 for clip in clips)
    verify_phone_recipe(contract, recipe, source_audio={GOAL_1: True, GOAL_2: True})

    def shifted(start: float):  # noqa: ANN202
        tampered = recipe.model_copy(deep=True)
        for track in tampered.tracks:
            for clip in track.clips:
                if clip in clips or clip.id in {c.id for c in clips}:
                    clip.source_start = start
        return tampered

    # Negative control: the same cut moved onto the kids (same length, same order).
    with pytest.raises(CreatorRenderContractError) as refused:
        verify_phone_recipe(contract, shifted(0.5), source_audio={GOAL_1: True, GOAL_2: True})
    assert refused.value.decline_reason == "evidence_missing"
    # P1: starting exactly on the creator's second can still show a kids frame.
    with pytest.raises(CreatorRenderContractError):
        verify_phone_recipe(contract, shifted(3.0), source_audio={GOAL_1: True, GOAL_2: True})


def test_a_contract_without_a_part_is_byte_identical() -> None:
    """N5: an older worker forbids unknown keys, so no other contract may carry the field."""
    contract = build_render_contract(_strategy(), generation_id="g")
    assert contract is not None
    assert "hidden_parts" not in contract.model_dump(mode="json")
    hidden = build_render_contract(_strategy(_hide(3.0)), generation_id="g")
    assert hidden is not None and hidden.digest != contract.digest


def test_the_cloud_refuses_a_hidden_part_before_rendering() -> None:
    contract = build_render_contract(_strategy(_hide(3.0)), generation_id="g")
    assert contract is not None
    with pytest.raises(CloudRenderContractError) as refused:
        preflight_cloud_contract({CONTRACT_FIELD: contract.model_dump(mode="json")})
    assert refused.value.decline_reason == "capability_unavailable"


# -- a re-draft never loses the part (P4) ----------------------------------------------------


def test_a_redraft_that_drops_the_ask_or_its_seconds_keeps_the_part() -> None:
    earlier = [ResolvedClipIntent(**_hide(3.0))]
    clips = {GOAL_1, GOAL_2}
    dropped, carried = carry_hidden_parts([], earlier, clip_ids=clips)
    assert carried == 1 and [(i.op, i.part_s) for i in dropped] == [("hide_part", 3.0)]
    fewer, carried = carry_hidden_parts([ResolvedClipIntent(**_hide(2.5))], earlier, clip_ids=clips)
    assert carried == 1 and [i.part_s for i in fewer] == [3.0]
    unsettled, _ = carry_hidden_parts([ResolvedClipIntent(**_hide(None))], earlier, clip_ids=clips)
    assert [i.part_s for i in unsettled] == [3.0]
    gone, carried = carry_hidden_parts([], earlier, clip_ids={GOAL_1})
    assert (gone, carried) == ([], 0)
