"""KRI-515: a runtime-v2 montage must render exactly the clips the Creator picked.

Incident (prod thread 0b1f9556, 2026-10-07, stress kit M3 "Berlin"): 12 clips, one a
byte-identical duplicate, "order by filming time ... if two videos are the same, use one".
The Creator selected 11 clips (``media_scope="selected"``) and the render contract pinned
those 11 in filming order. Dispatch only narrowed ``clip_paths`` for native renders, so the
proposal-less unified phone montage planned all 12 and the phone verifier refused it:
"This edit couldn't keep the confirmed clip order."

The end-to-end tests run the REAL dispatch narrowing, planner, guided compiler, phone
compiler, contract builder and verifier (the KRI-503 harness).
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_execution_plan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.unified_montage import brief_view, plan_unified_montage
from app.services.creator_render_contract import (
    CreatorRenderContractError,
    build_render_contract,
    verify_phone_recipe,
)
from app.services.phone_sources import PhoneSourceBinding
from app.tasks.content_plan_build import _creator_selected_clip_paths
from tests.pipeline.test_unified_montage import T0, clip
from tests.tasks.test_content_plan_build import _V2_PHONE_DISPATCH, _run_phone_dispatch

# The M3 kit in its scrambled attachment order: minutes after 07:00 it was filmed.
# c0 and c5 are the same U-Bahn file (attached twice); c9 is Elif, filmed last.
MINUTES = [48, 425, 715, 200, 885, 48, 750, 340, 12, 970, 95, 630]
DUPLICATE = 5
ELIF = 9
DURATIONS = {2: 1.433, ELIF: 7.2}


@pytest.fixture(autouse=True)
def _clip_intents_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)


def _id(index: int) -> str:
    return f"c{index}"


def _clips():
    return [
        clip(i, minutes=MINUTES[i], duration=DURATIONS.get(i, 5.0)) for i in range(len(MINUTES))
    ]


def _item():
    return type(
        "Item",
        (),
        {
            "clip_assignments": [
                {"media_id": c.media_id, "gcs_path": c.proxy_path} for c in _clips()
            ]
        },
    )()


def _strategy(selected: list[int], **extra) -> dict:
    return {
        "edit_format": "montage",
        "render_program": "guided",
        "media_scope": "selected",
        "selected_media_ids": [_id(i) for i in selected],
        "target_duration_s": 25,
        "target_duration_requested": True,
        "resolved_clip_intents": [
            {
                "op": "order",
                "status": "resolved",
                "intent_id": "order_elif_voice_end",
                "attribute": "Elif's original voice clip",
                "position": "last",
                "assignments": [{"media_id": _id(ELIF), "confidence": 0.9}],
            },
            {
                "op": "order",
                "status": "resolved",
                "intent_id": "order_chrono",
                "attribute": "all clips",
                "order_by": "capture_time",
                "assignments": [
                    {"media_id": _id(i), "confidence": 1.0} for i in range(len(MINUTES))
                ],
            },
        ],
        **extra,
    }


# The Creator's pick in the incident: every clip but the second copy, listed in its own order.
PICKED = [i for i in (3, 8, 1, 7, 10, 0, 4, 6, 2, 11, ELIF) if i != DUPLICATE]


def _contract(strategy: dict):
    snapshot = {
        "clip_assignments": [
            {
                "media_id": _id(i),
                "kind": "video",
                "capture": {
                    "capture_time": (T0 + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")
                },
            }
            for i, m in enumerate(MINUTES)
        ]
    }
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="order",
                scope="global",
                description="chronological from morning to night",
                facts={"key": "capture_time"},
            ),
            BriefRequirement(
                id="r2", kind="timing", scope="global", description="25 s", facts={"duration_s": 25}
            ),
        ],
    )
    binding = BriefBinding.create(
        uuid.uuid5(uuid.NAMESPACE_URL, "kri-515"),
        brief,
        latest_message="order by filming time, use one of two identical videos",
        media_snapshot=snapshot,
    )
    resolved = binding.resolve()
    contract = build_render_contract(
        strategy, generation_id="gen", brief=resolved, media_snapshot=binding.media_snapshot
    )
    return resolved, contract


def _dispatched_clips(strategy: dict, *, without_proposal: bool):
    every = _clips()
    paths = _creator_selected_clip_paths(
        _item(), [c.proxy_path for c in every], strategy, without_proposal=without_proposal
    )
    return [c for c in every if c.proxy_path in paths]


def _verify(contract, plan, clips) -> list[dict]:
    bindings = tuple(
        PhoneSourceBinding(
            media_id=c.media_id,
            proxy_path=c.proxy_path,
            generation=c.generation,
            original=OriginalMediaDescriptor(
                sha256="a" * 64,
                byte_count=1000,
                duration_s=c.duration_s,
                width=1080,
                height=1920,
                has_audio=True,
            ),
        )
        for c in clips
    )
    execution = compile_execution_plan(plan.guided_edit(), track=None)
    recipe = compile_phone_guided_plan(GuidedStoryExecutionPlan.model_validate(execution), bindings)
    return verify_phone_recipe(contract, recipe, source_audio={c.media_id: True for c in clips})


# -- the incident, end to end ----------------------------------------------------------


def test_incident_dispatch_hands_the_montage_exactly_the_pinned_clips_and_it_verifies() -> None:
    strategy = _strategy(PICKED)
    brief, contract = _contract(strategy)
    clips = _dispatched_clips(strategy, without_proposal=True)

    assert _id(DUPLICATE) not in [c.media_id for c in clips]
    plan = plan_unified_montage(
        clips, brief_view(brief), strategy=strategy, clip_intents_enabled=True
    )
    assert tuple(plan.clip_ids) == contract.order_ids
    assert plan.clip_ids[-1] == _id(ELIF)
    assert _verify(contract, plan, clips)


def test_incident_reproduction_every_attached_clip_is_refused_by_the_verifier() -> None:
    """The pre-fix dispatch: the montage plans the duplicate the Creator left out."""
    strategy = _strategy(PICKED)
    brief, contract = _contract(strategy)
    clips = _dispatched_clips(strategy, without_proposal=False)

    assert len(clips) == len(MINUTES)
    plan = plan_unified_montage(
        clips, brief_view(brief), strategy=strategy, clip_intents_enabled=True
    )
    assert _id(DUPLICATE) in plan.clip_ids
    with pytest.raises(CreatorRenderContractError, match="confirmed clip order"):
        _verify(contract, plan, clips)


# -- the narrowing rule ----------------------------------------------------------------


def _paths(indices) -> list[str]:
    every = _clips()
    return [every[i].proxy_path for i in indices]


ALL = list(range(len(MINUTES)))


def test_selection_keeps_attachment_order_not_the_models_listing_order() -> None:
    strategy = _strategy([7, 2, 0])
    assert _creator_selected_clip_paths(
        _item(), _paths(ALL), strategy, without_proposal=True
    ) == _paths([0, 2, 7])


def test_a_named_camera_audio_source_stays_even_outside_the_selection() -> None:
    strategy = _strategy(
        [0, 2], montage_audio={"preserve_source_audio": True, "source_media_ids": [_id(ELIF)]}
    )
    assert _creator_selected_clip_paths(
        _item(), _paths(ALL), strategy, without_proposal=True
    ) == _paths([0, 2, ELIF])


@pytest.mark.parametrize(
    "strategy",
    [
        _strategy(PICKED, media_scope="all"),  # "all" never narrows
        _strategy([]),  # an empty pick means everything
        _strategy([], selected_media_ids=["asset-photo-1"]),  # names no attached clip
    ],
    ids=["scope-all", "empty", "visuals-only"],
)
def test_no_explicit_clip_subset_keeps_every_clip(strategy: dict) -> None:
    assert _creator_selected_clip_paths(
        _item(), _paths(ALL), strategy, without_proposal=True
    ) == _paths(ALL)


def test_a_proposal_backed_guided_job_is_unchanged() -> None:
    """The approved proposal owns the exact media choice there (byte-identical dispatch)."""
    assert _creator_selected_clip_paths(_item(), _paths(ALL), _strategy(PICKED)) == _paths(ALL)


# -- the real dispatch -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dispatch_kwargs", "approved", "narrowed"),
    [
        (_V2_PHONE_DISPATCH, False, True),  # runtime-v2 approval: no proposal
        ({}, True, False),  # the approved proposal owns media choice
    ],
    ids=["v2-no-proposal", "approved-proposal"],
)
def test_phone_dispatch_binds_and_builds_only_the_picked_clips_without_a_proposal(
    monkeypatch: pytest.MonkeyPatch, dispatch_kwargs: dict, approved: bool, narrowed: bool
) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_phone_enabled", True)
    monkeypatch.setattr(settings, "kria_runtime_v2_phone_user_ids", [])
    strategy = {
        "edit_format": "montage",
        "render_program": "guided",
        "media_scope": "selected",
        # The fixture's clips are registered-spine-0..2; the Creator dropped the middle one.
        "selected_media_ids": ["registered-spine-2", "registered-spine-0"],
    }
    result, _job, mock_build, bind_mock = _run_phone_dispatch(
        monkeypatch,
        edit_format="montage",
        approved=approved,
        clip_count=3,
        dispatch_kwargs={**dispatch_kwargs, "creator_strategy": strategy},
    )

    assert result.outcome == "dispatched"
    every = [f"users/u/plan/i/analysis-proxy-source-{n}.mp4" for n in range(3)]
    expected = [every[0], every[2]] if narrowed else every
    assert mock_build.call_args.kwargs["clip_paths"] == expected
    assert bind_mock.call_args.args[1] == expected
