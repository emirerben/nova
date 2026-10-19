"""Visitor-guide incident replay through composition and immutable plan compilation.

Failure cases: timing-only input loses footage; unused media leaks into copy; analysis
becomes unverified history; replay changes cuts or calls the writer a second time.
"""

import json

from app.pipeline.guided_story import compile_proposal_execution_plan
from app.pipeline.unified_montage import BriefView, UnifiedClip, plan_unified_montage
from app.schemas.edit_proposal import EditProposalSnapshot
from app.services.creation_text_composition import CreationTextComposer, compose_creation_text


def test_visitor_guide_writer_gets_bound_footage_and_replays_without_replanning(monkeypatch):
    clips = [
        UnifiedClip(
            media_id=key,
            proxy_path=f"private/{key}.mp4",
            generation="1",
            duration_s=8,
            analysis={"understanding": {"summary": summary, "setting": setting}},
        )
        for key, summary, setting in [
            ("horse", "A large wooden horse visitors walk around", "Outdoor entrance"),
            ("ruins", "Visitors follow a boardwalk beside stone walls", "Archaeological site"),
            ("museum", "Display cases contain pottery and small objects", "Museum galleries"),
        ]
    ]
    plan = plan_unified_montage(clips, BriefView(target_duration_s=15))
    original = compile_proposal_execution_plan(plan.snapshot)
    seen = []

    def writer(self, input, *, ctx=None):
        seen.append(input)
        rows = input.variant_snapshot["footage_context"]
        assert [row["media_id"] for row in rows] == ["horse", "ruins", "museum"]
        assert "pottery" in json.dumps(rows[-1])
        assert "private/" not in json.dumps(rows)
        assert rows[-1]["source_end_s"] > rows[-1]["source_start_s"]
        prompt = self.render_prompt(input)
        assert "Footage evidence" in prompt
        assert "unverified" in prompt
        return self.parse(
            json.dumps(
                {
                    "intent": "edit",
                    "confidence": 0.95,
                    "reply": "Added a visitor guide.",
                    "ops": [
                        {
                            "op": "add_text",
                            "text": "Explore the museum galleries",
                            "start_s": rows[-1]["output_start_s"],
                            "end_s": rows[-1]["output_end_s"],
                        }
                    ],
                }
            ),
            input,
        )

    monkeypatch.setattr(CreationTextComposer, "run", writer)
    snapshot = compose_creation_text(
        plan.snapshot,
        creator_request=(
            "Create an edit for people looking for places to visit in Turkey. "
            "Highlight what they can see in the area and museum."
        ),
    )
    restored = EditProposalSnapshot.model_validate_json(snapshot.model_dump_json())
    compiled = compile_proposal_execution_plan(restored)
    assert compiled["story_timeline"] == original["story_timeline"]
    assert any(row["text"] == "Explore the museum galleries" for row in compiled["text_elements"])
    assert compose_creation_text(restored, creator_request="same") == restored
    assert len(seen) == 1


def test_approved_editorial_sequence_survives_length_plan_and_worker():
    from app.services.creator_render_contract import build_render_contract
    from app.services.length_planning import build_length_plan

    ids = ["ruins", "museum", "car", "horse"]
    story = {
        "viewer_goal": "Show visitors what they can see at Troya",
        "beats": [
            {"purpose": "Open on the Trojan Horse", "media_ids": ["horse"]},
            {"purpose": "Establish the road trip", "media_ids": ["car"]},
            {
                "purpose": "Explore the ruins",
                "media_ids": ["ruins"],
                "text_goal": "Explain the visible archaeological site",
            },
            {
                "purpose": "Finish inside the museum",
                "media_ids": ["museum"],
                "text_goal": "Describe what visitors see in the display cases",
            },
        ],
    }
    strategy = {
        "direction": "guided_story",
        "edit_format": "montage",
        "audio_strategy": "licensed_music",
        "render_program": "guided",
        "target_duration_s": 35,
        "rationale": "A visitor journey",
        "editorial_plan": story,
    }
    assignments = [
        {
            "media_id": key,
            "gcs_path": f"private/{key}.mp4",
            "generation": "1",
            "duration_s": 8,
            "kind": "video",
        }
        for key in ids
    ]
    contract = build_render_contract(
        strategy, generation_id="approved", media_snapshot={"clip_assignments": assignments}
    )
    assert contract.order_required
    assert contract.order_ids == ("horse", "car", "ruins", "museum")
    assert contract.duration_s is None
    envelope = build_length_plan(
        strategy, {"clip_assignments": assignments}, order_ids=contract.order_ids
    )
    clips = [
        UnifiedClip(
            media_id=row["media_id"], proxy_path=row["gcs_path"], generation="1", duration_s=8
        )
        for row in assignments
    ]
    planned = plan_unified_montage(clips, strategy=strategy, approved_length_plan=envelope)
    snapshot = EditProposalSnapshot.model_validate_json(planned.snapshot.model_dump_json())
    compiled = compile_proposal_execution_plan(snapshot)
    assert [row["media_id"] for row in compiled["story_timeline"]] == list(contract.order_ids)
    assert snapshot.editorial_plan.viewer_goal == story["viewer_goal"]
    assert snapshot.editorial_plan.beats[-1].text_goal == story["beats"][-1]["text_goal"]


