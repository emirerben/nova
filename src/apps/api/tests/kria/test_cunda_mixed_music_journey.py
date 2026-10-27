"""Replay the 27 October Cunda failure without private media or a model call.

Production approved eleven video windows inside a 16.8 s song section, then added
ten 1.2 s photos. The resulting 28.804 s timeline failed during phone compilation.

Failure modes pinned before the fix: approval precedes the timing choice; an answer
extends music without consent or beyond the catalog; photos disappear silently;
the same answer loops; a changed media pool reuses stale consent; frame rounding
rejects a valid video-only cut; the compiler accepts a genuinely excessive section.
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
    GuidedStoryError,
    GuidedStoryExecutionPlan,
    _song_reference,
    compile_execution_plan,
)
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.unified_montage import brief_view, plan_unified_montage, selected_visual_ids
from app.services.choice_questions import (
    build_choice_question,
    collect_conflicts,
    resolve_choices,
    tag_event,
)
from tests.pipeline.test_unified_montage import clip, visual
from tests.pipeline.user_song_helpers import bindings_for

CLIPS = [clip(i, duration=4.0) for i in range(11)]
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


def strategy(**changes):
    return {
        "edit_format": "montage",
        "render_program": "guided",
        "direction": "guided_story",
        "pacing": "balanced",
        "media_scope": "all",
        "selected_media_ids": [],
        "audio_strategy": "licensed_music",
        "target_duration_s": 35,  # Model proposal; the creator never asked for it.
        "selected_music_section": copy.deepcopy(SECTION),
        **changes,
    }


def snapshot(draft, photos=PHOTOS):
    selected = set(draft.get("selected_media_ids") or ())
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
        visual_photo_ids=[
            p.manifest_id for p in photos if not selected or p.manifest_id in selected
        ],
    )


def choice(draft=None):
    draft = draft or strategy()
    found = collect_conflicts(draft, CreativeBrief(), snapshot(draft))
    assert len(found) == 1, "The mixed-media timing choice must precede approval"
    assert found[0].kind == "music_vs_visuals"
    return found[0]


def answer(draft, offered, option):
    question = build_choice_question(offered.candidate(), question_id="cunda-timing")
    events = [
        ("assistant", {"choice_question": question}),
        ("user", {"choice_selection": {"question_id": "cunda-timing", "option_key": option}}),
    ]
    result = resolve_choices(draft, CreativeBrief(), snapshot(draft), events)
    assert result.question is None
    assert not result.exhausted
    assert result.answers[0]["source"] == "creator"
    return result.strategy


def compile_draft(draft):
    approved = snapshot(draft)
    selected = selected_visual_ids(draft)
    plan = plan_unified_montage(
        CLIPS,
        brief_view(CreativeBrief()),
        strategy=draft,
        visuals=[p for p in PHOTOS if selected is None or p.manifest_id in selected],
        creator_duration_proven=draft.get("creator_duration_s") is not None,
        approved_length_plan=approved["length_plan"],
    )
    track = draft.get("selected_music_section")
    execution = compile_execution_plan(plan.guided_edit(), track=track)
    footage, stills = bindings_for(plan)
    recipe = compile_phone_guided_plan(
        GuidedStoryExecutionPlan.model_validate(execution), footage, stills
    )
    return plan, execution, recipe


def test_cunda_mixed_media_needs_a_choice_before_approval():
    draft = strategy()
    offered = choice(draft)
    assert offered.recommended().key == "extend"
    assert {o.key for o in offered.options} >= {"extend", "videos_only"}
    assert "16.8" in offered.reason
    assert draft.get("creator_duration_s") is None
    assert snapshot(draft)["length_plan"]["target_duration_s"] is None


def test_accepted_longer_edit_keeps_all_media_and_compiles_for_iphone():
    draft = strategy()
    answered = answer(draft, choice(draft), "extend")
    assert answered["selected_music_section"]["track_id"] == SECTION["track_id"]
    assert collect_conflicts(answered, CreativeBrief(), snapshot(answered)) == []
    plan, execution, recipe = compile_draft(answered)
    assert len(plan.snapshot.fast_cuts) == 21
    assert plan.duration_s == pytest.approx(answered["creator_duration_s"], abs=1 / 30)
    assert execution["song_reference"]["end_s"] <= answered["selected_music_section"]["end_s"]
    assert recipe.duration == pytest.approx(plan.duration_s, abs=1 / 30)


def test_keeping_the_music_section_explicitly_removes_photos_and_compiles():
    draft = strategy()
    answered = answer(draft, choice(draft), "videos_only")
    assert answered["selected_music_section"] == SECTION
    assert answered["media_scope"] == "selected"
    assert set(answered["selected_media_ids"]) == {c.media_id for c in CLIPS}
    assert collect_conflicts(answered, CreativeBrief(), snapshot(answered)) == []
    plan, _execution, _recipe = compile_draft(answered)
    assert len(plan.snapshot.fast_cuts) == 11


@pytest.mark.parametrize("catalog", [None, 20.0])
def test_unavailable_music_cannot_be_offered_as_a_longer_section(catalog):
    draft = strategy(selected_music_section={**SECTION, "catalog_duration_s": catalog})
    assert "extend" not in {o.key for o in choice(draft).options}


def test_a_video_only_edit_does_not_ask_about_photos():
    draft = strategy(media_scope="selected", selected_media_ids=[c.media_id for c in CLIPS])
    assert collect_conflicts(draft, CreativeBrief(), snapshot(draft)) == []


def test_the_worker_still_rejects_the_original_unapproved_extension():
    with pytest.raises(GuidedStoryError, match="length choice"):
        compile_draft(strategy())


def test_the_exact_failed_source_windows_reproduce_28_804_seconds(monkeypatch):
    # Numeric windows from the failed approval; IDs, paths and content are synthetic.
    windows = [
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
    monkeypatch.setitem(globals(), "CLIPS", [clip(i, duration=8.0) for i in range(11)])
    draft = strategy()
    approved = snapshot(draft)["length_plan"]
    for row, (start, end) in zip(approved["windows"], windows, strict=True):
        row.update(source_start_s=start, source_end_s=end)
    plan = plan_unified_montage(
        CLIPS,
        brief_view(CreativeBrief()),
        strategy=draft,
        visuals=PHOTOS,
        approved_length_plan=approved,
    )
    assert plan.duration_s == pytest.approx(28.804)
    with pytest.raises(GuidedStoryError, match="length choice"):
        compile_execution_plan(plan.guided_edit(), track=SECTION)


def test_a_changed_photo_pool_does_not_reuse_an_old_timing_answer():
    draft = strategy()
    offered = choice(draft)
    question = build_choice_question(offered.candidate(), question_id="old-photos")
    events = [
        ("assistant", {"choice_question": question}),
        (
            "user",
            {
                "choice_selection": {"question_id": "old-photos", "option_key": "extend"},
            },
        ),
    ]
    changed = snapshot(draft, PHOTOS[:-1])
    result = resolve_choices(draft, CreativeBrief(), changed, events)
    assert result.question is not None
    assert result.question.input_digest != offered.input_digest
    assert result.answers == ()


@pytest.mark.parametrize("excess_s", [1 / 30, 0.1, 12.004])
def test_a_real_music_section_overrun_still_needs_consent(excess_s):
    with pytest.raises(GuidedStoryError, match="length choice"):
        _song_reference(SECTION, duration_s=16.8 + excess_s)


def test_subframe_rounding_does_not_extend_the_song_reference():
    reference = _song_reference(SECTION, duration_s=16.804)
    assert reference["end_s"] == SECTION["end_s"]


@pytest.mark.parametrize("option", ["extend", "videos_only"])
async def test_the_real_planning_gate_asks_then_compiles_the_answer(monkeypatch, option):
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
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(side_effect=lambda *args: events))
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=CreativeBrief()))
    matcher = AsyncMock(return_value=copy.deepcopy(SECTION))
    monkeypatch.setattr(planner, "_freeze_licensed_music_section", matcher)
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
    kwargs = {"thread_id": uuid.uuid4(), "creator_id": uuid.uuid4()}
    asked = await planner._gate_unresolved_choices(SimpleNamespace(), proposed, **kwargs)
    question = asked.plan.choice_question
    assert question["kind"] == "music_vs_visuals"
    assert asked.plan.mode == "respond"
    assert not asked.plan.intents
    events.extend(
        [
            tag_event(
                "assistant",
                {"choice_question": question},
                "assistant_response",
                asked.plan.response,
            ),
            tag_event(
                "user",
                {
                    "choice_selection": {
                        "question_id": question["question_id"],
                        "option_key": option,
                    }
                },
                "user_message",
                next(o["label"] for o in question["options"] if o["key"] == option),
            ),
        ]
    )
    accepted = await planner._gate_unresolved_choices(SimpleNamespace(), proposed, **kwargs)
    assert accepted.plan.choice_question is None
    assert accepted.plan.mode == "act"
    chosen = accepted.plan.intents[0].arguments["strategy"]
    plan, _execution, recipe = compile_draft(chosen)
    assert len(plan.snapshot.fast_cuts) == (21 if option == "extend" else 11)
    assert recipe.duration == pytest.approx(plan.duration_s, abs=1 / 30)
    again = await planner._gate_unresolved_choices(SimpleNamespace(), proposed, **kwargs)
    assert again.plan.intents[0].arguments["strategy"] == chosen
