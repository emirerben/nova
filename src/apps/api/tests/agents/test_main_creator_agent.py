import json

import pytest

from app.agents._schemas.creator_agent import (
    CapabilityAvailability,
    CreatorMediaRef,
    ProposeStrategy,
    ResolvedCreatorManifest,
)
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput


def _manifest() -> ResolvedCreatorManifest:
    available = CapabilityAvailability(available=True)
    return ResolvedCreatorManifest(
        item_id="item-1",
        edit_format="montage",
        render_program="guided",
        media=[
            CreatorMediaRef(
                media_id=f"clip-{index:02d}-11111111-1111-1111-1111-111111111111",
                kind="video",
            )
            for index in range(45)
        ]
        + [
            CreatorMediaRef(
                media_id=f"asset-{index:02d}-22222222-2222-2222-2222-222222222222",
                kind="image",
            )
            for index in range(5)
        ],
        capabilities={
            "edit_format:montage": available,
            "draft_guided_proposal": available,
            "dispatch_render": available,
        },
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )


def _input() -> MainCreatorInput:
    manifest = _manifest()
    return MainCreatorInput(
        user_message="Make this feel alive.",
        media_context=[{"media_id": media.media_id} for media in manifest.media],
        capability_manifest=manifest,
    )


def _raw(*, audio_strategy: str, selected: list[str], intercut: dict | None = None) -> str:
    return json.dumps(
        {
            "action": {
                "kind": "propose_strategy",
                "strategy": {
                    "direction": "guided_story",
                    "edit_format": "montage",
                    "audio_strategy": audio_strategy,
                    "intercut_comparison": intercut,
                    "render_program": "guided",
                    "selected_media_ids": selected,
                    "rationale": "Build a concise visual arc.",
                },
                "summary": "A concise visual story.",
            }
        }
    )


def test_guided_main_creator_output_drops_opaque_media_list() -> None:
    agent_input = _input()
    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        _raw(
            audio_strategy="licensed_music",
            selected=[media.media_id for media in agent_input.capability_manifest.media],
        ),
        agent_input,
    )

    assert isinstance(output.action, ProposeStrategy)
    assert output.action.strategy.render_program == "guided"
    assert output.action.strategy.selected_media_ids == []
    assert len(output.model_dump_json()) < 1800


def test_main_creator_prompt_explains_guided_media_and_music_contracts() -> None:
    prompt = MainCreatorAgent(None).render_prompt(_input())  # type: ignore[arg-type]

    assert "guided, return\n  `selected_media_ids: []`" in prompt
    assert "only when the manifest catalog contains a usable music entry" in prompt


def test_main_creator_recognizes_mixed_media_timing_request() -> None:
    agent_input = _input().model_copy(
        update={
            "user_message": "Photos should have a very fast transition, videos can be a bit longer"
        }
    )
    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        _raw(
            audio_strategy="licensed_music",
            selected=[media.media_id for media in agent_input.capability_manifest.media],
        ),
        agent_input,
    )

    assert isinstance(output.action, ProposeStrategy)
    assert output.action.strategy.mixed_media_timing is not None
    assert output.action.strategy.mixed_media_timing.model_dump() == {
        "image_hold": "very_fast",
        "video_hold": "longer",
        "boundary_style": "cut",
    }


def test_main_creator_keeps_typed_intercut_capability_for_guided_original_audio() -> None:
    agent_input = _input()
    source_ids = [media.media_id for media in agent_input.capability_manifest.media[:2]]
    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        _raw(
            audio_strategy="original_audio",
            selected=[],
            intercut={
                "source_count": 2,
                "source_media_ids": source_ids,
                "segment_duration_s": 1.0,
                "sequence_mode": "round_robin",
                "text_mode": "persistent_per_source",
                "audio_modes": ["interleaved", "source_a", "source_b"],
            },
        ),
        agent_input,
    )

    assert isinstance(output.action, ProposeStrategy)
    assert output.action.strategy.intercut_comparison is not None
    assert output.action.strategy.intercut_comparison.source_media_ids == source_ids