def test_editorial_plan_cannot_invent_or_drop_sources_or_override_creator_order():
    import pytest

    from app.services.editorial_plan import editorial_order

    plan = {
        "viewer_goal": "A journey",
        "beats": [
            {"purpose": "Opening", "media_ids": ["b"]},
            {"purpose": "Ending", "media_ids": ["a"]},
        ],
    }
    assert editorial_order(plan, ["a", "b"]) == ["b", "a"]
    assert editorial_order(plan, ["a", "b"], sequence=[("first", "b", ["b"], "resolved")]) == [
        "b",
        "a",
    ]
    with pytest.raises(ValueError):
        editorial_order(plan, ["a", "b"], sequence=[("last", "b", ["b"], "resolved")])
    with pytest.raises(ValueError):
        editorial_order(plan, ["a", "c"])
    with pytest.raises(ValueError):
        editorial_order(plan, ["a", "b", "c"])
    with pytest.raises(ValueError):
        editorial_order(plan, ["a", "b"], required_order=["a", "b"])


def test_explanatory_beat_cannot_silently_finish_with_only_a_title(monkeypatch):
    import pytest

    from app.services.creation_text_composition import CreationTextCompositionError

    plan = plan_unified_montage(
        [
            UnifiedClip(
                media_id="museum", proxy_path="private/museum.mp4", generation="1", duration_s=8
            )
        ],
        BriefView(target_duration_s=8),
        strategy={
            "editorial_plan": {
                "viewer_goal": "Explain what visitors can see",
                "beats": [
                    {
                        "purpose": "Museum",
                        "media_ids": ["museum"],
                        "text_goal": "Explain the pictured museum displays",
                    }
                ],
            }
        },
    )
    monkeypatch.setattr(
        CreationTextComposer,
        "run",
        lambda self, input, **kw: self.parse(
            json.dumps({"intent": "describe", "confidence": 0.9, "reply": "Done", "ops": []}),
            input,
        ),
    )
    with pytest.raises(CreationTextCompositionError, match="explanatory"):
        compose_creation_text(plan.snapshot, creator_request="Highlight what visitors can see")


def test_writer_gets_exact_short_beat_boundaries_and_reading_budget(monkeypatch):
    """Live Troya failure: a drive caption crossed into unrelated ruins footage."""
    plan = plan_unified_montage(
        [
            UnifiedClip(
                media_id=k,
                proxy_path=f"private/{k}.mp4",
                generation="1",
                duration_s=8,
                analysis={
                    "understanding": {
                        "summary": "Drive from Assos to Troya" if k == "car" else "Stone ruins"
                    }
                },
            )
            for k in ("car", "ruins")
        ],
        BriefView(target_duration_s=6),
        strategy={
            "editorial_plan": {
                "viewer_goal": "Explain the trip",
                "beats": [
                    {
                        "purpose": "Drive",
                        "media_ids": ["car"],
                        "text_goal": "Introduce the journey",
                    },
                    {"purpose": "Site", "media_ids": ["ruins"]},
                ],
            }
        },
    )
    expected = compile_proposal_execution_plan(plan.snapshot)["story_timeline"][0]

    def writer(self, input, **kw):
        beat = input.variant_snapshot["editorial_plan"]["beats"][0]
        assert beat["output_start_s"] == expected["output_start_s"]
        assert beat["output_end_s"] == expected["output_end_s"]
        assert beat["reading_time_rule"]
        return self.parse(
            json.dumps(
                {
                    "intent": "edit",
                    "confidence": 0.9,
                    "reply": "Added journey context",
                    "ops": [
                        {
                            "op": "add_text",
                            "text": "Assos → Troya",
                            "start_s": beat["output_start_s"],
                            "end_s": beat["output_end_s"],
                        }
                    ],
                }
            ),
            input,
        )

    monkeypatch.setattr(CreationTextComposer, "run", writer)
    result = compose_creation_text(plan.snapshot, creator_request="Explain the journey")
    assert (
        compile_proposal_execution_plan(result)["story_timeline"]
        == compile_proposal_execution_plan(plan.snapshot)["story_timeline"]
    )


