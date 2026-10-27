"""KRI-984: optional posting music cannot invalidate an approved mixed-media edit.

Replay the failed 28.804-second cut with synthetic identities and numeric source
windows. Pin the failure before changing the compiler. A short or invalid
reference must not drop media, change duration, add music or demand new consent.
"""

import copy
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.kria import planner
from app.kria.brief import CreativeBrief
from app.kria.contracts import KriaToolIntent, KriaTurnPlan
from app.kria.planner import _enrich_best_parts_snapshot
from app.pipeline.guided_story import (
    GuidedStoryExecutionPlan,
    _song_reference,
    compile_execution_plan,
    validate_execution_plan,
)
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.unified_montage import brief_view, plan_unified_montage
from app.services.choice_questions import tag_event
from tests.pipeline.test_unified_montage import clip, visual
from tests.pipeline.user_song_helpers import bindings_for

CLIPS = [clip(i, duration=8.0) for i in range(11)]
PHOTOS = [visual(i + 1) for i in range(10)]
SECTION = {
    "status": "selected",
    "track_id": "00000000-0000-4000-8000-000000000099",
    "title": "Cunda replay song",
    "artist": "Fixture",
    "start_s": 133.19,
    "end_s": 149.99,
    "catalog_duration_s": 260.976,
    "beat_timestamps_s": [],
}
WINDOWS = [
    (0, 1.683),
    (3, 3.84),
    (0, 1.5),
    (0, 1),
    (0, 1.683),
    (0, 1.683),
    (0, 1.683),
    (0, 1.683),
    (4.5, 6.183),
    (0, 1.683),
    (0, 1.683),
]


def strategy():
    return {
        "edit_format": "montage",
        "render_program": "guided",
        "direction": "guided_story",
        "pacing": "balanced",
        "media_scope": "all",
        "selected_media_ids": [],
        "audio_strategy": "licensed_music",
        "target_duration_s": 35,
        "selected_music_section": copy.deepcopy(SECTION),
    }


def snapshot(draft):
    return _enrich_best_parts_snapshot(
        draft,
        {
            "clip_assignments": [
                {
                    "media_id": c.media_id,
                    "kind": "video",
                    "gcs_path": c.proxy_path,
                    "storage_generation": c.generation,
                    "duration_s": c.duration_s,
                    "analysis": {},
                }
                for c in CLIPS
            ]
        },
        CreativeBrief(),
        visual_photo_ids=[p.manifest_id for p in PHOTOS],
    )


@pytest.mark.parametrize(
    "section", [SECTION, {**SECTION, "end_s": "invalid"}, {**SECTION, "end_s": 130.0}, None]
)
def test_exact_failed_approval_compiles_all_media_without_rendered_music(section):
    draft = strategy()
    approved = snapshot(draft)["length_plan"]
    for row, (start, end) in zip(approved["windows"], WINDOWS, strict=True):
        row.update(source_start_s=start, source_end_s=end)
    plan = plan_unified_montage(
        CLIPS,
        brief_view(CreativeBrief()),
        strategy=draft,
        visuals=PHOTOS,
        approved_length_plan=approved,
    )
    assert plan.duration_s == pytest.approx(28.804)
    guided = plan.guided_edit()
    execution = compile_execution_plan(guided, track=section)
    assert validate_execution_plan(execution, guided) == execution
    assert execution["music"] is None
    assert len(plan.snapshot.fast_cuts) == 21
    footage, stills = bindings_for(plan)
    recipe = compile_phone_guided_plan(
        GuidedStoryExecutionPlan.model_validate(execution), footage, stills
    )
    assert recipe.duration == pytest.approx(28.804, abs=1 / 30)
    assert sum(len(track.clips) for track in recipe.tracks if track.kind == "video") == 21
    assert execution.get("song_reference") is None


@pytest.mark.parametrize("duration", [16.8, 16.804, 16.8 + 1 / 30, 28.804])
def test_posting_reference_never_extends_selected_section(duration):
    reference = _song_reference(SECTION, duration_s=duration)
    if duration == 16.8:
        assert reference["start_s"] == SECTION["start_s"]
        assert reference["end_s"] == SECTION["end_s"]
    else:
        assert reference is None


async def test_planning_gate_does_not_trade_photos_for_optional_music(monkeypatch):
    for key in [
        "kria_choice_questions_enabled",
        "kria_creative_brief_enabled",
        "kria_brief_binding_enabled",
    ]:
        monkeypatch.setattr(settings, key, True)
    monkeypatch.setattr(settings, "kria_creative_brief_user_ids", [])
    monkeypatch.setattr(settings, "kria_brief_binding_user_ids", [])
    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    events = [tag_event("user", {}, "user_message", "Try again")]
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=events))
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=CreativeBrief()))
    monkeypatch.setattr(
        planner, "_freeze_licensed_music_section", AsyncMock(return_value=copy.deepcopy(SECTION))
    )
    draft = strategy()
    proposed = planner.PlannedKriaTurn(
        plan=KriaTurnPlan(
            mode="act",
            turn_value="action",
            intents=[
                KriaToolIntent(
                    intent_id="apply",
                    tool_name="draft.apply_strategy",
                    tool_version=1,
                    arguments={"strategy": draft, "summary": "Cunda montage"},
                )
            ],
        ),
        media_snapshot=snapshot(draft),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
        brief_manifest=SimpleNamespace(
            media=[SimpleNamespace(kind="image", media_id=p.manifest_id) for p in PHOTOS]
        ),
    )
    result = await planner._gate_unresolved_choices(
        SimpleNamespace(), proposed, thread_id=uuid.uuid4(), creator_id=uuid.uuid4()
    )
    assert result.plan.mode == "act"
    assert result.plan.choice_question is None
    chosen = result.plan.intents[0].arguments["strategy"]
    assert chosen["media_scope"] == "all"
    assert chosen.get("creator_duration_s") is None
    assert chosen["selected_music_section"] == SECTION