def test_main_creator_repairs_native_mixed_media_timing_to_guided() -> None:
    agent_input = _input().model_copy(
        update={
            "user_message": "Photos should have a very fast transition, videos can be a bit longer"
        }
    )
    raw = json.loads(
        _raw(
            audio_strategy="licensed_music",
            selected=[media.media_id for media in agent_input.capability_manifest.media[:8]],
        )
    )
    raw["action"]["strategy"]["render_program"] = "native"

    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(raw),
        agent_input,
    )

    assert isinstance(output.action, ProposeStrategy)
    assert output.action.strategy.render_program == "guided"
    assert output.action.strategy.selected_media_ids == []


def test_main_creator_recognizes_timing_request_from_an_earlier_user_turn() -> None:
    agent_input = _input().model_copy(
        update={
            "user_message": "Yes, use that direction.",
            "conversation": [
                {
                    "role": "user",
                    "content": (
                        "Photos should have a very fast transition, videos can be a bit longer"
                    ),
                }
            ],
        }
    )

    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        _raw(audio_strategy="licensed_music", selected=[]),
        agent_input,
    )

    assert isinstance(output.action, ProposeStrategy)
    assert output.action.strategy.mixed_media_timing is not None


def test_main_creator_recognizes_natural_mixed_media_timing_paraphrase() -> None:
    agent_input = _input().model_copy(
        update={"user_message": "Keep the photos snappy and let the videos breathe."}
    )

    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        _raw(audio_strategy="licensed_music", selected=[]),
        agent_input,
    )

    assert isinstance(output.action, ProposeStrategy)
    assert output.action.strategy.mixed_media_timing is not None


@pytest.mark.parametrize(
    "message",
    [
        "Don't make the photos very fast; let the videos breathe.",
        "Keep photos snappy, but do not hold the videos longer.",
    ],
)
def test_main_creator_rejects_negated_mixed_media_timing(message: str) -> None:
    raw = json.loads(_raw(audio_strategy="licensed_music", selected=[]))
    raw["action"]["strategy"]["mixed_media_timing"] = {
        "image_hold": "very_fast",
        "video_hold": "longer",
        "boundary_style": "cut",
    }

    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(raw),
        _input().model_copy(update={"user_message": message}),
    )

    assert isinstance(output.action, ProposeStrategy)
    assert output.action.strategy.mixed_media_timing is None


def test_main_creator_drops_model_invented_timing_for_unrelated_request() -> None:
    raw = json.loads(_raw(audio_strategy="licensed_music", selected=[]))
    raw["action"]["strategy"]["mixed_media_timing"] = {
        "image_hold": "very_fast",
        "video_hold": "longer",
        "boundary_style": "cut",
    }

    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(raw),
        _input(),
    )

    assert isinstance(output.action, ProposeStrategy)
    assert output.action.strategy.mixed_media_timing is None


def test_audio_led_main_creator_output_is_native_and_bounded_to_twelve_clips() -> None:
    agent_input = _input()
    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        _raw(
            audio_strategy="original_audio",
            selected=[media.media_id for media in agent_input.capability_manifest.media],
        ),
        agent_input,
    )

    assert isinstance(output.action, ProposeStrategy)
    strategy = output.action.strategy
    assert strategy.render_program == "native"
    assert len(strategy.selected_media_ids) == 12
    assert all(not media_id.startswith("asset-") for media_id in strategy.selected_media_ids)
    assert len(output.model_dump_json()) < 1800


@pytest.mark.parametrize(
    ("selected", "expected_count"),
    [
        ([], 12),
        (["clip-00-11111111-1111-1111-1111-111111111111"] * 2, 1),
        (["asset-00-22222222-2222-2222-2222-222222222222"], 12),
        (["invented-provider-id"], 12),
    ],
)
def test_native_main_creator_repairs_empty_duplicate_asset_and_invented_ids(
    selected: list[str],
    expected_count: int,
) -> None:
    agent_input = _input()

    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        _raw(audio_strategy="original_audio", selected=selected),
        agent_input,
    )

    assert isinstance(output.action, ProposeStrategy)
    resolved = output.action.strategy.selected_media_ids
    assert len(resolved) == expected_count
    assert len(resolved) == len(set(resolved))
    assert set(resolved) <= {
        media.media_id
        for media in agent_input.capability_manifest.media
        if not media.media_id.startswith("asset-")
    }
    assert len(output.model_dump_json()) < 1800