def test_song_length_reserves_reading_time_before_editorial_approval():
    from app.services.creator_render_contract import build_render_contract
    from app.services.length_planning import build_length_plan

    groups = [
        ["car"],
        [f"ruins-{i}" for i in range(8)],
        [f"layers-{i}" for i in range(9)],
        ["horse-1", "horse-2"],
        [f"museum-{i}" for i in range(8)],
    ]
    clips = [
        UnifiedClip(media_id=key, proxy_path=f"private/{key}.mp4", generation="1", duration_s=6)
        for group in groups
        for key in group
    ]
    strategy = {
        "edit_format": "montage",
        "audio_strategy": "licensed_music",
        "selected_music_section": {"start_s": 130, "end_s": 148},
        "editorial_plan": {
            "viewer_goal": "Explain the visitor journey",
            "beats": [
                {"purpose": str(i), "media_ids": group, "text_goal": "Explain this part"}
                for i, group in enumerate(groups)
            ],
        },
    }
    media = {
        "clip_assignments": [
            {
                "media_id": c.media_id,
                "gcs_path": c.proxy_path,
                "generation": c.generation,
                "duration_s": c.duration_s,
            }
            for c in clips
        ]
    }
    contract = build_render_contract(strategy, generation_id="reading-time", media_snapshot=media)
    length = build_length_plan(strategy, media, order_ids=contract.order_ids)
    assert length["conflicts"] == []
    plan = plan_unified_montage(clips, strategy=strategy, approved_length_plan=length)
    compiled = compile_proposal_execution_plan(plan.snapshot)
    assert abs(compiled["resolved_duration_s"] - 18) < 1 / 30
    for group in groups:
        rows = [r for r in compiled["story_timeline"] if r["media_id"] in group]
        assert sum(r["duration_s"] for r in rows) >= 3 - 1 / 30
    too_short = {**strategy, "selected_music_section": {"start_s": 130, "end_s": 138}}
    assert any(
        c["code"] == "editorial_text_capacity"
        for c in build_length_plan(too_short, media, order_ids=contract.order_ids)["conflicts"]
    )


async def test_unreadable_editorial_plan_is_not_offered_for_approval(monkeypatch):
    import uuid
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
    from app.kria import planner

    ids = ["drive", "ruins", "horse", "museum"]
    strategy = CreativeStrategy(
        direction="text_explainer",
        edit_format="montage",
        audio_strategy="licensed_music",
        pacing="balanced",
        render_program="guided",
        target_duration_s=8,
        rationale="A visitor guide",
        editorial_plan={
            "viewer_goal": "Explain the visit",
            "beats": [
                {"purpose": key, "media_ids": [key], "text_goal": "Explain what visitors see"}
                for key in ids
            ],
        },
    )
    planned = planner.PlannedKriaTurn(
        plan=planner.adapt_creator_action(
            ProposeStrategy(kind="propose_strategy", strategy=strategy, summary="A visitor guide")
        ),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
        media_snapshot={
            "clip_assignments": [
                {
                    "media_id": key,
                    "kind": "video",
                    "duration_s": 6,
                    "generation": "1",
                    "gcs_path": f"private/{key}.mp4",
                }
                for key in ids
            ]
        },
    )
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(type(planner.settings), "creative_brief_for", lambda *_: False)
    monkeypatch.setattr(
        planner,
        "_freeze_licensed_music_section",
        AsyncMock(return_value={"start_s": 130, "end_s": 138}),
    )
    monkeypatch.setattr(planner, "ground_closing_quote", AsyncMock(return_value=None))
    for enabled in (False, True):
        monkeypatch.setattr(planner.settings, "kria_choice_questions_enabled", enabled)
        result = await planner._gate_unresolved_choices(
            SimpleNamespace(), planned, thread_id=uuid.uuid4(), creator_id=uuid.uuid4()
        )
        assert result.plan.mode == "respond"
        assert not result.plan.intents
        assert "explanations" in result.plan.response
